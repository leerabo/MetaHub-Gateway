"""PRD-056 0046 迁移、冻结 Fact、回滚保护与 Audit 保真专项测试。

所有用例只使用 pytest 独占 SQLite 文件和保留域占位数据。测试不会访问网络，也不会保存
Provider Key、Token、Prompt、Response、Endpoint 正文或原始 Provider 错误信息。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import metahub_shared.db.migrate as migration_module
import pytest
from metahub_shared.db import (
    apply_migrations as apply_current_migrations,
)
from metahub_shared.db import (
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 迁移版本列表冻结 fresh database 必须登记的完整顺序，缺号或乱序都应使专项测试失败。
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 47)]
"""空库必须按顺序登记的 46 个正式迁移版本。"""

# 历史专项仍真实执行后续迁移，再按栈顺序回退，避免复制旧目录掩盖向前兼容问题。
FROZEN_VERSION = "0046"
"""PRD-056 历史断言必须恢复到的迁移栈顶。"""

# 目录版本、冻结摘要、内置 429 pattern 与测试时间共同复现 0046 的不可变种子合同。
CATALOG_V1 = "provider-error-builtin-v1"
CATALOG_HASH_V1 = "a209e9d5e86e5b08d6fe97a1d0475ad8533704f6f782fa20dd732fc4dafdc507"
EMPTY_OVERLAY_HASH = "994a863c694e05021d65d0f8b862f24a28da461080286b7b35d1695ee0775fdc"
BUILTIN_429_PATTERN_HASH = "65202bb23bfe8a2a79cb6914c4bf2560252acc1f86e80b9458343aecd0d883ed"
NOW = "2026-07-29T12:00:00Z"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占文件转换为项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def apply_migrations(database_url: str) -> migration_module.MigrationResult:
    """执行当前完整迁移链，再回退后续版本，使 PRD-056 专项稳定停在 0046。

    参数：pytest 独占数据库 URL。返回：过滤掉所有高于 0046 的迁移结果。
    关键约束：后续 up/down 必须真实成功，不能用历史迁移副本绕过 checksum 与回滚守卫。
    """

    result = apply_current_migrations(database_url)
    # 运行时求出真实后续版本，既覆盖当前 0058，也自动覆盖未来加入的迁移。
    later_migrations = tuple(
        sorted(
            {
                version
                for version in (*result.applied, *result.skipped)
                if version > FROZEN_VERSION
            },
            reverse=True,
        )
    )
    for version in later_migrations:
        # SQLite 栈必须严格逆序回滚，任何错序都说明迁移历史已经漂移。
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    excluded = set(later_migrations)
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def _context(connection: sqlite3.Connection, tenant_id: str) -> dict[str, str]:
    """按 tenant-qualified JOIN 读取测试所需 admin、Provider 与 Model owner，禁止跨租户拼接夹具。"""

    row = connection.execute(
        """
        SELECT actor.user_id, actor.email, provider.provider_id,
               provider.provider_code, model.model_id
        FROM users actor
        JOIN providers provider ON provider.tenant_id=actor.tenant_id
        JOIN models model
          ON model.tenant_id=provider.tenant_id AND model.provider_id=provider.provider_id
        WHERE actor.tenant_id=? AND actor.role='admin'
        ORDER BY actor.user_id, provider.provider_id, model.model_id LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_legacy_audit(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
) -> tuple[object, ...]:
    """插入 0045 已支持的 Provider Audit，并返回含 rowid 的完整冻结行。"""

    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
          action, object_type, object_id, object_name, before_summary_json,
          after_summary_json, changed_fields_json, client_ip, user_agent,
          request_id, trace_id, created_at
        ) VALUES(
          'aud_prd056_legacy_fidelity', ?, ?, ?, 'admin',
          'provider.updated', 'provider', ?, NULL, '{}',
          '{"status":"active"}', '["status"]', NULL, 'prd056-test-agent',
          'req_prd056_legacy_audit', 'trc_prd056_legacy_audit', ?
        )
        """,
        (
            tenant_id,
            context["user_id"],
            context["email"],
            context["provider_id"],
            NOW,
        ),
    )
    row = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd056_legacy_fidelity'"
    ).fetchone()
    assert row is not None
    return tuple(row)


def _insert_rule_head(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
) -> tuple[str, str, str]:
    """按正式事务顺序写入一条 wildcard rule、不可变版本和 revision-1 head。"""

    error_map_id = "pem_" + "1" * 32
    version_id = "pemv_" + "2" * 32
    head_version_id = "pemhv_" + "3" * 32
    overlay_hash = "4" * 64
    connection.execute(
        """
        INSERT INTO provider_error_map_versions(
          provider_error_map_version_id, tenant_id, provider_error_map_id, version,
          provider_id, priority, http_statuses_json, http_status_class, error_field,
          field_operator, field_pattern, message_operator, message_pattern,
          output_error_code, status, pattern_hash, condition_hash, head_revision,
          created_by, updated_by, created_at
        ) VALUES(?, ?, ?, 1, NULL, 100, '[404]', NULL, NULL, NULL, NULL, NULL, NULL,
                 'gateway.upstream_resource_not_found', 'active', ?, ?, 1, ?, ?, ?)
        """,
        (
            version_id,
            tenant_id,
            error_map_id,
            "5" * 64,
            "6" * 64,
            context["user_id"],
            context["user_id"],
            NOW,
        ),
    )
    connection.execute(
        "INSERT INTO provider_error_maps VALUES(?,?,?,?,?,?,?)",
        (
            error_map_id,
            tenant_id,
            1,
            context["user_id"],
            context["user_id"],
            NOW,
            NOW,
        ),
    )
    refs_json = json.dumps(
        [{"error_map_id": error_map_id, "version": 1}],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO provider_error_map_head_versions VALUES(?,?,?,?,?,?,?)",
        (head_version_id, tenant_id, 1, overlay_hash, CATALOG_V1, refs_json, NOW),
    )
    connection.execute(
        "INSERT INTO provider_error_map_heads VALUES(?,?,?,?)",
        (tenant_id, 1, overlay_hash, NOW),
    )
    return error_map_id, version_id, head_version_id


def _advance_rule_head(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    error_map_id: str,
) -> None:
    """把当前规则和 head 原子推进到 revision 2，同时保留 revision 1 不可变历史。"""

    updated_at = "2026-07-29T12:00:03Z"
    version_id = "pemv_" + "a" * 32
    head_version_id = "pemhv_" + "b" * 32
    overlay_hash = "c" * 64
    connection.execute(
        """
        INSERT INTO provider_error_map_versions(
          provider_error_map_version_id, tenant_id, provider_error_map_id, version,
          provider_id, priority, http_statuses_json, http_status_class, error_field,
          field_operator, field_pattern, message_operator, message_pattern,
          output_error_code, status, pattern_hash, condition_hash, head_revision,
          created_by, updated_by, created_at
        ) VALUES(?, ?, ?, 2, NULL, 101, '[404]', NULL, NULL, NULL, NULL, NULL, NULL,
                 'gateway.upstream_resource_not_found', 'active', ?, ?, 2, ?, ?, ?)
        """,
        (
            version_id,
            tenant_id,
            error_map_id,
            "7" * 64,
            "8" * 64,
            context["user_id"],
            context["user_id"],
            updated_at,
        ),
    )
    changed = connection.execute(
        """
        UPDATE provider_error_maps
        SET current_version=2, updated_by=?, updated_at=?
        WHERE tenant_id=? AND provider_error_map_id=?
        """,
        (context["user_id"], updated_at, tenant_id, error_map_id),
    )
    assert changed.rowcount == 1
    refs_json = json.dumps(
        [{"error_map_id": error_map_id, "version": 2}],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO provider_error_map_head_versions VALUES(?,?,?,?,?,?,?)",
        (
            head_version_id,
            tenant_id,
            2,
            overlay_hash,
            CATALOG_V1,
            refs_json,
            updated_at,
        ),
    )
    changed = connection.execute(
        """
        UPDATE provider_error_map_heads
        SET revision=2, content_hash=?, updated_at=?
        WHERE tenant_id=?
        """,
        (overlay_hash, updated_at, tenant_id),
    )
    assert changed.rowcount == 1


def _projection() -> tuple[str, str]:
    """生成 exact-schema 脱敏空正文投影及 canonical SHA-256，验证 Fact 不保存原始 Provider 正文。"""

    value = json.dumps(
        {
            "body_length_bucket": "0",
            "body_truncated": False,
            "is_json": False,
            "message": None,
            "message_present": False,
            "provider_error_code_class": None,
            "provider_error_code_present": False,
            "provider_error_type_class": None,
            "provider_error_type_present": False,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return value, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_attempt_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
) -> tuple[str, str]:
    """写入 429 attempt owner 与 revision-0 builtin Fact，返回安全 ID 并验证二者同租户归属。"""

    attempt_id = "rta_prd056_migration_fact"
    request_id = "req_prd056_migration_fact"
    trace_id = "trc_prd056_migration_fact"
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, attempt_sequence,
          attempt_role, trigger_type, model_id, internal_model_id, provider_id,
          provider_code, status, error_code, error_category, upstream_http_status,
          retryable, usage_status, created_at, completed_at, updated_at
        ) VALUES(?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?, 'failed',
                 'gateway.upstream_rate_limited', 'rate_limited', 429, 1,
                 'not_applicable', ?, ?, ?)
        """,
        (
            attempt_id,
            tenant_id,
            request_id,
            trace_id,
            context["model_id"],
            context["model_id"],
            context["provider_id"],
            context["provider_code"],
            NOW,
            NOW,
            NOW,
        ),
    )
    projection_json, projection_hash = _projection()
    fact_id = "pef_" + "9" * 32
    columns = (
        "provider_error_fact_id,tenant_id,request_id,trace_id,routing_attempt_id,"
        "stream_session_id,provider_id,model_id,provider_code,upstream_http_status,"
        "transport_kind,provider_error_code_class,provider_error_type_class,"
        "provider_error_code_present,provider_error_type_present,message_present,"
        "redacted_projection_json,projection_hash,body_length_bucket,body_truncated,is_json,"
        "matched_error_map_id,matched_rule_version,matched_pattern_hash,matched_rule_source,"
        "error_map_revision,overlay_content_hash,builtin_catalog_version,"
        "error_map_snapshot_source,match_basis,normalized_output_error_code,"
        "owner_terminal_error_code,created_at"
    )
    values = (
        fact_id,
        tenant_id,
        request_id,
        trace_id,
        attempt_id,
        None,
        context["provider_id"],
        context["model_id"],
        context["provider_code"],
        429,
        "none",
        None,
        None,
        0,
        0,
        0,
        projection_json,
        projection_hash,
        "0",
        0,
        0,
        "builtin_http_429",
        None,
        BUILTIN_429_PATTERN_HASH,
        "builtin",
        0,
        EMPTY_OVERLAY_HASH,
        CATALOG_V1,
        "builtin_fallback",
        "http_status",
        "gateway.upstream_rate_limited",
        "gateway.upstream_rate_limited",
        NOW,
    )
    connection.execute(
        f"INSERT INTO provider_error_facts({columns}) VALUES({','.join('?' for _ in values)})",
        values,
    )
    return attempt_id, fact_id


