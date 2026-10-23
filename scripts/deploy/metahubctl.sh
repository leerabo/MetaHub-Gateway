#!/usr/bin/env bash
# Ubuntu operator commands. Never print expanded Compose config or secret files.
set -euo pipefail
umask 077
controller_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
task_root=$controller_root
action=${1:-help}
state=${2:-}
if [[ $action == help || -z $state ]]; then
  printf 'Usage: sudo scripts/deploy/metahubctl.sh install|first-owner|status|restart|backup|restore-check|upgrade|recover STATE [BACKUP_PREFIX | RELEASE_ROOT VERSION | BACKUP_PREFIX NEW_STATE MATCHING_RELEASE_ROOT]\n'
  exit 0
fi
[[ $EUID == 0 ]] || { printf 'Run as root to access deployment secrets.\n' >&2; exit 1; }
state=$(realpath -- "$state")
[[ -f $state/compose.env && -f $state/backend/runtime.env ]] || { printf 'Prepared deployment state is missing.\n' >&2; exit 1; }
if [[ -f $state/current-release ]]; then task_root=$(cat -- "$state/current-release"); fi
[[ -d $task_root && -f $task_root/infra/production/compose.yml ]] || exit 1
volume_override=()
if [[ -f $state/recovery-volumes.json ]]; then volume_override=(-f "$state/recovery-volumes.json"); fi
compose() { docker compose --env-file "$state/compose.env" -f "$task_root/infra/production/compose.yml" "${volume_override[@]}" "$@"; }
start_services() {
  # Bound readiness independently so cold ASGI imports do not compete with
  # each other. Publish ingress only after both backends are ready.
  local runner=$1
  "$runner" up -d --no-deps --wait --wait-timeout 300 control-api
  "$runner" up -d --no-deps --wait --wait-timeout 300 gateway
  "$runner" up -d --no-deps --wait --wait-timeout 300 console edge
}
fingerprint() {
  # Old installed images may contain an older verification tool. Execute the
  # verified controller's read-only tool using the old image's dependencies and
  # retained secret mounts, without changing that image or the serving database.
  compose run -T --rm --no-deps --entrypoint python migrate -c \
    'import sys; __file__=sys.argv.pop(1); exec(compile(sys.argv.pop(1), __file__, "exec"))' \
    /app/tools/database-fingerprint.py "$(cat -- "$controller_root/scripts/deploy/database-fingerprint.py")" "$@"
}
if [[ -e $state/recovery-in-progress && $action != status && $action != quiesce-upgrade && $action != recover ]]; then
  printf 'This state was superseded or interrupted by recovery; inspect recovery.json and use the recovered state.\n' >&2
  exit 1
fi
case "$action" in
  install)
    compose config --quiet
    # Resolve external runtime images before changing database state. A registry
    # outage must not first appear after a successful schema installation.
    compose pull postgres edge
    compose build control-api console
    compose up -d --wait --wait-timeout 300 postgres
    compose run --rm --no-deps migrate preflight
    compose run --rm --no-deps migrate migrate
    start_services compose
    printf '%s\n' "$task_root" > "$state/current-release"
    compose ps
    ;;
  status) compose ps ;;
  first-owner)
    [[ -t 0 && -t 1 ]] || { printf 'First-owner enrollment requires an interactive terminal.\n' >&2; exit 1; }
    # Never send password or authenticator enrollment material to journald.
    compose run --rm --no-deps first-owner
    ;;
  restart)
    compose stop edge console gateway control-api
    trap 'compose stop edge console gateway control-api >/dev/null' EXIT
    start_services compose
    trap - EXIT
    ;;
  quiesce-upgrade)
    # A killed Compose client leaves daemon-owned one-off containers running.
    # Only this configured project is affected; preserve containers and volumes
    # for recovery instead of deleting either failed or customer data.
    project=$(compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')
    oneoff_ids=$(docker ps --quiet --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.oneoff=True)
    if [[ -n $oneoff_ids ]]; then
      mapfile -t oneoffs <<< "$oneoff_ids"
      docker stop --time 30 "${oneoffs[@]}" >/dev/null
    fi
    compose stop edge console gateway control-api >/dev/null
    ;;
  upgrade)
    [[ ! -e $state/recovery-in-progress ]] || { printf 'Recovery requires updater reconfiguration before another upgrade.\n' >&2; exit 1; }
    next_root=$(realpath -- "${3:?Supply a verified release directory}")
    next_version=${4:?Supply a verified version}
    [[ $next_version =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-z0-9.-]+)?$ ]] || exit 1
    [[ -f $next_root/release-manifest.json ]] || exit 1
    # The host queue agent authenticates the release. Do not update PostgreSQL's
    # base image in this routine; major-version upgrades need a separate plan.
    python3 - "$task_root" "$next_root" "$next_version" "$state/compose.env" <<'PY'
