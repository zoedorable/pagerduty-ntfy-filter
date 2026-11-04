#!/usr/bin/env bash
# PagerDuty filter on/off for Mac and Linux
#   ./pd-filter.sh start    start (in the background)
#   ./pd-filter.sh stop     stop
#   ./pd-filter.sh status   is it running?
#   ./pd-filter.sh log      follow the log (Ctrl+C only ends the display)
#   ./pd-filter.sh view     open the dashboard in the browser
#   ./pd-filter.sh test "Title" ["Service"]   check whether a title would alert

cd "$(dirname "$0")" || exit 1
PIDFILE="pd_filter.pid"
LOGFILE="pd_filter.log"
VENV=".venv"
PY="$VENV/bin/python"

# Own Python environment in this folder (one-time), so requests is always available
ensure_venv() {
  if [ ! -x "$PY" ]; then
    echo "Setting up Python environment (one-time)..."
    python3 -m venv "$VENV" || { echo "Could not create venv."; exit 1; }
  fi
  if ! "$PY" -c "import requests" 2>/dev/null; then
    "$PY" -m pip install --quiet requests || { echo "Installing requests failed."; exit 1; }
  fi
}

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "$1" in
  start|on|an)
    if running; then
      echo "Already running (PID $(cat "$PIDFILE"))."
      exit 0
    fi
    ensure_venv
    nohup "$PY" pd_filter.py >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if running; then
      echo "ON (PID $(cat "$PIDFILE")). Log: $LOGFILE  |  Dashboard: ./pd-filter.sh view"
    else
      echo "Start failed, last log lines:"
      tail -n 5 "$LOGFILE"
      rm -f "$PIDFILE"
      exit 1
    fi
    ;;
  stop|off|aus)
    if running; then
      PID="$(cat "$PIDFILE")"
      kill "$PID"
      # wait up to 5 s for a clean shutdown
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
      rm -f "$PIDFILE"
      echo "OFF."
    else
      rm -f "$PIDFILE"
      echo "Was not running."
    fi
    ;;
  status)
    if running; then echo "ON (PID $(cat "$PIDFILE"))"; else echo "OFF"; fi
    ;;
  log)
    tail -n 20 -f "$LOGFILE"
    ;;
  test)
    ensure_venv
    "$PY" pd_filter.py --test "$2" "${3:-CSOC (High)}"
    ;;
  view|ansicht)
    PORT=$(grep -E '^WEB_PORT=' config.env 2>/dev/null | cut -d= -f2 | tr -d ' "')
    URL="http://localhost:${PORT:-8790}"
    running || echo "Note: the filter is OFF, the dashboard only shows data after './pd-filter.sh start'."
    if command -v open >/dev/null; then open "$URL"
    elif command -v xdg-open >/dev/null; then xdg-open "$URL"
    else echo "Open in the browser: $URL"; fi
    ;;
  *)
    echo "Usage: $0 start | stop | status | log | view | test \"Title\" [\"Service\"]"
    exit 1
    ;;
esac
