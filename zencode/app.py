import os
import re
import json
import httpx
import tempfile
import subprocess
import shutil
import tarfile
import hashlib
import fnmatch
import secrets
from urllib.parse import urlsplit, urlunsplit
from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import logging

app = FastAPI()

# ---------------------------------------------------------------------------
# Local request guard
# ---------------------------------------------------------------------------
# Binding to 127.0.0.1 keeps other machines out, but not web pages open in the
# operator's own browser. So: the Host header must be a loopback name on this
# server's port (this stops DNS rebinding), and a state-changing request must
# come from this server's own page, not from another site (this stops
# cross-site "simple" POSTs, which CORS never preflights). Scripts that send
# no Origin and no Sec-Fetch-Site (curl, the operator's own HTTP calls) still work.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def allowed_hosts(port):
    hosts = {f"{name}:{port}" for name in LOOPBACK_HOSTS}
    if port == 80:
        hosts.update(LOOPBACK_HOSTS)
    return hosts


def local_request_refusal(headers, method, port):
    """Why this request must be refused, or None. A header sent twice never matches."""
    hosts = headers.getlist("host")
    allowed = allowed_hosts(port) if port else set()
    if len(hosts) != 1 or hosts[0].lower() not in allowed:
        return "Host not allowed: this server answers only on 127.0.0.1, localhost or [::1]."
    if method.upper() not in STATE_CHANGING_METHODS:
        return None
    origins = headers.getlist("origin")
    if origins:
        if len(origins) != 1 or origins[0].lower() not in {f"http://{host}" for host in allowed}:
            return "Cross-origin request refused."
        return None
    fetch_sites = headers.getlist("sec-fetch-site")
    if len(fetch_sites) > 1 or (fetch_sites and fetch_sites[0].lower() not in ("same-origin", "none")):
        return "Cross-site request refused."
    return None


@app.middleware("http")
async def local_request_guard(request: Request, call_next):
    server = request.scope.get("server") or (None, None)
    reason = local_request_refusal(request.headers, request.method, server[1])
    if reason:
        return JSONResponse(status_code=403, content={"error": reason})
    return await call_next(request)


# Mount frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

sandbox_state = {
    "base_url": None,
    "model_id": None,
    "sandbox_dir": None,
    "messages": [],
    "logs": []   # append-only, never cleared
}

SANDBOX_MODE = os.environ.get("SEEDLING_SANDBOX_MODE", "container").lower()
SANDBOX_IMAGE = os.environ.get("SEEDLING_SANDBOX_IMAGE", "python:3.12-slim")
SANDBOX_RUNTIME = os.environ.get("SEEDLING_CONTAINER_RUNTIME")
SANDBOX_NETWORK = os.environ.get("SEEDLING_SANDBOX_NETWORK", "none")
SANDBOX_CPUS = os.environ.get("SEEDLING_SANDBOX_CPUS", "1")
SANDBOX_MEMORY = os.environ.get("SEEDLING_SANDBOX_MEMORY", "512m")
SANDBOX_PIDS = os.environ.get("SEEDLING_SANDBOX_PIDS", "128")
SANDBOX_TIMEOUT = int(os.environ.get("SEEDLING_SANDBOX_TIMEOUT", "30"))
MAX_READ_BYTES = int(os.environ.get("SEEDLING_MAX_READ_BYTES", str(1024 * 1024)))
MAX_WRITE_BYTES = int(os.environ.get("SEEDLING_MAX_WRITE_BYTES", str(5 * 1024 * 1024)))
MAX_ARTIFACT_BYTES = int(os.environ.get("SEEDLING_MAX_ARTIFACT_BYTES", str(25 * 1024 * 1024)))
# What Ship Update leaves out of the artifact. Apply copies the artifact over
# the target codebase (operator/ or zencode/), so the agent's working state
# must not ship: keep scratch work under scratch/ (the system prompt says so).
# Directories are matched by name at any depth, files by name pattern. The
# supervisor keeps the same lists (supervisor/main.py) and refuses an artifact
# that still holds one of these; a test keeps the two copies equal.
ARTIFACT_EXCLUDED_DIRS = {
    ".git", "venv", ".venv", "node_modules", "__pycache__", "outbox",
    "scratch", "tmp", ".pytest_cache", ".mypy_cache",
}
ARTIFACT_EXCLUDED_FILES = ("*.tmp", "*.pyc", "*.pyo", "*.log", "*.swp", "*~", ".DS_Store", ".seedling-*")
DEFAULT_RUNTIME_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".seedling_runtime", "zencode"))

