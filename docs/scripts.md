# Operational scripts

The public release keeps installation and maintenance operations in `scripts/` and `infra/production/`:

- `scripts/deploy/prepare-production.py` prepares an isolated production state directory.
- `scripts/deploy/metahubctl.sh` installs, migrates, starts, checks, backs up, and recovers an Ubuntu deployment.
- `scripts/deploy/register-saas-admin.sh` creates the SaaS platform administrator during first installation. It is the discoverable wrapper for the one-time `metahubctl.sh first-owner` flow.
- `metahubctl.sh first-owner` invokes the interactive first-owner bootstrap flow and refuses non-empty databases.
- `scripts/deploy/github-releases.py` resolves and verifies GitHub Release artifacts.
- `scripts/deploy/package-trial.py` and `scripts/deploy/sign-release.py` build and sign release packages.
- `scripts/deploy/recover-deployment.py` and `scripts/deploy/verify-backup.py` provide recovery and backup gates.
- `scripts/acceptance/` contains runtime and browser acceptance harnesses.
- `scripts/ci/` contains repository checks.
- `infra/production/compose.yml` is the production Compose definition; `production.env.example` documents required variables.

The GitHub source includes the acceptance and CI harnesses for contributors. The signed deployment bundle is deliberately smaller: `package-trial.py` includes production runtime sources, deployment entrypoints, the console source, SDK examples, migrations, and operator documentation, while excluding tests, caches, local state, and contributor-only harnesses.

Run scripts from a checked-out release directory and keep state, credentials, volumes, and backups outside the repository. Scripts that need root or Docker access must be run only by an operator who has reviewed the target state path.

The platform-admin bootstrap is documented in [SaaS platform administrator](saas-administrator.md). It is intentionally separate from organization members and application Virtual Keys.
