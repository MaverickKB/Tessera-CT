#!/usr/bin/env bash
# One-command offline Tessera core for this VM.
#   bash scripts/dev-cloud.sh                  start in the background
#   bash scripts/dev-cloud.sh --foreground     start and wait (cloud VM boot)
#   bash scripts/dev-cloud.sh status
#   bash scripts/dev-cloud.sh stop
# Never touches ~/.tessera and never deletes sessions.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$root/.venv/bin/python"
if [[ ! -x "$python" ]]; then
  echo "Tessera cloud venv is missing. Run: bash scripts/cloud-agent-install.sh" >&2
  exit 1
fi
exec "$python" "$root/scripts/cloud_runtime.py" "$@"
