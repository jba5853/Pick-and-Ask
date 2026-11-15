#!/usr/bin/env bash
# Share Glance with your phone over HTTPS using a Cloudflare quick tunnel (no account needed).
#
# Usage:  scripts/share.sh [pack]      e.g.  scripts/share.sh 3d-printing
#
# Starts the server on 127.0.0.1, opens a temporary https://<random>.trycloudflare.com tunnel to it,
# and prints the URL and a QR code. Ctrl+C stops both. Anyone with the URL AND the access code can use
# the app (and your API credits) while it runs.
set -euo pipefail

cd "$(dirname "$0")/.."
PACK="${1:-}"
PORT="${PORT:-8000}"
PY=.venv/bin/python

if [[ ! -x "$PY" ]]; then
  echo "No .venv found. Run the setup steps in README.md first." >&2
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is not installed. Install it with:  brew install cloudflared" >&2
  exit 1
fi
if [[ -n "$PACK" && ! -f "docs/$PACK/pack.yaml" ]]; then
  echo "Unknown pack '$PACK'. Packs: $(ls docs | tr '\n' ' ')" >&2
  exit 1
fi

# The tunnel makes the app reachable from the internet, so an access code is required.
CODE_LEN=$("$PY" - <<'PY'
import os
from dotenv import dotenv_values
code = (os.environ.get("ACCESS_CODE") or dotenv_values(".env").get("ACCESS_CODE") or "").strip()
print(len(code))
PY
)
if (( CODE_LEN < 6 )); then
  echo "Set ACCESS_CODE in .env (at least 6 characters) before sharing, for example:" >&2
  echo "  echo 'ACCESS_CODE=pick-something-memorable' >> .env" >&2
  exit 1
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop the other server, or run with PORT=8001 scripts/share.sh" >&2
  exit 1
fi

LOGS=$(mktemp -d)
SERVER_PID="" TUNNEL_PID=""
cleanup() {
  [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  # shellcheck disable=SC2046
  kill $(jobs -p) 2>/dev/null || true   # the log viewer
  wait 2>/dev/null || true
  echo
  echo "Stopped. The tunnel URL no longer works."
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "Starting the server…"
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$LOGS/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/packs" >/dev/null 2>&1 && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "The server didn't start:" >&2; tail -n 20 "$LOGS/server.log" >&2; exit 1
  fi
  sleep 0.5
done

echo "Opening the HTTPS tunnel…"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOGS/tunnel.log" 2>&1 &
TUNNEL_PID=$!
BASE=""
for _ in $(seq 1 60); do
  BASE=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -n 1 || true)
  [[ -n "$BASE" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "cloudflared stopped:" >&2; tail -n 20 "$LOGS/tunnel.log" >&2; exit 1
  fi
  sleep 0.5
done
if [[ -z "$BASE" ]]; then
  echo "No tunnel URL after 30 seconds. cloudflared said:" >&2; tail -n 20 "$LOGS/tunnel.log" >&2; exit 1
fi

URL="$BASE/"
[[ -n "$PACK" ]] && URL="$BASE/?pack=$PACK"

echo
echo "  Glance is live at:"
echo
echo "    $URL"
echo
"$PY" - "$URL" <<'PY'
import sys
import qrcode
qr = qrcode.QRCode(border=2)
qr.add_data(sys.argv[1])
qr.print_ascii(invert=True)
PY
echo
echo "  WhatsApp webhook (paste in Meta dashboard > WhatsApp > Configuration if you use the glasses link):"
echo "    $BASE/webhooks/whatsapp"
echo
echo "  Scan the QR code with your iPhone camera. Enter the ACCESS_CODE from .env when asked."
echo "  The tunnel can take ~10 seconds to start answering. Logs: $LOGS"
echo "  Press Ctrl+C to stop."
echo
tail -n 0 -f "$LOGS/server.log" | grep --line-buffered -E "session=|ERROR|WARNING" &
wait "$TUNNEL_PID"
