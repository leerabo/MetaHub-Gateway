"""验证 PRD-104 0096 正向迁移、pristine 回滚和财务事实阻断。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD104_TABLES = {
    "billing_statements",
    "invoice_requests",
    "invoice_provider_outbox",
    "invoice_provider_receipts",
    "invoice_artifacts",
    "invoice_download_grants",
    "invoice_reversal_requests",
    "invoice_reversal_approvals",
    "reconciliation_runs",
    "reconciliation_differences",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.fixture(scope="module")
def migrated_0095(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd104-migrated-0095")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 95:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd104-0095.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0095"
    for filename in (
        "0096_invoices_tax_reconciliation.sql",
        "0096_invoices_tax_reconciliation.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0095_to_0096_pristine_roundtrip(
    migrated_0095: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0095
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0096"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD104_TABLES <= tables
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='billing_statements'"
        ).fetchone()[0]
        assert "total_minor=subtotal_minor+tax_minor" in sql
    finally:
        connection.close()
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0096"
    ).rolled_back == "0096"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD104_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0096"]
    _healthy(database_url)


def test_reconciliation_fact_blocks_0096_down(
    migrated_0095: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0095
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0096"]
    connection = connect_database(database_url)
    try:
        account_id = "gac_11111111111111111111111111111111"
        operator_id = "pto_11111111111111111111111111111111"
        connection.execute(
            """
            INSERT INTO global_accounts(
              account_id,email_normalized,phone_e164,display_name,status,login_enabled,
              credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
              last_login_at,created_at,updated_at)
            VALUES(?, 'prd104-migration@example.test',NULL,'PRD-104 Migration','active',0,
              1,1,'not_enrolled',0,NULL,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
            """,
            (account_id,),
        )
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO reconciliation_runs(
              reconciliation_run_id,scope,order_watermark,payment_watermark,meter_watermark,
              statement_watermark,invoice_watermark,order_source_sha256,payment_source_sha256,
              meter_source_sha256,statement_source_sha256,invoice_source_sha256,status,
              lease_owner,lease_token,lease_expires_at,generation,safe_error_code,
              created_by_operator_id,created_at,completed_at,updated_at)
            VALUES('rec_11111111111111111111111111111111','all',0,0,0,0,0,?,?,?,?,?,
              'queued',NULL,NULL,NULL,1,NULL,?,'2026-08-23T00:00:00Z',NULL,
              '2026-08-23T00:00:00Z')
            """,
            ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, operator_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0096"
        )
    assert blocked.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0] == 1
    finally:
        connection.close()
    _healthy(database_url)


def test_fresh_schema_installs_0096(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0096"
    _healthy(database_url)
