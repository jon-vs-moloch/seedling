#!/usr/bin/env bash
# Run Seedling's smoke tests in a throwaway virtualenv (.venv-test/).
# Needs only python3; never starts the stack, a container or a model.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -x .venv-test/bin/python ]; then
  python3 -m venv .venv-test
  .venv-test/bin/pip install -q -r zencode/requirements.txt -r supervisor/requirements.txt
fi
.venv-test/bin/python -m unittest discover -s tests -v
