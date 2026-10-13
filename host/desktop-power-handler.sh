#!/bin/bash
# desktop-power-handler.sh — systemd oneshot triggered by homelab-desktop.path.
# Reads one queued request file from the spool dir and dispatches it.
# The request file contains only an action selector ("poweroff"); nothing is
# ever passed to a shell from its contents — the case below matches a fixed,
# closed set, so the file contents can never be interpreted as code.
SPOOL_DIR="${DESKTOP_SPOOL_DIR:-$HOME/.homelab-ctrl/desktop-spool}"
LOG_FILE="${DESKTOP_LOG_FILE:-$HOME/.homelab-ctrl/desktop.log}"
# The desktop to power off: resolved from $DESKTOP_SSH if set, otherwise from
# the repo .env (gitignored) — real targets are never committed.
DESKTOP_SSH="${DESKTOP_SSH:-}"

shopt -s nullglob
requests=("$SPOOL_DIR"/*.request)
if [ ${#requests[@]} -eq 0 ]; then
    exit 0
fi

log() { printf '%s [desktop-handler] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

if [ -z "$DESKTOP_SSH" ]; then
    ENV_FILE="${DESKTOP_ENV_FILE:-/opt/stacks/dashboard/.env}"
    if [ -f "$ENV_FILE" ]; then
        DESKTOP_SSH="$(sed -n 's/^DESKTOP_SSH=//p' "$ENV_FILE" | sed 's/[[:space:]]*#.*//' | tail -1 | tr -d '[:space:]')"
    fi
fi
if [ -z "$DESKTOP_SSH" ]; then
    log "ERROR: DESKTOP_SSH not set (env or the repo .env) — cannot act"
    exit 1
fi

# Execute every queued request, oldest first. Note: the while loop below runs
# in a pipeline subshell, so failures do not affect this script's exit status.
printf '%s\n' "${requests[@]}" | sort | while read -r req; do
    action=$(tr -d '[:space:]' < "$req")
    rm -f "$req"
    case "$action" in
        poweroff)
            log "poweroff → ssh $DESKTOP_SSH systemctl poweroff"
            ssh -o BatchMode=yes -o ConnectTimeout=5 "$DESKTOP_SSH" \
                systemctl poweroff >> "$LOG_FILE" 2>&1
            rc=$?
            # The desktop shuts down while ssh is still connected, so 255
            # (connection dropped mid-shutdown) is the normal success path.
            case "$rc" in
                0)   log "poweroff accepted (rc=0)" ;;
                255) log "ssh connection dropped mid-shutdown (rc=255) — poweroff in progress" ;;
                *)   log "ERROR: ssh failed (rc=$rc)" ;;
            esac
            ;;
        *)
            log "WARN: unknown action '$action' ignored"
            ;;
    esac
done
exit 0
