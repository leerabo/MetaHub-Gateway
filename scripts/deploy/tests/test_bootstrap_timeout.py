"""Offline bootstrap timeouts must not leak into pooled request connections."""
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'shared-sdk/python'))
from metahub_shared.db.connection import _PostgresConnection


class NativeConnection:
    def __init__(self, *, fail_script=False, fail_reset=False):
        self.autocommit = False
        self.closed = False
        self.timeout = '30s'
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name='IDLE'))
        self.fail_script = fail_script
        self.fail_reset = fail_reset
        self.executed = []

    def cursor(self):
        return self

    def execute(self, sql, parameters=None, **kwargs):
        self.executed.append((sql, self.timeout, self.autocommit))
        if sql.startswith('SELECT set_config'):
            if parameters[0] == '30s' and self.fail_reset:
                raise RuntimeError('connection lost while resetting')
            self.timeout = parameters[0]
        elif sql == 'reviewed bootstrap' and self.fail_script:
            self.info.transaction_status.name = 'INERROR'
            raise RuntimeError('bootstrap failed')

    def fetchone(self):
        return (self.timeout,)

    def rollback(self):
        self.info.transaction_status.name = 'IDLE'

    def close(self):
        # The fake cursor is this object, so cursor close must be independent.
        pass


@pytest.mark.parametrize('fail_script', [False, True])
def test_bootstrap_restores_request_timeout_and_transaction_state(fail_script):
    raw = NativeConnection(fail_script=fail_script)
    connection = _PostgresConnection(raw, None)
    if fail_script:
        with pytest.raises(sqlite3.Error):
            connection.executescript('reviewed bootstrap', statement_timeout_ms=900000)
    else:
        connection.executescript('reviewed bootstrap', statement_timeout_ms=900000)
    assert ('reviewed bootstrap', '900000', True) in raw.executed
    assert raw.timeout == '30s'
    assert raw.autocommit is False
    assert not connection.in_transaction


@pytest.mark.parametrize('timeout', [0, -1, 900001, True, '900000'])
def test_invalid_or_unbounded_bootstrap_timeout_is_rejected(timeout):
    raw = NativeConnection()
    with pytest.raises(ValueError):
        _PostgresConnection(raw, None).executescript('reviewed bootstrap', statement_timeout_ms=timeout)
    assert raw.executed == []


def test_failed_reset_discards_physical_connection():
    raw = NativeConnection(fail_reset=True)
    # A separate cursor lets this case distinguish cursor and connection close.
    cursor = SimpleNamespace(execute=raw.execute, fetchone=raw.fetchone, close=lambda: None)
    raw.cursor = lambda: cursor
    raw.close = lambda: setattr(raw, 'closed', True)
    with pytest.raises(sqlite3.Error):
        _PostgresConnection(raw, None).executescript('reviewed bootstrap', statement_timeout_ms=900000)
    assert raw.closed
