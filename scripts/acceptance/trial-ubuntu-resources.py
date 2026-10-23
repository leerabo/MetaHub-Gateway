"""Measure only our installed private Ubuntu fixture, without reading secrets."""
import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_STATES = ("/opt/metahub-trial-state", "/opt/metahub-trial-cold-recovery-trial8-20261017")

REMOTE = r'''
import json,os,platform,shutil,subprocess,time
from pathlib import Path
state=Path(FIXTURE_STATE)
assert Path('/home/acceptance/data-verification-install-passed').is_file()
assert 'METAHUB_DOMAIN=trial.local' in (state/'compose.env').read_text()
release=Path((state/'current-release').read_text().strip())
assert str(release).startswith(('/opt/metahub-candidate-20261015-', '/opt/metahub-release-store/', '/opt/metahub-trial-upgrade-store/', '/opt/metahub-trial-upgrade-store-lifecycle-recovery/'))
version=json.loads((release/'release-manifest.json').read_text())['release']
if EXPECTED_RELEASE and version != EXPECTED_RELEASE:
    raise ValueError('Resource collection requires the requested installed release')
def run(*args):
    return subprocess.check_output(args,text=True,timeout=120).strip()
images=[]
locks=json.loads((release/'infra/production/images.lock.json').read_text())['images']
for name in (f'metahub/backend:{version}',f'metahub/console:{version}',locks['postgres']['image'],locks['caddy']['image']):
    value=json.loads(run('docker','image','inspect',name,'--format','{{json .}}'))
    images.append(dict(name=name,id=value['Id'],bytes=value['Size'],architecture=value['Architecture']))

containers=[]
container_names=['metahub-trial-'+name+'-1' for name in ('postgres','control-api','gateway','console','edge')]
for name in container_names:
    value=json.loads(run('docker','inspect',name,'--format','{{json .}}'))
    containers.append(dict(name=name,image=value['Config']['Image'],image_id=value['Image'],
        running=value['State']['Running'],health=value['State'].get('Health',{}).get('Status'),
        log_driver=value['HostConfig']['LogConfig']['Type']))
if REQUIRE_HEALTHY and not all(value['running'] and value['health'] in (None,'healthy') for value in containers):
    raise ValueError('Resource measurement requires all five services running and healthy')
stats=[json.loads(line) for line in run('docker','stats','--no-stream','--format','{{json .}}',*container_names).splitlines()]
database=run('docker','exec','--user','postgres','metahub-trial-postgres-1','psql',
    '--no-psqlrc','--username','metahub_admin','--dbname','metahub','--tuples-only','--no-align',
    '--command',"SELECT json_build_object('active_database_bytes',pg_database_size(current_database()),'public_tables',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'),'migration_count',(SELECT count(*) FROM schema_migrations WHERE status='applied'));" )
volumes=[]
recovery=json.loads((state/'recovery.json').read_text()) if (state/'recovery.json').is_file() else None
if recovery and recovery['status']!='recovered':
    raise ValueError('Recovered resource measurement requires a completed recovery')
for service,volume in (('postgres','postgres'),('control-api','files')):
    mounted=json.loads(run('docker','inspect','metahub-trial-'+service+'-1','--format','{{json .Mounts}}'))
    destination='/var/lib/postgresql/data' if volume=='postgres' else '/app/.data'
    candidates=[item for item in mounted if item.get('Destination')==destination and item.get('Type')=='volume']
    if len(candidates)!=1:
        raise ValueError('Cannot establish actual running volume scope')
    name=candidates[0]['Name']
    expected=recovery['volumes'][volume] if recovery else 'metahub-trial_'+volume
    if name!=expected:
        raise ValueError('Container mount does not match the selected fixture state')
    mount=Path(run('docker','volume','inspect',name,'--format','{{.Mountpoint}}'))
    assert mount.is_relative_to('/var/lib/docker/volumes')
    volumes.append(dict(name=name,bytes=int(run('du','-sb',str(mount)).split()[0])))
memory={line.split(':',1)[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:','SwapTotal:'))}
disk=shutil.disk_usage('/')
backups=[dict(name=p.name,bytes=p.stat().st_size) for p in (state/'backups').glob('*.gpg')]
retained_databases=json.loads(run('docker','exec','--user','postgres','metahub-trial-postgres-1','psql',
    '--no-psqlrc','--username','metahub_admin','--dbname','metahub','--tuples-only','--no-align',
    '--command',"SELECT json_build_object('restore_check_databases',count(*),'restore_check_database_bytes',coalesce(sum(pg_database_size(oid)),0)) FROM pg_database WHERE datname ~ '^restore_[0-9]{8}_[0-9]{6}_[0-9a-f]{12}$';"))
print(json.dumps(dict(status='measured',timestamp_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
    platform='Independent Ubuntu 24.04 ARM64 QEMU acceptance VM',cpu_count=os.cpu_count(),
    architecture=platform.machine(),memory_bytes=memory,root_filesystem_bytes=dict(total=disk.total,used=disk.used,free=disk.free),
    release=version,fixture_state=str(state),expected_release=EXPECTED_RELEASE,all_services_required_healthy=REQUIRE_HEALTHY,
    containers=containers,container_stats_single_sample=stats,
    images=images,docker_storage_summary=run('docker','system','df','--format','{{json .}}'),
    database=json.loads(database),volumes=volumes,journal_usage=run('journalctl','--disk-usage'),
    retained_isolated_restore_databases=retained_databases,
    encrypted_backup_files=backups,backup_bytes=sum(p['bytes'] for p in backups),
    caveats=['Emulated CPU timing cannot establish native server throughput or upgrade downtime.',
             'Volume bytes are read from actual running container mounts, separately from preserved original/failure volumes.',
             'Restore-check databases and partial encrypted backup attempts are retained and included in disk use.',
             'A single container statistics sample does not establish peak memory or load capacity.',
             'This is one observed fixture size, not a proven minimum capacity for arbitrary customer data.'])))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', help='Require this exact installed runtime version')
    parser.add_argument('--state', choices=FIXTURE_STATES, default=FIXTURE_STATES[0])
    parser.add_argument('--require-healthy', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/deployment-ubuntu-20261014/resources-ubuntu-validation.json')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
    ssh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ssh)
    client = ssh.connect()
    try:
        stdin, out, _ = client.exec_command("sudo python3 -", timeout=600)
        stdin.write(f'EXPECTED_RELEASE={args.release!r}\nREQUIRE_HEALTHY={args.require_healthy!r}\nFIXTURE_STATE={args.state!r}\n' + REMOTE)
        stdin.flush()
        stdin.channel.shutdown_write()
        body = out.read()
        if out.channel.recv_exit_status() != 0:
            raise ValueError("Private Ubuntu resource measurement failed")
        report = json.loads(body)
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
