#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { printf 'Run this installer with sudo.\n' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && ($VERSION_ID == 22.04 || $VERSION_ID == 24.04) ]] || {
  printf 'Supported server releases: Ubuntu 22.04 / 24.04 LTS.\n' >&2; exit 1;
}
task_arch=$(dpkg --print-architecture)
[[ $task_arch == amd64 || $task_arch == arm64 ]] || exit 1
apt-get update
apt-get install -y ca-certificates curl gnupg openssl python3
install -m 0755 -d /etc/apt/keyrings
task_key=$(mktemp)
trap 'rm -f -- "$task_key"' EXIT
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg --output "$task_key"
task_fingerprint=$(gpg --show-keys --with-colons "$task_key" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ $task_fingerprint == 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 ]] || { printf 'Docker repository key fingerprint mismatch.\n' >&2; exit 1; }
install -m 0644 "$task_key" /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$VERSION_CODENAME" "$task_arch" > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "$task_root/infra/production/journald-retention.conf" /etc/systemd/journald.conf.d/zz-metahub-retention.conf
systemctl restart systemd-journald
journalctl --flush
docker version
docker compose version
