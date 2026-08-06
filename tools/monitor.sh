#!/bin/bash
# The monitor: a render loop and a web server, both in the background.
#
#   tools/monitor.sh start     # or stop, status
#
# Two processes rather than one framework. `python -m http.server` serves a
# directory and knows nothing about ACKBAR; `monitor.py` writes that directory
# and knows nothing about HTTP. Either can be killed and restarted without the
# other noticing, and neither is something an experiment depends on.
#
# The docroot is `site/monitor/`, not `site/`, so that `activate.sh` and
# `rancor.sh` are not served: a docroot bound to 0.0.0.0 should contain only
# what was written to be published.
set -euo pipefail

ACKBAR_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DOCROOT="$ACKBAR_ROOT/site/monitor"
PORT=${ACKBAR_MONITOR_PORT:-8092}
EVERY=${ACKBAR_MONITOR_EVERY:-60}
RUN="$DOCROOT/.run"

start() {
  mkdir -p "$RUN"
  source "$ACKBAR_ROOT/site/activate.sh"

  if [[ -s $RUN/render.pid ]] && kill -0 "$(cat "$RUN/render.pid")" 2>/dev/null; then
    echo "monitor: already running"; status; return 0
  fi

  MPLCONFIGDIR=/tmp/ackbar-mpl \
  nohup "$ACKBAR_ROOT/.venv/bin/python" "$ACKBAR_ROOT/tools/monitor.py" \
        --docroot "$DOCROOT" --every "$EVERY" \
        >"$RUN/render.log" 2>&1 &
  echo $! >"$RUN/render.pid"

  nohup python3 -m http.server "$PORT" --bind 0.0.0.0 --directory "$DOCROOT" \
        >"$RUN/serve.log" 2>&1 &
  echo $! >"$RUN/serve.pid"

  sleep 1
  status
}

stop() {
  for name in render serve; do
    if [[ -s $RUN/$name.pid ]]; then
      kill "$(cat "$RUN/$name.pid")" 2>/dev/null || true
      rm -f "$RUN/$name.pid"
    fi
  done
  echo "monitor: stopped"
}

status() {
  for name in render serve; do
    if [[ -s $RUN/$name.pid ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then
      echo "monitor: $name running as $(cat "$RUN/$name.pid")"
    else
      echo "monitor: $name is not running"
    fi
  done
  echo "monitor: http://$(hostname):$PORT/"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  *) echo "usage: ${0##*/} {start|stop|restart|status}" >&2; exit 2 ;;
esac
