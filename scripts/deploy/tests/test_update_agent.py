"""Queue recovery tests; no real upgrade process, credentials or serving DB."""
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('trial_update_agent', ROOT / 'scripts/deploy/update-agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
JOB_A = '20261015T000000_' + 'a' * 32
JOB_B = '20261015T000001_' + 'b' * 32


def job(job_id=JOB_B, version='2.0.0-trial.2'):
    return {'job_id':job_id,'version':version,'account_id':'synthetic-owner',
            'requested_at':datetime.now(UTC).isoformat()}


@pytest.fixture
def queue(monkeypatch, tmp_path):
    updates, store, state, active = [tmp_path / name for name in ('updates','store','state','active')]
    for path in (updates / 'requests',updates / 'status',store,state,active / 'scripts/deploy'):
        path.mkdir(parents=True,exist_ok=True)
    (active / 'scripts/deploy/metahubctl.sh').write_text('exit 0')
    (state / 'current-release').write_text(str(active))
    (state / 'compose.env').write_text('METAHUB_RELEASE=2.0.0-trial.1\n')
    version = '2.0.0-trial.2'
    candidate_tools = store / version / 'scripts/deploy'
    candidate_tools.mkdir(parents=True)
    (candidate_tools / 'metahubctl.sh').write_text('exit 0')
    (store / f'{version}.verified.json').write_text(json.dumps({'version':version}))
    (updates / 'catalog.json').write_text(json.dumps({'releases':[{'version':version}]}))
    config = tmp_path / 'updater.json'
    config.write_text(json.dumps({'updates':str(updates),'release_store':str(store),'state':str(state)}))
    # Windows unit tests exercise queue logic only; root permissions and actual
    # flock serialization must be verified separately on the Ubuntu host.
    monkeypatch.setitem(sys.modules,'fcntl',SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *args:None))
    monkeypatch.setattr(agent.releases,'check_owned',lambda path:None)
    atomic_json = agent.releases.atomic_json
    def portable_atomic_json(path,value):
        # NTFS refuses replacement of a read-only destination; production uses
        # POSIX directory permissions. Do not change that production file mode.
        if os.name == 'nt' and path.exists():
            path.chmod(0o600)
        atomic_json(path,value)
    monkeypatch.setattr(agent.releases,'atomic_json',portable_atomic_json)
    calls=[]
    monkeypatch.setattr(agent,'run_bounded',lambda *args,**kwargs:(calls.append(args[0]) or SimpleNamespace(returncode=0)))
    return config,updates,calls


def test_verified_candidate_controller_supplies_upgrade_guards(queue):
    config,updates,calls=queue
    paths=json.loads(config.read_text())
    active=Path(Path(paths['state']).joinpath('current-release').read_text())
    (active / 'scripts/deploy/metahubctl.sh').write_text('exit 99 # legacy controller')
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job()))
    agent.process(config)
    candidate=Path(paths['release_store']) / '2.0.0-trial.2'
    assert calls==[['bash',str(candidate / 'scripts/deploy/metahubctl.sh'),
                   'upgrade',paths['state'],str(candidate),'2.0.0-trial.2']]


def test_old_state_worker_cannot_run_after_cold_recovery(queue):
    config,updates,calls=queue
    state=Path(json.loads(config.read_text())['state'])
    (state / 'recovery-in-progress').write_text('/opt/independent-recovered-state\n')
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job()))
    agent.process(config)
    assert calls==[]
    assert (updates / 'requests' / f'{JOB_B}.json').exists()


def test_missing_candidate_controller_never_falls_back_to_legacy(queue):
    config,updates,calls=queue
    paths=json.loads(config.read_text())
    candidate=Path(paths['release_store']) / '2.0.0-trial.2'
    (candidate / 'scripts/deploy/metahubctl.sh').unlink()
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job()))
    with pytest.raises(agent.releases.ReleaseError):
        agent.process(config)
    assert calls==[]


@pytest.mark.parametrize('content', ['{', 'null', '[]', '{}', 'x' * 4097], ids=['truncated','null','list','empty','oversized'])
def test_malformed_request_is_retained_and_does_not_block_next_job(queue,content):
    config,updates,calls=queue
    (updates / 'requests' / f'{JOB_A}.json').write_text(content)
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job()))
    agent.process(config)
    assert len(calls)==1
    assert json.loads((updates / 'status' / f'{JOB_A}.json').read_text(encoding="utf-8"))['status']=='rejected'
    assert json.loads((updates / 'status' / f'{JOB_B}.json').read_text(encoding="utf-8"))['status']=='completed'
    assert not list((updates / 'requests').glob('*.json'))
    assert len(list((updates / 'retained-requests').glob('*.json')))==2


