import os
import json
import httpx
import tempfile
import subprocess
import shutil
import tarfile
import hashlib
from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import logging

app = FastAPI()

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
EXCLUDED_ARTIFACT_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", "outbox"}
DEFAULT_RUNTIME_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".seedling_runtime", "zencode"))

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

            cmd = [
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
            result = subprocess.run(
                cmd,
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

    ensure_sandbox_dir()

    log_event(f"Sandbox initialized at {sandbox_state['sandbox_dir']}", "log-cmd")
    log_event(f"Sandbox mode: {SANDBOX_MODE}; network: {SANDBOX_NETWORK}; image: {SANDBOX_IMAGE}", "log-cmd")
    log_event(f"Using model {sandbox_state['model_id']} via {sandbox_state['base_url']}")
    return {"status": "success"}


SYSTEM_PROMPT = """\
You are ZenCode, a sandboxed minimal coding agent.
You live entirely inside an isolated sandbox directory. You can read and write files, run scripts, and install packages — but ONLY within your sandbox.

SANDBOX RULES:
- Your working directory is your entire world. Do not attempt to access paths outside it.
- Do not attempt to access the network unless explicitly asked to by the user.
- Do not attempt to modify system-level configurations or escape the sandbox in any way.

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
                final_reply = f"Error: Could not reach inference endpoint — {e}"
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

    try:
        with tarfile.open(temp_archive, "w:gz") as archive:
            for root, dirs, files in os.walk(sandbox_dir):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_ARTIFACT_DIRS]
                rel_root = os.path.relpath(root, sandbox_dir)
                if rel_root == ".":
                    rel_root = ""
                for filename in files:
                    path = os.path.join(root, filename)
                    rel_path = os.path.normpath(os.path.join(rel_root, filename))
                    if rel_path.startswith(".."):
                        continue
                    archive.add(path, arcname=rel_path)

        size = os.path.getsize(temp_archive)
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"Artifact exceeds limit of {MAX_ARTIFACT_BYTES} bytes")

        digest = hashlib.sha256()
        with open(temp_archive, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)

        final_path = os.path.join(outbox_dir, f"zencode_update_{digest.hexdigest()[:12]}.tar.gz")
        shutil.move(temp_archive, final_path)
        log_event(f"Shipped quarantinable artifact to outbox: {os.path.basename(final_path)}", "log-cmd")
        return {
            "status": "success",
            "artifact": os.path.basename(final_path),
            "sha256": digest.hexdigest(),
            "bytes": size,
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
