#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Seedling preflight"

if command -v docker >/dev/null 2>&1; then
  echo "ok: docker found at $(command -v docker)"
elif command -v podman >/dev/null 2>&1; then
  echo "ok: podman found at $(command -v podman)"
else
  echo "error: Docker or Podman is required for safe command sandboxing" >&2
  exit 1
fi

python3 -m py_compile zencode/app.py supervisor/main.py operator/main.py
echo "ok: python files compile"

if [ -d desktop/src-tauri ]; then
  echo "ok: desktop source present"
fi

echo "preflight complete"
