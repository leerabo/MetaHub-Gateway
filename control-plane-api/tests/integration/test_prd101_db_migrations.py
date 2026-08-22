"""验证 PRD-101 Gateway fleet 正向迁移与受保护回滚，边界覆盖完整性和业务事实阻断。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

# 迁移目录与新增表集合冻结 0093 验收范围，避免遗漏权限、容量或签名 outbox 表。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD101_TABLES = {
    "platform_gateway_permission_extensions",
    "platform_gateway_role_permissions",
    "gateway_cells",
    "workspace_gateway_placements",
    "gateway_capacity_leases",
    "gateway_config_snapshot_leases",
    "gateway_releases",
    "gateway_cell_events",
    "gateway_config_outbox",
}


def _url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为 SQLite URL，禁止测试连接共享运行库。"""

    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    """验证迁移候选数据库完整且无外键违规，并确保检查连接始终关闭。"""

    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.fixture(scope="module")
def migrated_0092(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """构造冻结到 0092 的模块级基线，并只追加 0093 正向与回滚脚本。"""

    root = tmp_path_factory.mktemp("prd101-migrated-0092")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 92:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd101-0092.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0092"
    for filename in (
        "0093_cloud_gateway_fleet.sql",
        "0093_cloud_gateway_fleet.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0092_to_0093_pristine_roundtrip(
    migrated_0092: tuple[Path, Path], tmp_path: Path
) -> None:
    """验证 0092 升级、pristine 回滚和再次升级均保持表集合与数据库健康。"""

    source, migrations = migrated_0092
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0093"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD101_TABLES <= tables
        assert connection.execute("SELECT COUNT(*) FROM gateway_cells").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_gateway_role_permissions"
        ).fetchone()[0] == 5
    finally:
        connection.close()
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0093"
    ).rolled_back == "0093"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD101_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0093"]
    _healthy(database_url)


def test_gateway_fact_blocks_0093_down(
    migrated_0092: tuple[Path, Path], tmp_path: Path
) -> None:
    """写入 Gateway 租约业务事实后验证 0093 回滚原子拒绝且数据库仍健康。"""

    source, migrations = migrated_0092
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0093"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO gateway_config_snapshot_leases(
              gateway_config_snapshot_lease_id,gateway_cell_id,worker_id,snapshot_id,
              snapshot_sha256,signing_key_version,config_watermark,generation,status,
              expires_at,acquired_at,released_at)
            VALUES('gcs_11111111111111111111111111111111',
              'gcl_00000000000000000000000000000001','prd101-worker',
              'snapshot-prd101','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              1,1,1,'active',datetime('now','+5 minutes'),datetime('now'),NULL)
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0093"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    """验证后续 PRD 追加后空数据库仍能完整安装当前迁移 head。"""

    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0094"
    _healthy(database_url)
