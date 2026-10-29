# Configuration

Use `infra/production/production.env.example` as a reference and let `scripts/deploy/prepare-production.py` generate the real state. Runtime secrets are mounted from `/opt/metahub-state`; they are not read from the Git checkout at runtime.

## Required operating settings

- `METAHUB_STATE_DIR` points Compose at the private state directory.
- `METAHUB_RELEASE` and `METAHUB_BUILD_COMMIT` identify the verified release.
- PostgreSQL credentials, TLS keys, and backup encryption keys are generated in the state directory.
- The public Caddy endpoint is the only Internet-facing service. Internal services use the private Compose network.

## Providers and email

Configure provider credentials and model pricing in the control plane. Configure SMTP in the platform administration UI when registration verification, email verification, or password recovery is enabled. Save the configuration first, then use the built-in connection and delivery tests; the test recipient is not persisted as a credential.

Use a provider app password where the mail provider requires it. Port 465 normally uses TLS; port 587 normally uses STARTTLS. Keep provider and SMTP secrets out of screenshots, logs, `.env` files, and Git.

## Public repository boundary

The public repository contains templates and examples only. It must never contain a live `.env`, API key, SMTP password, database export, customer data, backup encryption key, or server private key. Keep state, volumes, backups, and certificates outside the release directory.
