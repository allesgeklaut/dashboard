# HOMELAB//CTRL — Setup Guide

A single Docker container that replaces the old ttyd terminal setup.
Serves a green-on-black web dashboard with **container start/stop/restart** controls,
live system stats, storage monitoring, and AdGuard stats.

---

## Architecture

```
Browser (anywhere)
    │
Cloudflare Tunnel  ← no open ports needed
    │
cloudflared (existing container or tunnel)
    │  → http://localhost:7681
    │
homelab-ctrl container
    ├── FastAPI backend  (psutil, Portainer API, Docker CLI fallback)
    └── index.html       (green-on-black web UI, auto-refreshes every 2s)
```

---

## Local development (desktop workhorse)

Development happens on the desktop; the Pop!_OS server is deploy-only.
Run the app without Docker, using `uv`:

```bash
uv venv                           # creates .venv/
uv pip install -r requirements.txt
uv run uvicorn main:app --app-dir app --host 127.0.0.1 --port 7681
```

Then open **http://127.0.0.1:7681**. (`--app-dir app` mirrors the container,
which copies `app/` to `/app` and imports `main`.)

All optional features — Portainer, AdGuard, Shelly, Ollama, LLaMA, stream
spool — are env-gated and stay **off** unless enabled via `.env` in the repo
root. Keep `SHELLY_PLUG_URL` unset locally unless you deliberately want to
test the Shelly integration: it makes the energy tracker poll that device,
and Shelly polling is kept minimal on purpose.

The **OPENCODE** tab (visible when a key is configured) shows the Go plan's
rolling 5h / weekly / monthly usage plus the per-model limits table (est.
requests per 5h and monthly $ — Go plan, parsed from the opencode docs source
with a 1 h cache). The usage card polls only while the page is open (nothing
runs in the background). Enable it by setting `OPENCODE_GO_API_KEY` — use the
same token the CLI uses (read it on the server from
`~/.local/share/opencode/auth.json`, see `.env.example`). Locally it works with
no configuration: it falls back to the auth store on the dev machine
(`OPENCODE_AUTH_FILE` overrides the path).

> `dashboard.py` (the Textual terminal TUI) is **deprecated** — the web
> dashboard is the product.

---

## 1 — Configure

```bash
cp .env.example .env
nano .env          # fill in Portainer URL, API key, AdGuard creds
```

The only required field is `PORTAINER_API_KEY` if you use Portainer.
If Portainer is unavailable, container control falls back to local Docker CLI.

### Get a Portainer API key

Portainer → Account settings → Access tokens → Add access token

---

## 2 — Build & Run

```bash
cd homelab-ctrl
docker compose up -d --build
```

Visit **http://\<your-host-ip\>:7681** to confirm it's working locally.

---

## 3 — Cloudflare Tunnel

### Option A — existing tunnel (recommended)

Edit your existing `cloudflared` config and add a new hostname:

```yaml
# cloudflared config.yml (or in Zero Trust dashboard)
ingress:
  - hostname: ctrl.yourdomain.com
    service: http://localhost:7681
  # ... existing rules ...
  - service: http_status:404
```

### Option B — Zero Trust dashboard

1. Go to **Zero Trust → Networks → Tunnels** → click your tunnel → Edit
2. **Public Hostnames** → Add
   - Subdomain: `ctrl`
   - Domain: `yourdomain.com`
   - Service: `http://localhost:7681`
3. Save

**Done.** Visit `https://ctrl.yourdomain.com`.

---

## 4 — Secure with Cloudflare Access (strongly recommended)

1. **Zero Trust → Access → Applications** → Add application → Self-hosted
2. Application domain: `ctrl.yourdomain.com`
3. Add a policy: **Allow → Emails → your@email.com**
4. Save

Anyone reaching your URL now has to authenticate first.

---

## 5 — Steam streaming (Stream Up / Stream Down)

Lets the web dashboard switch the **same host** between headless
(`multi-user.target`) and a desktop session that launches Steam for
Remote Play to a Steam Deck — without exposing shell to the container.

