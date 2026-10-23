"""Exercise real Linux process-group termination and isolated Docker quiescence."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Use root in the isolated acceptance VM.')
    spec = importlib.util.spec_from_file_location('interruption_agent', args.tools / 'update-agent.py')
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    root = Path(tempfile.mkdtemp(prefix='metahub-interruption-', dir='/opt'))
    root.chmod(0o700)
    pidfile = root / 'child.pid'
    # Both generations ignore SIGTERM, so the forced group cleanup is required.
    program = root / 'ignoring-tree.py'
    program.write_text(
        'import os,signal,subprocess,sys,time\n'
        'from pathlib import Path\n'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        'if len(sys.argv)==2:\n'
        ' child=subprocess.Popen([sys.executable,__file__,sys.argv[1],"child"])\n'
        ' Path(sys.argv[1]).write_text(str(child.pid))\n'
        'while True: time.sleep(1)\n'
    )
    started = time.monotonic()
    try:
        agent.run_bounded([sys.executable, str(program), str(pidfile)], timeout=15, grace=1)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError('Ignoring process tree did not time out.')
    assert pidfile.exists(), 'Test process never initialized; no cleanup claim possible.'
    descendant = int(pidfile.read_text())
    proc = Path(f'/proc/{descendant}/stat')
    assert not proc.exists() or proc.read_text().split()[2] == 'Z', 'Descendant is still executing.'
    assert agent.run_bounded([sys.executable, '-c', 'raise SystemExit(7)'], timeout=30).returncode == 7
    process_seconds = round(time.monotonic() - started, 3)

    # Use a separate project and no data, secret, port or network mounts.
    project = 'metahub-interruption-' + uuid4().hex[:12]
    release = root / 'release'
    state = root / 'state'
    (release / 'infra/production').mkdir(parents=True)
    (state / 'backend').mkdir(parents=True)
    (state / 'backend/runtime.env').write_text('# independent interruption fixture\n')
    (state / 'compose.env').write_text('# no serving configuration\n')
    (state / 'current-release').write_text(str(release))
    image = 'metahub/backend:2.0.0-trial.1'
    services = {name: {'image': image, 'entrypoint': ['sleep', '600'], 'network_mode': 'none'}
                for name in ('control-api', 'gateway', 'console', 'edge')}
    composefile = release / 'infra/production/compose.yml'
    composefile.write_text(json.dumps({'name': project, 'services': services}))
    def run(command, timeout=90):
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    def state_of(identity):
        return run(['docker', 'inspect', '--format', '{{.State.Status}}', identity]).stdout.strip()
    owned = []
    try:
        run(['docker', 'compose', '-f', str(composefile), 'up', '-d', 'control-api', 'gateway'])
        writers = run(['docker', 'compose', '-f', str(composefile), 'ps', '-q']).stdout.split()
        owned.extend(writers)
        assert len(writers) == 2
        oneoff = run(['docker', 'run', '-d', '--network', 'none',
                      '--label', 'com.docker.compose.project=' + project,
                      '--label', 'com.docker.compose.oneoff=True', '--entrypoint', 'sleep', image, '600']).stdout.strip()
        owned.append(oneoff)
        sentinel = run(['docker', 'run', '-d', '--network', 'none',
                       '--label', 'com.docker.compose.project=' + project + '-sentinel',
                       '--label', 'com.docker.compose.oneoff=True', '--entrypoint', 'sleep', image, '600']).stdout.strip()
        owned.append(sentinel)
        assert all(state_of(identity) == 'running' for identity in owned)
        run(['bash', str(args.tools / 'metahubctl.sh'), 'quiesce-upgrade', str(state)], timeout=120)
        assert all(state_of(identity) == 'exited' for identity in [*writers, oneoff])
        assert state_of(sentinel) == 'running', 'Other project was affected.'
        report = {'status': 'passed', 'process_group_timeout_seconds': process_seconds,
                  'ignoring_grandchild_stopped': True, 'nonzero_exit_preserved': True,
                  'real_docker_oneoff_stopped': True, 'real_docker_writers_stopped': True,
                  'unrelated_project_preserved': True, 'fixture_root': str(root),
                  'customer_data_accessed': False, 'complete_upgrade_verified': False}
        args.report.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        # Only IDs created by this fixture are eligible for removal.
        for identity in owned:
            label = run(['docker', 'inspect', '--format', '{{index .Config.Labels "com.docker.compose.project"}}', identity]).stdout.strip()
            assert label in {project, project + '-sentinel'}
            run(['docker', 'rm', '-f', identity])


if __name__ == '__main__':
    main()
