#!/bin/sh
# External development-mode recovery: this process stays alive while the core
# exits, launchd reloads it, and this same session is reattached by its slug.
#
# POSIX sh. /bin/sh on Debian and Ubuntu is dash, which rejects bash arrays.
# The same text must also run on the bash 3.2 that macOS still ships.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: scripts/dev-mode.sh <session-slug> [--rejoin]" >&2
  exit 2
fi
SESSION="$1"
MODE="${2:---restart}"
case "$SESSION" in
  ''|*[!A-Za-z0-9._-]*) echo "dev-mode: invalid session slug" >&2; exit 2 ;;
esac
case "$MODE" in
  --restart|--rejoin) ;;
  *) echo "usage: scripts/dev-mode.sh <session-slug> [--rejoin]" >&2; exit 2 ;;
esac

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
PORT="${TESSERA_PORT:-8740}"
BASE="http://127.0.0.1:$PORT"
# server.auth_mode = 'always' needs the shared key even on loopback; default mode
# does not, so an unset TESSERA_KEY changes nothing here.
TIMEOUT="${TESSERA_RESTART_TIMEOUT:-300}"

http_get() {
  if [ -n "${TESSERA_KEY:-}" ]; then
    curl -fsS -m 3 -H "X-Tessera-Key: $TESSERA_KEY" "$1" 2>/dev/null || true
  else
    curl -fsS -m 3 "$1" 2>/dev/null || true
  fi
}

if [ "$MODE" = "--restart" ]; then
  # This script runs in the agent/operator shell, outside the backend process.
  # The session continues in this controller while the core is replaced.
  bash "$ROOT/scripts/restart-core.sh"
fi

waited=0
while [ "$waited" -lt "$TIMEOUT" ]; do
  status="$(http_get "$BASE/api/status")"
  session="$(http_get "$BASE/api/sessions/$SESSION")"
  if [ -n "$status" ] && [ -n "$session" ]; then
    python3 - "$status" "$session" "$SESSION" "$BASE" <<'PY'
import json
import sys
status = json.loads(sys.argv[1])
session = json.loads(sys.argv[2])
slug = sys.argv[3]
if session.get("slug") != slug:
    raise SystemExit("dev-mode: reconnected session slug did not match")
print("dev-mode: core ready; same session is available")
print(f"generation: {status.get('generation', 'unknown')}")
print(f"session: {slug} ({session.get('title', 'untitled')})")
print(f"session busy: {bool(status.get('sessions', {}).get(slug, {}).get('busy'))}")
print(f"rejoin: {sys.argv[4]}/#view=chat&session={slug}")
PY
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done

echo "dev-mode: core did not expose status and session $SESSION within ${TIMEOUT}s" >&2
exit 1
