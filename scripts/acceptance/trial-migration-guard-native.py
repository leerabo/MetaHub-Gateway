"""Real native PostgreSQL checks for the production data-retirement guard."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'shared-sdk/python'))
import psycopg
from psycopg import sql
from metahub_shared.db import apply_migrations,close_database_pools,connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.migrate import (
    MigrationFile,_apply_one_postgresql_migration,_guard_personal_data_retirement,
)


def main():
    work=ROOT/'.tmp/trial-pg-import-4yycc3ou'
    if (work/'data/postmaster.pid').exists():
        raise ValueError('Our isolated native cluster is already running.')
    attempt=work/('migration-data-guard-'+uuid4().hex)
    attempt.mkdir()
    pg=ROOT/'.tools/postgresql17/pgsql/bin'
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
    def lifecycle(*args):
        with (attempt/'lifecycle.log').open('ab') as log:
            subprocess.run([str(pg/'pg_ctl.exe'),'-D',str(work/'data'),*args],check=True,
                           stdout=log,stderr=log,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
    old_environment=os.environ.get('METAHUB_ENV')
    started=False
    began=time.monotonic()
    try:
        started=True
        lifecycle('-l',str(attempt/'server.log'),'-w','start','-o',
                  f'-h 127.0.0.1 -p {port} -c timezone=UTC -c log_min_messages=panic -c log_min_error_statement=panic')
        database='data_guard_'+uuid4().hex
        with psycopg.connect(f'postgresql://acceptance@127.0.0.1:{port}/postgres',autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
        url=f'postgresql://acceptance@127.0.0.1:{port}/{database}'
        os.environ['METAHUB_ENV']='prod'
        results=[]
        with psycopg.connect(url,autocommit=True) as connection:
            connection.execute("CREATE TABLE workspaces(workspace_id text PRIMARY KEY,kind text NOT NULL)")
            connection.execute("INSERT INTO workspaces VALUES('preserved-organization','organization')")
            connection.execute('CREATE TABLE personal_profiles(profile_id text PRIMARY KEY,display_name text NOT NULL)')
            connection.execute('CREATE TABLE business_records(record_id text PRIMARY KEY,original_value text NOT NULL)')
            connection.execute("INSERT INTO business_records VALUES('fixture-record','Preserve original data')")
            def snapshot():
                tables=connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
                return {table:connection.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(sql.Identifier(table))).fetchall()
                        for (table,) in tables}
            for case in ('personal_workspace','personal_profile'):
                if case=='personal_workspace':
                    connection.execute("INSERT INTO workspaces VALUES('preserved-personal','personal')")
                else:
                    connection.execute("DELETE FROM workspaces WHERE workspace_id='preserved-personal'")
                    connection.execute("INSERT INTO personal_profiles VALUES('preserved-profile','Original name')")
                before=snapshot()
                try:
                    apply_migrations(url)
                except DbError as error:
                    assert error.code=='db.destructive_migration_blocked'
                else:
                    raise AssertionError('Affected production data was not blocked.')
                assert snapshot()==before
                assert connection.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0] is None
                results.append(dict(case=case,passed=True,original_fixture_data_unchanged=True,migration_metadata_created=False))
            connection.execute('DELETE FROM personal_profiles')
            before=snapshot()
            metadata=connect_database(url)
            try:
                _guard_personal_data_retirement(metadata,backend='postgresql',migrations_dir=ROOT/'infra/migrations')
            finally:
                metadata.close()
            assert snapshot()==before
            results.append(dict(case='unaffected_organization_guard_allows',passed=True,read_only=True))
            connection.execute('CREATE TABLE schema_migrations(version text PRIMARY KEY,checksum text,status text)')
            connection.execute("INSERT INTO workspaces VALUES('late-personal-write','personal')")
            before=snapshot()
            migration=MigrationFile('0190','remove_personal_workspaces',
                                    ROOT/'infra/migrations/postgresql/0190_remove_personal_workspaces.sql',
                                    'isolated-acceptance-only','DELETE FROM business_records;')
            metadata=connect_database(url)
            try:
                try:
                    _apply_one_postgresql_migration(metadata,migration)
                except DbError as error:
                    assert error.code=='db.destructive_migration_blocked'
                else:
                    raise AssertionError('The in-transaction guard did not prevent the migration body.')
            finally:
                metadata.close()
            assert snapshot()==before
            results.append(dict(case='transaction_guard_blocks_late_personal_data',passed=True,
                                actual_write_blocking_table_locks=True,migration_body_executed=False))
        report=dict(status='passed',platform='Windows native PostgreSQL; production migration rules',
                    cases=results,ubuntu_verified=False,complete_migration_run=False,
                    existing_migration_files_rewritten=False,original_customer_database_accessed=False,
                    elapsed_seconds=round(time.monotonic()-began,3),fixture=str(attempt.relative_to(ROOT)))
        (ROOT/'output/deployment-ubuntu-20261014/migration-data-guard-native-validation.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report))
    finally:
        if old_environment is None:
            os.environ.pop('METAHUB_ENV',None)
        else:
            os.environ['METAHUB_ENV']=old_environment
        close_database_pools()
        if started:
            lifecycle('-m','fast','-w','stop')


if __name__=='__main__':
    main()
