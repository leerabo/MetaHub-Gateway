"""Locale mismatch must stop offline tools before migration writes can start."""
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'shared-sdk/python'))
from metahub_shared.db.errors import DbError
from metahub_shared.db.migrate import _guard_production_postgresql_collation


@pytest.mark.parametrize('locale,encoding',[('en_US.utf8','UTF8'),('C.UTF-8','UTF8'),('C','SQL_ASCII')])
def test_incompatible_database_is_rejected_after_read_only_discovery(monkeypatch,locale,encoding):
    monkeypatch.setenv('METAHUB_ENV','prod')
    connection=Mock()
    connection.execute.return_value.fetchone.return_value=dict(datcollate=locale,datctype=locale,encoding=encoding)
    with pytest.raises(DbError) as error:
        _guard_production_postgresql_collation(connection)
    assert error.value.code=='db.collation_unsupported'
    assert connection.execute.call_count==1
    assert connection.execute.call_args.args[0].startswith('SELECT ')
    connection.commit.assert_not_called()


@pytest.mark.parametrize('locale',['C','POSIX'])
def test_binary_utf8_database_is_accepted(monkeypatch,locale):
    monkeypatch.setenv('METAHUB_ENV','prod')
    connection=Mock()
    connection.execute.return_value.fetchone.return_value=dict(datcollate=locale,datctype=locale,encoding='UTF8')
    _guard_production_postgresql_collation(connection)
    connection.commit.assert_not_called()
