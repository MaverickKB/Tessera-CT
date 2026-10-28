#!/bin/sh
set -eu
os_name=$(uname -s 2>/dev/null || echo unknown)
if [ "$os_name" != "Darwin" ]; then
  echo "restart-core: this script supervises a macOS LaunchAgent." >&2
  echo "restart-core: LaunchAgent start and stop is not available on $os_name." >&2
  echo "restart-core: run the core in the foreground instead:" >&2
  echo "  python -m tessera serve" >&2
  exit 2
fi
PORT="${TESSERA_PORT:-8740}"
LABEL="${TESSERA_CORE_LABEL:-app.tessera.core}"
TIMEOUT="${TESSERA_RESTART_TIMEOUT:-300}"
BASE="http://127.0.0.1:$PORT"
AUTH=()
if [ -n "${TESSERA_KEY:-}" ]; then
  AUTH=(-H "X-Tessera-Key: $TESSERA_KEY")
fi

WHEN="now"
WAIT=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --when)
      [ "$#" -ge 2 ] || { echo "restart-core: --when needs a value" >&2; exit 2; }
      WHEN="$2"; shift 2 ;;
    --when=*) WHEN="${1#--when=}"; shift ;;
    --no-wait) WAIT=0; shift ;;
    -h|--help)
      echo "usage: restart-core.sh [--when now|idle] [--no-wait]" >&2
      exit 0 ;;
    *)
      echo "restart-core: unknown argument: $1" >&2
      exit 2 ;;
  esac
done
case "$WHEN" in
  now|idle) ;;
  *) echo "restart-core: --when must be 'now' or 'idle'" >&2; exit 2 ;;
esac

supervisor_pid() {
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}'
}

listening_pid() {
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1
}

supervised_pid="$(supervisor_pid)"
listen_pid="$(listening_pid)"

if [ -z "$supervised_pid" ]; then
  echo "restart-core: launchd job $LABEL is not loaded, so nothing would start the core again." >&2
  echo "restart-core: not asking the core to exit. Load the job first:" >&2
  echo "  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$LABEL.plist" >&2
  echo "restart-core: if desktop.toml has keep_core = true, the Tessera app is the supervisor:" >&2
  echo "  restart from the app (Settings -> Restart core), or quit and reopen it." >&2
  if [ -n "$listen_pid" ]; then
    echo "restart-core: a core is running unsupervised as pid $listen_pid on port $PORT." >&2
    echo "restart-core: to adopt it, stop that pid, bootstrap the job, and let KeepAlive start it." >&2
  fi
  exit 2
fi

if [ -n "$listen_pid" ] && [ "$listen_pid" != "$supervised_pid" ]; then
  echo "restart-core: port $PORT is served by pid $listen_pid but launchd reports pid $supervised_pid." >&2
  echo "restart-core: the running core is not the supervised one; not restarting it blind." >&2
  exit 2
fi

reply="$(curl -fsS -X POST ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' -d "{\"when\":\"$WHEN\"}" "$BASE/api/core/restart")" || {
  echo "restart-core: the core refused or is down; not forcing a kill. Check $BASE/api/status." >&2
  exit 1
}
echo "restart-core: $reply"

if [ "$WHEN" = "idle" ] && [ "$WAIT" = 0 ]; then
  # The calling turn is one of the sessions that must go idle before drain can finish.
  echo "restart-core: restart scheduled for idle; returning without waiting."
  exit 0
fi

waited=0
while [ "$waited" -lt "$TIMEOUT" ]; do
  now="$(supervisor_pid)"
  if [ -n "$now" ] && [ "$now" != "$supervised_pid" ] && curl -fsS -m 2 ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/status" >/dev/null 2>&1; then
    echo "restart-core: core is back (pid $supervised_pid -> $now) after ${waited}s"
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done
echo "restart-core: core not back after ${TIMEOUT}s; launchd job $LABEL is loaded, so check ~/Library/Logs/tessera-core.log" >&2
exit 1
