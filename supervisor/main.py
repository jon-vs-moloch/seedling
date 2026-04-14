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
        self.processes: dict[str, ManagedProcess] = {}
        self.logs = []
        self.update_history = []

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

    def initialize(self):
        self.base_dir = tempfile.mkdtemp(prefix="zen_stack_")
        self.log(f"Stack workspace: {self.base_dir}")

        # Verify venvs exist
        for label, python_path in [("ZenCode", ZENCODE_PYTHON), ("Operator", OPERATOR_PYTHON)]:
            if not os.path.exists(python_path):
                self.log(f"{label} venv not found at {python_path}. Run the module's run.sh first to create it.", level="error")
                raise RuntimeError(f"Missing venv: {python_path}")

        # Create sandbox work directories
        sandbox_a = os.path.join(self.base_dir, "zencode_a_workspace")
        sandbox_b = os.path.join(self.base_dir, "zencode_b_workspace")
        work_a = os.path.join(self.base_dir, "operator_a_work")
        work_b = os.path.join(self.base_dir, "operator_b_work")

        for d in [sandbox_a, sandbox_b, work_a, work_b]:
            os.makedirs(d, exist_ok=True)

        # --- ZenCode instances ---
        self.processes["zencode_a"] = ManagedProcess(
            name="zencode_a",
            role="zencode",
            port=8000,
            cwd=ZENCODE_SRC,
            cmd=[ZENCODE_PYTHON, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
            env_extras={"ZENCODE_SANDBOX_DIR": sandbox_a},
            sandbox_dir=sandbox_a,
        )
        self.processes["zencode_b"] = ManagedProcess(
            name="zencode_b",
            role="zencode",
            port=8001,
            cwd=ZENCODE_SRC,
            cmd=[ZENCODE_PYTHON, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8001"],
            env_extras={"ZENCODE_SANDBOX_DIR": sandbox_b},
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
        env = os.environ.copy()
        env.update(mp.env_extras)

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
            if artifacts:
                target = "operator" if name == "zencode_a" else "zencode"
                updates.append({
                    "source": name,
                    "target_codebase": target,
                    "artifact_path": os.path.join(outbox, artifacts[0]),
                    "detected_at": datetime.now().isoformat(),
                })
        return updates

    def teardown(self):
        self.stop_all()
        if self.base_dir and os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir, ignore_errors=True)
            self.log("Stack workspace destroyed.")


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
    asyncio.create_task(monitor_loop())


@app.on_event("shutdown")
async def shutdown():
    sup.teardown()


async def monitor_loop():
    """Periodic health checks and outbox polling."""
    while True:
        sup.health_check()

        updates = sup.poll_outboxes()
        for update in updates:
            sup.log(
                f"Update artifact detected! Source: {update['source']}, "
                f"Target: {update['target_codebase']}, "
                f"Path: {update['artifact_path']}",
                level="info",
            )
            sup.update_history.append(update)
            # TODO: Apply update, restart affected processes

        await asyncio.sleep(3)


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
