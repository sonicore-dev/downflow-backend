#!/bin/sh
# Starts the PO-token provider (if present) in the background, then the API in the foreground.
POT_PORT="${DOWNFLOW_POT_PORT:-4416}"
if [ "${DOWNFLOW_POT_SERVER:-0}" = "1" ] && [ -f /opt/bgutil/server/build/main.js ]; then
  ( cd /opt/bgutil/server && exec node build/main.js --port "$POT_PORT" >/tmp/pot-server.log 2>&1 ) &
  export DOWNFLOW_POT_URL="${DOWNFLOW_POT_URL:-http://127.0.0.1:$POT_PORT}"
  echo "PO token provider starting on 127.0.0.1:$POT_PORT"
fi
exec python main.py
