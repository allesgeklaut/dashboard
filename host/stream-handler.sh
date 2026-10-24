#!/bin/bash
# stream-handler.sh — systemd oneshot triggered by homelab-stream.path.
# Reads one queued request file from the spool dir and dispatches it.
# The request file contains only an action selector ("up"/"up-1440p"/"down");
# nothing is ever passed to a shell from its contents — the case below matches
# a fixed, closed set, so the file contents can never be interpreted as code.
SPOOL_DIR="${STREAM_SPOOL_DIR:-$HOME/.homelab-ctrl/stream-spool}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '%s [stream-handler] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# Resolution per "up" mode, overridable via env (see SETUP.md).
DECK_W="${DECK_STREAM_WIDTH:-1680}"
DECK_H="${DECK_STREAM_HEIGHT:-1050}"
HIGH_W="${HIGHP_STREAM_WIDTH:-2560}"
HIGH_H="${HIGHP_STREAM_HEIGHT:-1440}"

shopt -s nullglob
requests=("$SPOOL_DIR"/*.request)
if [ ${#requests[@]} -eq 0 ]; then
    exit 0
fi

# Execute every queued request, oldest first — by modification time, not by
# filename (the action prefix would sort "down" before "up"). Requests are
# dispatched sequentially, so the last one wins overall. Note: the while loop
# below runs in a pipeline subshell, so failures do not affect this script's
# exit status.
stat --printf '%.9Y %n\n' -- "${requests[@]}" | sort -n | while read -r _ts req; do
    if ! action=$(python3 "$SCRIPT_DIR/read-spool-action.py" "$req" up up-1440p down); then
        rm -f -- "$req"
        log "WARN: invalid request discarded"
        continue
    fi
    rm -f -- "$req"
    log "request → $action"
    case "$action" in
        up)        STREAM_WIDTH="$DECK_W" STREAM_HEIGHT="$DECK_H" \
                      "$SCRIPT_DIR/stream-up.sh" ;;
        up-1440p)  STREAM_WIDTH="$HIGH_W" STREAM_HEIGHT="$HIGH_H" \
                      "$SCRIPT_DIR/stream-up.sh" ;;
        down)      "$SCRIPT_DIR/stream-down.sh" ;;
        *)         log "WARN: unknown action" ;;
    esac
done
exit 0
