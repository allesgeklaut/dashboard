#!/bin/bash
# stream-down.sh — end streaming and return the host to headless.
# graceful steam shutdown → reset greetd runfile → isolate multi-user.target.
LOG_FILE="${STREAM_LOG_FILE:-$HOME/.homelab-ctrl/stream.log}"
STEAM_BIN="${STEAM_BIN:-/usr/games/steam}"

log() { printf '%s [stream-down] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

log "requested"

# Ask Steam to exit cleanly so Remote Play sessions terminate gracefully.
if pgrep -x steam > /dev/null; then
    "$STEAM_BIN" -shutdown >>"$LOG_FILE" 2>&1 || log "WARN: steam -shutdown returned nonzero"
    deadline=$(( $(date +%s) + 15 ))
    while pgrep -x steam > /dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "WARN: steam still running after 15 s — continuing anyway"
            break
        fi
        sleep 1
    done
fi

# Allow the next stream-up in this boot to autologin again (see stream-up.sh).
sudo -n rm -f /run/greetd.run || log "WARN: sudo rm /run/greetd.run failed"

sudo -n /usr/bin/systemctl isolate multi-user.target \
    || { log "ERROR: isolate multi-user.target failed"; exit 1; }

log "stream down OK"
exit 0