def _insert_historical_tenant_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    error_map_id: str,
) -> str:
    """在 current head=2 后写入仍引用 revision-1 tenant rule 的迟到失败事实。"""

    attempt_id = "rta_prd056_historical_fact"
    request_id = "req_prd056_historical_fact"
    trace_id = "trc_prd056_historical_fact"
    connection.execute(
        """
        INSERT INTO routing_attempts(
          routing_attempt_id, tenant_id, request_id, trace_id, attempt_sequence,
          attempt_role, trigger_type, model_id, internal_model_id, provider_id,
          provider_code, status, error_code, error_category, upstream_http_status,
          retryable, usage_status, created_at, completed_at, updated_at
        ) VALUES(?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?, 'failed',
                 'gateway.upstream_resource_not_found', 'other', 404, 0,
                 'not_applicable', ?, ?, ?)
        """,
        (
            attempt_id,
            tenant_id,
            request_id,
            trace_id,
            context["model_id"],
            context["model_id"],
            context["provider_id"],
            context["provider_code"],
            NOW,
            NOW,
            NOW,
        ),
    )
    projection_json, projection_hash = _projection()
    fact_id = "pef_" + "8" * 32
    columns = (
        "provider_error_fact_id,tenant_id,request_id,trace_id,routing_attempt_id,"
        "stream_session_id,provider_id,model_id,provider_code,upstream_http_status,"
        "transport_kind,provider_error_code_class,provider_error_type_class,"
        "provider_error_code_present,provider_error_type_present,message_present,"
        "redacted_projection_json,projection_hash,body_length_bucket,body_truncated,is_json,"
        "matched_error_map_id,matched_rule_version,matched_pattern_hash,matched_rule_source,"
        "error_map_revision,overlay_content_hash,builtin_catalog_version,"
        "error_map_snapshot_source,match_basis,normalized_output_error_code,"
        "owner_terminal_error_code,created_at"
    )
    values = (
        fact_id,
        tenant_id,
        request_id,
        trace_id,
        attempt_id,
        None,
        context["provider_id"],
        context["model_id"],
        context["provider_code"],
        404,
        "none",
        None,
        None,
        0,
        0,
        0,
        projection_json,
        projection_hash,
        "0",
        0,
        0,
        error_map_id,
        1,
        "5" * 64,
        "tenant",
        1,
        "4" * 64,
        CATALOG_V1,
        "lkg",
        "http_status",
        "gateway.upstream_resource_not_found",
        "gateway.upstream_resource_not_found",
        NOW,
    )
    connection.execute(
        f"INSERT INTO provider_error_facts({columns}) VALUES({','.join('?' for _ in values)})",
        values,
    )
    return fact_id


