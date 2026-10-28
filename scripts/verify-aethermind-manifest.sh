#!/usr/bin/env bash
# Verify an AetherMind engine tree against the pinned provenance manifest.
#
# The manifest (docs/packaging/aethermind-engine-manifest-20261023.json) pins the
# authoritative engine by sha256 and deliberately names no host path. The resolver
# is the operator: set the engine tree directory to check and, if the tree lives on
# another machine, the host.
#
# Usage:
#   TESSERA_ENGINE_ROOT=/path/to/engine/tree scripts/verify-aethermind-manifest.sh
#   TESSERA_ENGINE_ROOT=/path TESSERA_PACKAGING_HOST=otherhost scripts/verify-aethermind-manifest.sh [manifest.json]
#   TESSERA_ENGINE_ROOT=/path TESSERA_ENGINE_LOCAL=1 scripts/verify-aethermind-manifest.sh
#
# TESSERA_ENGINE_LOCAL=1 checks a tree on THIS machine instead of over ssh.
# A pin that exists only as a local copy still has to be verifiable without
# a serving host.
#
# The check re-hashes every file the manifest pins and compares byte-for-byte against
# the recorded hashes. Exit 0 means a complete match.
set -euo pipefail

HOST="${TESSERA_PACKAGING_HOST:-}"
ROOT="${TESSERA_ENGINE_ROOT:-}"
LOCAL="${TESSERA_ENGINE_LOCAL:-0}"
MANIFEST="$(cd "$(dirname "$0")/.." && pwd)/${1:-docs/packaging/aethermind-engine-manifest-20261023.json}"

if [ -z "$ROOT" ]; then
  echo "verify-aethermind-manifest: TESSERA_ENGINE_ROOT is not set." >&2
  echo "Set it to the engine tree directory (the folder holding lib/ and mcp/)." >&2
  exit 2
fi
if [ ! -f "$MANIFEST" ]; then
  echo "verify-aethermind-manifest: manifest not found: $MANIFEST" >&2
  exit 2
fi

# The checker code is base64 so shell quoting cannot mangle it. The manifest itself
# streams over stdin untouched.
RUNNER_B64="$(python3 - <<'PY' | base64
print('''import hashlib, json, os, sys

plan = json.load(sys.stdin)
pins = plan.get("files") or {}
if plan.get("algorithm") != "sha256":
    print("verify-aethermind-manifest: unsupported algorithm", file=sys.stderr)
    sys.exit(2)
checked = 0
for relpath in sorted(pins):
    if not os.path.isfile(relpath):
        print("MISSING  " + relpath, file=sys.stderr)
        continue
    digest = hashlib.sha256(open(relpath, "rb").read()).hexdigest()
    if digest == pins[relpath]:
        checked += 1
    else:
        print("MISMATCH " + relpath, file=sys.stderr)
if checked == len(pins):
    print("verify-aethermind-manifest: %d of %d pinned files match" % (checked, len(pins)))
    sys.exit(0)
print("verify-aethermind-manifest: verification failed (%d of %d matched)" % (checked, len(pins)), file=sys.stderr)
sys.exit(1)
''')
PY
)"

if [ "$LOCAL" = "1" ]; then
  ( cd "$ROOT" && python3 -c "$(printf '%s' "$RUNNER_B64" | base64 -d)" < "$MANIFEST" )
else
  if [ -z "$HOST" ]; then
    echo "verify-aethermind-manifest: no remote host is set." >&2
    echo "Set TESSERA_PACKAGING_HOST, or set TESSERA_ENGINE_LOCAL=1 to check TESSERA_ENGINE_ROOT on this machine." >&2
    exit 2
  fi
  cat "$MANIFEST" | ssh "$HOST" "cd \"$ROOT\" && (echo \"$RUNNER_B64\" | base64 -d > /tmp/aethermind-verify-runner.py) && python3 /tmp/aethermind-verify-runner.py && rm -f /tmp/aethermind-verify-runner.py"
fi
