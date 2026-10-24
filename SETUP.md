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
    ├── FastAPI backend  (psutil, Portainer API)
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

After installing requirements, run the regression tests with test-only `httpx`
for this invocation (it is not a production dependency):

```bash
PYTHON_DOTENV_DISABLED=1 uv run --with httpx python -m unittest discover -s tests -v
```

This skips local `.env` loading; the tests mock device polling and upstream
interactions or use temporary loopback servers. No real controls or paid
upstream calls are made. TLS fixtures require `openssl`; frontend regressions
also run with `node --test tests/frontend.test.js`.

Then open **http://127.0.0.1:7681**. (`--app-dir app` mirrors the container,
which copies `app/` to `/app` and imports `main`.)

All optional features — Portainer, AdGuard, Shelly, Ollama, LLaMA, stream
spool — are env-gated and stay **off** unless enabled via `.env` in the repo
root. Keep `SHELLY_PLUG_URL` unset locally unless you deliberately want to
test the Shelly integration: it makes the energy tracker poll that device,
and Shelly polling is kept minimal on purpose.

The **OPENCODE** tab (visible when a key is configured) shows the Go plan's
rolling 5h / weekly / monthly usage plus the per-model limits table (est.
requests per 5h, monthly $, and a benchmark score column — pick the benchmark
from the dropdown). Limits come from the opencode docs source (1 h cache).
Benchmark scores (Intelligence, Terminal-Bench, HLE, GPQA …) come from
Artificial Analysis' public leaderboard and are fetched **only when you press
⟳ FETCH SCORES** — nothing polls their site automatically; the result is
stored in `HOMELAB_BENCH_FILE` (`/data/benchmarks.json` in the container) so
restarts keep it. Attribution is shown; the fetch is an unofficial scrape of
a public page (see their ToS). The usage card polls only while the page is
open (nothing runs in the background). Enable it by setting
`OPENCODE_GO_API_KEY` — use the same token the CLI uses (read it on the
server from `~/.local/share/opencode/auth.json`, see `.env.example`). Locally
it works with no configuration: it falls back to the auth store on the dev
machine (`OPENCODE_AUTH_FILE` overrides the path).

The optional **MISTRAL** tab lists configured LiteLLM `mistral/` routes and
shows their Artificial Analysis benchmark table. It includes all matching
routes (including GLM, audio, and embedding models); non-chat or unranked
models show a dash for scores. It offers the same benchmark metric choices as
OPENCODE, but each tab stores its metric and sort selections independently;
the on-demand **FETCH SCORES** action and cache are shared. It works without
OpenCode being configured. The **OPEN MISTRAL** link opens Mistral's
subscription page in your browser (on iOS, you may need to sign in there).
This is not a Mistral usage integration: it does not scrape Mistral, use
cookies or a billing API, or require a Mistral or LiteLLM admin key. The
dashboard labels rows with LiteLLM model names (not provider names) and
exposes upstream model IDs, never the configured credentials.

For local development, set `LITELLM_CONFIG_FILE` to the existing LiteLLM YAML,
for example `../litellm/config.yaml` (relative to the server process working
directory) or an absolute path. Leave it unset to disable the tab locally.
The main Docker Compose file enables the tab and mounts the existing config
file read-only at `/run/litellm/config.yaml`:

```bash
docker compose up -d --build
```

The mount defaults to the host file `../litellm/config.yaml`, relative to
the Compose project directory. If needed, set `LITELLM_CONFIG_HOST_PATH` in
the repo's gitignored `.env` (or in the shell running Compose) to the path of
the existing YAML file. The source file must already exist; Compose does
not create a missing file or directory and does not mount the surrounding
secrets directory. The local-development `LITELLM_CONFIG_FILE` setting is
replaced by the Compose container path. No new `/opt/secrets` environment
fields or extra Compose file are needed. This deployment requires the config
file even if you do not open the MISTRAL tab. To remove the integration from a
different Docker deployment, remove both `LITELLM_CONFIG_FILE` and its bind
mount from the main Compose file before recreating the service.

---

## 1 — Configure

For local development, copy `.env.example` to `.env` and configure only the
integrations you intend to test. For Docker, that file supplies Compose
interpolation (host identity, optional spool flags and desktop identifiers),
not application credentials.

Docker loads `/opt/secrets/dashboard.env` (integration URLs, AdGuard credentials,
optional OpenCode key, etc.) and `/opt/secrets/portainer.env` (Portainer token
and, if needed, `PORTAINER_URL`). Provision both files outside the repository,
restrict access to the deploying administrator, and never commit them. The
Portainer file is also shared with LiteLLM. Container control uses Portainer
only; there is no Docker CLI or socket fallback.

### Get a Portainer API key

Portainer → username (top right) → My account → Access tokens → Add access token

### Portainer HTTPS verification

In Docker, replace `PORTAINER_API_KEY` in `/opt/secrets/portainer.env`. The
repository `.env` is for Compose interpolation and local development; it is
not the container's credential file. Recreate consumers after token changes;
a restart does not reload `env_file` values. LiteLLM and its ComfyUI proxy also
consume this shared file and must be recreated separately if the old token is
revoked.

