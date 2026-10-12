# AGENTS.md — HOMELAB//CTRL (homelab-ctrl)

## Product: the web dashboard

The FastAPI web UI (`app/main.py`, `app/homelab_core.py`, `app/static/`) is
the product — the green-on-black dashboard served by uvicorn on :7681 behind
a Cloudflare tunnel.

`dashboard.py` (repo root, Textual TUI) is **DEPRECATED**: do not extend it,
do not fix its bugs, do not wire new features into it. It stays in the repo
for reference only and will eventually be removed.

## Local development (desktop workhorse)

Dev happens on the desktop; the Pop!_OS server is deploy-only.

```bash
uv venv                           # .venv/  (matches pyrightconfig.json)
uv pip install -r requirements.txt
uv run uvicorn main:app --app-dir app --host 127.0.0.1 --port 7681
```

- All optional features (Portainer, AdGuard, Shelly, Ollama, LLaMA, stream
  spool) are env-gated and auto-disabled without `.env` (gitignored).
- Do **not** set `SHELLY_PLUG_URL` locally unless deliberately testing it —
  the energy tracker polls that device and Shelly polling stays minimal.
- Local test = uvicorn smoke run. Docker/compose checks happen on the server.

## Deploy

`git push` → the user manually runs on the server:

```bash
cd /opt/stacks/dashboard && git pull && docker compose up -d --build
```

**Never** merge branches, push to master, or attempt a server deploy without
the user's explicit approval. The manual `compose up` is always the user's
step.
