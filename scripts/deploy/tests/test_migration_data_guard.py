"""Production migration must refuse the historical personal-data purge."""
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'shared-sdk/python'))
from metahub_shared.db import apply_migrations
from metahub_shared.db.errors import DbError
from metahub_shared.db.migrate import _guard_personal_data_retirement


@pytest.fixture
def historical_database(tmp_path,monkeypatch):
    monkeypatch.setenv('METAHUB_ENV','prod')
    path=tmp_path/'historical.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY,kind TEXT NOT NULL);
            INSERT INTO workspaces VALUES('preserved-organization','organization');
            CREATE TABLE personal_profiles(profile_id TEXT PRIMARY KEY,display_name TEXT NOT NULL);
        """)
    migrations=tmp_path/'migrations'
    migrations.mkdir()
    (migrations/'0001_side_effect.sql').write_text('CREATE TABLE migration_side_effect(value TEXT);')
    (migrations/'0190_remove_personal_workspaces.sql').write_text(
        "DELETE FROM workspaces WHERE kind='personal'; DROP TABLE personal_profiles;")
    return path,migrations


@pytest.mark.parametrize('workspace,profile',[(True,False),(False,True),(True,True)])
def test_affected_customer_data_blocks_before_any_migration_writes(historical_database,workspace,profile):
    path,migrations=historical_database
    with sqlite3.connect(path) as connection:
        if workspace:
            connection.execute("INSERT INTO workspaces VALUES('preserved-personal','personal')")
        if profile:
            connection.execute("INSERT INTO personal_profiles VALUES('preserved-profile','Original name')")
    with sqlite3.connect(path) as connection:
        before='\n'.join(connection.iterdump())
    with pytest.raises(DbError) as error:
        apply_migrations('sqlite:///'+path.as_posix(),migrations_dir=migrations)
    assert error.value.code=='db.destructive_migration_blocked'
    with sqlite3.connect(path) as connection:
        assert '\n'.join(connection.iterdump())==before
        assert connection.execute("SELECT count(*) FROM sqlite_schema WHERE name IN "
                                  "('schema_migrations','migration_side_effect')").fetchone()[0]==0


def test_unaffected_organization_database_can_migrate(historical_database):
    path,migrations=historical_database
    result=apply_migrations('sqlite:///'+path.as_posix(),migrations_dir=migrations)
    assert result.applied==['0001','0190']
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT workspace_id FROM workspaces').fetchall()==[('preserved-organization',)]
        assert connection.execute("SELECT count(*) FROM schema_migrations WHERE status='applied'").fetchone()[0]==2


def test_already_applied_retirement_does_not_rewrite_history(historical_database):
    path,migrations=historical_database
    with sqlite3.connect(path) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('CREATE TABLE schema_migrations(version TEXT,status TEXT)')
        connection.execute("INSERT INTO schema_migrations VALUES('0190','applied')")
        connection.execute("INSERT INTO workspaces VALUES('independent-marker','personal')")
        before=connection.total_changes
        _guard_personal_data_retirement(connection,backend='sqlite',migrations_dir=migrations)
        assert connection.total_changes==before
        assert connection.execute("SELECT count(*) FROM workspaces WHERE kind='personal'").fetchone()[0]==1
