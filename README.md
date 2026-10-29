# HOMELAB//CTRL

A green-on-black web dashboard for a personal homelab: FastAPI backend,
responsive browser UI, and optional integrations controlled by environment settings.

## Features

- Host CPU, RAM, GPU, network, storage, and process monitoring.
- Portainer container status and start/stop/restart controls.
- Optional AdGuard, Shelly energy/power, llama.cpp, and Ollama monitoring.
- Optional host-side streaming and desktop wake/power controls.
- AI Services tab: OpenCode Go usage/limits, a Mistral console link, and one
  LiteLLM-backed inventory table with benchmark columns.
- Versioned benchmark snapshots from Harbor Terminal-Bench 4 and Epoch ECI by
  default; LiveBench Coding is opt-in pending a reuse review, and DeepSWE is
  optional (`HOMELAB_DEEPSWE_ENABLED=false` default) with a grading-defect
  warning.
- Read-only SECURITY tab for the host watch's daily report (optional, off by default).

## Run locally

```bash
uv venv
uv pip install -r requirements.txt
uv run uvicorn main:app --app-dir app --host 127.0.0.1 --port 7681
```

Open <http://127.0.0.1:7681>. Configure integrations through a gitignored `.env`
using `.env.example`. Leave device URLs unset unless deliberately testing them;
Shelly configuration starts background energy polling.

## Deployment and security

This is a **single-user administrative dashboard**, not a public unauthenticated
service. Keep the origin bound to loopback and require a Cloudflare Access
policy for the entire hostname before publishing a tunnel route. The app does
not provide its own login. API mutations require a same-host browser Origin
and the UI's custom request header; this is CSRF protection, not authentication.

Benchmark refreshes use fixed, allowlisted HTTPS feeds with certificate
verification, redirects disabled, response and time limits, and strict JSON/CSV
validation. They never send LiteLLM, OpenCode, or provider credentials, and the
manual refresh keeps the same-origin mutation guard. The AI Services tab reads
`GET /api/ai-services`; `POST /api/benchmarks/fetch` starts a guarded refresh.

Docker loads credentials from `/opt/secrets/dashboard.env` and
`/opt/secrets/portainer.env`, not the repository's local-development `.env`.
It mounts the trusted Portainer server certificate read-only from
`/opt/secrets/portainer-ca.pem`. Portainer HTTPS verifies that exact certificate
and refuses redirects; see [SETUP.md](SETUP.md#portainer-https-verification).

The main Compose file includes the AI Services inventory and its read-only
LiteLLM configuration mount:

```bash
docker compose up -d --build
```

It also enables the read-only SECURITY tab: the host watch's report directory
is bind-mounted read-only and `SECURITY_REPORT_DIR` is set. See
[SETUP.md](SETUP.md#security-watch-report-security-tab) for paths, overrides and safety.

See [SETUP.md](SETUP.md) for integration details and host-side controls.
Review host scripts before running them: they can change system targets or
power state. Removing a credential from current files does not remove it from
Git history; revoke exposed credentials before publishing source.

## Data sources and attribution

AI Services reads structured feeds only; it does not scrape leaderboard
HTML/JavaScript:

- **Harbor Terminal-Bench 4.0** (default) — public
  [leaderboard API](https://docs.harborframework.com/harbor-hub/leaderboards)
  ([endpoint](https://api.harborframework.com/functions/v1/leaderboard-read)).
  Harbor documents unauthenticated reads for displaying public leaderboards on
  your website. Rows mix native agents, reasoning effort, and trial counts; the
  dashboard preserves that provenance instead of claiming a pure-model rank.
- **LiveBench Coding** (optional, disabled by default) — pinned to release
  `20261025` ([table.csv](https://livebench.ai/data/20261025/table.csv),
  [categories.json](https://livebench.ai/data/20261025/categories.json),
  [site](https://livebench.ai/)). No explicit terms or discoverable license on
  the LiveBench site cover the published scores, and LiveBench2.0's code license
  does not cover them, so `HOMELAB_LIVEBENCH_ENABLED` defaults to false. Enable
  only after your own reuse review. Recommended project policy: keep visible
  attribution to **LiveBench 2.0, release2026-10-25** linked to
  <https://livebench.ai>; this does not establish reuse permission. Do not copy
  tasks. While disabled, the table keeps its Coding column but shows the
  source-disabled warning instead of data.
- **Epoch Capabilities Index (ECI)** — default source.
  [Scores](https://epoch.ai/data/eci_scores.csv),
  [documentation](https://epoch.ai/eci). ECI is an abstract composite, not a
  percentage. Epoch AI's data is
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); attribution is
  shown.
- **DeepSWE** (optional, off by default) — Datacurve
  [v1.1 artifact](https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json),
  which has no express license; the project repository's Apache-2.0 license
  covers original contributions and curation, but does not explicitly license
  the website leaderboard artifact. Cite Datacurve 2026 (Wenqi Huang, Charley
  Lee, Leonard Tng, Serena Ge).
  Epoch AI found grading defects in at least 23 of 113 v1.1 tasks
  ([review](https://epoch.ai/benchmarks/deepswe/review)); the column shows that
  warning and no invented correction.

Each dated result carries a `date_kind`: Harbor's Release Date
and Epoch's `date` are model releases (`model_release`), and LiveBench's pin is
a benchmark release (`benchmark_release`). DeepSWE's `generated_at` is
source-level `source_updated`, not a per-result date, so its rows stay undated.
The summary column uses the latest published dated result — ties break on
reasoning effort, then agent/configuration lexical order, never on score — and
the detail keeps every configuration's own value. An evaluation date
(`evaluation`) is shown only when the feed explicitly supplies one; otherwise
it is labeled unknown. Undated or ambiguous configurations remain in the detail
view without a summary.

Harbor's website-display documentation is not a broader redistribution
license. Epoch AI's data is CC BY 4.0. No explicit terms or discoverable license
on the LiveBench site cover its published scores, and its code license does not
cover them. The DeepSWE artifact has no express license, and the repository's
Apache-2.0 terms cover only original contributions/curation without explicitly
licensing the website leaderboard artifact. These notes record current terms,
not a verification of all reuse permissions or legal advice. Verify each
publisher's terms before republishing the data or exposing the dashboard beyond
personal use, and do not copy benchmark tasks, test data, or training material.
Snapshot and refresh details are in
[SETUP.md](SETUP.md#benchmark-sources).

## Tests

```bash
PYTHON_DOTENV_DISABLED=1 uv run --with httpx python -m unittest discover -s tests -v
node --test tests/*.test.js
```

The tests mock integration calls or use temporary loopback TLS servers, with
no real device controls or paid inference. Security tests read the sanitized
sample `tests/fixtures/security-report.json` (copied to a temporary directory as
`latest.json`) and never the host report directory. TLS tests also require
`openssl`.
