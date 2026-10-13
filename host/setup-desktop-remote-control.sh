#!/bin/bash
# host/setup-desktop-remote-control.sh — one-shot installer to make THIS
# DESKTOP controllable from the dashboard. Run it ON THE DESKTOP, not the
# server. It does two things, both under a single sudo prompt:
#   1. WoL: enable "wol g" now + a systemd unit re-applying it on every boot
#      (BIOS must have Wake-on-LAN enabled; disable ErP/Deep Sleep for S5).
#   2. polkit rule 49-homelab-poweroff.rules: allows user johannes to power
#      off over ssh (org.freedesktop.login1.power-off only).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IFACE="${WOL_IFACE:-$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')}"
IFACE="${IFACE:-enp4s0}"
POLKIT_FILE="/etc/polkit-1/rules.d/49-homelab-poweroff.rules"

echo "── HOMELAB//CTRL desktop remote-control installer (desktop side) ──"
echo "repo:   $REPO_DIR"
echo "iface:  $IFACE"
echo

for f in "49-homelab-poweroff.rules"; do
    [ -f "$REPO_DIR/host/$f" ] || { echo "✗ missing host/$f — run from the repo checkout"; exit 1; }
done

command -v ethtool >/dev/null || { echo "✗ ethtool not installed (sudo apt install ethtool)"; exit 1; }
ip link show "$IFACE" >/dev/null 2>&1 || { echo "✗ interface $IFACE not found (override: WOL_IFACE=... $0)"; exit 1; }

echo "The following will be installed:"
echo "  $POLKIT_FILE"
echo "  /etc/systemd/system/wol-enable.service   (ethtool -s $IFACE wol g at boot)"
echo "  and WoL will be enabled on $IFACE now"
echo
read -r -p "Proceed? [y/N] " reply
[ "$reply" = "y" ] || { echo "aborted."; exit 1; }

sudo -v || exit 1

# ── 1. WoL enable + persistence ───────────────────────────────────────────────
sudo ethtool -s "$IFACE" wol g
sudo tee /etc/systemd/system/wol-enable.service > /dev/null <<EOF
[Unit]
Description=Enable Wake-on-LAN on $IFACE (HOMELAB//CTRL)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/ethtool -s $IFACE wol g
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable wol-enable.service
echo "✓ wol-enable.service enabled"

# ── 2. polkit rule for poweroff over ssh ──────────────────────────────────────
sudo install -m 644 "$REPO_DIR/host/49-homelab-poweroff.rules" "$POLKIT_FILE"
echo "✓ $POLKIT_FILE installed"

# ── verify ────────────────────────────────────────────────────────────────────
echo "— verify —"
sudo ethtool "$IFACE" | grep -i "wake-on" || echo "⚠ could not read Wake-on state"
echo
echo "✓ done. From the SERVER, verify poweroff authorization (expect exit:0):"
IP="$(hostname -I | awk '{print $1}')"
echo "    ssh johannes@$IP 'pkcheck --action-id org.freedesktop.login1.power-off --process \$\$; echo exit:\$?'"
echo
echo "   Remember the BIOS part once: enable Wake on LAN / Power on by PCI-E,"
echo "   disable ErP/Deep Sleep — required for wake from full shutdown (S5)."
