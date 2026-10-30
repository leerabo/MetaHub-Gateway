# Ubuntu deployment

This is the supported self-hosting path for Ubuntu 22.04+ or 24.04+ on x86_64 or arm64. The release is a versioned, signed bundle; the database volume and deployment state stay outside the release directory.

## 1. Install from a GitHub Release

After a release is published, the operator can copy this block into an Ubuntu SSH session. Replace `VERSION` and `gateway.example.com` first. This downloads a versioned release bundle; it never clones the `main` branch.

```bash
set -e
REPOSITORY='leerabo/MetaHub-Gateway'
VERSION='2.0.0'
DOMAIN='gateway.example.com'
BUNDLE="metahub-${VERSION}-source.tar.gz"
RELEASE_ROOT="/opt/metahub-releases/${VERSION}"
STATE='/opt/metahub-state'
URL="https://github.com/${REPOSITORY}/releases/download/v${VERSION}/${BUNDLE}"

# Bootstrap only the tools needed to download and verify the release.
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg openssl python3
sudo install -d -m 0755 /opt/metahub-releases
if sudo test -e "$STATE"; then
  echo "${STATE} already exists; use the upgrade procedure instead of first install." >&2
  exit 1
fi
cd /tmp
curl -fL -o "$BUNDLE" "$URL"
curl -fL -o "${BUNDLE}.sha256" "${URL}.sha256"
sha256sum -c "${BUNDLE}.sha256"
sudo mkdir -p "$RELEASE_ROOT"
sudo tar -xzf "$BUNDLE" -C "$RELEASE_ROOT"
sudo bash "$RELEASE_ROOT/scripts/deploy/install-ubuntu-dependencies.sh"
sudo python3 "$RELEASE_ROOT/scripts/deploy/prepare-production.py" --state "$STATE" --domain "$DOMAIN"
sudo bash "$RELEASE_ROOT/scripts/deploy/metahubctl.sh" install "$STATE"
sudo bash "$RELEASE_ROOT/scripts/deploy/register-saas-admin.sh" "$STATE"
sudo bash "$RELEASE_ROOT/scripts/deploy/metahubctl.sh" status "$STATE"
```

The release page also provides `metahub-release.json` and `metahub-release.sig`; verify that Ed25519 signature with the operator's trusted release public key before extracting the bundle. The repository stores the corresponding public key at [`docs/release-public.pem`](../release-public.pem); copy it to `/etc/metahub/release-public.pem` through a separately reviewed channel. The checksum command above is an additional integrity check, not a substitute for signature verification. A production release should be rejected if the signature cannot be verified.

## 2. Prepare the host

Install Docker Engine, the Compose plugin, `curl`, `gpg`, `openssl`, and `python3`. Open only SSH and HTTPS at the security-group/firewall layer. Do not expose PostgreSQL, Redis, or the internal API ports to the Internet.

Create the immutable release directory. `prepare-production.py` creates the mutable state directory and refuses to overwrite it:

```bash
sudo install -d -m 0755 /opt/metahub-releases
```

## 3. Download and verify a release

Download the archive, its `.sha256` file, `metahub-release.json`, and `metahub-release.sig` from the same GitHub Release. Verify the SHA-256 and Ed25519 signature before extracting. The signed manifest names the exact bundle and version; do not substitute a branch archive or an unpinned image.

```bash
sha256sum --check metahub-<version>-source.tar.gz.sha256
openssl pkeyutl -verify -pubin -inkey /etc/metahub/release-public.pem \
  -rawin -in metahub-release.json -sigfile metahub-release.sig
```

The release package contains `release-manifest.json`, `infra/production/compose.yml`, the pinned image lock, migrations, and the deployment scripts. Never replace a verified release with a checkout of an arbitrary branch.

## 4. Prepare configuration

Run the preparation script once for the target domain. It creates TLS material, database credentials, the Compose environment, and backup encryption material under the new state directory:

```bash
python3 scripts/deploy/prepare-production.py \
  --state /opt/metahub-state \
  --domain gateway.example.com
```

Review `/opt/metahub-state/compose.env` and the generated files. Keep this directory root-readable only. Configure DNS and place the existing site behind a separate hostname or reverse-proxy route before starting MetaHub.

## 5. Install and bootstrap

From the extracted release root, run:

```bash
sudo scripts/deploy/metahubctl.sh install /opt/metahub-state
sudo scripts/deploy/register-saas-admin.sh /opt/metahub-state
sudo scripts/deploy/metahubctl.sh status /opt/metahub-state
```

The `register-saas-admin.sh` command is interactive and must be run from a terminal. It creates the SaaS platform administrator once, on an empty database; it does not create organization members or application keys. After sign-in, create providers, model aliases, ordered route candidates, projects, applications, budgets, and virtual keys before sending production traffic. See [SaaS platform administrator](../saas-administrator.md).

## 6. Backups and routine operations

```bash
sudo scripts/deploy/metahubctl.sh backup /opt/metahub-state
sudo scripts/deploy/metahubctl.sh restore-check /opt/metahub-state /opt/metahub-state/backups/<prefix>
sudo scripts/deploy/metahubctl.sh restart /opt/metahub-state
```

Backups are encrypted and include the database, runtime configuration, application files, and a verification fingerprint. Store the encryption key and backup files separately from the server.

Keep the previous release directory until health checks and business acceptance finish. Do not update production with `git pull`, delete Docker volumes during troubleshooting, or paste secrets into issues and logs.

## 7. After installation

Open `https://<your-domain>/identity.html?view=login`, sign in with the SaaS administrator, then configure providers, model aliases, ordered route candidates, projects, applications, budgets, and virtual keys. Send one test request and confirm the request record, route explanation, token usage, cost, and budget ledger before inviting users.

The install command starts the stack on loopback. Publish it through the existing HTTPS reverse proxy only after DNS, certificates, and the hostname have been checked. If another service already owns `example.com`, use a separate hostname such as `gateway.example.com`; do not replace that service's root-domain configuration.
