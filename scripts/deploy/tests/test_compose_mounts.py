"""Decode Compose YAML as Docker does; option commas must stay in one mount."""
from pathlib import Path

import yaml
import pytest


def test_every_read_only_service_has_one_valid_writable_temporary_directory():
    root=Path(__file__).resolve().parents[3]
    config=yaml.safe_load((root/'infra/production/compose.yml').read_text())
    for name,service in config['services'].items():
        if not service.get('read_only'):
            continue
        mounts=service['tmpfs']
        assert len(mounts)==1,name
        destination,separator,options=mounts[0].partition(':')
        assert destination=='/tmp' and separator,name
        settings=dict(option.split('=',1) for option in options.split(','))
        assert settings['mode']=='1777',name
        assert settings['size'].endswith('m') and int(settings['size'][:-1])>0,name


def test_fresh_postgresql_uses_binary_locale_and_utf8_instead_of_host_defaults():
    root=Path(__file__).resolve().parents[3]
    config=yaml.safe_load((root/'infra/production/compose.yml').read_text())
    arguments=config['services']['postgres']['environment']['POSTGRES_INITDB_ARGS'].split()
    assert '--locale=C' in arguments
    assert '--encoding=UTF8' in arguments


def test_postgresql_readiness_excludes_the_temporary_socket_only_init_server():
    root=Path(__file__).resolve().parents[3]
    config=yaml.safe_load((root/'infra/production/compose.yml').read_text())
    command=config['services']['postgres']['healthcheck']['test']
    assert command[:2]==['CMD','pg_isready']
    assert command[command.index('-h')+1]=='127.0.0.1'
    assert command[command.index('-U')+1]=='metahub_admin'
    assert command[command.index('-d')+1]=='metahub'


@pytest.mark.parametrize('service,port', [('control-api',8001),('gateway',8000)])
def test_backend_probe_verifies_tls_and_readiness_within_container_deadline(service,port):
    root=Path(__file__).resolve().parents[3]
    config=yaml.safe_load((root/'infra/production/compose.yml').read_text())
    health=config['services'][service]['healthcheck']
    command=health['test']
    assert command[:2]==['CMD','curl']
    assert '--insecure' not in command and '-k' not in command
    assert command[command.index('--cacert')+1]=='/run/tls/ca.crt'
    assert '--fail' in command and '--output' in command
    assert command[-1]==f'https://{service}:{port}/health/ready'
    assert 0<int(command[command.index('--max-time')+1])<int(health['timeout'][:-1])