import json,sys
from pathlib import Path
old,new,version=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
if json.loads((new/'release-manifest.json').read_text())['release'] != version: raise SystemExit(1)
def order(value):
 base,sep,pre=value.partition('-')
 ids=tuple((0,int(p)) if p.isdigit() else (1,p) for p in pre.split('.')) if sep else ()
 return (*(int(p) for p in base.split('.')),not bool(sep),ids)
current=next(line.split('=',1)[1] for line in Path(sys.argv[4]).read_text().splitlines() if line.startswith('METAHUB_RELEASE='))
if order(version)<=order(current): raise SystemExit('Only forward version upgrades are allowed.')
for name in ('postgres',):
 a=json.loads((old/'infra/production/images.lock.json').read_text())['images'][name]['image']
 b=json.loads((new/'infra/production/images.lock.json').read_text())['images'][name]['image']
 if a != b: raise SystemExit('Database image changed; separate database upgrade required.')
PY
    next_commit="release-manifest-sha256:$(sha256sum "$next_root/release-manifest.json" | cut -d ' ' -f 1)"
    next_compose() { METAHUB_RELEASE="$next_version" METAHUB_BUILD_COMMIT="$next_commit" docker compose --env-file "$state/compose.env" -f "$next_root/infra/production/compose.yml" "${volume_override[@]}" "$@"; }
    next_compose config --quiet
    # Build and preflight while the old release continues serving.
    next_compose pull edge
    next_compose build control-api console
    next_compose run --rm --no-deps migrate preflight
    compose stop edge console gateway control-api
    migration_started=false
    resume_before_migration() {
      if [[ $migration_started == false ]]; then
        compose start control-api gateway console edge >/dev/null
      else
        # A failed health check may have started only part of the new release.
        # Keep all public/writer services stopped until recovery is reviewed.
        next_compose stop edge console gateway control-api >/dev/null
      fi
    }
    trap resume_before_migration EXIT
    trap 'exit 143' TERM
    trap 'exit 130' INT
    backup_prefix=$(bash "$controller_root/scripts/deploy/metahubctl.sh" backup "$state" --print-prefix)
    # A file that exists is not proof of a usable backup. Restore to an isolated
    # database and compare the complete logical snapshot before migrating live data.
    bash "$controller_root/scripts/deploy/metahubctl.sh" restore-check "$state" "$backup_prefix"
    migration_started=true
    next_compose run --rm --no-deps migrate migrate
    start_services next_compose
    # Commit version metadata only after all health checks succeed.
    python3 - "$state/compose.env" "$next_version" "$next_commit" <<'PY'
