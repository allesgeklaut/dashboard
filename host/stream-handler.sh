#!/bin/bash
# stream-handler.sh — systemd oneshot triggered by homelab-stream.path.
# Reads one queued request file from the spool dir and dispatches it.
# The request file contains only an action selector ("up"/"up-1440p"/"down");
# nothing is ever passed to a shell from its contents — the case below matches
# a fixed, closed set, so the file contents can never be interpreted as code.
SPOOL_DIR="${STREAM_SPOOL_DIR:-$HOME/.homelab-ctrl/stream-spool}"
LOG_FILE="${STREAM_LOG_FILE:-$HOME/.homelab-ctrl/stream.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# Execute every queued request, oldest first. Requests are dispatched
# sequentially, so the last one wins overall. Note: the while loop below runs
# in a pipeline subshell, so failures do not affect this script's exit status.
printf '%s\n' "${requests[@]}" | sort | while read -r req; do
    action=$(tr -d '[:space:]' < "$req")
    rm -f "$req"
    printf '%s [stream-handler] request %s -> %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$req")" "$action" >> "$LOG_FILE"
    case "$action" in
        up)        STREAM_WIDTH="$DECK_W" STREAM_HEIGHT="$DECK_H" \
                      "$SCRIPT_DIR/stream-up.sh" ;;
        up-1440p)  STREAM_WIDTH="$HIGH_W" STREAM_HEIGHT="$HIGH_H" \
                      "$SCRIPT_DIR/stream-up.sh" ;;
        down)      "$SCRIPT_DIR/stream-down.sh" ;;
        *)         printf '%s [stream-handler] WARN: unknown action\n' \
                       "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE" ;;
    esac
done
exit 0
