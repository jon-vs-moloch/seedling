import asyncio
import base64
import json
import httpx
import os
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration — all tunable via environment variables
# ---------------------------------------------------------------------------
OPERATOR_TARGET = os.environ.get("OPERATOR_TARGET", "http://127.0.0.1:8000/static/index.html")
VLM_ENDPOINT = os.environ.get("VLM_ENDPOINT", "http://127.0.0.1:1234/v1/chat/completions")
OPERATOR_GOAL = os.environ.get("OPERATOR_GOAL", "Use ZenCode to improve ZenCode")
OPERATOR_ROLE = os.environ.get("OPERATOR_ROLE", "Operator")
WORK_DIR = os.environ.get("OPERATOR_WORK_DIR", os.path.dirname(os.path.abspath(__file__)))

# Ensure work dir exists
os.makedirs(WORK_DIR, exist_ok=True)

SCREENSHOT_PATH = os.path.join(WORK_DIR, "current_view.png")
TELEMETRY_LOG = os.path.join(WORK_DIR, "operator_telemetry.log")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI()

# Serve operator dashboard static files from the source directory
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

op_state = {
    "status": "paused",
    "goal": OPERATOR_GOAL,
    "endpoint_url": OPERATOR_TARGET,
    "vlm_endpoint": VLM_ENDPOINT,
    "role": OPERATOR_ROLE,
    "history": [],
    "recent_action": "Awaiting commands..."
}

# ---------------------------------------------------------------------------
# VLM interaction
# ---------------------------------------------------------------------------

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def _write_telemetry(line):
    try:
        with open(TELEMETRY_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

async def get_vlm_action(screenshot_path, goal):
    base64_image = encode_image(screenshot_path)

    system_msg = """\
You are an autonomous VLM Operator orchestrating the ZenCode UI.
You are given a goal and a screenshot of the current UI.
Your output MUST be a strict JSON object specifying your next action.
Allowed actions:
{ "action": "click", "selector": "button:has-text('Connect')" }
{ "action": "fill", "selector": "input[type='url']", "value": "text to type" }
{ "action": "wait", "ms": 2000 }
{ "action": "done" }
IMPORTANT: You do NOT have the HTML source code, only the screenshot.
DO NOT guess CSS IDs like `#buttonId`! Instead, use structural selectors
(e.g. `textarea`, `input[type='url']`) or Playwright text selectors
(e.g. `text="Connect & Probe"` or `button:has-text('Ship')`).
Output ONLY the raw JSON object, no markdown blocks."""

    if not op_state["history"]:
        op_state["history"].append({"role": "system", "content": system_msg})

    user_content = [
        {"type": "text", "text": f"Goal: {goal}\nAnalyze the UI and output the next json action."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
    ]

    messages = op_state["history"] + [{"role": "user", "content": user_content}]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                op_state["vlm_endpoint"],
                json={
                    "model": "local-model",
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 1000
                },
                timeout=120
            )
            data = resp.json()
            if "choices" not in data:
                print(f"[Operator] Provider API Error: {data}")
                return {"action": "wait", "ms": 3000, "error": f"API provided no choices: {data}"}

            reply = data["choices"][0]["message"]["content"]

            # Keep history compact — store text-only summary, not the full image
            op_state["history"].append({"role": "user", "content": f"Goal: {goal}. Provide next action."})
            op_state["history"].append({"role": "assistant", "content": reply})

            import re
            match = re.search(r'\{.*\}', reply, re.DOTALL)
            if match:
                raw_json = match.group(0)
                return json.loads(raw_json)
            else:
                return json.loads(reply.strip('`').strip('json').strip())
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        _write_telemetry(f"VLM Error: {err_msg}\nRaw Reply: {locals().get('reply', 'None')}")
        return {"action": "wait", "ms": 3000, "error": str(e)}

# ---------------------------------------------------------------------------
# Main operator loop
# ---------------------------------------------------------------------------

async def operator_loop():
    print(f"[{OPERATOR_ROLE}] Booting — targeting {OPERATOR_TARGET}")
    _write_telemetry(f"--- NEW SESSION: {OPERATOR_ROLE} ---")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()

        await page.goto(op_state["endpoint_url"])

        while True:
            # Self-healing: reopen browser if closed
            try:
                if page.is_closed() or not browser.is_connected():
                    op_state["recent_action"] = "Browser closed. Reopening..."
                    try:
                        if browser.is_connected():
                            await browser.close()
                    except Exception:
                        pass
                    browser = await p.chromium.launch(headless=False)
                    context = await browser.new_context(no_viewport=True)
                    page = await context.new_page()
                    await page.goto(op_state["endpoint_url"])
                    continue
            except Exception:
                pass

            # Always take screenshots so the dashboard stays live
            shot_tmp = SCREENSHOT_PATH + ".tmp"
            try:
                await page.screenshot(path=shot_tmp)
                os.replace(shot_tmp, SCREENSHOT_PATH)
            except Exception:
                pass

            if op_state["status"] == "paused":
                await asyncio.sleep(1)
                continue

            op_state["recent_action"] = "Taking screenshot and consulting VLM..."

            decision = await get_vlm_action(SCREENSHOT_PATH, op_state["goal"])
            op_state["recent_action"] = f"Action decided: {decision}"

            action = decision.get("action")
            selector = decision.get("selector")

            try:
                if action == "click" and selector:
                    await page.click(selector, timeout=3000)
                elif action == "fill" and selector:
                    val = decision.get("value", "")
                    await page.fill(selector, val, timeout=3000)
                    await page.press(selector, 'Enter')
                elif action == "wait":
                    ms = decision.get("ms", 2000)
                    await page.wait_for_timeout(ms)
                elif action == "done":
                    op_state["recent_action"] = "Goal Achieved. Pausing."
                    op_state["status"] = "paused"
                    _write_telemetry(f"[{OPERATOR_ROLE}] Goal Achieved.")

                _write_telemetry(f"[{OPERATOR_ROLE}] SUCCESS: {decision}")
            except Exception as e:
                op_state["recent_action"] = f"Failed UI action: {e}"
                _write_telemetry(f"[{OPERATOR_ROLE}] FAILED UI ACTION: {decision} | Error: {e}")

            await asyncio.sleep(1)

        await browser.close()

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    import logging
    log = logging.getLogger("uvicorn.access")
    log.setLevel(logging.WARNING)
    asyncio.create_task(operator_loop())

@app.get("/api/state")
async def get_state():
    return op_state

@app.get("/api/image")
async def get_image():
    try:
        with open(SCREENSHOT_PATH, "rb") as f:
            return Response(content=f.read(), media_type="image/png")
    except Exception:
        return Response(content=b"", media_type="image/png")

@app.post("/api/control")
async def post_control(req: Request):
    data = await req.json()
    if "status" in data:
        op_state["status"] = data["status"]
    if "goal" in data:
        op_state["goal"] = data["goal"]
    return {"ok": True}