def _prepare_seeded_database(
    tmp_path: Path, name: str
) -> tuple[str, sqlite3.Connection, str, dict[str, str]]:
    """应用 0001-0046、seed，并返回独占连接和同租户 owner 上下文。"""

    database_url = _sqlite_url(tmp_path / name)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    # 返回值中的 tenant 和 owner 均来自同一 seed 查询，后续 Fact 不得混用其他租户标识。
    return database_url, connection, seed.tenant_id, _context(connection, seed.tenant_id)


def _assert_database_healthy(connection: sqlite3.Connection) -> None:
    """验证 SQLite 结构与外键健康，失败时不输出业务正文。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prd056_empty_repeat_audit_fidelity_and_roundtrip(tmp_path: Path) -> None:
    """空库 0001-0046、重复 apply、旧 Audit 保真以及 0046 down/up 全部可运行。"""

    database_url = _sqlite_url(tmp_path / "prd056-empty-roundtrip.db")
    first = apply_migrations(database_url)
    second = apply_migrations(database_url)
    assert first.applied == EXPECTED_VERSIONS
    assert second.applied == []
    assert second.skipped == EXPECTED_VERSIONS

    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    context = _context(connection, seed.tenant_id)
    audit_before = _insert_legacy_audit(connection, seed.tenant_id, context)
    connection.commit()
    assert (
        connection.execute("SELECT COUNT(*) FROM provider_error_builtin_rule_versions").fetchone()[
            0
        ]
        == 13
    )
    assert (
        connection.execute(
            "SELECT catalog_hash FROM provider_error_builtin_catalog_versions "
            "WHERE builtin_catalog_version=?",
            (CATALOG_V1,),
        ).fetchone()[0]
        == CATALOG_HASH_V1
    )
    _assert_database_healthy(connection)
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0046"
    connection = connect_database(database_url)
    assert (
        connection.execute("SELECT status FROM schema_migrations WHERE version='0046'").fetchone()[
            0
        ]
        == "rolled_back"
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='provider_error_facts'"
        ).fetchone()[0]
        == 0
    )
    stream_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(stream_sessions)")
    }
    assert "upstream_http_status" not in stream_columns
    assert "retryable" not in stream_columns
    audit_after_down = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd056_legacy_fidelity'"
    ).fetchone()
    assert audit_after_down is not None and tuple(audit_after_down) == audit_before
    _assert_database_healthy(connection)
    connection.close()

    assert apply_migrations(database_url).applied == ["0046"]
    connection = connect_database(database_url)
    audit_after_up = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd056_legacy_fidelity'"
    ).fetchone()
    assert audit_after_up is not None and tuple(audit_after_up) == audit_before
    _assert_database_healthy(connection)
    connection.close()


def test_prd056_fact_owner_immutability_and_late_revision_catalog(tmp_path: Path) -> None:
    """Fact 与 owner 双向不可变。

    当前 head 与后续 catalog 不得否定迟到的 revision-0 或历史非零 revision 事实。
    """

    _, connection, tenant_id, context = _prepare_seeded_database(tmp_path, "prd056-fact-owner.db")
    error_map_id, _, _ = _insert_rule_head(connection, tenant_id, context)
    _advance_rule_head(connection, tenant_id, context, error_map_id)
    # 滚动升级先登记新目录；旧请求仍必须能引用请求开始时冻结的 v1 与历史 head。
    connection.execute(
        "INSERT INTO provider_error_builtin_catalog_versions VALUES(?,?,?)",
        ("provider-error-builtin-v2", "c" * 64, "2026-07-29T12:00:01Z"),
    )
    attempt_id, fact_id = _insert_attempt_fact(connection, tenant_id, context)
    historical_fact_id = _insert_historical_tenant_fact(
        connection, tenant_id, context, error_map_id
    )
    connection.commit()
    row = connection.execute(
        "SELECT error_map_revision,builtin_catalog_version,error_map_snapshot_source "
        "FROM provider_error_facts WHERE provider_error_fact_id=?",
        (fact_id,),
    ).fetchone()
    assert tuple(row) == (0, CATALOG_V1, "builtin_fallback")
    historical = connection.execute(
        "SELECT error_map_revision,matched_error_map_id,matched_rule_version,"
        "builtin_catalog_version,error_map_snapshot_source "
        "FROM provider_error_facts WHERE provider_error_fact_id=?",
        (historical_fact_id,),
    ).fetchone()
    assert tuple(historical) == (1, error_map_id, 1, CATALOG_V1, "lkg")
    assert (
        connection.execute(
            "SELECT revision FROM provider_error_map_heads WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        == 2
    )

    # 全同值 UPDATE 可供旧代码使用；任一业务列漂移、删除和 REPLACE 都必须失败关闭。
    connection.execute(
        "UPDATE routing_attempts SET updated_at=updated_at WHERE routing_attempt_id=?",
        (attempt_id,),
    )
    with pytest.raises(
        sqlite3.IntegrityError, match="routing_attempt_provider_error_fact_immutable"
    ):
        connection.execute(
            "UPDATE routing_attempts SET latency_ms=1 WHERE routing_attempt_id=?",
            (attempt_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="provider_error_fact_immutable"):
        connection.execute(
            "UPDATE provider_error_facts SET created_at=created_at WHERE provider_error_fact_id=?",
            (fact_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="provider_error_fact_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO provider_error_facts "
            "SELECT * FROM provider_error_facts WHERE provider_error_fact_id=?",
            (fact_id,),
        )
    _assert_database_healthy(connection)
    connection.close()


@pytest.mark.parametrize("dependency_kind", ["fact", "catalog", "webhook"])
def test_prd056_down_guard_preserves_business_dependencies_atomically(
    tmp_path: Path,
    dependency_kind: str,
) -> None:
    """Fact、后续 builtin 目录或订阅 Endpoint 存在时，0046 down 原子拒绝并保留证据。"""

    database_url, connection, tenant_id, context = _prepare_seeded_database(
        tmp_path, f"prd056-down-{dependency_kind}.db"
    )
    if dependency_kind == "fact":
        _, evidence_id = _insert_attempt_fact(connection, tenant_id, context)
        evidence_table = "provider_error_facts"
        evidence_column = "provider_error_fact_id"
    elif dependency_kind == "catalog":
        evidence_id = "provider-error-builtin-v2"
        connection.execute(
            "INSERT INTO provider_error_builtin_catalog_versions VALUES(?,?,?)",
            (evidence_id, "d" * 64, "2026-07-29T12:00:02Z"),
        )
        evidence_table = "provider_error_builtin_catalog_versions"
        evidence_column = "builtin_catalog_version"
    else:
        evidence_id = "wh_prd056_error_map"
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
              encrypted_secret, secret_prefix, secret_fingerprint, status,
              created_by, updated_by, created_at, updated_at
            ) VALUES(?, ?, 'PRD056', 'https://example.invalid/webhook',
                     '["error_map.updated"]', 'ciphertext-test-only', 'whsec_test',
                     'fingerprint-prd056', 'active', ?, ?, ?, ?)
            """,
            (
                evidence_id,
                tenant_id,
                context["user_id"],
                context["user_id"],
                NOW,
                NOW,
            ),
        )
        evidence_table = "webhook_endpoints"
        evidence_column = "webhook_endpoint_id"
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(database_url)
    assert error.value.code == "db.migration_failed"
    assert "prd056_down_dependency_exists" in error.value.message
    connection = connect_database(database_url)
    assert (
        connection.execute("SELECT status FROM schema_migrations WHERE version='0046'").fetchone()[
            0
        ]
        == "applied"
    )
    # 表名和列名只来自上方固定分支，不接收外部输入；ID 始终使用绑定参数。
    assert (
        connection.execute(
            f"SELECT COUNT(*) FROM {evidence_table} WHERE {evidence_column}=?",
            (evidence_id,),
        ).fetchone()[0]
        == 1
    )
    _assert_database_healthy(connection)
    connection.close()


