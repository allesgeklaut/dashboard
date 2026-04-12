# HOMELAB//CTRL — Terminal Dashboard

A hacker-style interactive terminal dashboard for Ubuntu Server. Shows live system stats, Docker containers (via Portainer or CLI), NFS storage, and network info. Supports touch input on the ThinkPad Yoga's screen.

***

## Requirements

- Ubuntu 22.04 / 24.04 (Server or Desktop)
- Python 3.10+
- Docker + Portainer (optional but recommended)
- GCC (for touch support)

***

## Installation

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install gcc python3-dev python3-venv -y
```

### 2. Set up the project

```bash
mkdir ~/dashboard && cd ~/dashboard
python3 -m venv venv
source venv/bin/activate
pip install textual psutil requests evdev
```

### 3. Copy the script

Place `homelab-term.py` into `~/dashboard/`.

### 4. Configure

Edit the top of `homelab-term.py`:

```python
PORTAINER_URL     = "https://192.168.0.43:9443"   # your Portainer address
PORTAINER_API_KEY = "ptr_xxxxxxxxxxxxxxxxxxxx"     # see below
NFS_MOUNTS        = ["/mnt/nas"]                   # your NFS mount point(s)
SCREEN_TIMEOUT    = 60                             # seconds, 0 = disabled
BACKLIGHT_PATH    = "/sys/class/backlight/intel_backlight"  # check: ls /sys/class/backlight/
```

#### Getting the Portainer API key

1. Open Portainer → click your username (top right) → **Account**
2. Scroll to **Access tokens** → **Add access token**
3. Name it (e.g. `homelab-term`), re-enter your password, click **Add**
4. Copy the `ptr_...` token — it is only shown once

***

## Permissions

### Touch input

```bash
sudo usermod -a -G input $USER
```

### Backlight control (screen timeout)

```bash
sudo usermod -a -G video $USER
```

Log out and back in (or reboot) after adding groups.

***

## Running

```bash
cd ~/dashboard
source venv/bin/activate
python3 homelab-term.py
```

### Touch calibration (first run, recommended)

```bash
python3 homelab-term.py --calibrate
```

Touch each corner when prompted. Calibration is saved to `~/.homelab_cal.json` and loaded automatically on every subsequent run.

***

## Controls

| Input | Action |
|-------|--------|
| `↑` / `↓` | Select container |
| `s` | Start selected container |
| `x` | Stop selected container |
| `t` | Restart selected container |
| `r` | Refresh |
| `q` | Quit |
| Touch row | Select container |
| Touch button | Trigger action |
| Touch (screen off) | Wake screen |

***

## Auto-start on boot

```bash
sudo nano /etc/systemd/system/homelab-dashboard.service
```

```ini
[Unit]
Description=Homelab Terminal Dashboard
After=network.target docker.service

[Service]
User=johannes
WorkingDirectory=/home/johannes/dashboard
ExecStart=/home/johannes/dashboard/venv/bin/python3 homelab-term.py
Restart=always
RestartSec=5
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable homelab-dashboard
sudo systemctl start homelab-dashboard
```

***

## Font size (TTY)

```bash
sudo dpkg-reconfigure console-setup
```

Choose **Terminus**, size **14×28** or **16×32** for a wall-mounted screen.

***

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No module named psutil` | `pip install psutil` inside venv |
| `evdev` build fails | `sudo apt install gcc python3-dev` |
| Touch not reacting | `sudo usermod -a -G input $USER` then re-login |
| Backlight not changing | `sudo usermod -a -G video $USER` then re-login; verify `BACKLIGHT_PATH` |
| Portainer shows "FALLBACK → CLI" | Check API key and URL; falls back to `docker` CLI automatically |
| Wrong touch alignment | Run `python3 homelab-term.py --calibrate` |