import os,sys
from pathlib import Path
p=Path(sys.argv[1]); data=p.read_text().splitlines()
data=[line for line in data if not line.startswith(('METAHUB_RELEASE=', 'METAHUB_BUILD_COMMIT='))]
temporary=p.with_suffix('.pending');temporary.write_text('\n'.join(data)+f'\nMETAHUB_RELEASE={sys.argv[2]}\nMETAHUB_BUILD_COMMIT={sys.argv[3]}\n')
temporary.chmod(0o600);os.replace(temporary,p)
PY
    printf '%s\n' "$next_root" > "$state/current-release.pending"
    mv -- "$state/current-release.pending" "$state/current-release"
    trap - EXIT
    printf 'Upgrade completed. Database was migrated forward; no downgrade was attempted.\n'
    ;;
  backup)
    command -v gpg >/dev/null
    [[ -f $state/backups/encryption.key ]] || { printf 'Backup encryption key is missing.\n' >&2; exit 1; }
    backup="$state/backups/$(date -u +%Y%m%dT%H%M%SZ)_$(openssl rand -hex 6)"
    # Pause writes for a mutually consistent database + files snapshot.
    services=()
    while IFS= read -r name; do
      case "$name" in control-api|gateway) services+=("$name");; esac
    done < <(compose ps --status running --services)
    resume() { if ((${#services[@]})); then compose start "${services[@]}" >/dev/null; fi; }
    trap resume EXIT
    if ((${#services[@]})); then compose stop "${services[@]}" >/dev/null; fi
    encrypt() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$state/backups/encryption.key" --symmetric --cipher-algo AES256 --output "$1"; }
    compose exec -T --user postgres postgres pg_dump --username metahub_admin --dbname metahub --no-owner --format custom | encrypt "$backup.database.gpg"
    tar -C "$state" -cf - backend postgres authority edge caddy-data caddy-config compose.env | encrypt "$backup.configuration.gpg"
    compose run --rm --no-deps --entrypoint tar control-api -C /app/.data -cf - . | encrypt "$backup.files.gpg"
    fingerprint | encrypt "$backup.verification.gpg"
    # Relative names keep the backup verifiable after transfer to another host.
    (
      cd -- "$(dirname -- "$backup")"
      name=$(basename -- "$backup")
      sha256sum "$name.database.gpg" "$name.configuration.gpg" "$name.files.gpg" "$name.verification.gpg"
    ) > "$backup.sha256"
    if [[ ${3:-} == --print-prefix ]]; then
      printf '%s\n' "$backup"
    else
      printf 'Encrypted backup prefix: %s\nKeep the backup encryption key separately.\n' "$backup"
    fi
    ;;
  restore-check)
    backup=${3:?Supply the encrypted backup prefix}
    [[ -f $backup.sha256 && -f $state/backups/encryption.key ]] || exit 1
    python3 "$controller_root/scripts/deploy/verify-backup.py" --prefix "$backup"
    [[ -f $backup.verification.gpg ]] || { printf 'Backup verification manifest is missing.\n' >&2; exit 1; }
    restore_db="restore_$(date -u +%Y%m%d_%H%M%S)_$(openssl rand -hex 6)"
    # Restore into a NEW database; never overwrite the serving database.
    compose exec -T --user postgres postgres createdb --username metahub_admin --owner metahub --template=template0 --encoding=UTF8 --lc-collate=C --lc-ctype=C "$restore_db"
    gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$state/backups/encryption.key" --decrypt "$backup.database.gpg" | compose exec -T --user postgres postgres pg_restore --username metahub_admin --dbname "$restore_db" --no-owner --role metahub --exit-on-error
    gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$state/backups/encryption.key" --decrypt "$backup.verification.gpg" | fingerprint --database "$restore_db" --expected-stdin
    printf 'Restore check database: %s. Active database and encryption roots were preserved.\n' "$restore_db"
    ;;
  recover)
    python3 "$controller_root/scripts/deploy/recover-deployment.py" --state "$state" --prefix "${3:?Supply the backup prefix}" --target "${4:?Supply a NEW state directory}" --release "${5:?Supply the trusted matching backup release root}"
    ;;
  *) printf 'Unknown command.\n' >&2; exit 1 ;;
esac
