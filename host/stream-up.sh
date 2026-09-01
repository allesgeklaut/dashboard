#!/bin/bash
# stream-up.sh — prepare headless host for Steam Remote Play to the Steam Deck.
# isolate graphical.target → wait for autologin session → set 1680x1050 → launch Steam.
LOG_FILE="${STREAM_LOG_FILE:-$HOME/.homelab-ctrl/stream.log}"
STEAM_BIN="${STEAM_BIN:-/usr/games/steam}"
STEAM_ARGS="${STEAM_ARGS:--bigpicture}"
DECK_WIDTH="${STREAM_WIDTH:-1680}"
DECK_HEIGHT="${STREAM_HEIGHT:-1050}"
SESSION_WAIT="${SESSION_WAIT:-120}"
STEAM_WAIT="${STEAM_WAIT:-60}"

log() { printf '%s [stream-up] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

log "requested"

# Fresh greetd state: without this, a second cycle in the same boot would show
# the greeter instead of autologging in (runfile marks "first run already used").
sudo -n rm -f /run/greetd.run || { log "ERROR: sudo rm /run/greetd.run failed"; exit 1; }

sudo -n /usr/bin/systemctl isolate graphical.target \
    || { log "ERROR: isolate graphical.target failed"; exit 1; }

# Wait for greetd's initial_session (autologin) to bring up the COSMIC session.
deadline=$(( $(date +%s) + SESSION_WAIT ))
while ! pgrep -x cosmic-session > /dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "ERROR: cosmic-session not up within ${SESSION_WAIT}s"
        exit 1
    fi
    sleep 2
done
sleep 3  # compositor + outputs settle

# Best-effort 16:10 mode for the Deck's Remote Play client.
# This script runs as a system service, outside the desktop session — export
# the session env explicitly or cosmic-randr fails with NoCompositor.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"
OUTPUT=$(cosmic-randr list 2>/dev/null | awk '/^Output/ {print $2; exit}')
if [ -n "$OUTPUT" ]; then
    if cosmic-randr mode "$OUTPUT" "$DECK_WIDTH" "$DECK_HEIGHT" 2>>"$LOG_FILE"; then
        log "mode ${DECK_WIDTH}x${DECK_HEIGHT} set on ${OUTPUT}"
    else
        log "WARN: cosmic-randr mode failed on ${OUTPUT} (continuing)"
    fi
else
    log "WARN: no output detected for cosmic-randr (continuing)"
fi

# Launch Steam detached from this service context.
if pgrep -x steam > /dev/null; then
    log "steam already running — skipping launch"
else
    setsid "$STEAM_BIN" $STEAM_ARGS </dev/null >>"$LOG_FILE" 2>&1 &
    deadline=$(( $(date +%s) + STEAM_WAIT ))
    while ! pgrep -x steam > /dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "ERROR: steam process not up within ${STEAM_WAIT}s"
            exit 1
        fi
        sleep 2
    done
fi

log "stream up OK"
exit 0
