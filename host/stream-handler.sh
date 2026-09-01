#!/bin/bash
# stream-handler.sh — systemd oneshot triggered by homelab-stream.path.
# Reads one queued request file from the spool dir and dispatches it.
# The request file contains only an action selector ("up"/"down"); nothing is
# ever passed to a shell from its contents.
SPOOL_DIR="${STREAM_SPOOL_DIR:-$HOME/.homelab-ctrl/stream-spool}"
LOG_FILE="${STREAM_LOG_FILE:-$HOME/.homelab-ctrl/stream.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

shopt -s nullglob
requests=("$SPOOL_DIR"/*.request)
if [ ${#requests[@]} -eq 0 ]; then
    exit 0
fi

# Handle only the newest request; older ones are consumed and ignored.
printf '%s\n' "${requests[@]}" | sort | while read -r req; do
    action=$(tr -d '[:space:]' < "$req")
    rm -f "$req"
    printf '%s [stream-handler] request %s -> %s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$req")" "$action" >> "$LOG_FILE"
    case "$action" in
        up)   "$SCRIPT_DIR/stream-up.sh"   ;;
        down) "$SCRIPT_DIR/stream-down.sh" ;;
        *)    printf '%s [stream-handler] WARN: unknown action\n' \
                  "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE" ;;
    esac
done
exit 0