The "up" side has **two modes**: **Deck** (1680×1050, for Remote Play) and
**1440p** (2560×1440, the host's native desktop resolution). Both launch
Steam; they only differ in the resolution the output is set to first.

### Architecture

```
Web UI ── POST /api/stream/{up | up-1440p | down} ──► FastAPI (container)
                                                       │ writes .request file (atomic)
                                                       │ content = action token
                                       bind-mount  ▼
               ~/.homelab-ctrl/stream-spool/   (on host)
                                       │ systemd .path unit watches
                                       ▼
               stream-handler.sh (as your user, via systemd)
                   ├─ "up"       → stream-up.sh  @ STREAM_WIDTH/HEIGHT = 1680×1050
                   ├─ "up-1440p" → stream-up.sh  @ STREAM_WIDTH/HEIGHT = 2560×1440
                   └─ "down"     → stream-down.sh
                        stream-up.sh:
                           sudo rm /run/greetd.run
                           sudo systemctl isolate graphical.target
                           wait for autologin session (greetd)
                           cosmic-randr mode <out> <W> <H>
                           steam -bigpicture
                        stream-down.sh:
                           steam -shutdown
                           sudo rm /run/greetd.run
                           sudo systemctl isolate multi-user.target
```

Status (HEADLESS / READY / STEAM) is read in-process via psutil (`pid: host`
already gives the container the host process list) — no privileges needed.

### Prerequisites on the host

- greetd autologin (one-time, so the session comes up unattended):

  ```bash
  sudo tee -a /etc/greetd/cosmic-greeter.toml > /dev/null <<'EOF'

  [initial_session]
  command = "/usr/bin/cosmic-session"
  user = "johannes"
  EOF
  ```

  greetd runs `initial_session` only on its **first start since boot** and
  records that in `/run/greetd.run` — the stream scripts clear that file so
  every Stream Up behaves like a fresh boot.

### Host setup (one-time)

All steps are bundled in one script — review it, then run it:

```bash
/opt/stacks/dashboard/host/setup-stream.sh
```

It does, in order: installs the scoped sudoers file (single sudo prompt,
`visudo -c` validated, auto-removes itself on syntax error), installs +
enables the systemd path unit, creates the spool dir, appends
`STREAM_SPOOL_DIR` to `.env` (idempotent), runs a **negative test** (sudo
`reboot` must be denied — aborts if the scope is somehow too broad), and
finally rebuilds the container. Re-running it is safe.

<details>
<summary>Manual equivalent (what the script does)</summary>

```bash
# 1. spool dir (container app-user is uid 1000 = your user; plain mkdir suffices)
mkdir -p ~/.homelab-ctrl/stream-spool

# 2. passwordless sudo — scoped to exactly the three commands the scripts use
sudo cp /opt/stacks/dashboard/host/99-homelab-stream-sudoers /etc/sudoers.d/
sudo chmod 440 /etc/sudoers.d/99-homelab-stream-sudoers
sudo visudo -c    # must report no syntax errors

# 3. systemd path watcher + handler
sudo cp /opt/stacks/dashboard/host/homelab-stream.path \
        /opt/stacks/dashboard/host/homelab-stream.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-stream.path

# 4. enable the feature in the container
echo 'STREAM_SPOOL_DIR=/stream-spool' >> /opt/stacks/dashboard/.env
cd /opt/stacks/dashboard && docker compose up -d --build
```

</details>

### Non-root container

The image runs the app as a dedicated `homelab` user (uid 1000), not root.
`pid: host` still works for psutil (reads /proc, no privileges needed), and all
other mounts are read-only. On this box uid 1000 = your user, so the spool dir
and `/data` need no ownership changes.

### Sanity checks

```bash
# sudoers scope — all three must succeed silently; anything else must fail:
sudo -n /usr/bin/systemctl isolate multi-user.target && echo ok1
sudo -n rm -f /run/greetd.run && echo ok2
sudo -n systemctl reboot   # must FAIL (not in sudoers)

# watcher active?
systemctl status homelab-stream.path
```

The scripts read env overrides — `STEAM_ARGS`, `STREAM_WIDTH/HEIGHT`,
`SESSION_WAIT`, … in `host/stream-up.sh`, and the per-mode resolutions
`DECK_STREAM_WIDTH/HEIGHT` (default 1680×1050) and
`HIGHP_STREAM_WIDTH/HEIGHT` (default 2560×1440) in `host/stream-handler.sh`.
Defaults live at the top of each script.
Log: `~/.homelab-ctrl/stream.log`.

---

## 6 — Desktop control (wake + power off)

The DESKTOP card wakes the desktop machine (Wake-on-LAN), shuts it down over
SSH, and shows its up/down state:

- **Wake** — the container queues a `wake` request in the spool dir; the
  host-side systemd unit (`host/desktop-power-handler.sh`) then broadcasts the
  magic packet from the server's LAN interface (a directed broadcast can't
  traverse the docker bridge the web container lives on — the kernel drops
  forwarded directed broadcasts, `bc_forwarding=0`).
