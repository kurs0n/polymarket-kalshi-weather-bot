#!/bin/bash
# Watchdog for the weather trading bot server.
#
# Root-caused 2026-08-14: the server silently died mid-session with no
# alert — the scheduler (guardrails, position liquidator, settlement) was
# down for 12+ minutes before anyone noticed, while live positions sat
# unmonitored. This script checks health on an interval (see the
# com.weatherbot.watchdog launchd agent) and restarts + notifies if it's
# down, so "the bot is unattended" doesn't silently become "the bot isn't
# running at all."
set -u

PROJECT_DIR="/Users/luisgaviria/Documents/polymarket-kalshi-weather-bot"
LOG_FILE="$PROJECT_DIR/watchdog.log"
HEALTH_URL="http://127.0.0.1:8000/api/dashboard"
PORT=8000

log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $1" >> "$LOG_FILE"
}

notify() {
    osascript -e "display notification \"$1\" with title \"Weather Bot Watchdog\" sound name \"Basso\"" >/dev/null 2>&1
}

if curl -sf -m 5 "$HEALTH_URL" > /dev/null 2>&1; then
    exit 0  # healthy — nothing to do, nothing to log (avoid log spam every 2 min)
fi

log "Health check FAILED — server not responding on port $PORT. Attempting restart."
notify "Weather bot server was down — restarting now."

# Clear any stale/hung process still bound to the port before relaunching.
lsof -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null
sleep 1

cd "$PROJECT_DIR" || { log "FATAL: cannot cd to $PROJECT_DIR"; exit 1; }
nohup "$PROJECT_DIR/venv/bin/uvicorn" backend.api.main:app --port $PORT \
    >> "$PROJECT_DIR/server_watchdog_restart.log" 2>&1 &
disown

sleep 10
if curl -sf -m 5 "$HEALTH_URL" > /dev/null 2>&1; then
    log "Restart SUCCEEDED — server back up."
    notify "Weather bot server restarted successfully."
else
    log "Restart FAILED — server still not responding after restart attempt."
    notify "Weather bot restart FAILED — needs manual attention."
fi
