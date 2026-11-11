# HOMELAB//CTRL

A green-on-black web dashboard for a personal homelab: FastAPI backend,
responsive browser UI, and optional integrations controlled by environment settings.

## Features

- Host CPU, RAM, GPU, network, storage, and process monitoring.
- Portainer container status and start/stop/restart controls.
- Optional AdGuard, Shelly energy/power, llama.cpp, and Ollama monitoring.
- Optional host-side streaming and desktop wake/power controls.
- AI Services tab: always-visible OpenCode Go usage, collapsed allowances, a
  Mistral console link, and one LiteLLM-backed inventory table grouped by upstream
  model and mode. A dropdown selects its single benchmark score column.
- Versioned benchmark snapshots from Harbor Terminal-Bench 4 and Epoch ECI by
  default; LiveBench's seven suites are opt-in (enabled on this private install).
   BenchLM (BenchAlign composites) is optional, enabled only when
   `BENCHLM_DATA_KEY` is set.
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

Benchmark refreshes use fixed, allowlisted HTTPS feeds fetched with `httpx` as
streamed responses inside one absolute per-source deadline that bounds the HTTP
connect, response headers, and body transfer, with a fixed response byte
budget. TLS verification stays on, redirects and user-supplied URLs are
refused, proxy/netrc environment settings and ambient credentials are ignored,
and responses pass strict JSON/CSV validation. They never send LiteLLM,
OpenCode, or provider credentials, and the manual refresh keeps the same-origin
mutation guard. The AI Services tab reads `GET /api/ai-services` (a 60-second
poll only while that tab is visible and active); `POST /api/benchmarks/fetch`
is the only way to start a refresh. Reading the tab never fetches upstream data,
even with an empty or stale cache. Refreshes are limited server-side to one run
per rolling 24 hours across all sources; failed runs count. The reservation is
persisted before network work, survives restarts, and is shared by the legacy
refresh endpoint too. A run can require multiple feed/page requests and bounded
HTTP retries. Cached scores remain readable during the cooldown.

For container-control credentials and TLS setup, see [LIFECYCLE.md](LIFECYCLE.md).

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
- **LiveBench 2.0** (optional, disabled by default) — seven suites pinned to release
  `20261025` ([table.csv](https://livebench.ai/data/20261025/table.csv),
  [categories.json](https://livebench.ai/data/20261025/categories.json),
  [site](https://livebench.ai/)). The original 2024 dataset's
  [Apache-2.0 datasheet](https://github.com/LiveBench/LiveBench/blob/main/docs/DATASHEET.md)
  does not conclusively establish reuse terms for the newer 2.0 score feed;
  [results-reuse clarification](https://github.com/LiveBench/LiveBench/issues/558)
  remains open. `HOMELAB_LIVEBENCH_ENABLED` defaults to false pending each
  operator's review. This single-user private installation opts in; that is not
  a blanket legal guarantee or permission to redistribute tasks. Disabled
  sources disappear from the dropdown and never expose cached scores.
- **Epoch Capabilities Index (ECI)** — default source.
  [Scores](https://epoch.ai/data/eci_scores.csv),
  [documentation](https://epoch.ai/eci). ECI is an abstract composite, not a
  percentage. Epoch AI's data is
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); attribution is
  shown.
- **BenchLM (BenchLM.ai Data API)** (optional, off unless `BENCHLM_DATA_KEY` is
  set) —
  [rankings API](https://data.benchlm.ai/v1/rankings/current?surface=overall|coding|agentic|knowledge)
  and [MCP endpoint](https://data.benchlm.ai/mcp). Adds **BenchAlign Overall**,
  **BenchAlign Coding**, **BenchAlign Agentic** and **BenchAlign Knowledge**
  (0–100 composite point estimates, not percentages; one manual refresh reads
  11 requests in practice — 9 GETs + 2 MCP calls — and at most 14 across the
  4 surfaces + 2 MCP calls; retries can multiply these logical requests on
  the wire, so this count assumes a clean run)
  plus Terminal-Bench 4.0 rows from its two TB4 boards: the displayed TB4
  value is the highest recorded result across Harbor and those boards, so a
  TB4 row's harness varies by model (its `version` names the board); it does
  not replace any other source — no ECI composite, no LiveBench per-suite
  breakdown, and it covers roughly 19 of this install's 28 inventory models.
  BenchLM data is [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/);
  this private, non-commercial single-user install shows it with attribution —
  cite BenchLM.ai (CC BY-NC 4.0) — as the operator's choice; redistribution or
  commercial display requires a separate licence from BenchLM. Not legal
  advice. Its scores carry the `source_snapshot` date kind (the date BenchLM
  captured the board), never an evaluation date.

Each dated result carries a `date_kind`: Harbor's Release Date
and Epoch's `date` are model releases (`model_release`), and LiveBench's pin is
a benchmark release (`benchmark_release`).
The score column uses the latest published dated result — ties break on
reasoning effort, then agent/configuration lexical order, never on score — and
the API keeps every configuration's own value. Terminal-Bench 4.0 is the one
exception: its column shows the highest recorded result across Harbor and
BenchLM's two TB4 boards, so a displayed TB4 row may come from a different
harness than another model's row. Score hover text shows the
selected result's agent, effort, confidence interval, date kind, trial count and
benchmark model identity when available. Stale cached scores and missing reasons
are also identified on hover. An evaluation date
(`evaluation`) is used only when the feed explicitly supplies one. Undated or
ambiguous configurations remain in the API without a summary score. There are
no configuration panels, provider filters, source cards or multi-column picker.

Harbor's website-display documentation is not a broader redistribution
license. Epoch AI's data is CC BY 4.0. The scope of LiveBench's older dataset/code
licensing for the newer published scores remains unconfirmed. These notes
record current terms,
not a verification of all reuse permissions or legal advice. Verify each
publisher's terms before republishing the data or exposing the dashboard beyond
personal use, and do not copy benchmark tasks, test data, or training material.
Snapshot and refresh details are in
[SETUP.md](SETUP.md#benchmark-sources).

## Tests

```bash
PYTHON_DOTENV_DISABLED=1 uv run python -m unittest discover -s tests -v
node --test tests/*.test.js
```

The tests mock integration calls or use temporary loopback TLS servers, with
no real device controls or paid inference. Security tests read the sanitized
sample `tests/fixtures/security-report.json` (copied to a temporary directory as
`latest.json`) and never the host report directory. TLS tests also require
`openssl`.
