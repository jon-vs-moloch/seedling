"""
ZenCode Supervisor — Dual-Operator Control Plane

Manages the nested sandbox topology:
  Operator A (meta)  →  ZenCode-A  →  produces operator patches
  Operator B (ground) →  ZenCode-B  →  produces zencode patches

All four processes are started and monitored by this supervisor.
A web dashboard exposes the live state of the system.
"""

import os
import sys
import signal
import shutil
import tempfile
import subprocess
import asyncio
import time
import json
import tarfile
import hashlib
import uuid
from datetime import datetime
from fastapi import FastAPI, Request, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response, FileResponse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ZENCODE_SRC = os.path.join(PROJECT_ROOT, 'zencode')
OPERATOR_SRC = os.path.join(PROJECT_ROOT, 'operator')

ZENCODE_PYTHON = os.path.join(ZENCODE_SRC, 'venv', 'bin', 'python')
OPERATOR_PYTHON = os.path.join(OPERATOR_SRC, 'venv', 'bin', 'python')

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
RUNTIME_ROOT = os.path.join(PROJECT_ROOT, ".seedling_runtime")
MAX_ARTIFACT_BYTES = int(os.environ.get("SEEDLING_MAX_ARTIFACT_BYTES", str(25 * 1024 * 1024)))
MAX_ARTIFACT_EXPANDED_BYTES = int(os.environ.get("SEEDLING_MAX_ARTIFACT_EXPANDED_BYTES", str(50 * 1024 * 1024)))
MAX_ARTIFACT_MEMBERS = int(os.environ.get("SEEDLING_MAX_ARTIFACT_MEMBERS", "1000"))

# ---------------------------------------------------------------------------
# Process registry
# ---------------------------------------------------------------------------

class ManagedProcess:
    """Tracks a subprocess managed by the Supervisor."""
    def __init__(self, name, role, port, cwd, cmd, env_extras=None, sandbox_dir=None):
        self.name = name
        self.role = role          # "zencode" or "operator"
        self.port = port
        self.cwd = cwd
        self.cmd = cmd
        self.env_extras = env_extras or {}
        self.sandbox_dir = sandbox_dir  # For ZenCode instances: where their workspace lives
        self.process = None
        self.status = "stopped"
        self.started_at = None
        self.restart_count = 0

    def to_dict(self):
        return {
            "name": self.name,
            "role": self.role,
            "port": self.port,
            "status": self.status,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "sandbox_dir": self.sandbox_dir,
            "url": f"http://127.0.0.1:{self.port}",
        }


