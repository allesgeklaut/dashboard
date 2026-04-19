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
