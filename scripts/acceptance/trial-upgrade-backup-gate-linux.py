"""Exercise the upgrade backup gate with substituted Docker/GPG, never a live DB."""
from pathlib import Path
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile


DOCKER = '''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
with open(os.environ['GATE_EVENTS'],'a') as f: f.write(json.dumps(a)+'\\n')
if 'pg_dump' in a:
 if os.environ['GATE_CASE']=='backup_failure': sys.exit(1)
 sys.stdout.write('substituted-database-dump')
elif 'pg_restore' in a: sys.stdin.buffer.read()
elif '--expected-stdin' in a:
 sys.stdin.buffer.read()
 if os.environ['GATE_CASE']=='restore_failure': sys.exit(1)
 print('{"status":"verified","substituted":true}')
elif '/app/tools/database-fingerprint.py' in a: print('{"substituted":true}')
elif '--entrypoint' in a and 'tar' in a: sys.stdout.write('substituted-files')
elif a[-2:] == ['migrate','migrate']:
 if os.environ['GATE_CASE']=='migration_failure': sys.exit(1)
elif 'up' in a and '--no-deps' in a:
 if os.environ['GATE_CASE']=='health_failure': sys.exit(1)
 if os.environ['GATE_CASE']=='gateway_health_failure' and a[-1]=='gateway': sys.exit(1)
 if os.environ['GATE_CASE']=='frontend_health_failure' and a[-2:]==['console','edge']: sys.exit(1)
'''

