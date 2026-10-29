"""验证 PRD-102 0094 正向迁移、pristine 回滚和企业资料事实阻断。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

# 迁移目录与表集合冻结 0094 的权限、主体、证据、合同、开票、审核和下载授权边界。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD102_TABLES = {
    "platform_verification_permission_extensions",
    "platform_verification_role_permissions",
    "legal_entity_profiles",
    "business_verification_intents",
    "verification_evidence",
    "contract_profiles",
    "workspace_billing_profiles",
    "verification_reviews",
    "verification_evidence_download_tokens",
}


def _url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为 SQLite URL，禁止指向共享开发数据库。"""

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
def migrated_0093(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """构造冻结到 0093 的模块基线，并只追加 0094 正反迁移。"""

    root = tmp_path_factory.mktemp("prd102-migrated-0093")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 93:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd102-0093.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0093"
    for filename in (
        "0094_business_verification_billing_profile.sql",
        "0094_business_verification_billing_profile.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0093_to_0094_pristine_roundtrip(
    migrated_0093: tuple[Path, Path], tmp_path: Path
) -> None:
    """验证 0093 升级、pristine 回滚和再次升级均保持权限种子与数据库健康。"""

    source, migrations = migrated_0093
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0094"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD102_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_verification_role_permissions"
        ).fetchone()[0] == 2
    finally:
        connection.close()
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0094"
    ).rolled_back == "0094"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD102_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0094"]
    _healthy(database_url)


def test_business_fact_blocks_0094_down(
    migrated_0093: tuple[Path, Path], tmp_path: Path
) -> None:
    """写入加密法定主体事实后验证回滚原子拒绝且数据仍保留。"""

    source, migrations = migrated_0093
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0094"]
    connection = connect_database(database_url)
    try:
        connection.executescript(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('ten_11111111111111111111111111111111','prd102-down-guard',
              'PRD-102 Down Guard','internal_only','active',datetime('now'),datetime('now'));
            """
        )
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE tenant_id='ten_11111111111111111111111111111111'"
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO legal_entity_profiles(
              legal_entity_profile_id,workspace_id,tenant_id,country_code,entity_type,
              legal_name_ciphertext,registration_number_ciphertext,
              registration_blind_index,key_version,revision,status,created_at,updated_at)
            VALUES('lep_11111111111111111111111111111111',?,?,'CN','company',?,?,?,
              1,1,'active',datetime('now'),datetime('now'))
            """,
            (
                owner["workspace_id"],
                owner["tenant_id"],
                "x" * 64,
                "y" * 64,
                "hmac-sha256:" + "a" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0094"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    """验证空数据库完整迁移链最终安装当前 0095。"""

    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0096"
    _healthy(database_url)
