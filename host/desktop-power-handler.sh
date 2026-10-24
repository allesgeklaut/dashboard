#!/bin/bash
# desktop-power-handler.sh — systemd oneshot triggered by homelab-desktop.path.
# Dispatches every queued request file from the spool dir:
#   wake     — broadcast a WoL magic packet from THIS host (the container sits
#              on a docker bridge, where the kernel drops directed broadcasts,
#              so the packet can only be sent from the LAN side)
#   poweroff — ssh to the desktop and run systemctl poweroff
# The request file contains only an action selector ("wake"/"poweroff");
# nothing is ever passed to a shell from its contents — the case below matches
# a fixed, closed set, so file contents can never be interpreted as code.
SPOOL_DIR="${DESKTOP_SPOOL_DIR:-$HOME/.homelab-ctrl/desktop-spool}"
ENV_FILE="${DESKTOP_ENV_FILE:-/opt/stacks/dashboard/.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

shopt -s nullglob
requests=("$SPOOL_DIR"/*.request)
if [ ${#requests[@]} -eq 0 ]; then
    exit 0
fi

log() { printf '%s [desktop-handler] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

# Action targets: env wins, otherwise the repo .env (gitignored) — real device
# identifiers are never committed.
env_val() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | sed 's/[[:space:]]*#.*//' | tail -1 | tr -d '[:space:]'; }
DESKTOP_SSH="${DESKTOP_SSH:-$(env_val DESKTOP_SSH)}"
WOL_DESKTOP_MAC="${WOL_DESKTOP_MAC:-$(env_val WOL_DESKTOP_MAC)}"
WOL_BROADCAST="${WOL_BROADCAST:-$(env_val WOL_BROADCAST)}"
WOL_BROADCAST="${WOL_BROADCAST:-255.255.255.255}"
WOL_PORT="${WOL_PORT:-$(env_val WOL_PORT)}"
WOL_PORT="${WOL_PORT:-9}"

# Execute every queued request, oldest first — by modification time, not by
# filename (the action prefix would sort "poweroff" before "wake"). Note: the
# while loop below runs in a pipeline subshell, so failures do not affect this
# script's exit status.
stat --printf '%.9Y %n\n' -- "${requests[@]}" | sort -n | while read -r _ts req; do
    if ! action=$(python3 "$SCRIPT_DIR/read-spool-action.py" "$req" wake poweroff); then
        rm -f -- "$req"
        log "WARN: invalid request discarded"
        continue
    fi
    rm -f -- "$req"
    case "$action" in
        wake)
            if [ -z "$WOL_DESKTOP_MAC" ]; then
                log "ERROR: WOL_DESKTOP_MAC not set (env or the repo .env) — cannot wake"
            else
                log "wake → magic packet to $WOL_DESKTOP_MAC via $WOL_BROADCAST:$WOL_PORT"
                # 6×0xff + the target MAC 16 times; MAC/broadcast/port passed
                # as argv and never interpreted by a shell.
                python3 -c 'import socket, sys
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.sendto(b"\xff" * 6 + bytes.fromhex(sys.argv[1].replace(":", "")) * 16,
            (sys.argv[2], int(sys.argv[3])))' \
                    "$WOL_DESKTOP_MAC" "$WOL_BROADCAST" "$WOL_PORT"
                rc=$?
                case "$rc" in
                    0) log "magic packet sent" ;;
                    *) log "ERROR: magic packet send failed (rc=$rc)" ;;
                esac
            fi
            ;;
        poweroff)
            if [ -z "$DESKTOP_SSH" ]; then
                log "ERROR: DESKTOP_SSH not set (env or the repo .env) — cannot act"
            else
                log "poweroff → ssh $DESKTOP_SSH systemctl poweroff"
                ssh -o BatchMode=yes -o ConnectTimeout=5 "$DESKTOP_SSH" \
                    systemctl poweroff
                rc=$?
                # 255 can mean a shutdown disconnect OR an authentication,
                # host-key, or network failure. Only the status probe can tell.
                case "$rc" in
                    0)   log "poweroff accepted (rc=0)" ;;
                    255) log "WARN: ssh exited 255 — shutdown unconfirmed; check desktop status" ;;
                    *)   log "ERROR: ssh failed (rc=$rc)" ;;
                esac
            fi
            ;;
        *)
            log "WARN: unknown action ignored"
            ;;
    esac
done
exit 0
