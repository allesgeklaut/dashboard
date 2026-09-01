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

### Architecture

```
Web UI ── POST /api/stream/up ──► FastAPI (unprivileged container)
                                      │  writes .request file (atomic)
                          bind-mount  ▼
              ~/.homelab-ctrl/stream-spool/   (on host)
                                      │ systemd .path unit watches
                                      ▼
              stream-handler.sh (as your user, via systemd)
                  ├─ stream-up.sh   sudo rm /run/greetd.run
                  │                 sudo systemctl isolate graphical.target
                  │                 wait for autologin session (greetd)
                  │                 cosmic-randr mode <out> 1680 1050
                  │                 steam -bigpicture
                  └─ stream-down.sh steam -shutdown
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

The scripts read env overrides (`STEAM_ARGS`, `STREAM_WIDTH/HEIGHT`,
`SESSION_WAIT`, …) — defaults live at the top of `host/stream-up.sh`.
Log: `~/.homelab-ctrl/stream.log`.

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