- **Power off** — the container queues `poweroff` in the same spool dir; the
  host-side unit then ssh'es to the desktop and runs `systemctl poweroff`.
  The container never holds an SSH key or a shell.
- **Status** — cached TCP probe to the desktop's sshd (`:22`); even a refused
  connection counts as UP (the kernel answered a SYN with a RST).

### Server side (one-time)

Add to `.env` (see `.env.example` → Desktop control):

```bash
WOL_DESKTOP_MAC=<desktop-mac>       # feature on when set; packet broadcast by the host handler
WOL_DESKTOP_IP=<desktop-ip>         # optional — enables the UP/OFF status
WOL_BROADCAST=<lan-broadcast>       # optional — default 255.255.255.255 (host wake handler)
WOL_PORT=9                          # optional — default 9
DESKTOP_SSH=johannes@<desktop-ip>   # target for the poweroff ssh handler
```

Then run the spool installer (installs `homelab-desktop.{path,service}`,
creates the spool dir, appends `DESKTOP_SPOOL_DIR` to `.env`, rebuilds):

```bash
/opt/stacks/dashboard/host/setup-desktop-power-spool.sh
```

### Desktop side (one-time, run ON THE DESKTOP)

1. **BIOS**: enable "Wake on LAN" / "Power on by PCI-E device"; disable
   ErP/Deep Sleep if present — required for wake from full shutdown (S5).

2. Run the installer (enables WoL on the NIC + a boot unit re-applying it,
   installs the polkit rule allowing password-free poweroff over SSH):

   ```bash
   /opt/stacks/dashboard/host/setup-desktop-remote-control.sh
   ```

3. Verify from the **server** (expect `exit:0`):

   ```bash
   ssh johannes@<desktop-ip> 'pkcheck --action-id org.freedesktop.login1.power-off --process $$; echo exit:$?'
   ```

### Testing the full loop

1. **⏻ POWER OFF** (two presses — arm, then confirm) → desktop shuts down.
2. **⚡ WAKE** → desktop boots; the status dot flips to UP once sshd is up.
3. Logs on the server host: `~/.homelab-ctrl/desktop.log` (same file is
   visible inside the container at `/data/desktop.log`).

---

## Container Controls

- **Tap** any container row to select it
- **▶ Start** / **■ Stop** / **↺ Restart** appear in the bottom bar
- Filter buttons: ALL / RUNNING / STOPPED
- Containers auto-refresh every 3 seconds

---

## Volumes explained

| Volume | Why |
|--------|-----|
| `network_mode: host` | psutil sees real host network interfaces & stats |
| `pid: host` | psutil sees real host CPU % and process list |
| `/var/run/docker.sock` | Docker CLI fallback when Portainer is unreachable |
| `/mnt:/mnt:ro` | NFS mounts are visible for disk usage stats |

---

## Updating

```bash
docker compose pull   # if using a registry image
docker compose up -d --build
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| CPU/RAM show wrong values | Ensure `pid: host` is set |
| Network stats are 0 | Ensure `network_mode: host` |
| Containers list empty | Check `PORTAINER_API_KEY`; docker.sock fallback requires docker to be installed in container (it is) |
| NFS mount shows "not mounted" | Add the host path to `NFS_MOUNTS` in `.env` |
| AdGuard section missing | Section auto-hides on error — check `ADGUARD_URL` / credentials |
