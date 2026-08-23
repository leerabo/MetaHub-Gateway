"""验证 PRD-103 0095 正向迁移、pristine 回滚和支付事实阻断。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

# 迁移目录与表集合冻结正式订单、receipt、attempt、退款、催收和商业效果边界。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD103_TABLES = {
    "subscription_orders",
    "payment_intents",
    "payment_webhook_receipts",
    "payment_attempts",
    "refunds",
    "dunning_cases",
    "dunning_attempts",
    "commercial_effects",
}


def _url(path: Path) -> str:
    """把 pytest 独占数据库转换为 SQLite URL，禁止指向共享开发数据库。"""

    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    """验证数据库完整性和外键，并确保检查连接关闭。"""

    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.fixture(scope="module")
def migrated_0094(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """构造冻结到 0094 的模块基线，并只追加 0095 正反迁移。"""

    root = tmp_path_factory.mktemp("prd103-migrated-0094")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 94:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd103-0094.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0094"
    for filename in (
        "0095_orders_payments_refunds.sql",
        "0095_orders_payments_refunds.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0094_to_0095_pristine_roundtrip(
    migrated_0094: tuple[Path, Path], tmp_path: Path
) -> None:
    """验证 0094 升级、pristine 回滚和再次升级均保持数据库健康。"""

    source, migrations = migrated_0094
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0095"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD103_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_index_list('payment_webhook_receipts') WHERE \"unique\"=1"
        ).fetchone()[0] >= 1
    finally:
        connection.close()
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0095"
    ).rolled_back == "0095"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD103_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0095"]
    _healthy(database_url)


def test_payment_receipt_fact_blocks_0095_down(
    migrated_0094: tuple[Path, Path], tmp_path: Path
) -> None:
    """写入已验签 receipt 后验证回滚原子拒绝且事实仍保留。"""

    source, migrations = migrated_0094
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0095"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO payment_webhook_receipts(
              receipt_id,provider,provider_event_id,provider_event_version,
              provider_occurred_at,raw_body_object_reference,raw_body_sha256,
              signature_key_version,encryption_key_version,status,processing_owner,
              processing_lease_until,safe_error_code,received_at,processed_at)
            VALUES('pwr_11111111111111111111111111111111','mockpay','event-prd103-down',1,
              '2026-08-23T00:00:00Z','mockpay/11/pwr_11111111111111111111111111111111.pay',
              ?,'1','1','pending',NULL,NULL,NULL,'2026-08-23T00:00:00Z',NULL)
            """,
            ("a" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0095"
        )
    assert blocked.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM payment_webhook_receipts"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    _healthy(database_url)


def test_fresh_schema_installs_0096(tmp_path: Path) -> None:
    """验证空数据库完整迁移链最终安装当前 0096。"""

    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0096"
    _healthy(database_url)