class Supervisor:
    def __init__(self):
        self.base_dir = None
        self.quarantine_dir = None
        self.processes: dict[str, ManagedProcess] = {}
        self.logs = []
        self.update_history = []
        self.seen_artifacts = set()
        self.resetting = False

    def log(self, msg, source="supervisor", level="info"):
        entry = {
            "ts": datetime.now().isoformat(),
            "source": source,
            "level": level,
            "msg": msg,
        }
        self.logs.append(entry)
        tag = source.upper()
        print(f"[{tag}] {msg}")

    def _ensure_venv(self, label, src_dir, python_path):
        """Create and populate a module's venv if it doesn't already exist."""
        if os.path.exists(python_path):
            return
        self.log(f"{label} venv missing — provisioning now...", source="supervisor")
        reqs = os.path.join(src_dir, "requirements.txt")
        venv_dir = os.path.join(src_dir, "venv")

        subprocess.run(
            [sys.executable, "-m", "venv", venv_dir],
            check=True, capture_output=True
        )
        subprocess.run(
            [python_path, "-m", "pip", "install", "-r", reqs, "-q"],
            check=True, capture_output=True
        )
        if label == "Operator":
            subprocess.run(
                [python_path, "-m", "playwright", "install", "chromium"],
                check=True, capture_output=True
            )
        self.log(f"{label} venv ready.", source="supervisor")

    def _process_env(self, mp):
        """Build a minimal child environment without forwarding host secrets."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONUNBUFFERED": "1",
            "HOME": self.base_dir or tempfile.gettempdir(),
            "TMPDIR": self.base_dir or tempfile.gettempdir(),
            "NO_COLOR": "1",
        }
        env.update(mp.env_extras)
        return env

    def initialize(self):
        runtime_root = os.environ.get("SEEDLING_RUNTIME_DIR", RUNTIME_ROOT)
        os.makedirs(runtime_root, exist_ok=True)
        self.base_dir = tempfile.mkdtemp(prefix="zen_stack_", dir=runtime_root)
        self.quarantine_dir = os.path.join(self.base_dir, "quarantine")
        self.seen_artifacts = set()
        self.log(f"Stack workspace: {self.base_dir}")

        # Auto-provision venvs if not already set up
        self._ensure_venv("ZenCode", ZENCODE_SRC, ZENCODE_PYTHON)
        self._ensure_venv("Operator", OPERATOR_SRC, OPERATOR_PYTHON)

        # Create sandbox work directories
        sandbox_a = os.path.join(self.base_dir, "zencode_a_workspace")
        sandbox_b = os.path.join(self.base_dir, "zencode_b_workspace")
        work_a = os.path.join(self.base_dir, "operator_a_work")
        work_b = os.path.join(self.base_dir, "operator_b_work")

        for d in [sandbox_a, sandbox_b, work_a, work_b, self.quarantine_dir]:
            os.makedirs(d, exist_ok=True)

        # --- ZenCode instances ---
        self.processes["zencode_a"] = ManagedProcess(
            name="zencode_a",
            role="zencode",
            port=8000,
            cwd=ZENCODE_SRC,
            cmd=[ZENCODE_PYTHON, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
            env_extras={
                "ZENCODE_SANDBOX_DIR": sandbox_a,
                "SEEDLING_SANDBOX_MODE": os.environ.get("SEEDLING_SANDBOX_MODE", "container"),
                "SEEDLING_SANDBOX_NETWORK": os.environ.get("SEEDLING_SANDBOX_NETWORK", "none"),
                "SEEDLING_SANDBOX_IMAGE": os.environ.get("SEEDLING_SANDBOX_IMAGE", "python:3.12-slim"),
            },
            sandbox_dir=sandbox_a,
        )
        self.processes["zencode_b"] = ManagedProcess(
            name="zencode_b",
            role="zencode",
            port=8001,
            cwd=ZENCODE_SRC,
            cmd=[ZENCODE_PYTHON, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8001"],
            env_extras={
                "ZENCODE_SANDBOX_DIR": sandbox_b,
                "SEEDLING_SANDBOX_MODE": os.environ.get("SEEDLING_SANDBOX_MODE", "container"),
                "SEEDLING_SANDBOX_NETWORK": os.environ.get("SEEDLING_SANDBOX_NETWORK", "none"),
                "SEEDLING_SANDBOX_IMAGE": os.environ.get("SEEDLING_SANDBOX_IMAGE", "python:3.12-slim"),
            },
            sandbox_dir=sandbox_b,
        )

        # --- Operator instances ---
        self.processes["operator_b"] = ManagedProcess(
            name="operator_b",
            role="operator",
            port=9001,
            cwd=OPERATOR_SRC,
            cmd=[OPERATOR_PYTHON, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "9001"],
            env_extras={
                "OPERATOR_TARGET": "http://127.0.0.1:8001/static/index.html",
                "OPERATOR_GOAL": "Use ZenCode to improve the ZenCode codebase",
                "OPERATOR_ROLE": "Ground Operator (B)",
                "OPERATOR_WORK_DIR": work_b,
                "VLM_ENDPOINT": "http://127.0.0.1:1234/v1/chat/completions",
            },
        )
        self.processes["operator_a"] = ManagedProcess(
            name="operator_a",
            role="operator",
            port=9000,
            cwd=OPERATOR_SRC,
            cmd=[OPERATOR_PYTHON, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "9000"],
            env_extras={
                "OPERATOR_TARGET": "http://127.0.0.1:8000/static/index.html",
                "OPERATOR_GOAL": "Use ZenCode to improve the Operator codebase",
                "OPERATOR_ROLE": "Meta Operator (A)",
                "OPERATOR_WORK_DIR": work_a,
                "VLM_ENDPOINT": "http://127.0.0.1:1234/v1/chat/completions",
            },
        )

    def start_process(self, name):
        mp = self.processes[name]
        env = self._process_env(mp)

        self.log(f"Starting {name} on :{mp.port}...", source=name)

        mp.process = subprocess.Popen(
            mp.cmd,
            cwd=mp.cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
            text=True,
        )
        mp.status = "running"
        mp.started_at = datetime.now().isoformat()
        self.log(f"{name} started (PID {mp.process.pid})", source=name)

    def stop_process(self, name):
        mp = self.processes.get(name)
        if not mp or not mp.process:
            return
        self.log(f"Stopping {name}...", source=name)
        try:
            os.killpg(os.getpgid(mp.process.pid), signal.SIGTERM)
            mp.process.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(mp.process.pid), signal.SIGKILL)
            except Exception:
                pass
        mp.status = "stopped"
        mp.process = None

    def start_all(self):
        """Start all processes in dependency order."""
        # ZenCode instances first (operators depend on them)
        for name in ["zencode_a", "zencode_b"]:
            self.start_process(name)
        # Brief delay for servers to bind
        time.sleep(2)
        # Then operators
        for name in ["operator_b", "operator_a"]:
            self.start_process(name)
        self.log("All processes started.")

    def stop_all(self):
        """Stop all processes in reverse order."""
        for name in ["operator_a", "operator_b", "zencode_b", "zencode_a"]:
            self.stop_process(name)
        self.log("All processes stopped.")

    def health_check(self):
        """Check which processes are still alive."""
        results = {}
        for name, mp in self.processes.items():
            if mp.process and mp.process.poll() is not None:
                mp.status = f"crashed (exit {mp.process.returncode})"
                self.log(f"{name} died with exit code {mp.process.returncode}", source=name, level="error")
                mp.process = None
            results[name] = mp.status
        return results

    def poll_outboxes(self):
        """Check ZenCode outboxes for update artifacts."""
        updates = []
        for name in ["zencode_a", "zencode_b"]:
            mp = self.processes[name]
            if not mp.sandbox_dir:
                continue
            outbox = os.path.join(mp.sandbox_dir, "outbox")
            if not os.path.exists(outbox):
                continue
            artifacts = os.listdir(outbox)
            for artifact_name in sorted(artifacts):
                target = "operator" if name == "zencode_a" else "zencode"
                artifact_path = os.path.join(outbox, artifact_name)
                artifact_key = self._artifact_key(artifact_path)
                if artifact_key in self.seen_artifacts:
                    continue
                self.seen_artifacts.add(artifact_key)
                try:
                    updates.append(self.quarantine_artifact(name, target, artifact_path))
                except Exception as e:
                    self.log(f"Artifact quarantine failed for {artifact_name}: {e}", source="supervisor", level="error")
        return updates

    def _artifact_key(self, artifact):
        stat = os.stat(artifact)
        return f"{os.path.realpath(artifact)}:{stat.st_mtime_ns}:{stat.st_size}"

    def _sha256(self, path):
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_archive(self, artifact):
        if not tarfile.is_tarfile(artifact):
            raise ValueError("Update artifacts must be tar archives")
        archive_size = os.path.getsize(artifact)
        if archive_size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"Artifact is {archive_size} bytes; limit is {MAX_ARTIFACT_BYTES}")

        expanded_bytes = 0
        with tarfile.open(artifact, "r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_ARTIFACT_MEMBERS:
                raise ValueError(f"Artifact has {len(members)} members; limit is {MAX_ARTIFACT_MEMBERS}")
            for member in members:
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsupported archive member type: {member.name}")
                normalized = os.path.normpath(member.name)
                if normalized.startswith("..") or os.path.isabs(normalized):
                    raise ValueError(f"Archive member escapes target: {member.name}")
                expanded_bytes += max(member.size, 0)
                if expanded_bytes > MAX_ARTIFACT_EXPANDED_BYTES:
                    raise ValueError(
                        f"Artifact expands past {MAX_ARTIFACT_EXPANDED_BYTES} bytes"
                    )
        return {"archive_bytes": archive_size, "expanded_bytes": expanded_bytes, "members": len(members)}

    def quarantine_artifact(self, source, target, artifact):
        artifact_id = uuid.uuid4().hex[:12]
        safe_name = os.path.basename(artifact)
        staged_path = os.path.join(self.quarantine_dir, f"{artifact_id}-{safe_name}")
        shutil.copy2(artifact, staged_path)
        validation = self._validate_archive(staged_path)
        digest = self._sha256(staged_path)
        update = {
            "id": artifact_id,
            "source": source,
            "target_codebase": target,
            "artifact_path": artifact,
            "quarantine_path": staged_path,
            "sha256": digest,
            "status": "quarantined",
            "detected_at": datetime.now().isoformat(),
            **validation,
        }
        self.log(
            f"Quarantined artifact {artifact_id} from {source} for {target}/ ({validation['archive_bytes']} bytes)",
            source="supervisor",
        )
        return update

    def teardown(self):
        self.stop_all()
        if self.base_dir and os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir, ignore_errors=True)
            self.log("Stack workspace destroyed.")

    def reset_stack(self):
        """Tear down runtime state and restart from clean sandboxes."""
        self.resetting = True
        self.log("Reset requested. Recreating stack workspace...", source="supervisor")
        try:
            self.teardown()
            self.processes = {}
            self.update_history = []
            self.initialize()
            self.start_all()
            self.log("Reset complete.", source="supervisor")
        finally:
            self.resetting = False

    def safety_status(self):
        runtime = os.environ.get("SEEDLING_CONTAINER_RUNTIME") or shutil.which("docker") or shutil.which("podman")
        return {
            "sandbox_mode": os.environ.get("SEEDLING_SANDBOX_MODE", "container"),
            "container_runtime": runtime,
            "network": os.environ.get("SEEDLING_SANDBOX_NETWORK", "none"),
            "artifact_policy": "quarantine-and-manual-apply",
            "env_policy": "allowlist",
            "reset": "available",
            "artifact_limits": {
                "archive_bytes": MAX_ARTIFACT_BYTES,
                "expanded_bytes": MAX_ARTIFACT_EXPANDED_BYTES,
                "members": MAX_ARTIFACT_MEMBERS,
            },
        }

    def _copy_tree_contents(self, source_dir, target_dir):
        """Copy source directory contents over the target directory."""
        for item in os.listdir(source_dir):
            source = os.path.join(source_dir, item)
            target = os.path.join(target_dir, item)
            if os.path.isdir(source):
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)

    def _safe_extract_archive(self, artifact, extract_dir):
        """Extract a tar artifact without allowing paths to escape extract_dir."""
        extract_root = os.path.abspath(extract_dir)
        with tarfile.open(artifact, "r:*") as archive:
            for member in archive.getmembers():
                if not (member.isfile() or member.isdir()):
                    raise ValueError(f"Unsupported archive member type: {member.name}")
                member_path = os.path.abspath(os.path.join(extract_root, member.name))
                if os.path.commonpath([extract_root, member_path]) != extract_root:
                    raise ValueError(f"Archive member escapes extraction directory: {member.name}")
            archive.extractall(extract_root)

    def _apply_artifact_contents(self, artifact, target_src):
        """Apply either a directory artifact or supported archive into target_src."""
        if os.path.isdir(artifact):
            self._copy_tree_contents(artifact, target_src)
            return

        if tarfile.is_tarfile(artifact):
            with tempfile.TemporaryDirectory(prefix="zen_update_") as extract_dir:
                self._safe_extract_archive(artifact, extract_dir)
                self._copy_tree_contents(extract_dir, target_src)
            return

        raise ValueError(f"Unsupported update artifact format: {artifact}")

    def apply_update(self, update_id):
        """Applies a detected code artifact to the source tree."""
        update = next((u for u in self.update_history if u.get("id") == update_id), None)
        if not update:
            raise ValueError(f"Unknown update artifact: {update_id}")
        if update.get("status") != "quarantined":
            raise ValueError(f"Update {update_id} is not ready to apply")

        target_path = update["target_codebase"]  # "operator" or "zencode"
        artifact = update["quarantine_path"]
        target_src = ZENCODE_SRC if target_path == "zencode" else OPERATOR_SRC
        affected = (
            ["operator_a", "operator_b"]
            if target_path == "operator"
            else ["zencode_a", "zencode_b"]
        )
        running_before_update = [
            name for name in affected
            if self.processes[name].process and self.processes[name].process.poll() is None
        ]

        self.log(f"Applying approved update {update_id} to {target_src}...", source="supervisor")
        update["status"] = "applying"
        update["approved_at"] = datetime.now().isoformat()

        for name in affected:
            self.stop_process(name)

        try:
            self._apply_artifact_contents(artifact, target_src)

            update["status"] = "applied"
            update["applied_at"] = datetime.now().isoformat()
            self.log(f"Applied update {update_id}.", source="supervisor")
        except Exception as e:
            update["status"] = "failed"
            update["error"] = str(e)
            self.log(f"Update application failed: {e}", source="supervisor", level="error")
        finally:
            if running_before_update:
                self.log("Restarting affected stack...", source="supervisor")
            for name in running_before_update:
                self.start_process(name)

    async def monitor(self):
        """Periodic health checks and outbox polling."""
        while True:
            if not self.resetting:
                self.health_check()
                updates = self.poll_outboxes()
                self.update_history.extend(updates)
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

sup = Supervisor()
app = FastAPI(title="ZenCode Supervisor")

# Serve dashboard
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def startup():
    import logging
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    try:
        sup.initialize()
        sup.start_all()
    except RuntimeError as e:
        sup.log(str(e), level="error")
        return

    # Background monitor loop
    asyncio.create_task(sup.monitor())


@app.on_event("shutdown")
async def shutdown():
    sup.teardown()


# --- API endpoints ---

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/topology")
async def get_topology():
    """Return the full system topology."""
    return {
        "processes": {name: mp.to_dict() for name, mp in sup.processes.items()},
        "base_dir": sup.base_dir,
        "safety": sup.safety_status(),
    }


@app.get("/api/logs")
async def get_logs(offset: int = Query(0, ge=0)):
    return {
        "logs": sup.logs[offset:],
        "next_offset": len(sup.logs),
    }


@app.get("/api/updates")
async def get_updates():
    return {"updates": sup.update_history}


@app.post("/api/updates/{update_id}/apply")
async def apply_update_endpoint(update_id: str):
    try:
        sup.apply_update(update_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/reset")
async def reset_stack_endpoint():
    try:
        sup.reset_stack()
        return {"ok": True, "base_dir": sup.base_dir}
    except Exception as e:
        sup.log(f"Reset failed: {e}", source="supervisor", level="error")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/safety")
async def safety_endpoint():
    return sup.safety_status()


@app.post("/api/process/{name}/start")
async def start_proc(name: str):
    if name not in sup.processes:
        return JSONResponse(status_code=404, content={"error": f"Unknown process: {name}"})
    sup.start_process(name)
    return {"ok": True}


@app.post("/api/process/{name}/stop")
async def stop_proc(name: str):
    if name not in sup.processes:
        return JSONResponse(status_code=404, content={"error": f"Unknown process: {name}"})
    sup.stop_process(name)
    return {"ok": True}


@app.post("/api/process/{name}/restart")
async def restart_proc(name: str):
    if name not in sup.processes:
        return JSONResponse(status_code=404, content={"error": f"Unknown process: {name}"})
    sup.stop_process(name)
    time.sleep(1)
    sup.start_process(name)
    sup.processes[name].restart_count += 1
    return {"ok": True}


@app.get("/api/operator/{name}/image")
async def get_operator_image(name: str):
    """Proxy an operator's live screenshot."""
    mp = sup.processes.get(name)
    if not mp or mp.role != "operator":
        return Response(content=b"", media_type="image/png")
    work_dir = mp.env_extras.get("OPERATOR_WORK_DIR", "")
    img_path = os.path.join(work_dir, "current_view.png")
    try:
        with open(img_path, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    except Exception:
        return Response(content=b"", media_type="image/png")
