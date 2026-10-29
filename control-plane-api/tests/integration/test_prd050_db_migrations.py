"""PRD-050 Prompt Cache 亲和迁移专项测试。

本模块只验证数据库结构、租户图、状态机和条件回滚，不保存 Prompt、Response、
Provider Key、Endpoint URL 或任何可逆敏感内容。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError


# PRD-050 的单版本重放只消费仓库原始 0040 文件；_apply_prd050_only 创建的目录不得
# 复制 0041，避免 roundtrip 越过被测版本。模块级历史快照则保留 0041，专门验证其 down
# 该模块快照保留真实迁移依赖，确保 0041 down 能把完整迁移链可靠恢复到 0040。
MIGRATIONS_DIRECTORY = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD050_MIGRATION_FILENAMES = (
    "0040_prompt_cache_affinity_routing.sql",
    "0040_prompt_cache_affinity_routing.down.sql",
)


@pytest.fixture(scope="module", autouse=True)
def prd050_migration_snapshot(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """冻结只含 0001..0041 的迁移目录，避免后续 PRD 改变本专项的栈顶语义。

    参数：pytest 模块级临时目录工厂。返回：无业务值，仅在 fixture 生命周期内切换迁移目录。
    关键约束：PRD-050 仍须通过真实 0041 down 回到 0040；0042 及以后迁移由各自专项覆盖，
    不得让新增版本把本文件的 rollback_last_migration 静默改成其他目标。
    """

    snapshot_directory = tmp_path_factory.mktemp("prd050-migrations")
    copied_versions: set[str] = set()
    for source_file in MIGRATIONS_DIRECTORY.glob("*.sql"):
        version = source_file.name[:4]
        if not version.isdigit() or int(version) > 41:
            continue
        shutil.copy2(source_file, snapshot_directory / source_file.name)
        if not source_file.name.endswith(".down.sql"):
            copied_versions.add(version)
    assert copied_versions == {f"{version:04d}" for version in range(1, 42)}

    previous_directory = os.environ.get("METAHUB_MIGRATIONS_DIR")
    # 只在本模块生命周期内覆盖迁移发现目录；finally 必须恢复调用进程原值，
    # 避免同一 pytest 进程中的其他迁移专项继承 PRD-050 的历史版本边界。
    os.environ["METAHUB_MIGRATIONS_DIR"] = str(snapshot_directory)
    try:
        yield
    finally:
        if previous_directory is None:
            os.environ.pop("METAHUB_MIGRATIONS_DIR", None)
        else:
            os.environ["METAHUB_MIGRATIONS_DIR"] = previous_directory


def _prepare_prd050_candidate(database_url: str) -> None:
    """建立以 0040 为最高已应用版本的真实候选库。

    参数：隔离数据库 URL。返回：无。关键约束：先运行当前完整迁移链，再显式执行并核对
    0041 down；这样测试既覆盖 0041 到 0040 的兼容回退，也不会把 0041 down 误当成
    PRD-050 自己的 0040 down。
    """

    apply_migrations(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0041"
    connection = connect_database(database_url)
    try:
        latest = connection.execute(
            "SELECT MAX(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0]
        assert latest == "0040"
    finally:
        connection.close()


def _apply_prd050_only(database_url: str, tmp_path: Path) -> None:
    """从 0039 状态只重放仓库原始 0040 migration，供 up/down/up 保真测试使用。

    参数：隔离数据库 URL 与 pytest 临时目录。返回：无。关键约束：复制文件而不改写 SQL，
    迁移 runner 仍校验 0040 的正式 checksum；0041 不进入临时目录，因此不会污染 0040
    roundtrip 的版本终态。
    """

    migration_directory = tmp_path / "prd050-migrations-only"
    migration_directory.mkdir(exist_ok=True)
    for filename in PRD050_MIGRATION_FILENAMES:
        shutil.copyfile(MIGRATIONS_DIRECTORY / filename, migration_directory / filename)
    assert apply_migrations(
        database_url,
        migrations_dir=migration_directory,
    ).applied == ["0040"]


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时路径转换为项目约定的 SQLite URL。

    参数：隔离数据库路径。返回：迁移入口可解析的 URL；不允许回落到共享开发库。
    """

    return f"sqlite:///{path.as_posix()}"


def _metadata(connection: sqlite3.Connection, tenant_id: str) -> dict[str, str]:
    """创建并返回同租户 Alias、Model、Provider、Virtual Key 和管理员夹具。

    参数：数据库连接和可信租户 ID。返回：仅含数据库稳定 ID 与安全 code 的字典。
    关键约束：所有父对象从同一 tenant 查询，测试本身也不能绕过租户隔离。
    """

    row = connection.execute(
        """
        SELECT
          model.model_id, model.model_alias AS internal_model_id,
          provider.provider_id, provider.provider_code,
          environment.environment_id, environment.code AS environment_code,
          key.virtual_key_id, user.user_id, user.email, user.role
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        JOIN environments environment
          ON environment.tenant_id=model.tenant_id AND environment.code='dev'
        JOIN virtual_keys key
          ON key.tenant_id=model.tenant_id
         AND (
           key.environment_id=environment.environment_id
           OR (key.environment_id IS NULL AND key.environment=environment.code)
         )
        JOIN users user
          ON user.tenant_id=model.tenant_id AND user.role='admin'
        WHERE model.tenant_id=?
        ORDER BY model.model_id, key.virtual_key_id, user.user_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    alias_id = "ma_prd050_migration_fixture"
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_by, created_at, updated_at
        )
        VALUES(?, ?, 'prd050-migration-fixture', ?, ?, ?, 'active', ?,
               '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z')
        ON CONFLICT(model_alias_id) DO NOTHING
        """,
        (
            alias_id,
            tenant_id,
            row["environment_code"],
            row["environment_id"],
            row["model_id"],
            row["user_id"],
        ),
    )
    connection.commit()
    result = {key: str(row[key]) for key in row.keys()}
    result["model_alias_id"] = alias_id
    return result


