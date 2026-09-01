#!/bin/bash
# host/setup-stream.sh — one-shot installer for HOMELAB//CTRL steam streaming.
# Installs: sudoers scope, systemd path watcher, spool dir, .env flag, container rebuild.
# Everything that needs root is batched into one sudo call up front.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPOOL_DIR="$HOME/.homelab-ctrl/stream-spool"
ENV_FILE="$REPO_DIR/.env"

echo "── HOMELAB//CTRL stream installer ──────────────────────────────"
echo "repo:  $REPO_DIR"
echo "spool: $SPOOL_DIR"
echo

# ── preflight ─────────────────────────────────────────────────────────────────
for f in "99-homelab-stream-sudoers" "homelab-stream.path" "homelab-stream.service" \
         "stream-up.sh" "stream-down.sh" "stream-handler.sh"; do
    [ -f "$REPO_DIR/host/$f" ] || { echo "✗ missing host/$f — run from the repo checkout"; exit 1; }
done

# sanity: the user the units run as must be uid 1000 (container app-user match)
if [ "$(id -u)" != "1000" ]; then
    echo "✗ this must run as the dashboard user (uid 1000), currently uid $(id -u)"
    exit 1
fi

echo "The following will be installed:"
echo "  /etc/sudoers.d/99-homelab-stream-sudoers   (3 scoped commands)"
echo "  /etc/systemd/system/homelab-stream.{path,service}"
echo "  $SPOOL_DIR"
echo "  STREAM_SPOOL_DIR in $ENV_FILE"
echo
read -r -p "Proceed? [y/N] " reply
[ "$reply" = "y" ] || { echo "aborted."; exit 1; }

# ── one sudo batch ────────────────────────────────────────────────────────────
sudo -v || exit 1

sudo install -m 440 "$REPO_DIR/host/99-homelab-stream-sudoers" /etc/sudoers.d/99-homelab-stream-sudoers \
    || { echo "✗ sudoers install failed"; exit 1; }
if ! sudo visudo -c > /dev/null; then
    echo "✗ sudoers syntax check failed — removing and aborting"
    sudo rm -f /etc/sudoers.d/99-homelab-stream-sudoers
    exit 1
fi
echo "✓ sudoers installed (scope: 2× isolate + rm /run/greetd.run)"

sudo install -m 644 "$REPO_DIR/host/homelab-stream.path" "$REPO_DIR/host/homelab-stream.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-stream.path
echo "✓ homelab-stream.path $(systemctl is-active homelab-stream.path)"

# ── user-level steps (no sudo) ────────────────────────────────────────────────
mkdir -p "$SPOOL_DIR"
echo "✓ spool dir ready"

if [ -f "$ENV_FILE" ] && grep -q "^STREAM_SPOOL_DIR=" "$ENV_FILE"; then
    echo "• $ENV_FILE already has STREAM_SPOOL_DIR"
else
    printf '\n# ── Steam streaming ─────────────────────────────────────────────\nSTREAM_SPOOL_DIR=/stream-spool\n' >> "$ENV_FILE"
    echo "✓ STREAM_SPOOL_DIR added to .env"
fi

# ── negative test: anything outside the scope must fail ──────────────────────
echo "— negative test (sudo -n systemctl reboot must fail) —"
if sudo -n systemctl reboot 2>/dev/null; then
    echo "✗ SECURITY PROBLEM: reboot succeeded — sudoers too broad!"
    exit 1
else
    echo "✓ scope confirmed: reboot denied"
fi

# ── rebuild container ─────────────────────────────────────────────────────────
echo "— rebuilding dashboard container —"
(cd "$REPO_DIR" && docker compose up -d --build)

echo
echo "── done. open the dashboard → SERVER tab → 'steam stream' ──"
echo "   watch progress:  tail -f $HOME/.homelab-ctrl/stream.log"