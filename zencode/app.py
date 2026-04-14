import os
import json
import httpx
import tempfile
import subprocess
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

def log_event(msg, evt_type="log-out"):
    sandbox_state["logs"].append({"msg": msg, "type": evt_type})

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
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=sandbox_state["sandbox_dir"],
            capture_output=True,
            text=True,
            timeout=30
        )
        out = result.stdout
        err = result.stderr
        if out: log_event(out, "log-out")
        if err: log_event(err, "log-err")
        return {"stdout": out, "stderr": err, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        msg = f"Command timed out after 30s: {command}"
        log_event(msg, "log-err")
        return {"error": msg}
    except Exception as e:
        log_event(str(e), "log-err")
        return {"error": str(e)}

def write_file(path: str, content: str):
    full_path = os.path.join(sandbox_state["sandbox_dir"], path)
    log_event(f"write_file: {path} ({len(content)} bytes)", "log-cmd")
    try:
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, 'w') as f:
            f.write(content)
        return {"status": "success", "path": path, "bytes_written": len(content)}
    except Exception as e:
        log_event(str(e), "log-err")
        return {"error": str(e)}

def read_file(path: str):
    full_path = os.path.join(sandbox_state["sandbox_dir"], path)
    log_event(f"read_file: {path}", "log-cmd")
    try:
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

    if not sandbox_state["sandbox_dir"]:
        env_dir = os.environ.get("ZENCODE_SANDBOX_DIR")
        if env_dir:
            os.makedirs(env_dir, exist_ok=True)
            sandbox_state["sandbox_dir"] = env_dir
        else:
            sandbox_state["sandbox_dir"] = tempfile.mkdtemp(prefix="zen_sandbox_")

    log_event(f"Sandbox initialized at {sandbox_state['sandbox_dir']}", "log-cmd")
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
    outbox_dir = os.path.join(sandbox_state["sandbox_dir"], "outbox")
    os.makedirs(outbox_dir, exist_ok=True)
    import shutil
    shutil.make_archive(os.path.join(outbox_dir, "zencode_update"), 'gztar', os.getcwd())
    log_event("Shipped self-update to outbox!", "log-cmd")
    return {"status": "success"}


@app.get("/api/logs")
async def get_logs(offset: int = Query(0, ge=0)):
    """Return logs from the given offset onward. Never clears the buffer."""
    return {
        "logs": sandbox_state["logs"][offset:],
        "next_offset": len(sandbox_state["logs"])
    }