def artifact_exclusion(rel_path):
    """Why rel_path (relative to the sandbox) never ships, or None if it ships."""
    parts = rel_path.replace(os.sep, "/").split("/")
    for part in parts[:-1]:
        if part in ARTIFACT_EXCLUDED_DIRS:
            return f"{part}/ is not shipped"
    if any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in ARTIFACT_EXCLUDED_FILES):
        return "scratch, temp or cache file"
    return None

def log_event(msg, evt_type="log-out"):
    sandbox_state["logs"].append({"msg": msg, "type": evt_type})

def ensure_sandbox_dir():
    if sandbox_state["sandbox_dir"]:
        return sandbox_state["sandbox_dir"]

    env_dir = os.environ.get("ZENCODE_SANDBOX_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        sandbox_state["sandbox_dir"] = env_dir
    else:
        runtime_root = os.environ.get("SEEDLING_RUNTIME_DIR", DEFAULT_RUNTIME_ROOT)
        os.makedirs(runtime_root, exist_ok=True)
        sandbox_state["sandbox_dir"] = tempfile.mkdtemp(prefix="zen_sandbox_", dir=runtime_root)
    return sandbox_state["sandbox_dir"]

def existing_sandbox_dir():
    """The sandbox dir if it exists already. Never creates one (for read-only endpoints)."""
    current = sandbox_state["sandbox_dir"] or os.environ.get("ZENCODE_SANDBOX_DIR")
    return current if current and os.path.isdir(current) else None

def resolve_sandbox_path(path: str):
    root = os.path.realpath(ensure_sandbox_dir())
    if not path or "\x00" in path:
        raise ValueError("Path must be a non-empty relative path")
    if os.path.isabs(path):
        raise ValueError("Absolute paths are not allowed")

    candidate = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError(f"Path escapes sandbox: {path}")
    return candidate

def container_runtime():
    if SANDBOX_RUNTIME:
        return SANDBOX_RUNTIME
    return shutil.which("docker") or shutil.which("podman")

def container_command(runtime, sandbox_dir, command):
    """The one-shot container that runs a command with the sandbox at /workspace."""
    return [
        runtime, "run", "--rm",
        "--network", SANDBOX_NETWORK,
        "--cpus", SANDBOX_CPUS,
        "--memory", SANDBOX_MEMORY,
        "--pids-limit", SANDBOX_PIDS,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "-v", f"{sandbox_dir}:/workspace:rw",
        "-w", "/workspace",
        SANDBOX_IMAGE,
        "/bin/sh", "-lc", command,
    ]

# Sandboxes whose bind mount has been seen to work, in both directions.
verified_mounts = set()

def verify_container_mount(runtime, sandbox_dir):
    """Check that the container sees the sandbox, and that its writes reach the host.

    A container runtime in a VM (Colima, Docker Desktop) mounts only the host
    paths it shares; any other path is mounted as an empty directory, with no
    error, and writes to it are lost. So once per sandbox, a marker file with
    a random value is written on the host, the container copies it to a reply
    file, and the host reads the reply back. Returns None if that worked, or
    an error to show instead of running anything. Both files are removed.
    """
    key = os.path.realpath(sandbox_dir)
    if key in verified_mounts:
        return None
    nonce = secrets.token_hex(16)
    probe = f".seedling-mount-probe-{nonce}"
    reply = f".seedling-mount-reply-{nonce}"
    probe_path = os.path.join(sandbox_dir, probe)
    reply_path = os.path.join(sandbox_dir, reply)
    detail = ""
    try:
        with open(probe_path, "w") as f:
            f.write(nonce)
        result = subprocess.run(
            container_command(runtime, sandbox_dir, f"cat {probe} > {reply}"),
            cwd=sandbox_dir,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
            capture_output=True,
            text=True,
            timeout=SANDBOX_TIMEOUT,
        )
        detail = ((result.stderr or "").strip().splitlines() or [""])[-1]
        with open(reply_path) as f:
            ok = f.read() == nonce
    except FileNotFoundError:
        ok = False  # the container's copy never reached the host
    except (OSError, subprocess.SubprocessError) as e:
        ok = False
        detail = detail or type(e).__name__
    finally:
        for path in (probe_path, reply_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    if ok:
        verified_mounts.add(key)
        return None
    msg = (
        f"Container mount check failed: the container cannot see the sandbox {sandbox_dir} "
        "(a marker file written on the host was not readable in the container, or its "
        "copy did not come back). No command was run. Docker Desktop and Colima share "
        "only some host paths (Colima: your home directory by default): set "
        "SEEDLING_RUNTIME_DIR to a directory under a shared path, or share this path "
        "with the container VM, then restart Seedling."
    )
    if detail:
        msg += f" Runtime said: {detail}"
    return msg

def redact_userinfo(url):
    """Drop any user:password@ from a URL before it is shown or logged."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable URL)"
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))

def endpoint_error_text(error, url):
    """Describe a failed inference request without the endpoint URL or host.

    httpx messages can quote the request URL (invalid-URL and redirect errors,
    for example), and that URL can carry credentials in userinfo or the query.
    The text is logged and served by /api/logs, so every URL and every part of
    the endpoint's address is replaced with <endpoint>.
    """
    text = re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]*", "<endpoint>", str(error))
    secrets = set()
    if url:
        secrets.add(url)
        try:
            parts = urlsplit(url)
            secrets.update({parts.netloc, parts.netloc.rpartition("@")[2], parts.hostname, parts.username, parts.password})
        except ValueError:
            pass
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = re.sub(re.escape(secret), "<endpoint>", text, flags=re.IGNORECASE)
    name = type(error).__name__
    return f"{name}: {text}" if text.strip() else name

# ---------------------------------------------------------------------------
# Native Tool Definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Execute a bash command inside the sandbox working directory. Use for running scripts, installing packages, listing files, compiling, testing, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute"
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file inside the sandbox. Creates parent directories if needed. Overwrites if the file already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the sandbox directory (e.g. 'src/main.py')"
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write to the file"
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file inside the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path within the sandbox directory"
                    }
                },
                "required": ["path"]
            }
        }
    }
]

# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def run_bash(command: str):
    log_event(f"$ {command}", "log-cmd")
    sandbox_dir = ensure_sandbox_dir()

    if SANDBOX_MODE not in {"container", "local"}:
        msg = f"Unsupported sandbox mode: {SANDBOX_MODE}"
        log_event(msg, "log-err")
        return {"error": msg}

    try:
        if SANDBOX_MODE == "container":
            runtime = container_runtime()
            if not runtime:
                msg = "Container sandbox runtime not found. Install Docker or Podman to run commands."
                log_event(msg, "log-err")
                return {"error": msg}

            # Fail closed: never run against a workspace the container can't see.
            mount_error = verify_container_mount(runtime, sandbox_dir)
            if mount_error:
                log_event(mount_error, "log-err")
                return {"error": mount_error}

            result = subprocess.run(
                container_command(runtime, sandbox_dir, command),
                cwd=sandbox_dir,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT,
            )
        else:
            result = subprocess.run(
                command,
                shell=True,
                cwd=sandbox_dir,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
                capture_output=True,
                text=True,
                timeout=SANDBOX_TIMEOUT
            )
        out = result.stdout
        err = result.stderr
        if out: log_event(out, "log-out")
        if err: log_event(err, "log-err")
        return {"stdout": out, "stderr": err, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        msg = f"Command timed out after {SANDBOX_TIMEOUT}s: {command}"
        log_event(msg, "log-err")
        return {"error": msg}
    except Exception as e:
        log_event(str(e), "log-err")
        return {"error": str(e)}

def write_file(path: str, content: str):
    log_event(f"write_file: {path} ({len(content)} bytes)", "log-cmd")
    try:
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError(f"File exceeds write limit of {MAX_WRITE_BYTES} bytes")
        full_path = resolve_sandbox_path(path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, 'w') as f:
            f.write(content)
        return {"status": "success", "path": path, "bytes_written": len(content)}
    except Exception as e:
        log_event(str(e), "log-err")
        return {"error": str(e)}

def read_file(path: str):
    log_event(f"read_file: {path}", "log-cmd")
    try:
        full_path = resolve_sandbox_path(path)
        if os.path.getsize(full_path) > MAX_READ_BYTES:
            raise ValueError(f"File exceeds read limit of {MAX_READ_BYTES} bytes")
        with open(full_path, 'r') as f:
            content = f.read()
        log_event(f"  ({len(content)} bytes)", "log-out")
        return {"content": content}
    except Exception as e:
        log_event(str(e), "log-err")
        return {"error": str(e)}

TOOL_DISPATCH = {
    "run_bash": lambda args: run_bash(args.get("command", "")),
    "write_file": lambda args: write_file(args.get("path", ""), args.get("content", "")),
    "read_file": lambda args: read_file(args.get("path", "")),
}

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/probe")
async def probe_endpoint(req: Request):
    data = await req.json()
    url = data.get("url")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            else:
                return JSONResponse(status_code=400, content={"error": f"Endpoint returned {resp.status_code}"})
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

@app.post("/api/init")
async def init_agent(req: Request):
    data = await req.json()
    sandbox_state["base_url"] = data.get("base_url")
    sandbox_state["model_id"] = data.get("model_id")

    sandbox_dir = ensure_sandbox_dir()

    log_event(f"Sandbox initialized at {sandbox_state['sandbox_dir']}", "log-cmd")
    log_event(f"Sandbox mode: {SANDBOX_MODE}; network: {SANDBOX_NETWORK}; image: {SANDBOX_IMAGE}", "log-cmd")
    log_event(f"Using model {sandbox_state['model_id']} via {redact_userinfo(sandbox_state['base_url'])}")
    runtime = container_runtime() if SANDBOX_MODE == "container" else None
    if runtime:
        # Check the mount now, so a broken one shows before the agent starts.
        # run_bash checks again (once) and refuses to run until it passes.
        mount_error = verify_container_mount(runtime, sandbox_dir)
        if mount_error:
            log_event(mount_error, "log-err")
            return JSONResponse(status_code=409, content={"status": "error", "error": mount_error})
        log_event("Container mount check passed: the container sees the sandbox.", "log-cmd")
    return {"status": "success"}


SYSTEM_PROMPT = """\
You are ZenCode, a sandboxed minimal coding agent.
You live entirely inside an isolated sandbox directory. You can read and write files, run scripts, and install packages — but ONLY within your sandbox.

SANDBOX RULES:
- Your working directory is your entire world. Do not attempt to access paths outside it.
- Do not attempt to access the network unless explicitly asked to by the user.
- Do not attempt to modify system-level configurations or escape the sandbox in any way.
- Put scratch work (test scripts, example inputs, notes, copies of files) under scratch/. When the user ships an update, every file outside scratch/ is shipped and may be applied over the real codebase; scratch/ is never shipped.

You have three tools:
1. run_bash — execute shell commands in your sandbox
2. write_file — create or overwrite files (more reliable than echo for multi-line content)
3. read_file — read file contents

When asked to perform a task:
1. Break it down into steps
2. Use your tools to execute each step
3. Verify your work by inspecting outputs
4. Report results clearly to the user

Be concise and action-oriented. Prefer showing working code over lengthy explanations.\
"""


@app.post("/api/chat")
async def chat(req: Request):
    data = await req.json()
    user_msg = data.get("message")

    if not sandbox_state["base_url"] or not sandbox_state["model_id"]:
        return {"response": "Not initialized yet: choose an inference endpoint and a model first."}

    if not sandbox_state["messages"]:
        sandbox_state["messages"].append({"role": "system", "content": SYSTEM_PROMPT})

    sandbox_state["messages"].append({"role": "user", "content": user_msg})

    base = sandbox_state['base_url']
    if not base.endswith('/v1'):
        base += '/v1'
    api_url = f"{base}/chat/completions"
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient() as client:
        max_turns = 10
        final_reply = ""

        for _ in range(max_turns):
            payload = {
                "model": sandbox_state["model_id"],
                "messages": sandbox_state["messages"],
                "tools": TOOLS,
            }

            try:
                resp = await client.post(api_url, json=payload, headers=headers, timeout=120)
            except Exception as e:
                final_reply = f"Error: Could not reach inference endpoint — {endpoint_error_text(e, api_url)}"
                log_event(final_reply, "log-err")
                break

            if resp.status_code != 200:
                final_reply = f"Error: Provider API returned {resp.status_code}"
                log_event(final_reply, "log-err")
                break

            resp_data = resp.json()
            if "choices" not in resp_data:
                final_reply = f"Error from Provider API: {resp_data}"
                log_event(final_reply, "log-err")
                break

            choice = resp_data["choices"][0]
            message = choice["message"]

            # Append the raw assistant message to conversation history
            sandbox_state["messages"].append(message)

            # Check for tool calls
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                # No tool calls — model produced a final text response
                final_reply = message.get("content", "")
                break

            # Execute each tool call and feed results back
            for tc in tool_calls:
                fn = tc["function"]
                tool_name = fn["name"]

                try:
                    args = json.loads(fn["arguments"])
                except json.JSONDecodeError:
                    args = {}

                executor = TOOL_DISPATCH.get(tool_name)
                if executor:
                    result = executor(args)
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}

                sandbox_state["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result)
                })

    return {"response": final_reply}


@app.post("/api/ship_update")
async def ship_update():
    sandbox_dir = ensure_sandbox_dir()
    outbox_dir = os.path.join(sandbox_dir, "outbox")
    os.makedirs(outbox_dir, exist_ok=True)

    fd, temp_archive = tempfile.mkstemp(prefix="seedling_update_", suffix=".tar.gz")
    os.close(fd)

    shipped, excluded = [], []
    try:
        with tarfile.open(temp_archive, "w:gz") as archive:
            for root, dirs, files in os.walk(sandbox_dir):
                rel_root = os.path.relpath(root, sandbox_dir)
                if rel_root == ".":
                    rel_root = ""
                kept_dirs = []
                for d in sorted(dirs):
                    if d in ARTIFACT_EXCLUDED_DIRS:
                        if d != "outbox" or rel_root:
                            excluded.append({"path": os.path.join(rel_root, d) + "/", "reason": f"{d}/ is not shipped"})
                    else:
                        kept_dirs.append(d)
                dirs[:] = kept_dirs
                for filename in sorted(files):
                    path = os.path.join(root, filename)
                    rel_path = os.path.normpath(os.path.join(rel_root, filename))
                    if rel_path.startswith(".."):
                        continue
                    reason = artifact_exclusion(rel_path)
                    if reason:
                        excluded.append({"path": rel_path, "reason": reason})
                        continue
                    archive.add(path, arcname=rel_path)
                    shipped.append(rel_path)

        size = os.path.getsize(temp_archive)
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"Artifact exceeds limit of {MAX_ARTIFACT_BYTES} bytes")

        digest = hashlib.sha256()
        with open(temp_archive, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)

        final_path = os.path.join(outbox_dir, f"zencode_update_{digest.hexdigest()[:12]}.tar.gz")
        shutil.move(temp_archive, final_path)
        # What actually shipped, from the archive itself, not from the agent's account.
        log_event(
            f"Shipped quarantinable artifact to outbox: {os.path.basename(final_path)} "
            f"with {len(shipped)} files: {', '.join(shipped) or '(none)'}",
            "log-cmd",
        )
        if excluded:
            log_event("Left out of the artifact: " + ", ".join(e["path"] for e in excluded), "log-cmd")
        return {
            "status": "success",
            "artifact": os.path.basename(final_path),
            "sha256": digest.hexdigest(),
            "bytes": size,
            "files": shipped,
            "excluded": excluded,
        }
    except Exception as e:
        if os.path.exists(temp_archive):
            os.remove(temp_archive)
        log_event(str(e), "log-err")
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/api/logs")
async def get_logs(offset: int = Query(0, ge=0)):
    """Return logs from the given offset onward. Never clears the buffer."""
    return {
        "logs": sandbox_state["logs"][offset:],
        "next_offset": len(sandbox_state["logs"])
    }


@app.get("/api/status")
async def get_status():
    """Read-only facts the UI shows: model, sandbox mode and network policy."""
    runtime = container_runtime()
    return {
        "model_id": sandbox_state["model_id"],
        "base_url": redact_userinfo(sandbox_state["base_url"]),
        "sandbox_mode": SANDBOX_MODE,
        "container_runtime": os.path.basename(runtime) if runtime else None,
        "network": SANDBOX_NETWORK,
        "image": SANDBOX_IMAGE,
    }


LISTING_EXCLUDED_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__"}
MAX_LISTED_FILES = 500


@app.get("/api/files")
async def list_files():
    """List files inside the sandbox (relative paths and sizes), for the Files tab.

    Read-only, bounded, and never follows symlinks out of the sandbox.
    """
    existing = existing_sandbox_dir()
    if existing is None:
        return {"files": [], "truncated": False}
    root = os.path.realpath(existing)
    files = []
    truncated = False
    for current, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in LISTING_EXCLUDED_DIRS)
        for filename in sorted(filenames):
            path = os.path.join(current, filename)
            if os.path.islink(path):
                continue
            if len(files) >= MAX_LISTED_FILES:
                truncated = True
                break
            try:
                size = os.path.getsize(path)
            except OSError:
                continue  # removed between the walk and the stat
            files.append({"path": os.path.relpath(path, root), "bytes": size})
        if truncated:
            break
    return {"files": files, "truncated": truncated}