@pytest.mark.parametrize('outcome',['completed','rejected','recovered'])
@pytest.mark.parametrize('malformed',[False,True])
def test_leftover_terminal_request_does_not_rerun_or_overwrite_status(queue,outcome,malformed):
    config,updates,calls=queue
    (updates / 'requests' / f'{JOB_B}.json').write_text('{' if malformed else json.dumps(job()))
    status={'job_id':JOB_B,'version':'2.0.0-trial.2','status':outcome,'message':'original record'}
    path=updates / 'status' / f'{JOB_B}.json'
    path.write_text(json.dumps(status))
    original=path.read_bytes()
    agent.process(config)
    assert calls==[]
    assert path.read_bytes()==original
    assert len(list((updates / 'retained-requests').glob('*.json')))==1


@pytest.mark.parametrize('outcome',['running','failed'])
def test_uncertain_database_state_blocks_all_following_jobs(queue,outcome):
    config,updates,calls=queue
    path=updates / 'requests' / f'{JOB_B}.json'
    path.write_text(json.dumps(job()))
    (updates / 'status' / f'{JOB_A}.json').write_text(json.dumps({'status':outcome}))
    agent.process(config)
    assert calls==[]
    assert path.exists()


def test_removed_release_is_rejected_before_opening_missing_receipt(queue):
    config,updates,calls=queue
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job(version='2.0.0-trial.99')))
    agent.process(config)
    assert calls==[]
    assert json.loads((updates / 'status' / f'{JOB_B}.json').read_text(encoding="utf-8"))['status']=='rejected'
    assert not list((updates / 'requests').glob('*.json'))


@pytest.mark.parametrize('field,value', [('version','../../escape'),('account_id','bad\nowner'),
                                       ('requested_at','2026-10-15T00:00:00'),('job_id',JOB_A)])
def test_invalid_fields_cannot_become_a_host_command(tmp_path,field,value):
    payload=job();payload[field]=value
    path=tmp_path / f'{JOB_B}.json'
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        agent.read_request(path)


def test_update_failure_retains_request_and_blocks_future_attempts(queue,monkeypatch):
    config,updates,calls=queue
    monkeypatch.setattr(agent,'run_bounded',lambda *args,**kwargs:SimpleNamespace(returncode=1))
    (updates / 'requests' / f'{JOB_A}.json').write_text(json.dumps(job(job_id=JOB_A)))
    agent.process(config)
    assert json.loads((updates / 'status' / f'{JOB_A}.json').read_text(encoding="utf-8"))['status']=='failed'
    (updates / 'requests' / f'{JOB_B}.json').write_text(json.dumps(job()))
    agent.process(config)
    assert (updates / 'requests' / f'{JOB_B}.json').exists()


def test_failure_stops_requests_already_in_same_batch(queue, monkeypatch):
    config, updates, calls = queue
    def fail(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(agent, 'run_bounded', fail)
    for identity in (JOB_A, JOB_B):
        (updates / 'requests' / f'{identity}.json').write_text(json.dumps(job(job_id=identity)))
    agent.process(config)
    assert len(calls) == 1
    assert (updates / 'requests' / f'{JOB_B}.json').exists()
    assert not (updates / 'status' / f'{JOB_B}.json').exists()


@pytest.mark.parametrize('failure', ['timeout', 'signal', 'interrupt'])
@pytest.mark.parametrize('cleanup_failure', [False, True])
def test_interrupted_upgrade_quiesces_daemon_containers_before_next_job(queue, monkeypatch, failure, cleanup_failure):
    config, updates, calls = queue
    def execute(command, **kwargs):
        calls.append(command)
        if command[2] == 'quiesce-upgrade':
            assert kwargs['timeout'] == 120
            return SimpleNamespace(returncode=int(cleanup_failure))
        if failure == 'signal':
            return SimpleNamespace(returncode=-9)
        if failure == 'interrupt':
            raise SystemExit(143)
        raise agent.subprocess.TimeoutExpired(command, 7200)
    monkeypatch.setattr(agent, 'run_bounded', execute)
    for identity in (JOB_A, JOB_B):
        (updates / 'requests' / f'{identity}.json').write_text(json.dumps(job(job_id=identity)))
    agent.process(config)
    assert [command[2] for command in calls] == ['upgrade', 'quiesce-upgrade']
    assert (updates / 'requests' / f'{JOB_B}.json').exists()
    result = json.loads((updates / 'status' / f'{JOB_A}.json').read_text(encoding='utf-8'))
    assert result['status'] == 'failed'
    assert ('停止服务未通过' in result['message']) == cleanup_failure