def _insert_config(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    *,
    affinity_hex: str = "a",
) -> str:
    """插入一条 enabled CacheAffinity 配置并返回稳定配置 ID。"""

    affinity_id = f"caf_{affinity_hex * 32}"
    connection.execute(
        """
        INSERT INTO cache_affinities(
          cache_affinity_id, tenant_id, model_alias_id, status, data_class,
          minimum_prefix_chars, ttl_seconds, version, created_by, updated_by,
          created_at, updated_at
        )
        VALUES(?, ?, ?, 'enabled', 'internal', 1024, 3600, 1, ?, ?,
               '2026-07-20T01:00:00Z', '2026-07-20T01:00:00Z')
        """,
        (
            affinity_id,
            tenant_id,
            metadata["model_alias_id"],
            metadata["user_id"],
            metadata["user_id"],
        ),
    )
    return affinity_id


def _insert_scope_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    *,
    target_kind: str = "environment",
) -> tuple[str, str]:
    """插入同租户 Scope 与 Binding，返回二者 ID。

    target_kind 使用 environment 或 unassigned，不伪造 Provider Credential。所有摘要均为
    不可逆测试值，不包含 Prompt 或 URL。
    """

    scope_id = f"cafs_{'b' * 32}"
    binding_id = f"cab_{'c' * 32}"
    now = "2026-07-20T01:01:00Z"
    connection.execute(
        """
        INSERT INTO cache_affinity_scopes(
          cache_affinity_scope_id, tenant_id, virtual_key_id,
          digest_schema_version, affinity_scope_digest,
          last_selected_at, created_at, updated_at
        )
        VALUES(?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (scope_id, tenant_id, metadata["virtual_key_id"], "b" * 64, now, now, now),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_bindings(
          cache_affinity_binding_id, tenant_id, cache_affinity_scope_id,
          affinity_key_digest, model_endpoint_id, endpoint_revision_digest,
          provider_id, provider_credential_id, target_kind, target_generation,
          pending_lease_generation, pending_lease_started_at,
          pending_lease_expires_at, active_selection_count, last_success_at,
          selection_count, estimated_hit_count, last_selected_at, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, NULL, ?, 1, 1, ?,
               '2026-07-20T01:01:30Z', 0, NULL, 0, 0, ?, ?, ?)
        """,
        (
            binding_id,
            tenant_id,
            scope_id,
            "c" * 64,
            metadata["model_id"],
            "d" * 64,
            metadata["provider_id"],
            target_kind,
            now,
            now,
            now,
            now,
        ),
    )
    return scope_id, binding_id


def _insert_decision_candidate(
    connection: sqlite3.Connection,
    tenant_id: str,
    metadata: dict[str, str],
    *,
    suffix: str,
    candidate_hex: str,
    reason_code: str,
    candidate_summary: str = "{}",
    status: str = "selected",
    rank: int = 1,
) -> tuple[str, str]:
    """插入可解释 Decision/Candidate，用于 Candidate CHECK 与历史保真测试。"""

    decision_id = f"rtd_prd050_{suffix}"
    candidate_id = f"rcand_{candidate_hex * 32}"
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id,
          selected_model_id, selected_internal_model_id, provider_id, provider_code,
          decision_status, decision_reason, candidate_summary, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'prd050-migration-fixture', ?, ?, ?, ?, ?, ?,
               'selected', ?, ?, '2026-07-20T01:02:00Z')
        """,
        (
            decision_id,
            tenant_id,
            f"req_prd050_{suffix}",
            f"trc_prd050_{suffix}",
            metadata["virtual_key_id"],
            metadata["model_alias_id"],
            metadata["environment_code"],
            metadata["environment_id"],
            metadata["model_id"],
            metadata["internal_model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
            reason_code,
            candidate_summary,
        ),
    )
    connection.execute(
        """
        INSERT INTO routing_candidates(
          routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
          model_alias_id, model_id, provider_id, candidate_role, configured_priority,
          rank, status, reason_code, health_state, health_fresh, keypool_state,
          price_available, price_source, created_at, updated_at
        )
        VALUES(?, ?, ?, NULL, ?, ?, ?, 'default', 0, ?, ?, ?,
               'healthy', 1, 'available', 0, 'not_evaluated',
               '2026-07-20T01:02:00Z', '2026-07-20T01:02:00Z')
        """,
        (
            candidate_id,
            tenant_id,
            decision_id,
            metadata["model_alias_id"],
            metadata["model_id"],
            metadata["provider_id"],
            rank,
            status,
            reason_code,
        ),
    )
    return decision_id, candidate_id


def _database_snapshot(
    connection: sqlite3.Connection,
) -> tuple[tuple[tuple[object, ...], ...], dict[str, tuple[tuple[object, ...], ...]]]:
    """读取全部正式结构和逐表数据，证明失败 down 没有留下半迁移状态。"""

    schema = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """
        ).fetchall()
    )
    data: dict[str, tuple[tuple[object, ...], ...]] = {}
    table_names = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name
        """
    ).fetchall()
    for row in table_names:
        # 表名来自 sqlite_master；双引号转义仍保留，避免测试辅助函数形成 SQL 歧义。
        table_name = str(row["name"])
        quoted = table_name.replace('"', '""')
        data[table_name] = tuple(
            tuple(item) for item in connection.execute(f'SELECT * FROM "{quoted}"').fetchall()
        )
    return schema, data


def test_prd050_candidate_audit_roundtrip_preserves_every_column(tmp_path: Path) -> None:
    """验证 0039 存量执行 up/down/up 后完整保留 Candidate 与 Audit。

    覆盖全部字段、NULL 语义、索引和触发器，防止可逆迁移静默丢失历史事实。
    """

    database_url = _sqlite_url(tmp_path / "prd050-roundtrip.db")
    _prepare_prd050_candidate(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0040"
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    _, candidate_id = _insert_decision_candidate(
        connection,
        seed.tenant_id,
        metadata,
        suffix="preserved",
        candidate_hex="1",
        reason_code="routing.single_eligible_candidate",
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name, before_summary_json,
          after_summary_json, changed_fields_json, client_ip, user_agent,
          request_id, trace_id, created_at
        )
        VALUES('aud_prd050_preserved', ?, ?, ?, ?, 'provider.updated', 'provider', ?,
               NULL, '{}', '{"status":"active"}', '["status"]', NULL, NULL,
               'req_prd050_audit', 'trc_prd050_audit', '2026-07-20T01:03:00Z')
        """,
        (
            seed.tenant_id,
            metadata["user_id"],
            metadata["email"],
            metadata["role"],
            metadata["provider_id"],
        ),
    )
    connection.commit()
    candidate_before = tuple(
        connection.execute(
            "SELECT * FROM routing_candidates WHERE routing_candidate_id=?",
            (candidate_id,),
        ).fetchone()
    )
    audit_before = tuple(
        connection.execute(
            "SELECT * FROM audit_logs WHERE audit_log_id='aud_prd050_preserved'"
        ).fetchone()
    )

    _apply_prd050_only(database_url, tmp_path)
    assert tuple(
        connection.execute(
            "SELECT * FROM routing_candidates WHERE routing_candidate_id=?",
            (candidate_id,),
        ).fetchone()
    ) == candidate_before
    assert tuple(
        connection.execute(
            "SELECT * FROM audit_logs WHERE audit_log_id='aud_prd050_preserved'"
        ).fetchone()
    ) == audit_before
    candidate_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='routing_candidates'"
    ).fetchone()[0]
    assert "routing.cache_affinity_pending_preferred" in candidate_sql
    assert "routing.cache_affinity_warm_preferred" in candidate_sql

    assert rollback_last_migration(database_url).rolled_back == "0040"
    assert tuple(
        connection.execute(
            "SELECT * FROM routing_candidates WHERE routing_candidate_id=?",
            (candidate_id,),
        ).fetchone()
    ) == candidate_before
    assert tuple(
        connection.execute(
            "SELECT * FROM audit_logs WHERE audit_log_id='aud_prd050_preserved'"
        ).fetchone()
    ) == audit_before
    restored_candidate_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='routing_candidates'"
    ).fetchone()[0]
    restored_audit_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_logs'"
    ).fetchone()[0]
    assert "routing.cache_affinity_pending_preferred" not in restored_candidate_sql
    assert "cache_affinity.created" not in restored_audit_sql
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'cache_affinity%'"
    ).fetchone()[0] == 0

    _apply_prd050_only(database_url, tmp_path)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


