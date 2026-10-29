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

After installing requirements, run the regression tests (`httpx` is a runtime
dependency for benchmark fetches; the OpenCode usage calls keep using
`requests`):

```bash
PYTHON_DOTENV_DISABLED=1 uv run python -m unittest discover -s tests -v
```

This skips local `.env` loading; the tests mock device polling and upstream
interactions or use temporary loopback servers. No real controls or paid
upstream calls are made. TLS fixtures require `openssl`; frontend regressions
also run with `node --test tests/*.test.js`. Security tests use the sanitized
sample `tests/fixtures/security-report.json`, copied to a temporary directory as
`latest.json`; they never read the host report directory.

Then open **http://127.0.0.1:7681**. (`--app-dir app` mirrors the container,
which copies `app/` to `/app` and imports `main`.)

All optional features — Portainer, AdGuard, Shelly, Ollama, LLaMA, stream
spool, the security report, and the AI Services inventory — are env-gated and
stay **off** unless their environment variable enables them (a local `.env` or
the Compose environment). Keep `SHELLY_PLUG_URL`
unset locally unless you deliberately want to test the Shelly integration: it
makes the energy tracker poll that device, and Shelly polling is kept minimal
on purpose.

### AI Services tab

The former OPENCODE and MISTRAL tabs are now one **AI SERVICES** tab. It has a
single LiteLLM-backed inventory table, with the OpenCode Go usage/limits card
and a Mistral console link preserved above it:

- **OpenCode Go usage and limits** — the Go plan's rolling 5h / weekly /
  monthly usage plus the per-model limits table (est. requests per 5h and
  monthly $). Usage is always visible; only **allowances · go plan** is collapsed
  by default. Limits still come from the opencode docs source (1 h cache). Both
  sections stay hidden when no key is available. Use the same token the CLI uses
  (read it on the server from `~/.local/share/opencode/auth.json`, see
  `.env.example`); locally it can fall back to the dev machine's auth store
  (`OPENCODE_AUTH_FILE` overrides the path). Usage polls only while the page
  is open — nothing runs in the background.
- **Mistral console** — an **OPEN MISTRAL** link to Mistral's admin console for
  subscription/usage. It is only a link: the dashboard does not scrape Mistral,
  use cookies or a billing API, or require a Mistral or LiteLLM admin key.
- **Inventory table** — rows come from the valid routes in the LiteLLM
  `model_list`, not only `mistral/` routes. Routes are grouped by exact upstream
  model ID and mode, so a chat route and an embedding route that share one
  upstream model are separate rows; aliases with the same upstream model and
  mode stay together on one row and list their routes. The table is **MODEL |
  SCORE | AVAILABLE ROUTES**, with a narrow score column. A dropdown selects
  Terminal-Bench 4, Epoch ECI, or an enabled optional suite; its selection is
  saved locally. Only enabled sources appear in that dropdown. Model and score
  headers sort the rows. Audio and embedding routes stay visible but are
  excluded from coding comparisons. There are no provider filters, column
  pickers, source cards or configuration panels, and no invented combined score.

When any AI Services feature is enabled, the tab button is visible. The browser
loads `GET /api/ai-services` on tab entry and then every 60 seconds, but only
while that tab is active and the page is visible; hidden pages and other tabs do
not poll. That polling is a read-only GET and never sends the refresh POST;
it also never triggers upstream work, even when scores are missing or stale.
Only **⟳ REFRESH SCORES** sends the guarded `POST /api/benchmarks/fetch`, with
one refresh run allowed per rolling 24 hours. The button displays the remaining
cooldown and disables itself; the server enforces the same limit across tabs,
both refresh endpoints and restarts, including failed runs.

The dashboard exports only `model_name`, `litellm_params.model`, and
`model_info.mode` from the LiteLLM configuration. Other route parameters and
all credentials stay server-side and are never logged, cached, or returned to
the browser. A prefix on `litellm_params.model` (for example `openai/`)
identifies the LiteLLM route provider or gateway, not the model's developer; do
not read it as "made by OpenAI".

