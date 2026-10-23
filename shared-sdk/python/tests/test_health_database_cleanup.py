"""Readiness must recover after a failed ping without exhausting its pool."""
from types import SimpleNamespace

import pytest

from metahub_shared.db.errors import DbError
from metahub_shared.runtime import health


@pytest.mark.parametrize('stage', ['execute', 'fetch'])
@pytest.mark.parametrize('database_error', [True, False])
def test_failed_pings_return_checkout_and_next_ping_recovers(monkeypatch, stage, database_error):
    available = 1
    attempts = 0
    failure = (DbError('db.connection_failed', 'private diagnostic', 'private detail')
               if database_error else health.sqlite3.OperationalError('private diagnostic'))
    class Connection:
        def execute(self, sql):
            assert sql == 'SELECT 1'
            if attempts == 1 and stage == 'execute':
                raise failure
            return self
        def fetchone(self):
            if attempts == 1 and stage == 'fetch':
                raise failure
            return (1,)
        def close(self):
            nonlocal available
            available += 1
    def connect(_):
        nonlocal available, attempts
        assert available == 1, 'Failed readiness retained a checked-out connection.'
        available -= 1
        attempts += 1
        return Connection()
    monkeypatch.setattr(health, 'connect_database', connect)
    config = SimpleNamespace(database_url='independent-test-database')
    failed = health._check_database(config)
    assert failed.status == 'fail' and failed.required
    assert 'private' not in failed.message
    assert available == 1
    recovered = health._check_database(config)
    assert recovered.status == 'ok' and recovered.required
    assert available == 1
