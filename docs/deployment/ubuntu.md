# Ubuntu deployment

This is the supported self-hosting path for Ubuntu 22.04+ or 24.04+ on x86_64 or arm64. The release is a versioned, signed bundle; the database volume and deployment state stay outside the release directory.

## 1. Prepare the host

Install Docker Engine, the Compose plugin, `curl`, `gpg`, `openssl`, and `python3`. Open only SSH and HTTPS at the security-group/firewall layer. Do not expose PostgreSQL, Redis, or the internal API ports to the Internet.

Create separate directories for immutable releases and mutable state:

```bash
sudo install -d -m 0755 /opt/metahub-releases
sudo install -d -m 0700 /opt/metahub-state
```

## 2. Download and verify a release

Download the archive, `release-manifest.json`, and its signature from the same GitHub Release. Verify the SHA-256 and Ed25519 signature before extracting:

```bash
sha256sum --check release-manifest.sha256
python3 scripts/deploy/github-releases.py --help
```

The release package contains `release-manifest.json`, `infra/production/compose.yml`, the pinned image lock, migrations, and the deployment scripts. Never replace a verified release with a checkout of an arbitrary branch.

## 3. Prepare configuration

Run the preparation script once for the target domain. It creates TLS material, database credentials, the Compose environment, and backup encryption material under the state directory:

```bash
python3 scripts/deploy/prepare-production.py \
  --state /opt/metahub-state \
  --domain gateway.example.com
```

Review `/opt/metahub-state/compose.env` and the generated files. Keep this directory root-readable only. Configure DNS and place the existing site behind a separate hostname or reverse-proxy route before starting MetaHub.

## 4. Install and bootstrap

From the extracted release root, run:

```bash
sudo scripts/deploy/metahubctl.sh install /opt/metahub-state
sudo scripts/deploy/register-saas-admin.sh /opt/metahub-state
sudo scripts/deploy/metahubctl.sh status /opt/metahub-state
```

The `register-saas-admin.sh` command is interactive and must be run from a terminal. It creates the SaaS platform administrator once, on an empty database; it does not create organization members or application keys. After sign-in, create providers, model aliases, ordered route candidates, projects, applications, budgets, and virtual keys before sending production traffic. See [SaaS platform administrator](../saas-administrator.md).

## 5. Backups and routine operations

```bash
sudo scripts/deploy/metahubctl.sh backup /opt/metahub-state
sudo scripts/deploy/metahubctl.sh restore-check /opt/metahub-state /opt/metahub-state/backups/<prefix>
sudo scripts/deploy/metahubctl.sh restart /opt/metahub-state
```

Backups are encrypted and include the database, runtime configuration, application files, and a verification fingerprint. Store the encryption key and backup files separately from the server.

Keep the previous release directory until health checks and business acceptance finish. Do not update production with `git pull`, delete Docker volumes during troubleshooting, or paste secrets into issues and logs.