@pytest.mark.parametrize(
    ("action", "object_type", "object_id", "suffix"),
    [
        (
            "policy.dry_run_executed",
            "policy_explanation",
            "pex_prd050_replace_guard",
            "dry_run",
        ),
        (
            "model_quality_score.updated",
            "model_quality_score",
            "mqs_prd050_replace_guard",
            "model_quality",
        ),
    ],
    ids=("dry-run", "model-quality"),
)
def test_prd050_audit_replace_guard_covers_partial_unique_keys(
    tmp_path: Path,
    action: str,
    object_type: str,
    object_id: str,
    suffix: str,
) -> None:
    """默认非递归 trigger 下，两条部分唯一键都不能被换 ID 的 REPLACE 绕过。"""

    database_url = _sqlite_url(tmp_path / f"prd050-audit-replace-{suffix}.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    request_id = f"req_prd050_audit_replace_{suffix}"
    original_id = f"aud_prd050_original_{suffix}"
    replacement_id = f"aud_prd050_replacement_{suffix}"
    audit_values = (
        seed.tenant_id,
        metadata["user_id"],
        metadata["email"],
        metadata["role"],
        action,
        object_type,
        object_id,
        request_id,
        f"trc_prd050_audit_replace_{suffix}",
        "2026-07-20T00:30:00Z",
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]', ?, ?, ?)
        """,
        (original_id, *audit_values),
    )
    connection.commit()
    assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        connection.execute(
            """
            INSERT OR REPLACE INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]', ?, ?, ?)
            """,
            (replacement_id, *audit_values),
        )
    connection.rollback()
    rows = connection.execute(
        """
        SELECT audit_log_id FROM audit_logs
        WHERE tenant_id=? AND actor_user_id=? AND request_id=? AND action=?
        """,
        (seed.tenant_id, metadata["user_id"], request_id, action),
    ).fetchall()
    assert [row["audit_log_id"] for row in rows] == [original_id]
    connection.close()


def test_prd050_audit_rowid_contract_survives_roundtrip_and_blocks_replace(
    tmp_path: Path,
) -> None:
    """保留 0039 负 rowid，同时允许隐式 INSERT 并阻断显式负 rowid REPLACE。

    参数：隔离数据库目录。返回值：无。关键约束：up/down/up 必须逐值保留隐藏 rowid；
    SQLite 在 BEFORE INSERT 中以 -1 表示尚未分配的隐式 rowid，只有 AFTER 阶段仍为负数
    才能判定为显式输入。失败的 REPLACE 必须原子恢复被隐式删除的历史审计行。
    """

    database_url = _sqlite_url(tmp_path / "prd050-audit-negative-rowid.db")
    _prepare_prd050_candidate(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0040"
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    values = (
        seed.tenant_id,
        metadata["user_id"],
        metadata["email"],
        metadata["role"],
        metadata["provider_id"],
    )
    connection.execute(
        """
        INSERT INTO audit_logs(
          rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES(-1, 'aud_prd050_legacy_negative', ?, ?, ?, ?,
               'provider.updated', 'provider', ?, '{}', '{}', '[]',
               'req_prd050_legacy_negative', 'trc_prd050_legacy_negative',
               '2026-07-20T00:40:00Z')
        """,
        values,
    )
    connection.commit()
    assert connection.execute(
        "SELECT rowid FROM audit_logs WHERE audit_log_id='aud_prd050_legacy_negative'"
    ).fetchone()[0] == -1

    _apply_prd050_only(database_url, tmp_path)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='trigger' AND name='trg_audit_logs_prd050_rowid_insert'
        """
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT rowid FROM audit_logs WHERE audit_log_id='aud_prd050_legacy_negative'"
    ).fetchone()[0] == -1

    # 未指定 rowid 的正常业务 INSERT 在 AFTER 阶段已获得数据库分配值，不能与 -1 哨兵混淆。
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, before_summary_json, after_summary_json,
          changed_fields_json, request_id, trace_id, created_at
        )
        VALUES('aud_prd050_implicit_rowid', ?, ?, ?, ?,
               'provider.updated', 'provider', ?, '{}', '{}', '[]',
               'req_prd050_implicit_rowid', 'trc_prd050_implicit_rowid',
               '2026-07-20T00:41:00Z')
        """,
        values,
    )
    connection.commit()
    implicit_rowid = connection.execute(
        "SELECT rowid FROM audit_logs WHERE audit_log_id='aud_prd050_implicit_rowid'"
    ).fetchone()[0]
    # 仅有历史 -1 时 SQLite 可把首个隐式 rowid 分配为 0；合同只禁止负数。
    assert int(implicit_rowid) >= 0

    # REPLACE 会先按 rowid 隐式删除旧行；AFTER ABORT 必须回滚整条语句并恢复原审计事实。
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        connection.execute(
            """
            INSERT OR REPLACE INTO audit_logs(
              rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            )
            VALUES(-1, 'aud_prd050_negative_replacement', ?, ?, ?, ?,
                   'provider.updated', 'provider', ?, '{}', '{}', '[]',
                   'req_prd050_negative_replacement',
                   'trc_prd050_negative_replacement', '2026-07-20T00:42:00Z')
            """,
            values,
        )
    connection.rollback()
    legacy = connection.execute(
        """
        SELECT rowid, audit_log_id FROM audit_logs
        WHERE rowid=-1 OR audit_log_id='aud_prd050_negative_replacement'
        """
    ).fetchall()
    assert [tuple(row) for row in legacy] == [(-1, "aud_prd050_legacy_negative")]

    # down 必须移除 0040 专属 trigger，但仍按隐藏 rowid 保真恢复 0039；再次 up 后合同重建。
    assert rollback_last_migration(database_url).rolled_back == "0040"
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='trigger' AND name IN (
          'trg_audit_logs_prd050_conflict_insert',
          'trg_audit_logs_prd050_rowid_insert'
        )
        """
    ).fetchone()[0] == 0
    down_rows = connection.execute(
        """
        SELECT rowid, audit_log_id FROM audit_logs
        WHERE audit_log_id IN ('aud_prd050_legacy_negative', 'aud_prd050_implicit_rowid')
        ORDER BY audit_log_id
        """
    ).fetchall()
    assert {tuple(row) for row in down_rows} == {
        (-1, "aud_prd050_legacy_negative"),
        (implicit_rowid, "aud_prd050_implicit_rowid"),
    }

    _apply_prd050_only(database_url, tmp_path)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='trigger' AND name IN (
          'trg_audit_logs_prd050_conflict_insert',
          'trg_audit_logs_prd050_rowid_insert'
        )
        """
    ).fetchone()[0] == 2
    reapplied_rows = connection.execute(
        """
        SELECT rowid, audit_log_id FROM audit_logs
        WHERE audit_log_id IN ('aud_prd050_legacy_negative', 'aud_prd050_implicit_rowid')
        ORDER BY audit_log_id
        """
    ).fetchall()
    assert {tuple(row) for row in reapplied_rows} == {
        (-1, "aud_prd050_legacy_negative"),
        (implicit_rowid, "aud_prd050_implicit_rowid"),
    }
    connection.close()


def test_prd050_replace_guards_pointer_delete_and_same_second_rebind(tmp_path: Path) -> None:
    """默认非递归触发器下阻断 REPLACE/悬空指针，并允许同秒 generation 重绑。"""

    database_url = _sqlite_url(tmp_path / "prd050-replace.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    affinity_id = _insert_config(connection, seed.tenant_id, metadata)
    scope_id, binding_id = _insert_scope_binding(
        connection, seed.tenant_id, metadata, target_kind="unassigned"
    )
    connection.commit()
    assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0

    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_replace_forbidden"):
        connection.execute(
            "INSERT OR REPLACE INTO cache_affinities SELECT * FROM cache_affinities "
            "WHERE cache_affinity_id=?",
            (affinity_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_scope_replace_forbidden"):
        connection.execute(
            "INSERT OR REPLACE INTO cache_affinity_scopes SELECT * FROM cache_affinity_scopes "
            "WHERE cache_affinity_scope_id=?",
            (scope_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_binding_replace_forbidden"):
        connection.execute(
            "INSERT OR REPLACE INTO cache_affinity_bindings "
            "SELECT * FROM cache_affinity_bindings WHERE cache_affinity_binding_id=?",
            (binding_id,),
        )
    connection.rollback()

    # started_at 保持同一秒仍是合法新 lease；generation 已提供 ABA 防护，不要求伪造未来时间。
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET target_kind='environment', target_generation=2,
            pending_lease_generation=2, active_selection_count=0,
            pending_lease_started_at=pending_lease_started_at,
            pending_lease_expires_at='2026-07-20T01:01:30Z', updated_at=updated_at
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (seed.tenant_id, binding_id),
    )
    connection.execute(
        """
        UPDATE cache_affinity_scopes
        SET preferred_binding_id=?, preferred_lease_generation=2
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        """,
        (binding_id, seed.tenant_id, scope_id),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_binding_delete_unsafe"):
        connection.execute(
            "DELETE FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
            (seed.tenant_id, binding_id),
        )
    connection.rollback()
    connection.execute(
        """
        UPDATE cache_affinity_scopes
        SET preferred_binding_id=NULL, preferred_lease_generation=NULL
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        """,
        (seed.tenant_id, scope_id),
    )
    connection.execute(
        "DELETE FROM cache_affinity_bindings WHERE tenant_id=? AND cache_affinity_binding_id=?",
        (seed.tenant_id, binding_id),
    )
    connection.execute(
        "DELETE FROM cache_affinity_scopes WHERE tenant_id=? AND cache_affinity_scope_id=?",
        (seed.tenant_id, scope_id),
    )
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd050_selection_requires_gate_and_only_moves_to_matching_terminal(tmp_path: Path) -> None:
    """Selection 必须绑定可信 Claim/执行门，且 outcome 只能按真实 gate 终态单向推进。"""

    database_url = _sqlite_url(tmp_path / "prd050-selection.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    affinity_id = _insert_config(connection, seed.tenant_id, metadata)
    scope_id, binding_id = _insert_scope_binding(connection, seed.tenant_id, metadata)
    request_id = "req_prd050_selection"
    trace_id = "trc_prd050_selection"
    attempt_id = "rta_prd050_selection"
    selection_id = f"cas_{'e' * 32}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-20T01:04:00Z')
        """,
        (seed.tenant_id, request_id, trace_id, metadata["virtual_key_id"]),
    )
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
          attempt_sequence, attempt_role, trigger_type, model_id, internal_model_id,
          provider_id, provider_code, status, retryable, usage_status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?,
               'started', 0, 'pending', '2026-07-20T01:04:00Z', '2026-07-20T01:04:00Z')
        """,
        (
            attempt_id,
            seed.tenant_id,
            request_id,
            trace_id,
            metadata["model_alias_id"],
            metadata["model_id"],
            metadata["internal_model_id"],
            metadata["provider_id"],
            metadata["provider_code"],
        ),
    )
    connection.execute(
        """
        INSERT INTO cache_affinity_selections(
          cache_affinity_selection_id, tenant_id, request_id, trace_id,
          attempt_sequence, routing_attempt_id, stream_session_id,
          cache_affinity_id, cache_affinity_version,
          cache_affinity_scope_id_snapshot, cache_affinity_binding_id_snapshot,
          model_alias_id, model_endpoint_id, endpoint_revision_digest_snapshot,
          provider_id, provider_code_snapshot, provider_credential_id_snapshot,
          target_kind, target_generation, pending_lease_generation_snapshot,
          ttl_seconds_snapshot, warm_evidence_at_snapshot,
          endpoint_preference_applied, endpoint_preference_state,
          binding_state_before, selection_reason, estimated_hit,
          outcome_status, outcome_at, created_at
        )
        VALUES(?, ?, ?, ?, 1, ?, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?, NULL,
               'environment', 1, 1, 3600, NULL, 1, 'pending', 'pending',
               'binding_pending_reused', 0, 'pending', NULL, '2026-07-20T01:04:01Z')
        """,
        (
            selection_id,
            seed.tenant_id,
            request_id,
            trace_id,
            attempt_id,
            affinity_id,
            scope_id,
            binding_id,
            metadata["model_alias_id"],
            metadata["model_id"],
            "d" * 64,
            metadata["provider_id"],
            metadata["provider_code"],
        ),
    )
    connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="selection_transition_invalid"):
        connection.execute(
            """
            UPDATE cache_affinity_selections
            SET outcome_status='succeeded', outcome_at='2026-07-20T01:04:02Z'
            WHERE tenant_id=? AND cache_affinity_selection_id=?
            """,
            (seed.tenant_id, selection_id),
        )
    connection.rollback()
    connection.execute(
        """
        UPDATE routing_attempts
        SET status='succeeded', completed_at='2026-07-20T01:04:02Z',
            updated_at='2026-07-20T01:04:02Z'
        WHERE tenant_id=? AND routing_attempt_id=?
        """,
        (seed.tenant_id, attempt_id),
    )
    connection.execute(
        """
        UPDATE cache_affinity_selections
        SET outcome_status='succeeded', outcome_at='2026-07-20T01:04:02Z'
        WHERE tenant_id=? AND cache_affinity_selection_id=?
        """,
        (seed.tenant_id, selection_id),
    )
    connection.commit()
    # 同终态恢复幂等；反向或互换必须继续由数据库拒绝。
    connection.execute(
        """
        UPDATE cache_affinity_selections
        SET outcome_status='succeeded', outcome_at='2026-07-20T01:04:02Z'
        WHERE tenant_id=? AND cache_affinity_selection_id=?
        """,
        (seed.tenant_id, selection_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="selection_transition_invalid"):
        connection.execute(
            """
            UPDATE cache_affinity_selections
            SET outcome_status='failed', outcome_at='2026-07-20T01:04:02Z'
            WHERE tenant_id=? AND cache_affinity_selection_id=?
            """,
            (seed.tenant_id, selection_id),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_selection_immutable"):
        connection.execute(
            "DELETE FROM cache_affinity_selections WHERE cache_affinity_selection_id=?",
            (selection_id,),
        )
    connection.rollback()
    connection.close()


@pytest.mark.parametrize("fact_kind", ["config", "decision", "audit", "subscription", "delivery"])
def test_prd050_nonempty_down_is_atomic_for_each_fact_family(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """非空 down：配置、解释、审计、订阅或投递任一事实都在 DDL 前固定拒绝。"""

    database_url = _sqlite_url(tmp_path / f"prd050-down-{fact_kind}.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    if fact_kind == "config":
        _insert_config(connection, seed.tenant_id, metadata)
    elif fact_kind == "decision":
        connection.execute(
            """
            INSERT INTO routing_decisions(
              routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
              model_alias_id, requested_model, environment, environment_id,
              selected_model_id, selected_internal_model_id, provider_id, provider_code,
              decision_status, decision_reason, candidate_summary, created_at
            )
            VALUES('rtd_prd050_down_decision', ?, 'req_prd050_down_decision',
                   'trc_prd050_down_decision', ?, ?, 'prd050-migration-fixture', ?, ?,
                   ?, ?, ?, ?, 'selected', 'routing.single_eligible_candidate',
                   '{"cache_affinity":{"state":"pending"}}', '2026-07-20T01:05:00Z')
            """,
            (
                seed.tenant_id,
                metadata["virtual_key_id"],
                metadata["model_alias_id"],
                metadata["environment_code"],
                metadata["environment_id"],
                metadata["model_id"],
                metadata["internal_model_id"],
                metadata["provider_id"],
                metadata["provider_code"],
            ),
        )
    elif fact_kind == "audit":
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
              action, object_type, object_id, before_summary_json, after_summary_json,
              changed_fields_json, request_id, trace_id, created_at
            )
            VALUES('aud_prd050_down', ?, ?, ?, ?, 'cache_affinity.created',
                   'cache_affinity', ?, '{}', '{"status":"enabled"}', '["status"]',
                   'req_prd050_down_audit', 'trc_prd050_down_audit',
                   '2026-07-20T01:05:00Z')
            """,
            (
                seed.tenant_id,
                metadata["user_id"],
                metadata["email"],
                metadata["role"],
                f"caf_{'f' * 32}",
            ),
        )
    else:
        endpoint_id = f"wh_prd050_{fact_kind}"
        event_types = '["cache.affinity_selected"]' if fact_kind == "subscription" else "[]"
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status,
              created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, 'PRD050', 'https://webhook.invalid/prd050', ?,
                   'encrypted-test-secret', 'whsec_test', 'fingerprint-test', 'active',
                   ?, ?, '2026-07-20T01:05:00Z', '2026-07-20T01:05:00Z')
            """,
            (
                endpoint_id,
                seed.tenant_id,
                event_types,
                metadata["user_id"],
                metadata["user_id"],
            ),
        )
        if fact_kind == "delivery":
            connection.execute(
                """
                INSERT INTO webhook_deliveries(
                  webhook_delivery_id, tenant_id, webhook_endpoint_id, event_id,
                  event_type, event_timestamp, payload_json, status,
                  attempt_count, max_attempts, created_at, updated_at
                )
                VALUES('whd_prd050_down', ?, ?, 'cas_prd050_down',
                       'cache.affinity_selected', '2026-07-20T01:05:00Z', '{}',
                       'pending', 0, 3, '2026-07-20T01:05:00Z',
                       '2026-07-20T01:05:00Z')
                """,
                (seed.tenant_id, endpoint_id),
            )
    connection.commit()
    before = _database_snapshot(connection)

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd050_down_business_facts_present" in exc.value.message
    assert _database_snapshot(connection) == before
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0040'"
    ).fetchone()[0] == "applied"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_prd050_candidate_reason_and_down_preflight_contract_are_complete(tmp_path: Path) -> None:
    """新 reason 只允许 selected/rank1，且 down SQL 明确枚举全部业务事实家族。"""

    database_url = _sqlite_url(tmp_path / "prd050-reason.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    _insert_decision_candidate(
        connection,
        seed.tenant_id,
        metadata,
        suffix="new_reason",
        candidate_hex="2",
        reason_code="routing.cache_affinity_pending_preferred",
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        _insert_decision_candidate(
            connection,
            seed.tenant_id,
            metadata,
            suffix="bad_reason",
            candidate_hex="3",
            reason_code="routing.cache_affinity_warm_preferred",
            status="standby",
            rank=2,
        )
    connection.rollback()

    down_sql = (
        Path("infra/migrations/0040_prompt_cache_affinity_routing.down.sql")
        .read_text(encoding="utf-8")
    )
    for required in (
        "cache_affinities",
        "cache_affinity_scopes",
        "cache_affinity_bindings",
        "cache_affinity_selections",
        "cache_affinity_config_events",
        "routing.cache_affinity_pending_preferred",
        "$.cache_affinity",
        "cache_affinity.created",
        "cache.affinity_selected",
        "webhook_deliveries",
        "webhook_delivery_attempts",
        "prd050_down_business_facts_present",
    ):
        assert required in down_sql
    connection.close()


def test_prd050_runtime_table_columns_and_config_event_are_exact(tmp_path: Path) -> None:
    """五张表字段名与跨模块合同一致，配置事件只接受完整安全快照。"""

    database_url = _sqlite_url(tmp_path / "prd050-schema-columns.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    expected_columns = {
        "cache_affinities": [
            "cache_affinity_id", "tenant_id", "model_alias_id", "status", "data_class",
            "minimum_prefix_chars", "ttl_seconds", "version", "created_by", "updated_by",
            "created_at", "updated_at",
        ],
        "cache_affinity_scopes": [
            "cache_affinity_scope_id", "tenant_id", "virtual_key_id",
            "digest_schema_version", "affinity_scope_digest", "preferred_binding_id",
            "preferred_lease_generation", "last_selected_at", "created_at", "updated_at",
        ],
        "cache_affinity_bindings": [
            "cache_affinity_binding_id", "tenant_id", "cache_affinity_scope_id",
            "affinity_key_digest", "model_endpoint_id", "endpoint_revision_digest",
            "provider_id", "provider_credential_id", "target_kind", "target_generation",
            "pending_lease_generation", "pending_lease_started_at",
            "pending_lease_expires_at", "active_selection_count", "last_success_at",
            "selection_count", "estimated_hit_count", "last_selected_at", "created_at",
            "updated_at",
        ],
        "cache_affinity_selections": [
            "cache_affinity_selection_id", "tenant_id", "request_id", "trace_id",
            "attempt_sequence", "routing_attempt_id", "stream_session_id",
            "cache_affinity_id", "cache_affinity_version",
            "cache_affinity_scope_id_snapshot", "cache_affinity_binding_id_snapshot",
            "model_alias_id", "model_endpoint_id", "endpoint_revision_digest_snapshot",
            "provider_id", "provider_code_snapshot", "provider_credential_id_snapshot",
            "target_kind", "target_generation", "pending_lease_generation_snapshot",
            "ttl_seconds_snapshot", "warm_evidence_at_snapshot",
            "endpoint_preference_applied", "endpoint_preference_state",
            "binding_state_before", "selection_reason", "estimated_hit",
            "outcome_status", "outcome_at", "created_at",
        ],
        "cache_affinity_config_events": [
            "cache_affinity_config_event_id", "tenant_id", "cache_affinity_id",
            "model_alias_id", "cache_affinity_version", "actor_user_id",
            "operation_request_id", "trace_id", "request_fingerprint", "event_type",
            "before_json", "after_json", "created_at",
        ],
    }
    for table_name, columns in expected_columns.items():
        assert [
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        ] == columns

    affinity_id = _insert_config(connection, seed.tenant_id, metadata)
    after_json = json.dumps(
        {
            "status": "enabled",
            "data_class": "internal",
            "minimum_prefix_chars": 1024,
            "ttl_seconds": 3600,
            "version": 1,
        },
        separators=(",", ":"),
    )
    event_id = f"cae_{'4' * 32}"
    connection.execute(
        """
        INSERT INTO cache_affinity_config_events(
          cache_affinity_config_event_id, tenant_id, cache_affinity_id,
          model_alias_id, cache_affinity_version, actor_user_id,
          operation_request_id, trace_id, request_fingerprint, event_type,
          before_json, after_json, created_at
        )
        VALUES(?, ?, ?, ?, 1, ?, 'req_prd050_config_event',
               'trc_prd050_config_event', ?, 'created', '{}', ?,
               '2026-07-20T01:06:00Z')
        """,
        (
            event_id,
            seed.tenant_id,
            affinity_id,
            metadata["model_alias_id"],
            metadata["user_id"],
            "4" * 64,
            after_json,
        ),
    )
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="cache_affinity_config_event_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO cache_affinity_config_events "
            "SELECT * FROM cache_affinity_config_events "
            "WHERE cache_affinity_config_event_id=?",
            (event_id,),
        )
    connection.rollback()
    connection.close()


def test_prd050_deleted_managed_credential_allows_terminal_updates_and_blocks_down(
    tmp_path: Path,
) -> None:
    """已选中 Credential 删除后仍可完成终态与恢复，但新目标写入继续校验所有权。

    Binding 与 Selection 刻意不设置 Credential 长期外键；终态回调只消费冻结快照和
    generation，不能因管理员在并发请求期间删 Key 而永久遗留 pending 或阻塞启动恢复。
    """

    database_url = _sqlite_url(tmp_path / "prd050-deleted-credential.db")
    _prepare_prd050_candidate(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    metadata = _metadata(connection, seed.tenant_id)
    affinity_id = _insert_config(connection, seed.tenant_id, metadata)
    scope_id, binding_id = _insert_scope_binding(connection, seed.tenant_id, metadata)
    credential_id = "pcred_prd050_ephemeral"
    connection.execute(
        """
        INSERT INTO provider_credentials(
          provider_credential_id, tenant_id, provider_id, key_fingerprint,
          encrypted_key, name, key_prefix, weight, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, 'aesgcm-v1:test-only-nonsecret',
               'PRD050 Ephemeral', 'test-only', 100, 'active',
               '2026-07-20T01:05:00Z', '2026-07-20T01:05:00Z')
        """,
        (credential_id, seed.tenant_id, metadata["provider_id"], "5" * 64),
    )
    # 从环境目标切成托管目标属于新写入，必须先通过同租户、同 Provider Credential 校验。
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET provider_credential_id=?, target_kind='managed', target_generation=2,
            pending_lease_generation=2,
            pending_lease_started_at='2026-07-20T01:05:00Z',
            pending_lease_expires_at='2026-07-20T01:05:30Z',
            updated_at='2026-07-20T01:05:00Z'
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (credential_id, seed.tenant_id, binding_id),
    )
    connection.execute(
        """
        UPDATE cache_affinity_scopes
        SET preferred_binding_id=?, preferred_lease_generation=2,
            updated_at='2026-07-20T01:05:00Z'
        WHERE tenant_id=? AND cache_affinity_scope_id=?
        """,
        (binding_id, seed.tenant_id, scope_id),
    )
    gates = (
        (
            "req_prd050_deleted_finalize",
            "trc_prd050_deleted_finalize",
            "rta_prd050_deleted_finalize",
            f"cas_{'1' * 32}",
        ),
        (
            "req_prd050_deleted_recovery",
            "trc_prd050_deleted_recovery",
            "rta_prd050_deleted_recovery",
            f"cas_{'2' * 32}",
        ),
    )
    connection.executemany(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-20T01:05:00Z')
        """,
        [
            (seed.tenant_id, request_id, trace_id, metadata["virtual_key_id"])
            for request_id, trace_id, _, _ in gates
        ],
    )
    connection.executemany(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, model_alias_id,
          attempt_sequence, attempt_role, trigger_type, model_id, internal_model_id,
          provider_id, provider_code, status, retryable, usage_status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?,
               'started', 0, 'pending', '2026-07-20T01:05:00Z',
               '2026-07-20T01:05:00Z')
        """,
        [
            (
                attempt_id,
                seed.tenant_id,
                request_id,
                trace_id,
                metadata["model_alias_id"],
                metadata["model_id"],
                metadata["internal_model_id"],
                metadata["provider_id"],
                metadata["provider_code"],
            )
            for request_id, trace_id, attempt_id, _ in gates
        ],
    )
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=2, selection_count=2,
            last_selected_at='2026-07-20T01:05:01Z',
            updated_at='2026-07-20T01:05:01Z'
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (seed.tenant_id, binding_id),
    )
    connection.executemany(
        """
        INSERT INTO cache_affinity_selections(
          cache_affinity_selection_id, tenant_id, request_id, trace_id,
          attempt_sequence, routing_attempt_id, stream_session_id,
          cache_affinity_id, cache_affinity_version,
          cache_affinity_scope_id_snapshot, cache_affinity_binding_id_snapshot,
          model_alias_id, model_endpoint_id, endpoint_revision_digest_snapshot,
          provider_id, provider_code_snapshot, provider_credential_id_snapshot,
          target_kind, target_generation, pending_lease_generation_snapshot,
          ttl_seconds_snapshot, warm_evidence_at_snapshot,
          endpoint_preference_applied, endpoint_preference_state,
          binding_state_before, selection_reason, estimated_hit,
          outcome_status, outcome_at, created_at
        )
        VALUES(?, ?, ?, ?, 1, ?, NULL, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?,
               'managed', 2, 2, 3600, NULL, 1, 'pending', 'pending',
               'binding_pending_reused', 0, 'pending', NULL,
               '2026-07-20T01:05:01Z')
        """,
        [
            (
                selection_id,
                seed.tenant_id,
                request_id,
                trace_id,
                attempt_id,
                affinity_id,
                scope_id,
                binding_id,
                metadata["model_alias_id"],
                metadata["model_id"],
                "d" * 64,
                metadata["provider_id"],
                metadata["provider_code"],
                credential_id,
            )
            for request_id, trace_id, attempt_id, selection_id in gates
        ],
    )
    connection.commit()

    # Selection 已冻结 Credential 快照后允许管理员物理删除 Key，不以长期 FK 锁住治理操作。
    connection.execute(
        "DELETE FROM provider_credentials WHERE tenant_id=? AND provider_credential_id=?",
        (seed.tenant_id, credential_id),
    )
    connection.commit()
    assert connection.execute(
        "SELECT 1 FROM provider_credentials WHERE provider_credential_id=?",
        (credential_id,),
    ).fetchone() is None

    finalize_request, _, finalize_attempt, finalize_selection = gates[0]
    connection.execute(
        """
        UPDATE routing_attempts
        SET status='succeeded', completed_at='2026-07-20T01:05:02Z',
            updated_at='2026-07-20T01:05:02Z'
        WHERE tenant_id=? AND routing_attempt_id=? AND request_id=?
        """,
        (seed.tenant_id, finalize_attempt, finalize_request),
    )
    connection.execute(
        """
        UPDATE cache_affinity_selections
        SET outcome_status='succeeded', outcome_at='2026-07-20T01:05:02Z'
        WHERE tenant_id=? AND cache_affinity_selection_id=?
        """,
        (seed.tenant_id, finalize_selection),
    )
    # 成功回调只验证冻结 generation；Credential 已删也必须递减计数并写入 warm 证据。
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=1, last_success_at='2026-07-20T01:05:02Z',
            updated_at='2026-07-20T01:05:02Z'
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (seed.tenant_id, binding_id),
    )
    connection.commit()

    recovery_request, _, recovery_attempt, recovery_selection = gates[1]
    connection.execute(
        """
        UPDATE routing_attempts
        SET status='failed', completed_at='2026-07-20T01:05:03Z',
            updated_at='2026-07-20T01:05:03Z'
        WHERE tenant_id=? AND routing_attempt_id=? AND request_id=?
        """,
        (seed.tenant_id, recovery_attempt, recovery_request),
    )
    connection.execute(
        """
        UPDATE cache_affinity_selections
        SET outcome_status='failed', outcome_at='2026-07-20T01:05:03Z'
        WHERE tenant_id=? AND cache_affinity_selection_id=?
        """,
        (seed.tenant_id, recovery_selection),
    )
    # 启动恢复的最终重算同样属于同目标维护，不能重新要求已删除的 Credential 存在。
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=0,
            pending_lease_expires_at='2026-07-20T01:05:03Z',
            updated_at='2026-07-20T01:05:03Z'
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (seed.tenant_id, binding_id),
    )
    connection.commit()
    binding = connection.execute(
        """
        SELECT active_selection_count, last_success_at, provider_credential_id
        FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (seed.tenant_id, binding_id),
    ).fetchone()
    assert tuple(binding) == (0, "2026-07-20T01:05:02Z", credential_id)
    assert [
        row["outcome_status"]
        for row in connection.execute(
            """
            SELECT outcome_status FROM cache_affinity_selections
            WHERE tenant_id=? ORDER BY cache_affinity_selection_id
            """,
            (seed.tenant_id,),
        )
    ] == ["succeeded", "failed"]

    # 旧目标维护被允许，但写入一个新的缺失 Credential 仍必须由 transition trigger 拒绝。
    with pytest.raises(sqlite3.IntegrityError, match="binding_transition_invalid"):
        connection.execute(
            """
            UPDATE cache_affinity_bindings
            SET provider_credential_id='pcred_prd050_missing', target_generation=3,
                pending_lease_generation=3,
                pending_lease_started_at='2026-07-20T01:05:04Z',
                pending_lease_expires_at='2026-07-20T01:05:34Z',
                last_success_at=NULL, updated_at='2026-07-20T01:05:04Z'
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (seed.tenant_id, binding_id),
        )
    connection.rollback()

    # 删除 Credential 不会让降级静默丢事实；0040 down 仍须在 DDL 前原子拒绝。
    before = _database_snapshot(connection)
    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd050_down_business_facts_present" in exc.value.message
    assert _database_snapshot(connection) == before
    connection.close()
