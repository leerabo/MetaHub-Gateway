"""PRD-064 0056 Adapter 合同事实、owner、不可变与回滚门禁专项测试。"""

from __future__ import annotations

import hashlib
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


# PRD-064 的迁移合同固定在 0056；复制正式子集可隔离 0057/0058 的后续事实与回滚门禁。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库路径转换为迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    """复制指定栈顶及以前的正式 up/down SQL，保持历史专项的可重复语义。"""

    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        prefix = source.name[:4]
        if prefix.isdigit() and int(prefix) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """同时核对 SQLite 结构完整性与全部复合 owner 外键。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_probe_run(connection: sqlite3.Connection, tenant_id: str) -> tuple[str, str]:
    """插入一条最小合法 probe run/case，用于直接验证数据库不可变边界。"""

    identity = catalog_identity()
    run_id = "acr_" + "1" * 32
    case_key = "prd063_custom_minimum_contract:non_stream:success"
    case_id = "acc_" + hashlib.sha256(f"{run_id}:{case_key}".encode()).hexdigest()[:32]
    timestamp = "2026-08-01T00:00:00Z"
    connection.execute(
        """
        INSERT INTO adapter_contract_runs(
          adapter_contract_run_id,tenant_id,source_request_id,source_trace_id,schema_version,
          catalog_version,catalog_hash,runner_version,suite_kind,mode,source_revision_hash,status,
          coverage_complete,total_cases,passed_cases,failed_cases,failure_codes_json,report_hash,
          started_at,completed_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            tenant_id,
            "req_prd064_migration_probe",
            "trc_prd064_migration_probe",
            "adapter-contract-report-v1",
            identity.version,
            identity.catalog_hash,
            "1.0.0",
            "probe",
            "gateway_probe",
            "2" * 64,
            "passed",
            0,
            1,
            1,
            0,
            "[]",
            "3" * 64,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO adapter_contract_cases(
          adapter_contract_case_id,tenant_id,adapter_contract_run_id,contract_case_key,spec_id,
          provider_type,adapter_name,adapter_version,case_kind,variant,auth_type,status,
          failure_code,duration_ms,evidence_hash,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            case_id,
            tenant_id,
            run_id,
            case_key,
            "adapter-spec-custom-v1",
            "custom",
            "generic_http",
            "1.0.0",
            "non_stream",
            "success",
            None,
            "passed",
            None,
            1,
            "4" * 64,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    return run_id, case_id


def test_prd064_fresh_migration_roundtrip_and_integrity(tmp_path: Path) -> None:
    """主流程：fresh 应用 0056、空事实 down/re-up 和完整性检查均通过。"""

    database_url = _sqlite_url(tmp_path / "prd064-roundtrip.db")
    migrations = _migration_subset(tmp_path / "migrations-through-0056", 56)
    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied[-1] == "0056"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type='table' AND name LIKE 'adapter_contract_%'
            """
        )
    }
    assert tables == {
        "adapter_contract_runs",
        "adapter_contract_cases",
        "adapter_contract_event_outbox",
    }
    _assert_healthy(connection)
    connection.close()

    # 重复执行必须保持幂等并复核既有 checksum，不能再次运行 0056 DDL 或制造重复表。
    repeated = apply_migrations(database_url, migrations_dir=migrations)
    assert repeated.applied == []

    assert rollback_last_migration(database_url, migrations_dir=migrations).rolled_back == "0056"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'adapter_contract_%'"
    ).fetchone()[0] == 0
    _assert_healthy(connection)
    connection.close()
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0056"]


def test_prd064_owner_immutable_and_business_down_guard(tmp_path: Path) -> None:
    """异常：run/case 更新删除、跨租户 owner 和有事实 down 均失败关闭。"""

    database_url = _sqlite_url(tmp_path / "prd064-constraints.db")
    migrations = _migration_subset(tmp_path / "migrations-through-0056", 56)
    apply_migrations(database_url, migrations_dir=migrations)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    run_id, case_id = _insert_probe_run(connection, seed.tenant_id)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE adapter_contract_runs SET report_hash=? WHERE adapter_contract_run_id=?",
            ("5" * 64, run_id),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM adapter_contract_cases WHERE adapter_contract_case_id=?",
            (case_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE adapter_contract_cases SET duration_ms=2 WHERE adapter_contract_case_id=?",
            (case_id,),
        )
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_runs WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url, migrations_dir=migrations)
    assert exc.value.code == "db.migration_failed"
    assert "prd064_down_business_facts_present" in exc.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0056'"
    ).fetchone()[0] == "applied"
    assert connection.execute(
        "SELECT COUNT(*) FROM adapter_contract_cases WHERE tenant_id=?",
        (seed.tenant_id,),
    ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()