GPG = '''#!/usr/bin/python3
import sys
from pathlib import Path
a=sys.argv[1:]
if '--output' in a: Path(a[a.index('--output')+1]).write_bytes(sys.stdin.buffer.read())
elif '--decrypt' in a: sys.stdout.buffer.write(Path(a[a.index('--decrypt')+1]).read_bytes())
else: sys.exit(1)
'''


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run this isolated command-boundary fixture as root.')
    source = Path(sys.argv[1]).resolve()
    fixture = Path(tempfile.mkdtemp(prefix='metahub-upgrade-gate-'))
    results = []
    for case in ('backup_failure', 'restore_failure', 'migration_failure', 'health_failure',
                 'gateway_health_failure', 'frontend_health_failure',
                 'success', 'legacy_active_success'):
        root = fixture / case
        active, candidate, state, binary = (root / n for n in ('active', 'candidate', 'state', 'bin'))
        for release, version in ((active, '2.0.0-trial.1'), (candidate, '2.0.0-trial.2')):
            (release / 'scripts/deploy').mkdir(parents=True)
            (release / 'infra/production').mkdir(parents=True)
            shutil.copyfile(source, release / 'scripts/deploy/metahubctl.sh')
            # Use the actual verifier dependency of the candidate controller.
            # Docker/GPG remain substituted only for these branch-order cases.
            shutil.copyfile(source.with_name('verify-backup.py'), release / 'scripts/deploy/verify-backup.py')
            shutil.copyfile(source.with_name('database-fingerprint.py'), release / 'scripts/deploy/database-fingerprint.py')
            (release / 'infra/production/compose.yml').write_text('services: {}\n')
            (release / 'infra/production/images.lock.json').write_text(json.dumps({'images': {'postgres': {'image': 'substituted-fixed-db-image'}}}))
            (release / 'release-manifest.json').write_text(json.dumps({'release': version}))
        if case == 'legacy_active_success':
            (active / 'scripts/deploy/metahubctl.sh').write_text(
                '#!/bin/bash\nprintf "Legacy controller must not run.\\n" >&2\nexit 99\n')
        for name in ('backups', 'backend', 'postgres', 'authority', 'edge', 'caddy-data', 'caddy-config'):
            (state / name).mkdir(parents=True)
        (state / 'compose.env').write_text('METAHUB_RELEASE=2.0.0-trial.1\n')
        (state / 'backend/runtime.env').write_text('substituted-test-configuration\n')
        (state / 'backups/encryption.key').write_text('not-a-production-key\n')
        (state / 'current-release').write_text(str(active) + '\n')
        binary.mkdir()
        for name, script in (('docker', DOCKER), ('gpg', GPG)):
            path = binary / name
            path.write_text(script)
            path.chmod(0o700)
        events_path = root / 'events.jsonl'
        environment = dict(os.environ, PATH=str(binary) + ':' + os.environ['PATH'],
                           GATE_CASE=case, GATE_EVENTS=str(events_path))
        controller = candidate if case == 'legacy_active_success' else active
        command=['bash',str(controller / 'scripts/deploy/metahubctl.sh'),'upgrade',
                 str(state),str(candidate),'2.0.0-trial.2']
        process=subprocess.Popen(command,env=environment,stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,start_new_session=True)
        try:
            stdout,stderr=process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            # Only terminate this fixture's own session; never the serving stack
            # or the independent image build when a slow emulated VM times out.
            os.killpg(process.pid,signal.SIGTERM)
            try:
                stdout,stderr=process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                stdout,stderr=process.communicate()
            (root / 'stdout.txt').write_bytes(stdout)
            (root / 'stderr.txt').write_bytes(stderr)
            raise
        (root / 'stdout.txt').write_bytes(stdout)
        (root / 'stderr.txt').write_bytes(stderr)
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        migrations = [i for i, a in enumerate(events) if a[-2:] == ['migrate', 'migrate']]
        restore_checks = [i for i, a in enumerate(events) if '--expected-stdin' in a]
        resumes = [a for a in events if 'start' in a and 'control-api' in a]
        startups = [a[a.index('300')+1:] for a in events if 'up' in a and '--no-deps' in a]
        expected_startups = [['control-api'], ['gateway'], ['console', 'edge']]
        if case in ('success', 'legacy_active_success'):
            assert process.returncode == 0, case
            assert len(migrations) == 1 and len(restore_checks) == 1
            assert restore_checks[0] < migrations[0]
            assert startups == expected_startups
            assert all('300' in a and '--wait' in a for a in events if 'up' in a and '--no-deps' in a)
            assert (state / 'current-release').read_text().strip() == str(candidate)
            for arguments in events:
                if ('pg_dump' in arguments or 'pg_restore' in arguments
                        or '/app/tools/database-fingerprint.py' in arguments):
                    assert arguments[arguments.index('-f')+1] == str(active / 'infra/production/compose.yml')
                if '/app/tools/database-fingerprint.py' in arguments:
                    assert '-c' in arguments and '-T' in arguments
                    path_index = arguments.index('/app/tools/database-fingerprint.py')
                    # Bash command substitution strips trailing newline bytes.
                    expected_source = (controller / 'scripts/deploy/database-fingerprint.py').read_text().rstrip('\n')
                    assert arguments[path_index + 1] == expected_source
            migration = events[migrations[0]]
            assert migration[migration.index('-f')+1] == str(candidate / 'infra/production/compose.yml')
        else:
            assert process.returncode != 0, case
            assert (state / 'current-release').read_text().strip() == str(active)
            assert 'METAHUB_RELEASE=2.0.0-trial.1' in (state / 'compose.env').read_text()
            if case in ('migration_failure','health_failure','gateway_health_failure','frontend_health_failure'):
                assert len(migrations)==1 and len(restore_checks)==1
                assert restore_checks[0] < migrations[0]
                assert not resumes, 'Never restart old writers after a migration attempt.'
                stages = {'migration_failure': 0, 'health_failure': 1,
                          'gateway_health_failure': 2, 'frontend_health_failure': 3}
                assert startups == expected_startups[:stages[case]]
                final=events[-1]
                assert 'stop' in final and all(service in final for service in ('edge','console','gateway','control-api'))
                assert final[final.index('-f')+1] == str(candidate / 'infra/production/compose.yml')
            else:
                assert not migrations and resumes, case
                assert len(restore_checks) == (1 if case == 'restore_failure' else 0)
        results.append({'case': case, 'passed': True, 'live_migration_calls': len(migrations),
                        'restore_verification_calls': len(restore_checks)})
    report = {'status': 'passed', 'platform': 'Ubuntu', 'cases': results,
              'docker_and_gpg_substituted': True, 'actual_database_upgrade_verified': False,
              'fixture': str(fixture)}
    (fixture / 'result.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
