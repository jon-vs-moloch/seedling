#!/bin/bash
# Seedling Desktop Launcher
# Starts the Supervisor backend and opens the native desktop window.
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUPERVISOR_DIR="$REPO_ROOT/supervisor"
DESKTOP_DIR="$REPO_ROOT/desktop"

echo "🌱 Starting Seedling..."

# --- Step 1: Ensure Supervisor venv exists ---
if [ ! -f "$SUPERVISOR_DIR/venv/bin/python" ]; then
    echo "  Setting up Supervisor venv..."
    python3 -m venv "$SUPERVISOR_DIR/venv"
    "$SUPERVISOR_DIR/venv/bin/pip" install -r "$SUPERVISOR_DIR/requirements.txt" -q
fi

# --- Step 2: Start Supervisor backend in background ---
echo "  Starting Supervisor on :7000..."
"$SUPERVISOR_DIR/venv/bin/python" -m uvicorn main:app \
    --host 127.0.0.1 --port 7000 \
    --app-dir "$SUPERVISOR_DIR" &
SUPERVISOR_PID=$!

# Wait for the server to be ready
echo "  Waiting for Supervisor to come online..."
for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:7000/ >/dev/null 2>&1; then
        echo "  Supervisor ready."
        break
    fi
    sleep 0.5
done

# --- Step 3: Launch Tauri desktop window ---
echo "  Opening Seedling window..."
cd "$DESKTOP_DIR/src-tauri"
cargo run --release 2>&1

# --- Cleanup: kill supervisor when desktop window closes ---
echo "  Desktop closed. Stopping Supervisor..."
kill $SUPERVISOR_PID 2>/dev/null
echo "🌱 Seedling stopped."
