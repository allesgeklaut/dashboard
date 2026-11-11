# Dashboard lifecycle integration

The canonical provisioning, migration and recovery runbook is
`portainer/lifecycle/README.md` in the stacks repository. Deploy its gateway
before recreating this dashboard; coordinate both repositories' revisions.

- Compose connects to `https://lifecycle-gateway:8443` on `lifecycle_control`.
- The dashboard token comes from `/opt/secrets/lifecycle-dashboard.env`.
- Only the public certificate, `/opt/secrets/lifecycle-server.pem`, is mounted.
- All containers on the three approved hosts remain visible and controllable.
- Critical-service stop/restart prompts prevent accidents, not unauthorized use.
  Stopping the management path may require host SSH/console recovery.

Development also requires the gateway (`LIFECYCLE_URL`, `LIFECYCLE_API_KEY`,
`LIFECYCLE_CA_FILE`). There is no direct Portainer or Docker-socket fallback.

```sh
python -B -m unittest discover -s tests -p 'test_lifecycle*.py'
```
