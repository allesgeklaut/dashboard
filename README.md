# HOMELAB//CTRL

A green-on-black web dashboard for a personal homelab: FastAPI backend,
responsive browser UI, and optional integrations controlled by environment settings.

## Features

- Host CPU, RAM, GPU, network, storage, and process monitoring.
- Portainer container status and start/stop/restart controls.
- Optional AdGuard, Shelly energy/power, llama.cpp, and Ollama monitoring.
- Optional host-side streaming and desktop wake/power controls.
- OpenCode Go usage and model limits; Mistral model table from LiteLLM config.
- Shared, explicitly fetched Artificial Analysis benchmark scores.

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
not provide its own login or CSRF protection.

Docker loads credentials from `/opt/secrets/dashboard.env` and
`/opt/secrets/portainer.env`, not the repository's local-development `.env`.
It mounts the trusted Portainer server certificate read-only from
`/opt/secrets/portainer-ca.pem`. Portainer HTTPS verifies that exact certificate
and refuses redirects; see [SETUP.md](SETUP.md#portainer-https-verification).

The deployed Mistral integration requires both Compose files:

```bash
docker compose -f docker-compose.yml -f docker-compose.mistral.yml up -d --build
```

See [SETUP.md](SETUP.md) for integration details and host-side controls.
Review host scripts before running them: they can change system targets or
power state. Removing a credential from current files does not remove it from
Git history; revoke exposed credentials before publishing source.

## Tests

```bash
PYTHON_DOTENV_DISABLED=1 uv run --with httpx python -m unittest discover -s tests -v
```

The tests mock integration calls or use temporary loopback TLS servers, with
no real device controls or paid inference. TLS tests also require `openssl`.
