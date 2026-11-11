# Restricted container lifecycle integration

Production Compose uses `https://lifecycle-gateway:8443` on the private
`lifecycle_control` network. The gateway lives in the separate stacks repository
at `portainer/lifecycle/`; deploy it **before** recreating this dashboard.

The dashboard keeps visibility and start/stop/restart of **all containers** on
the three approved environments. It no longer receives the central Portainer
administrator token. Its independent token comes from
`/opt/secrets/lifecycle-dashboard.env`; the gateway public certificate is mounted
from `/opt/secrets/lifecycle-server.pem`. No direct Docker socket is added.

Existing UI/API paths remain unchanged. List metadata includes a `critical`
marker; stopping/restarting marked infrastructure requires a browser confirmation.
The prompt is not an authorization control and does not exclude any containers.
Stopping this dashboard, Portainer, an agent or the gateway may require host
SSH/console recovery. Names requiring warnings are configured gateway-side.

The gateway rejects create/delete/exec/configuration operations and filters raw
inspection data. All-container control can still disrupt all managed services.
Protect dashboard authentication/Cloudflare Access as before; the gateway does
not add per-human user authorization to the dashboard's public endpoints.

Use separate dummy tokens for local development. When `LIFECYCLE_URL` is set,
`LIFECYCLE_API_KEY` and `LIFECYCLE_CA_FILE` are required, TLS is verified and
redirects are not followed. Failure does not fall back to direct Portainer.
When no gateway URL is configured, legacy direct-API local development remains
available. Do not put an administrator key in the production dashboard env files.

Coordinate this branch with the matching stacks feature branch. Do not deploy
from a worktree with bind mounts pointing at a different deployed revision.
Follow the gateway README's trial, validation, revocation and rollback sequence;
revoking the old shared key is a separate, explicit cutover step.

Run offline tests using the dashboard's development dependencies:

```sh
python -B -m unittest discover -s tests -p 'test_lifecycle_gateway.py'
python -B -m unittest discover -s tests -p 'test_portainer_tls.py'
```

Live deployment and service restarts require separate approval. The image/update
policy of central Portainer is unchanged.
