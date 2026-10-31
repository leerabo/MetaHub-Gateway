#!/usr/bin/env bash
# Install the signed GitHub Release updater on an Ubuntu host.
# This is deliberately separate from application containers: it runs as root,
# owns the release trust key, and is the only process allowed to request upgrades.
set -euo pipefail
umask 077

usage() {
  printf 'Usage: sudo %s STATE RELEASE_ROOT PUBLIC_KEY [OWNER/REPOSITORY]\n' "$0" >&2
  printf 'Example: sudo %s /opt/metahub-state /opt/metahub-releases/2.0.0-trial.53 /root/metahub-release-public.pem leerabo/MetaHub-Gateway\n' "$0" >&2
  exit 2
}

[[ ${EUID:-999} == 0 ]] || { printf 'Run this command with sudo on Ubuntu.\n' >&2; exit 1; }
[[ $# -ge 3 && $# -le 4 ]] || usage

state=$(realpath -- "$1")
release_root=$(realpath -- "$2")
public_key_input=$3
repository=${4:-leerabo/MetaHub-Gateway}

[[ -f "$state/compose.env" && -d "$state/updates" ]] || { printf 'Deployment state is missing: %s\n' "$state" >&2; exit 1; }
[[ -f "$release_root/scripts/deploy/github-releases.py" && -f "$release_root/scripts/deploy/update-agent.py" ]] || {
  printf 'Release does not contain the verified updater scripts: %s\n' "$release_root" >&2; exit 1;
}
[[ -f "$release_root/infra/production/metahub-update.service" && -f "$release_root/infra/production/metahub-update.timer" ]] || {
  printf 'Release does not contain updater systemd units.\n' >&2; exit 1;
}
[[ -f "$public_key_input" && ! -L "$public_key_input" ]] || { printf 'Trusted public key is missing or is a symlink.\n' >&2; exit 1; }
public_key=$(realpath -- "$public_key_input")
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { printf 'Invalid GitHub repository.\n' >&2; exit 1; }

install -d -m 0755 /etc/metahub /opt/metahub /opt/metahub/scripts /opt/metahub/scripts/deploy
install -m 0444 "$public_key" /etc/metahub/release-public.pem
install -m 0755 "$release_root/scripts/deploy/github-releases.py" /opt/metahub/scripts/deploy/github-releases.py
install -m 0755 "$release_root/scripts/deploy/update-agent.py" /opt/metahub/scripts/deploy/update-agent.py
install -m 0644 "$release_root/infra/production/metahub-update.service" /etc/systemd/system/metahub-update.service
install -m 0644 "$release_root/infra/production/metahub-update.timer" /etc/systemd/system/metahub-update.timer
install -m 0644 "$release_root/infra/production/metahub-release-sync.service" /etc/systemd/system/metahub-release-sync.service
install -m 0644 "$release_root/infra/production/metahub-release-sync.timer" /etc/systemd/system/metahub-release-sync.timer

python3 - "$state" "$repository" <<'PY'
import json
import os
import sys
from pathlib import Path

state = Path(sys.argv[1])
repository = sys.argv[2]
updates = state / "updates"
release_store = Path("/opt/metahub-releases")
config = {
    "repository": repository,
    "include_prereleases": True,
    "public_key": "/etc/metahub/release-public.pem",
    "release_store": str(release_store),
    "updates": str(updates),
    "state": str(state),
}
config_path = Path("/etc/metahub/updater.json")
temporary = config_path.with_suffix(".json.pending")
temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
temporary.chmod(0o600)
os.replace(temporary, config_path)
release_store.mkdir(mode=0o755, parents=True, exist_ok=True)
PY

chown -R root:root /etc/metahub /opt/metahub /opt/metahub-releases
chmod 0755 /opt/metahub /opt/metahub/scripts /opt/metahub/scripts/deploy /opt/metahub-releases
chmod 0600 /etc/metahub/updater.json
systemctl daemon-reload
systemctl enable --now metahub-update.timer metahub-release-sync.timer

# Verify the current catalog immediately. A failed first sync leaves the
# services installed but returns non-zero so operators cannot mistake an empty
# catalog for a working updater.
if ! systemctl start metahub-release-sync.service; then
  printf 'Updater services installed, but the first GitHub Release sync failed. Check: journalctl -u metahub-release-sync.service.\n' >&2
  exit 1
fi
printf 'Signed Release updater enabled.\n'
systemctl --no-pager --full status metahub-update.timer metahub-release-sync.timer | sed -n '1,24p'