Identity matching is explicit. A row gets a benchmark only after an exact
normalized identity match or a reviewed, source-specific mapping. There is no
fuzzy prefix matching, version guessing, automatic `-latest` alias mapping, or
copying a base model's score onto a modified/abliterated variant. An unmatched
row shows a dash with a hover-only missing reason (not evaluated, unmatched
identity, unsupported modality, or source unavailable). Missing coverage is not
a fetch failure.

When a source has several results or configurations for an identity, the
summary column uses the latest published dated result, never the maximum.
Every published date carries a `date_kind`: Harbor's `metadata.date` (Release
Date) and Epoch's `date` are model releases (`model_release`); LiveBench's
pinned release is a benchmark release (`benchmark_release`). DeepSWE's
`generated_at` is source-level `source_updated`, not a per-result date, so its
rows stay undated and have no summary. An actual evaluation or submission date,
when the feed includes one, is labeled `evaluation`; otherwise the evaluation
date is shown as unknown. Ties are broken by reasoning-effort priority, then
agent/configuration lexical order — never by score. An undated or ambiguous
configuration has no single summary. Hovering a scored cell shows its selected
agent, effort, confidence interval, date kind, trial count and matched benchmark
model when supplied; stale cached scores are identified on hover too.
All configurations, their own values, evaluator/version and provenance remain
in the API, as do per-source warnings and enabled/disabled/stale/error state.
Those details have no separate visible panels in the simplified tab. The small
Epoch CC BY attribution remains below the table.

For local development, set `LITELLM_CONFIG_FILE` to the existing LiteLLM YAML,
for example `../litellm/config.yaml` (relative to the server process working
directory) or an absolute path. Leave it unset to omit the inventory locally.
The main Docker Compose file enables the inventory and mounts the existing
config file read-only at `/run/litellm/config.yaml`:

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
file even if you do not open the AI Services tab. To remove the integration
from a different Docker deployment, remove both `LITELLM_CONFIG_FILE` and its
bind mount from the main Compose file before recreating the service.

#### Benchmark sources

Harbor Terminal-Bench 4 and Epoch ECI are enabled by default. LiveBench 2.0
is opt-in and DeepSWE is optional, so neither is fetched until explicitly
enabled. The two opt-in flags are ordinary feature flags in the repository
`.env` (used for Compose interpolation and local startup), not `/opt/secrets`
credentials. This private, single-user installation uses
`HOMELAB_LIVEBENCH_ENABLED=true`; DeepSWE remains off. Defaults in `.env.example`
stay false for other installations. All
benchmark fetches use `httpx` (a runtime dependency) against fixed, allowlisted
HTTPS endpoints. Each response is streamed inside one absolute per-source
deadline that bounds the HTTP connect, response-header, and body-read phases,
with per-request timeouts capped by the remaining deadline and a fixed response
byte budget. The client verifies TLS, refuses redirects, rejects user-supplied
URLs, ignores proxy/netrc configuration and ambient credentials, and parses
strict JSON/CSV with schema
and range checks. No downloaded code is executed. Benchmark requests never carry
LiteLLM, OpenCode, or provider credentials, and publisher text is rendered
escaped, not as raw HTML; provenance links are validated HTTPS URLs. A reputable
publisher is not a trusted input: a compromised or wrong feed can publish bad
scores even though parsing is safe, so treat results as provenance-bearing
evidence.

