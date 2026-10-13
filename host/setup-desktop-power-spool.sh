#!/bin/bash
# host/setup-desktop-power-spool.sh — one-shot installer for desktop power
# control, SERVER side: spool dir + systemd path unit + .env flag + rebuild.
# The desktop-side steps (WoL persistence, polkit rule) are separate — run
# host/setup-desktop-remote-control.sh ON THE DESKTOP (see SETUP.md § 6).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPOOL_DIR="$HOME/.homelab-ctrl/desktop-spool"
ENV_FILE="$REPO_DIR/.env"
DESKTOP_SSH="${DESKTOP_SSH:-}"
if [ -z "$DESKTOP_SSH" ] && [ -f "$ENV_FILE" ]; then
    DESKTOP_SSH="$(sed -n 's/^DESKTOP_SSH=//p' "$ENV_FILE" | sed 's/[[:space:]]*#.*//' | tail -1 | tr -d '[:space:]')"
fi
if [ -z "$DESKTOP_SSH" ]; then
    echo "✗ DESKTOP_SSH not set — export it, or add 'DESKTOP_SSH=johannes@<desktop-ip>' to $ENV_FILE"
    exit 1
fi
echo "── HOMELAB//CTRL desktop power spool installer (server side) ───"
echo "repo:   $REPO_DIR"
echo "spool:  $SPOOL_DIR"
echo "target: $DESKTOP_SSH"
echo

# ── preflight ─────────────────────────────────────────────────────────────────
for f in "homelab-desktop.path" "homelab-desktop.service" "desktop-power-handler.sh"; do
    [ -f "$REPO_DIR/host/$f" ] || { echo "✗ missing host/$f — run from the repo checkout"; exit 1; }
done

if [ "$(id -u)" != "1000" ]; then
    echo "✗ this must run as the dashboard user (uid 1000), currently uid $(id -u)"
    exit 1
fi

echo "The following will be installed:"
echo "  /etc/systemd/system/homelab-desktop.{path,service}"
echo "  $SPOOL_DIR"
echo "  DESKTOP_SPOOL_DIR in $ENV_FILE"
echo "  container rebuild (docker compose up -d --build)"
echo
read -r -p "Proceed? [y/N] " reply
[ "$reply" = "y" ] || { echo "aborted."; exit 1; }

sudo -v || exit 1

# ── connectivity + authorization check (desktop side must be set up first) ────
echo "— checking desktop poweroff authorization (polkit) —"
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$DESKTOP_SSH" true 2>/dev/null; then
    if ssh -o BatchMode=yes -o ConnectTimeout=5 "$DESKTOP_SSH" \
           'pkcheck --action-id org.freedesktop.login1.power-off --process $$' 2>/dev/null; then
        echo "✓ poweroff authorized for $DESKTOP_SSH"
    else
        echo "⚠ desktop reachable, but poweroff is NOT authorized yet — install the polkit rule:"
        echo "    host/setup-desktop-remote-control.sh   (run ON the desktop)"
        echo "  continuing installation anyway — the dashboard button will fail until fixed."
    fi
else
    echo "⚠ desktop unreachable via ssh ($DESKTOP_SSH) right now — cannot verify authorization."
    echo "  continuing installation anyway; re-run the pkcheck later (see SETUP.md § 6)."
fi

# ── spool dir must exist BEFORE the path unit is enabled ─────────────────────
mkdir -p "$SPOOL_DIR"
echo "✓ spool dir ready"

sudo install -m 644 "$REPO_DIR/host/homelab-desktop.path" "$REPO_DIR/host/homelab-desktop.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-desktop.path
echo "✓ homelab-desktop.path $(systemctl is-active homelab-desktop.path)"

if [ -f "$ENV_FILE" ] && grep -q "^DESKTOP_SPOOL_DIR=" "$ENV_FILE"; then
    echo "• $ENV_FILE already has DESKTOP_SPOOL_DIR"
else
    printf '\n# ── Desktop control ──────────────────────────────────────────────\nDESKTOP_SPOOL_DIR=/desktop-spool\n' >> "$ENV_FILE"
    echo "✓ DESKTOP_SPOOL_DIR added to .env"
fi

echo "— rebuilding dashboard container —"
(cd "$REPO_DIR" && docker compose up -d --build)

echo
echo "── done. dashboard → SERVER tab → desktop card ──"
echo "   remember to set WOL_DESKTOP_MAC / WOL_DESKTOP_IP in $ENV_FILE (see .env.example)"
echo "   logs:  tail -f $HOME/.homelab-ctrl/desktop.log"
