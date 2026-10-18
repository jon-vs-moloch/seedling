#!/usr/bin/env bash
# Checks that the Python compiles and that a container runtime exists for the
# command sandbox. Exits non-zero without Docker or Podman: that is the safe
# default, not a bug. Run scripts/test.sh for the smoke tests.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "Seedling preflight"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  && echo "ok: python $(python3 -c 'import platform; print(platform.python_version())')" \
  || echo "warn: Python 3.12+ is recommended (found $(python3 -V 2>&1))"

python3 -m py_compile zencode/app.py supervisor/main.py operator/main.py
echo "ok: python files compile"

if [ -d desktop/src-tauri ]; then
  echo "ok: desktop source present"
fi

if command -v docker >/dev/null 2>&1; then
  echo "ok: docker found at $(command -v docker)"
elif command -v podman >/dev/null 2>&1; then
  echo "ok: podman found at $(command -v podman)"
else
  echo "error: Docker or Podman is required for safe command sandboxing" >&2
  exit 1
fi

echo "preflight complete"
