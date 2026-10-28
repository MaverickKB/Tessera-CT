#!/usr/bin/env bash
# Idempotent dependency install for a Cursor cloud VM.
# Safe to run more than once. Does not start the core and does not read a Tessera home.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (3.11 or newer)." >&2
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python3 3.11 or newer is required." >&2
  python3 --version >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "node is required for the web tests." >&2
  exit 1
fi

# The default image ships Python without the stdlib venv module.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi

if [[ ! -x .venv/bin/python ]]; then
  rm -rf .venv
fi
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -e .
.venv/bin/python -m pip install --disable-pip-version-check pytest

echo "Tessera cloud install ready."
echo "python: $(.venv/bin/python -V)"
echo "pytest: $(.venv/bin/python -m pytest --version | head -n 1)"
echo "node: $(node -v)"