- **Harbor Terminal-Bench 4.0** (default) — official public JSON API:
  `POST https://api.harborframework.com/functions/v1/leaderboard-read` with
  package `terminal-bench/terminal-bench` and leaderboard `4-0-0`. All pages
  are fetched and validated before a snapshot replaces the last good one.
  Accuracy and its 95% CI are 0–100 units. Rows mix native agents (for example
  Claude Code, Codex, and mini-SWE-agent), reasoning effort, the publisher's
  Release Date (and any explicit evaluation date), and trial counts, so the tab
  keeps that provenance and does not claim a pure-model ranking. A model with
  no current result is legitimate missing coverage, not a fetch failure. See the
  [Harbor leaderboard docs](https://docs.harborframework.com/harbor-hub/leaderboards)
  and [Terminal-Bench](https://www.tbench.ai/).
- **LiveBench 2.0** (optional, disabled by default) — the original June 2024
  [dataset datasheet](https://github.com/LiveBench/LiveBench/blob/main/docs/DATASHEET.md)
  says Apache-2.0 and no restrictions, but does not conclusively establish reuse
  terms for the newer 2.0 score feed. The
  [results-reuse question](https://github.com/LiveBench/LiveBench/issues/558)
  remains open. `HOMELAB_LIVEBENCH_ENABLED` defaults to false (Compose forwards
  it); disabled suites are absent from the dropdown and cached scores stay
  hidden. Enable after your own review; this private installation opts in. The
  source is pinned to release `20261025`, because the feed has no structured
  "latest release" manifest and the tab never scrapes HTML/JS to discover one.
  It reads
  [table.csv](https://livebench.ai/data/20261025/table.csv) and
  [categories.json](https://livebench.ai/data/20261025/categories.json) from
  that release. Scores are 0–100. `categories.json` defines the version-specific
  suite weights; Coding, Long Agentic, Reasoning, Browser, Data Analysis,
  Research and Design are selectable, and
  the tab does not compute an equal-weight overall score. Coding is
  deterministic; research and design use rubric/visual grading and can be
  harder to compare. Provenance identifies **LiveBench 2.0, release 2026-10-25**
  and <https://livebench.ai>. Private use is an operator choice, not a blanket
  legal guarantee. Do not copy tasks.
- **Epoch Capabilities Index (ECI)** (default source) — reads
  [eci_scores.csv](https://epoch.ai/data/eci_scores.csv). ECI is an abstract
  composite index, not a percentage, and mixes independent evaluations with
  vendor-reported results; show it with its own units and dates. Epoch AI's
  data is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); keep the
  attribution and link to [epoch.ai/eci](https://epoch.ai/eci).
- **DeepSWE** (optional, off by default) — enable with
  `HOMELAB_DEEPSWE_ENABLED=true` (Compose forwards the variable; no new
  service or volume). It reads Datacurve's v1.1
  [leaderboard JSON](https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json)
  and stores each pass@1 result (0–1 in the feed) as a percentage in the API,
  not best-of-four. Because its rows are undated, there is no single table
  summary value. The artifact itself has no express license; the
  project repository's Apache-2.0 license covers original contributions and
  curation, but does not explicitly license the website leaderboard artifact.
  Cite Datacurve 2026 (Wenqi Huang, Charley Lee, Leonard Tng, Serena Ge) and do
  not copy tasks or training data. Epoch AI found grading defects in at least 23
  of 113 v1.1 tasks, chiefly agent/hidden-test collisions; the API carries
  that warning and a link to the
  [DeepSWE v1.1 review](https://epoch.ai/benchmarks/deepswe/review). No score
  correction is invented. Datacurve's benchmark is not the Together or
  Agentica coding agent of the same name; do not fill gaps from community
  leaderboards.

Harbor documents unauthenticated reads so you can display a public leaderboard
on your website; this repository does not assert a broader redistribution
license for the results. Epoch AI's data is CC BY 4.0 — attribution is shown and
must be preserved. The scope of LiveBench's older dataset/code licensing for
the newer published scores remains unconfirmed; it is an operator opt-in,
enabled here for private use after review. The DeepSWE artifact has
no express license, and the repository's Apache-2.0 terms cover only original
contributions/curation without explicitly licensing the website leaderboard
artifact. These notes record current terms; they are not a verification of all
reuse permissions or legal advice. Public availability alone
is not display permission. Check each publisher's terms before republishing the
data or exposing the dashboard beyond personal use, and do not copy benchmark
tasks, test data, or training material.

#### Cache and refresh

Snapshots are persisted per source in a versioned cache file.
`HOMELAB_BENCH_FILE` defaults to `~/.homelab-ctrl/benchmarks.json`; Compose sets
`/data/benchmarks.json` on the existing `~/.homelab-ctrl:/data` volume, so the
cache survives restarts without a new mount. The old Artificial Analysis cache
is not read, reinterpreted, or displayed; the first run starts with empty
per-source snapshots and uses only the sources above.

Page loads read local snapshots and render them immediately, even when stale
or empty. A snapshot older than about 24 hours is marked stale, but **no read,
timer or tab activation fetches an upstream feed**. The **⟳ REFRESH SCORES**
button is the only refresh trigger. At most one refresh run may start in a
rolling 24-hour window, shared across every source and both refresh endpoints.
Concurrent tabs cannot reserve separate runs. Failed runs and worker-start
failures consume the window too; reopening the page or restarting the service
does not reset it. A run may need several feed/page requests and bounded retries
within its existing per-source deadline; it is not a single HTTP request.

A manual run refreshes the enabled sources independently, with bounded HTTP
retries/backoff (including rate-limit responses) inside that run only. There
are no automatic retry runs after a source fails.
One source failing, or returning invalid data, does not erase another source's
snapshot or fail the whole refresh; the last good snapshot is retained and its
error and age are preserved in the API. Refresh feedback reports partial or
failed refreshes; the minimal table has no source-status panel. Disabled sources
are not fetched or refreshed, never contribute cached rows, and are absent from
the dropdown. Any prior values are not treated as current. Empty, malformed,
schema-drifted, or suspiciously short feeds are rejected before replacing a
snapshot. Each source distinguishes when it was last checked, when it last
succeeded, when the publisher updated it, and each result's published date plus
`date_kind`; an evaluation date is labeled unknown when the feed does not
provide one. A removed score stops being current; a row without an exact
current match shows a missing reason or a separately dated historical record,
never an undated or guessed score. Snapshots are written atomically.

With persistence enabled, a cache write failure is a refresh failure, not a
silent success. A refresh that validates data but cannot persist it reports the
fixed `cache storage unavailable` error (never a path or OS exception), is
marked stale, and cannot be retried until the daily window expires. The last
good in-memory snapshot stays readable, and the previous cache file is left
untouched because snapshots replace it atomically.

The global `last_refresh_attempt` is persisted atomically in the same versioned
cache **before any outbound work**. An older cache is migrated using its latest
per-source check/success timestamp, including failed checks. This preserves
existing scores and avoids an extra run immediately after upgrading. If refresh
history is corrupt/unreadable, persistence is disabled, or the reservation
cannot be saved, refresh fails closed without network work; repair the cache
path/permissions before retrying. The deployment must retain its single uvicorn
worker (`--workers 1`); this is not a distributed multi-worker scheduler.

#### API

- `GET /api/ai-services` returns the read-only AI Services payload: `models`
  (inventory rows with `bench`, `results` carrying `date` and `date_kind`, and
  `missing` reasons),
  `metrics` metadata, per-source `sources` state, a `refreshing` flag and
  `refresh_policy` (manual-only, interval, last attempt, next allowed time,
  eligibility and any history error). It
  also includes `error` when the local LiteLLM configuration is missing or
  invalid. It reads local configuration and snapshot files only; a page load
  never waits for an upstream feed. OpenCode usage and limits remain available
  through their existing read endpoints and caches.
- `POST /api/benchmarks/fetch` reserves one asynchronous manual refresh and
  returns `ok`, `refreshing`, `started`, the enabled `sources`, and
  `refresh_policy`. Cooldown requests return **429** with `Retry-After` and the
  next allowed timestamp; unavailable reservation storage/history returns
  **503**, without starting any requests. An already-running refresh is joined
  rather than duplicated. The legacy OpenCode refresh endpoint uses the same
  reservation and responses. The tab
  polls `GET /api/ai-services` every 2 seconds, up to 40 attempts, until
  `refreshing` is false, and keeps the current inventory visible meanwhile;
  reaching that bound reports an unknown refresh status, never success. It is an
  API mutation and keeps the same-origin guard: the browser `Origin` must match
  the request `Host`, and the request must carry `X-Homelab-Request: 1`. It
  accepts no source URLs.

A refresh is best-effort per source: one unavailable feed does not fail the
others, and cached data stays visible while it runs.

### Security watch report (SECURITY tab)

The optional **SECURITY** tab reads the host watch's fixed `latest.json`
through `GET /api/security` (`no-store`, 8 MiB cap), never `latest.md`. It is
read-only: no scans, paid API calls, or package updates. `SECURITY_REPORT_DIR`
empty keeps the tab off; a configured directory shows it even before a report
exists, reporting it unavailable until then.

Local development points at a synthetic or copied report outside the repository
(e.g. `/tmp/opencode/security-report`, never a real report in the checkout):

```bash
SECURITY_REPORT_DIR=/tmp/opencode/security-report \
  uv run uvicorn main:app --app-dir app --host 127.0.0.1 --port 7681
```

Docker sets `SECURITY_REPORT_DIR=/run/security-watch` and bind-mounts
`${HOME}/.local/state/security-watch` there read-only. Override the source with
`SECURITY_REPORT_HOST_PATH` in the repo's gitignored `.env` or the Compose
shell — **not** `/opt/secrets/dashboard.env`, which supplies container
credentials and is not read for Compose interpolation. Use only the dedicated
report directory, never `$HOME` or a secrets directory.

Mount the whole directory, never `latest.json` alone: the watch replaces the
report atomically (`os.replace`), so a single-file mount would keep serving the
old inode. `create_host_path: false` requires the source to already exist and
fails container start otherwise; let the watch create it.

Reports are `0600` in a `0700` directory, replaced atomically, so the producer's
numeric UID must be 1000 (the container UID) for ongoing access, not just
today's file: a one-off `chmod`/ACL does not survive. Do not use recursive
`chmod` or rerun the watch as another account. Verify:

```bash
stat -c '%u:%g %a %n' ~/.local/state/security-watch ~/.local/state/security-watch/latest.json
```

The tab polls every 60 seconds only while active and visible, re-reads on tab
entry/visibility, and manual refresh re-reads the same report. Age over 36
hours is report age, not job status; carried-over findings are **NOT RECHECKED**.
Coverage is limited to inventoried sources, so a clean report is not an all-clear.

Without the host watch (or to disable it), remove both the
`SECURITY_REPORT_DIR` entry and the security bind mount from the Compose file;
setting the variable empty alone leaves the required mount in place.

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
the certificate, integration config, security report, NAS and sysfs mounts are
read-only. On this box uid 1000 = your user, so the spool dir and `/data` need
no ownership changes. The security report directory is `0700`/`0600`, so its
owner must be uid 1000 as well (see the SECURITY tab section).
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
| `~/.homelab-ctrl:/data` | Writable energy and versioned AI Services benchmark snapshots |
| Request spool directories | Writable fixed-action queues for host handlers |
| Portainer certificate | Read-only exact server-certificate trust |
| `../litellm/config.yaml:/run/litellm/config.yaml:ro` | Read-only AI Services inventory source; only safe route fields are exported |
| `~/.local/state/security-watch:/run/security-watch:ro` | Read-only daily security report (`latest.json`) |

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
| AI Services inventory empty or missing | Check `LITELLM_CONFIG_FILE` locally or the read-only `/run/litellm/config.yaml` mount in Docker; only valid `model_list` routes are listed |
| Benchmark score is a dash | Hover for the missing reason: no exact identity match, audio/embedding modality, no dated summary, or an unavailable source. Missing coverage is not a fetch failure |
| Benchmark data stale or a source unavailable | Last-good snapshots stay visible; use the manual refresh and inspect `sources` in `/api/ai-services`. An old Artificial Analysis cache is intentionally ignored |
| Refresh button shows a countdown / API returns 429 | One run was reserved within the last 24 hours, even if it failed; wait until `refresh_policy.next_allowed_at`. Page reloads/restarts do not reset the limit |
| Refresh history unavailable / API returns 503 | No requests start without a durable reservation. Check `HOMELAB_BENCH_FILE`, cache validity and `/data` permissions; do not erase history to reset a valid cooldown |
| Benchmark cache cannot be written after a run | Sources report a storage error and stay stale; the daily reservation still counts. Last-good in-memory data and the previous readable file are retained |
| LiveBench suites absent from dropdown | Defaults off pending operator review; set `HOMELAB_LIVEBENCH_ENABLED=true` in the repo `.env` and recreate the container (or restart local uvicorn). This private install opts in |
| DeepSWE absent or scoreless | Optional and off unless `HOMELAB_DEEPSWE_ENABLED=true`; current undated results have no summary, and grading-defect warnings remain in the API (see the review above) |
| SECURITY tab missing | Locally, `SECURITY_REPORT_DIR` is unset; in Docker, check the report directory, its uid-1000 ownership, and the read-only mount (see the SECURITY tab section) |
| SECURITY report unavailable | Check `latest.json` exists, is valid JSON, and is under 8 MiB; "stale" reflects report age, not the watch's job status |
