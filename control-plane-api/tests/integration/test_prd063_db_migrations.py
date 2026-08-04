"""PRD-063 0055 Adapter 目录激活迁移、约束与保真回滚专项测试。"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.provider_adapter_spec import catalog_identity


# PRD-063 专项冻结在 0055；复制明确版本可防止后续迁移改变栈顶和 down guard 语义。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为迁移器 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制不高于指定版本的正式迁移，构造与当前栈顶隔离的历史验收目录。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        prefix = source.name[:4]
        if prefix.isdigit() and int(prefix) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """同时核对 SQLite 结构完整性与全部 owner 外键。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _identity_tuple(row: sqlite3.Row) -> tuple[object, ...]:
    """提取不含随机 ID/时间的初始目录身份，供 down/re-up 保真比较。"""

    return (
        row["tenant_id"],
        row["catalog_version"],
        row["catalog_hash"],
        row["spec_count"],
        row["status"],
        row["changed"],
        row["activated_by"],
    )


def test_prd063_migration_seeds_existing_and_new_tenants_with_frozen_identity(
    tmp_path: Path,
) -> None:
    """主流程：fresh 0055 与新租户 trigger 均写入 Shared 冻结三元组。"""

    database_url = _sqlite_url(tmp_path / "prd063-seed.db")
    migrations = _migration_subset(tmp_path / "migrations-through-0055", 55)
    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied[-1] == "0055"
    seed = apply_seed_data(database_url)
    identity = catalog_identity()
    connection = connect_database(database_url)
    first = connection.execute(
        "SELECT * FROM provider_adapter_catalog_activations WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()
    assert first is not None
    assert (
        first["catalog_version"],
        first["catalog_hash"],
        first["spec_count"],
        first["status"],
        first["changed"],
        first["activated_by"],
    ) == (
        identity.version,
        identity.catalog_hash,
        identity.spec_count,
        "active",
        0,
        "adapter-spec-bootstrap",
    )
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_prd063_second','prd063-second','PRD063 Second','internal_only','active',
               datetime('now'),datetime('now'))
        """
    )
    connection.commit()
    second = connection.execute(
        "SELECT * FROM provider_adapter_catalog_activations WHERE tenant_id='tenant_prd063_second'"
    ).fetchone()
    assert second is not None
    assert _identity_tuple(second)[1:] == _identity_tuple(first)[1:]
    assert connection.execute(
        "SELECT COUNT(*) FROM provider_adapter_catalog_activations WHERE status='active'"
    ).fetchone()[0] == 2
    _assert_healthy(connection)
    connection.close()


def test_prd063_owner_unique_active_and_immutable_history_constraints(tmp_path: Path) -> None:
    """异常：跨租户、第二 active、核心 UPDATE 与 DELETE 均由数据库拒绝。"""

    database_url = _sqlite_url(tmp_path / "prd063-constraints.db")
    migrations = _migration_subset(tmp_path / "migrations-through-0055", 55)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    identity = catalog_identity()
    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT * FROM provider_adapter_catalog_activations WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()
    assert row is not None
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE provider_adapter_catalog_activations SET catalog_hash=? WHERE activation_id=?",
            ("0" * 64, row["activation_id"]),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM provider_adapter_catalog_activations WHERE activation_id=?",
            (row["activation_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO provider_adapter_catalog_activations(
              activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
              request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
            ) VALUES(
              'paca_00000000000000000000000000000001',?,?,?,?, 'active',0,
              'req_prd063_second_active','trc_prd063_second_active','adapter-spec-publisher',
              '2026-08-01T00:00:00Z',NULL,'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z'
            )
            """,
            (seed.tenant_id, identity.version, identity.catalog_hash, identity.spec_count),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO provider_adapter_catalog_activations(
              activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
              request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
            ) VALUES(
              'paca_00000000000000000000000000000002','tenant_missing',?,?,?,'active',0,
              'req_prd063_missing_owner','trc_prd063_missing_owner','adapter-spec-publisher',
              '2026-08-01T00:00:00Z',NULL,'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z'
            )
            """,
            (identity.version, identity.catalog_hash, identity.spec_count),
        )
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM provider_adapter_catalog_activations WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()


def test_prd063_down_reup_preserves_initial_identity_and_blocks_later_facts(
    tmp_path: Path,
) -> None:
    """回滚：纯 bootstrap 可保真重建，出现发布/no-op 事实后在 DDL 前阻断。"""

    migrations = _migration_subset(tmp_path / "migrations-through-0055", 55)
    clean_url = _sqlite_url(tmp_path / "prd063-roundtrip.db")
    apply_migrations(clean_url, migrations_dir=migrations)
    seed = apply_seed_data(clean_url)
    connection = connect_database(clean_url)
    before = connection.execute(
        "SELECT * FROM provider_adapter_catalog_activations WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()
    before_identity = _identity_tuple(before)
    connection.close()
    assert rollback_last_migration(clean_url, migrations_dir=migrations).rolled_back == "0055"
    connection = connect_database(clean_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name='provider_adapter_catalog_activations'"
    ).fetchone()[0] == 0
    connection.close()
    assert apply_migrations(clean_url, migrations_dir=migrations).applied == ["0055"]
    connection = connect_database(clean_url)
    after = connection.execute(
        "SELECT * FROM provider_adapter_catalog_activations WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()
    assert _identity_tuple(after) == before_identity
    _assert_healthy(connection)
    connection.close()

    guarded_url = _sqlite_url(tmp_path / "prd063-down-guard.db")
    apply_migrations(guarded_url, migrations_dir=migrations)
    guarded_seed = apply_seed_data(guarded_url)
    identity = catalog_identity()
    connection = connect_database(guarded_url)
    connection.execute(
        """
        INSERT INTO provider_adapter_catalog_activations(
          activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
          request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
        ) VALUES(
          'paca_00000000000000000000000000000003',?,?,?,?, 'superseded',0,
          'req_prd063_noop_fact','trc_prd063_noop_fact','adapter-spec-publisher',
          '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z',
          '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z'
        )
        """,
        (
            guarded_seed.tenant_id,
            identity.version,
            identity.catalog_hash,
            identity.spec_count,
        ),
    )
    connection.commit()
    connection.close()
    with pytest.raises(DbError) as exc:
        rollback_last_migration(guarded_url, migrations_dir=migrations)
    assert exc.value.code == "db.migration_failed"
    assert "prd063_down_business_facts_present" in exc.value.message
    connection = connect_database(guarded_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0055'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT COUNT(*) FROM provider_adapter_catalog_activations"
    ).fetchone()[0] == 2
    _assert_healthy(connection)
    connection.close()
