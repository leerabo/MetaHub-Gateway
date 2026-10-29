"""Root-only Ubuntu queue checks with real flock and no real deployment calls."""
import argparse
from datetime import UTC, datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--tools',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise ValueError('This acceptance check needs root on our isolated Ubuntu VM.')
    spec=importlib.util.spec_from_file_location('queue_acceptance',args.tools / 'update-agent.py')
    agent=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    root=Path(tempfile.mkdtemp(prefix='metahub-queue-acceptance-',dir='/opt'))
    root.chmod(0o700)
    job_a='20261015T000000_'+'a'*32
    job_b='20261015T000001_'+'b'*32
    version='2.0.0-trial.2'
    def job(identity=job_b,target=version):
        return {'job_id':identity,'version':target,'account_id':'synthetic-owner',
                'requested_at':datetime.now(UTC).isoformat()}
    def fixture(name):
        home=root / name
        home.mkdir(mode=0o700)
        updates,store,state,active=[home / label for label in ('updates','store','state','active')]
        for path in (updates / 'requests',updates / 'status',store / version,state,active / 'scripts/deploy'):
            path.mkdir(parents=True,exist_ok=True)
        os.chown(updates / 'requests',10001,10001)
        (active / 'scripts/deploy/metahubctl.sh').write_text('exit 0')
        candidate_tools=store / version / 'scripts/deploy'
        candidate_tools.mkdir(parents=True)
        (candidate_tools / 'metahubctl.sh').write_text('exit 0')
        (state / 'current-release').write_text(str(active))
        (state / 'compose.env').write_text('METAHUB_RELEASE=2.0.0-trial.1\n')
        (store / (version+'.verified.json')).write_text(json.dumps({'version':version}))
        (updates / 'catalog.json').write_text(json.dumps({'releases':[{'version':version}]}))
        config=home / 'config.json'
        config.write_text(json.dumps({'updates':str(updates),'release_store':str(store),'state':str(state)}))
        return home,config,updates,state,store
    calls=[]
    # Only the actual deployment command is substituted. Files, permissions,
    # regular-file checks, root ownership and flock use real Linux behavior.
    original_run=agent.run_bounded
    agent.run_bounded=lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0))
    completed=[]
    try:
        for name,content in [('truncated','{'),('null','null'),('oversized','x'*4097)]:
            home,config,updates,state,store=fixture(name)
            (updates / 'requests' / (job_a+'.json')).write_text(content)
            (updates / 'requests' / (job_b+'.json')).write_text(json.dumps(job()))
            before=len(calls)
            agent.process(config)
            assert len(calls)==before+1
            assert calls[-1][1]==str(store / version / 'scripts/deploy/metahubctl.sh')
            assert json.loads((updates / 'status' / (job_a+'.json')).read_text())['status']=='rejected'
            assert json.loads((updates / 'status' / (job_b+'.json')).read_text())['status']=='completed'
            assert not list((updates / 'requests').glob('*.json'))
            retained=updates / 'retained-requests'
            assert retained.stat().st_uid==0 and stat.S_IMODE(retained.stat().st_mode)==0o700
            assert len(list(retained.glob('*.json')))==2
            completed.append(name)
        for malformed in (False,True):
            home,config,updates,state,store=fixture('completed-'+str(malformed))
            status_path=updates / 'status' / (job_b+'.json')
            agent.releases.atomic_json(status_path,{'status':'completed','message':'原成功记录'})
            original=status_path.read_bytes()
            (updates / 'requests' / (job_b+'.json')).write_text('{' if malformed else json.dumps(job()))
            before=len(calls)
            agent.process(config)
            assert len(calls)==before and status_path.read_bytes()==original
            completed.append('completed-preserved-'+str(malformed))
        for outcome in ('failed','running'):
            home,config,updates,state,store=fixture(outcome)
            agent.releases.atomic_json(updates / 'status' / (job_a+'.json'),{'status':outcome})
            request=updates / 'requests' / (job_b+'.json')
            request.write_text(json.dumps(job()))
            before=len(calls)
            agent.process(config)
            assert len(calls)==before and request.exists()
            completed.append(outcome+'-blocks-next')
        home,config,updates,state,store=fixture('real-lock')
        request=updates / 'requests' / (job_b+'.json')
        request.write_text(json.dumps(job()))
        before=len(calls)
        with (state / 'update.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX | fcntl.LOCK_NB)
            agent.process(config)
            assert len(calls)==before and request.exists()
        agent.process(config)
        assert len(calls)==before+1
        completed.append('real-flock-serialization')
        home,config,updates,state,store=fixture('same-batch-failure')
        for identity in (job_a,job_b):
            (updates / 'requests' / (identity+'.json')).write_text(json.dumps(job(identity)))
        before=len(calls)
        agent.run_bounded=lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=1))
        agent.process(config)
        assert len(calls)==before+1
        assert (updates / 'requests' / (job_b+'.json')).exists()
        assert not (updates / 'status' / (job_b+'.json')).exists()
        completed.append('same-batch-failure-blocks-next')
        home,config,updates,state,store=fixture('timeout-quiesce')
        for identity in (job_a,job_b):
            (updates / 'requests' / (identity+'.json')).write_text(json.dumps(job(identity)))
        before=len(calls)
        def timeout_command(command,**kwargs):
            calls.append(command)
            if command[2]=='quiesce-upgrade':
                return SimpleNamespace(returncode=0)
            raise agent.subprocess.TimeoutExpired(command,7200)
        agent.run_bounded=timeout_command
        agent.process(config)
        assert [command[2] for command in calls[before:]]==['upgrade','quiesce-upgrade']
        assert (updates / 'requests' / (job_b+'.json')).exists()
        assert json.loads((updates / 'status' / (job_a+'.json')).read_text())['status']=='failed'
        completed.append('timeout-quiesce-before-next')
        agent.run_bounded=lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0))
        for kind in ('symlink','fifo'):
            home,config,updates,state,store=fixture(kind)
            invalid=updates / 'requests' / (job_a+'.json')
            if kind=='symlink':
                target=home / 'unchanged-sentinel'
                target.write_text('independent acceptance sentinel')
                invalid.symlink_to(target)
            else:
                os.mkfifo(invalid)
            (updates / 'requests' / (job_b+'.json')).write_text(json.dumps(job()))
            before=len(calls)
            agent.process(config)
            assert len(calls)==before+1 and not os.path.lexists(invalid)
            retained=list((updates / 'retained-requests').glob('*.json'))
            if kind=='symlink':
                assert target.read_text()=='independent acceptance sentinel'
                assert any(path.is_symlink() for path in retained)
            else:
                assert any(stat.S_ISFIFO(path.lstat().st_mode) for path in retained)
            completed.append(kind+'-not-read')
        home,config,updates,state,store=fixture('ownership')
        request=updates / 'requests' / (job_b+'.json')
        request.write_text(json.dumps(job()))
        unsafe=store / version / 'unsafe-owner.txt'
        unsafe.write_text('fixture')
        os.chown(unsafe,10001,10001)
        before=len(calls)
        try:
            agent.process(config)
        except agent.releases.ReleaseError:
            pass
        else:
            raise AssertionError('Application-owned release file was accepted.')
        assert len(calls)==before
        completed.append('unsafe-release-owner-rejected')
        home,config,updates,state,store=fixture('atomic-pending')
        destination=home / 'record.json'
        stale=home / 'record.json.pending'
        stale.write_text('retained interrupted write')
        agent.releases.atomic_json(destination,{'status':'running'})
        agent.releases.atomic_json(destination,{'status':'completed'})
        assert json.loads(destination.read_text())['status']=='completed'
        assert stale.read_text()=='retained interrupted write'
        assert stat.S_IMODE(destination.stat().st_mode)==0o444
        completed.append('atomic-replacement-with-old-pending')
    finally:
        agent.run_bounded=original_run
    report={'status':'passed','platform':'Ubuntu root filesystem and real flock',
            'cases':completed,'case_count':len(completed),'fixture_root':str(root),
            'real_deployment_command_executed':False,'real_database_modified':False,
            'complete_ubuntu_upgrade_verified':False}
    args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    main()
