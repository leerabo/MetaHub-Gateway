# Troubleshooting

Start with the component health page and the request log. On Ubuntu, collect the service state without printing secrets:

```bash
sudo scripts/deploy/metahubctl.sh status /opt/metahub-state
docker compose --env-file /opt/metahub-state/compose.env \
  -f /opt/metahub-releases/<version>/infra/production/compose.yml ps
```

Check the failure class in this order:

1. **Gateway unavailable:** verify DNS/HTTPS, Caddy, and the `control-api`/`gateway` readiness checks.
2. **Provider failure:** test the provider, model availability, credentials, timeout, and fallback candidates.
3. **Budget block:** inspect the request explanation and the organization/project/application/user ledgers.
4. **Email failure:** verify SMTP host, port, TLS mode, app password, egress firewall, and the built-in connection test before sending a delivery test.
5. **Database failure:** check PostgreSQL health and disk space. Do not remove the volume; use the encrypted backup and restore-check flow.

Redact credentials, tokens, email addresses, request bodies, and customer content before sharing diagnostics. Include the release version, request ID, component, timestamp, and the relevant health result.