@pytest.mark.parametrize(
    ("dependency_name", "dependency_sql"),
    [
        (
            "later_prd056_fact_view",
            "CREATE VIEW later_prd056_fact_view AS SELECT provider_error_fact_id "
            "FROM provider_error_facts",
        ),
        (
            "later_prd056_catalog_view",
            "CREATE VIEW later_prd056_catalog_view AS SELECT builtin_catalog_version "
            "FROM provider_error_builtin_catalog_versions",
        ),
        (
            "later_prd056_stream_view",
            "CREATE VIEW later_prd056_stream_view AS SELECT upstream_http_status,retryable "
            "FROM stream_sessions",
        ),
    ],
)
def test_prd056_down_guard_rejects_unknown_later_schema_dependencies(
    tmp_path: Path,
    dependency_name: str,
    dependency_sql: str,
) -> None:
    """未知后续对象仅依赖 Fact、builtin 或 stream 新列时也必须阻止破坏性 down。"""

    database_url = _sqlite_url(tmp_path / f"{dependency_name}.db")
    apply_migrations(database_url)
    connection = connect_database(database_url)
    # SQL 仅来自上方静态参数表，用于模拟尚未进入当前仓库的后续迁移对象。
    connection.execute(dependency_sql)
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(database_url)
    assert error.value.code == "db.migration_failed"
    assert "prd056_down_dependency_exists" in error.value.message
    connection = connect_database(database_url)
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name=?", (dependency_name,)
        ).fetchone()[0]
        == 1
    )
    assert (
        connection.execute("SELECT status FROM schema_migrations WHERE version='0046'").fetchone()[
            0
        ]
        == "applied"
    )
    _assert_database_healthy(connection)
    connection.close()
