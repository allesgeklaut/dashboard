# Wake‑on‑LAN Setup Guide

## Overview
Enables remote wake of your Ollama PC via the HOMELAB//CTRL dashboard.

## Prerequisites
* MAC address of target NIC.
* WoL enabled in BIOS/UEFI and OS.
* UDP port 9000 (or chosen) open on firewall.

## Configuration Steps

1. **Find MAC**
   ```bash
   ip link show | grep -E '^[0-9]+:'   # Linux
   Get-NetAdapter …                    # Windows
   ```

2. **Enable WoL on target**
   *Linux* – create `/etc/systemd/network/90-wol.network`:
   ```ini
   [Match]
   Name=eth0
   [Network]
   WakeOnLan=yes
   ```
   Reload: `sudo systemctl reload systemd-networkd`.  
   *Windows* – in Device Manager → Power Management → “Allow this device to wake the computer”.

3. **Open firewall** (Linux example):
   ```bash
   sudo ufw allow 9000/udp
   ```

4. **Set environment variables** (`.env`):
   ```bash
   WOL_TARGET_MAC=aa:bb:cc:dd:ee:ff
   WOL_BROADCAST_IP=255.255.255.255
   WOL_PORT=9000
   ```

5. **Restart dashboard**:
   ```bash
   docker compose restart   # or python3 main.py
   ```

## Using the Dashboard

* Click **⚡ WAKE TARGET** → sends a magic packet to `WOL_TARGET_MAC`.  
* Status appears next to the button.

## Troubleshooting
| Symptom | Fix |
|---------|-----|
| PC doesn’t wake | Verify MAC, WoL enabled in BIOS/UEFI, firewall allows UDP 9000. |
| “Invalid MAC” | Use colon‑separated format (`aa:bb:cc:dd:ee:ff`). |

## Files Modified
- `app/homelab_core.py` – added WOL functions.
- `app/main.py` – `/api/wol/{hostname}` and `/api/wol/config`.
- `app/static/index.html` – WoL UI section.
- `.env.example` – template with WOL variables.