Place **one trusted Portainer server certificate**, never a private key or a
general CA bundle, at `/opt/secrets/portainer-ca.pem`. Obtain it through a
trusted channel, not from an unverified network connection. Compose mounts
only this file read-only at `/run/secrets/portainer-ca.pem` and refuses to
create a missing source. It must be readable by the container's uid 1000.

The dashboard requires HTTPS and pins the certificate's SHA256 fingerprint
while also requiring certificate verification against that PEM. Exact
certificate identity replaces hostname matching for Portainer's existing
localhost-only self-signed certificate. The configured URL must still reach
Portainer (a container's `localhost` is not the host's loopback). Missing,
unreadable, invalid, expired, or changed certificates fail closed; redirects
are not followed and no integration falls back to `verify=False`.

For local development, `PORTAINER_CA_FILE` can point at the same trusted
server certificate. When unset, Portainer uses ordinary Requests CA and
hostname verification instead. This trust setting applies only to Portainer;
other integrations keep their existing TLS behavior. When rotating the
server certificate, update the trusted PEM too.

---

## 2 — Build & Run

```bash
cd homelab-ctrl
docker compose up -d --build
```

Visit **http://127.0.0.1:7681** on the server. From another machine, use an SSH
forward (`ssh -N -L 7681:127.0.0.1:7681 your-server`) and open the same URL.
Do not broaden the loopback binding to work around remote access.

---

## 3 — Require Cloudflare Access before publishing

The app is an administrative control panel with no application login. Create
the Access application **before adding its public tunnel hostname**:

1. **Zero Trust → Access → Applications** → Add application → Self-hosted.
2. Cover the entire `ctrl.yourdomain.com` hostname, including `/api/*`.
3. Add a narrowly scoped **Allow → Emails → your@email.com** policy; no bypass.
4. Verify unauthenticated access is denied after adding the tunnel route.

API mutations additionally require an `Origin` whose host/port matches the
browser-facing `Host`, plus `X-Homelab-Request: 1`. Cross-site Fetch Metadata
is rejected. The UI supplies the header, and no CORS permission is granted to
other origins. Both HTTP and HTTPS Origin schemes are accepted for the same
host because Cloudflare terminates TLS ahead of the loopback HTTP origin.
This protects against browser CSRF, not direct access by someone who can
bypass the perimeter. Keep the origin loopback-only.

## 4 — Cloudflare Tunnel

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

Visit `https://ctrl.yourdomain.com` and confirm Access authentication is
required. A containerized tunnel must use an explicitly reachable origin;
its own `localhost` is not automatically the dashboard host's loopback.

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
`STREAM_SPOOL_DIR` to `.env` (idempotent), then rebuilds the container.
It never tests authorization by executing a reboot or target switch.

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
`pid: host` still works for psutil. `/data` and request spools are writable;
certificate, integration config, NAS and sysfs mounts are read-only. On this
box uid 1000 = your user, so the spool dir and `/data` need no ownership changes.
Host handlers reject symlinks, special files, oversized input and unknown
tokens. Their logs go to journald, never to a container-writable path.

### Sanity checks

```bash
# Inspect syntax and effective permissions without executing allowed commands:
sudo visudo -c
sudo -l

# watcher active?
systemctl status homelab-stream.path
```

The scripts read env overrides — `STEAM_ARGS`, `STREAM_WIDTH/HEIGHT`,
`SESSION_WAIT`, … in `host/stream-up.sh`, and the per-mode resolutions
`DECK_STREAM_WIDTH/HEIGHT` (default 1680×1050) and
`HIGHP_STREAM_WIDTH/HEIGHT` (default 2560×1440) in `host/stream-handler.sh`.
Defaults live at the top of each script.
Logs: `journalctl -fu homelab-stream.service`.

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
3. Host logs: `journalctl -fu homelab-desktop.service`; queue-side logs:
   `docker compose logs -f homelab-ctrl`. SSH exit 255 means shutdown is
   unconfirmed, not successful; check the desktop status independently.

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
| `pid: host` | psutil sees real host CPU % and process list |
| `/mnt/nas:/mnt/nas:ro` | NAS disk usage visibility |
| `/sys/class/{drm,hwmon}:ro` | GPU and sensor readings |
| `~/.homelab-ctrl:/data` | Writable energy and benchmark persistence |
| Request spool directories | Writable fixed-action queues for host handlers |
| Portainer certificate | Read-only exact server-certificate trust |

The service uses bridge networking, not host networking, and has no Docker
socket mount. Host handlers read scripts directly from the checkout, so edits
affect their next invocation even without a dashboard rebuild. Do not run
setup scripts or install/reload units without reviewing the changes first.

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
| Network stats are 0 | Check visible interfaces; do not switch to host networking as a workaround |
| Containers marked stale/unavailable | Check Portainer availability, token and pinned certificate; there is no CLI fallback |
| NFS mount shows "not mounted" | Check the read-only bind and `NFS_MOUNTS` in the Docker application env file |
| AdGuard section missing | Section auto-hides on error — check `ADGUARD_URL` / credentials |
