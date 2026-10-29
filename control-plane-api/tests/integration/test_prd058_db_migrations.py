"""PRD-058 0049 审计导出、稳定序列、source lease 与回滚保护专项测试。

所有用例只写 pytest 独占 SQLite 文件，使用安全 ID、固定哈希和 ``example.invalid``；
不保存 CSV 正文、Provider Key、Token、Prompt、Response、Secret 或真实 Endpoint。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import metahub_shared.db.migrate as migration_module
import pytest
from metahub_shared.db import (
    apply_migrations as apply_current_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

# 版本清单冻结 fresh database 连续升级边界，缺号或乱序都必须使专项失败。
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 50)]
"""空库必须连续登记到 0049，防止部署时跳过前序治理迁移。"""

# PRD-058 专项冻结在 0049；必须先真实执行全部后续迁移再回退，才能覆盖向前兼容。
FROZEN_VERSION = "0049"
"""PRD-058 历史断言必须恢复到的迁移栈顶。"""

# 固定时间同时用于不可变事实与 source lease，测试不得读取机器当前时钟。
NOW = "2026-07-31T10:00:00Z"
"""固定 UTC 秒级时间，确保 lease、排序和回滚断言不依赖测试机器时钟。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换成项目迁移器支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def apply_migrations(database_url: str) -> migration_module.MigrationResult:
    """执行当前完整迁移链，再回退全部后续版本以冻结 PRD-058 的 0049 栈顶。

    参数：pytest 独占数据库 URL。返回：只包含截至 0049 的迁移结果。
    关键约束：所有后续版本必须真实完成 up/down，不能复制旧目录绕过迁移守卫。
    """

    result = apply_current_migrations(database_url)
    # 从真实执行结果推导当前栈顶，避免新增版本后仍错误地从 0050 开始断言。
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
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    excluded = set(later_migrations)
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def _utc_after(raw: str, seconds: int) -> str:
    """基于固定 UTC 文本增加秒数，生成状态机要求的精确 lease/退避时间。"""

    value = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    return (value + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _admin(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    """按 tenant-qualified 条件读取管理员，夹具自身不能掩盖跨租户 owner 缺陷。"""

    row = connection.execute(
        "SELECT user_id,email,role FROM users WHERE tenant_id=? AND role='admin' "
        "ORDER BY user_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return row


def _insert_plain_audit(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor: sqlite3.Row,
    audit_log_id: str,
    created_at: str = NOW,
) -> tuple[object, ...]:
    """写入不依赖具体业务表的 Provider 更新审计，并返回含 rowid 的保真投影。"""

    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            audit_log_id,
            tenant_id,
            actor["user_id"],
            actor["email"],
            actor["role"],
            "provider.updated",
            "provider",
            "provider_prd058_placeholder",
            None,
            '{"status":"inactive"}',
            '{"status":"active"}',
            '["status"]',
            None,
            "prd058-migration-test",
            f"req_{audit_log_id}",
            f"trc_{audit_log_id}",
            created_at,
        ),
    )
    row = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id=?", (audit_log_id,)
    ).fetchone()
    assert row is not None
    return tuple(row)


def _export_values(
    *,
    tenant_id: str,
    actor_id: str,
    suffix: str,
    snapshot_sequence: int = 0,
    snapshot_audit_log_id: str | None = None,
    snapshot_created_at: str | None = None,
) -> dict[str, object]:
    """构造一组满足固定 ID、摘要、幂等键和事件 payload 合同的安全导出字段。"""

    export_id = f"aex_{int(suffix, 16):032x}"
    audit_id = f"aud_prd058_export_{suffix}"
    event_id = f"aexevt_{int(suffix, 16):032x}"
    content_sha = hashlib.sha256(f"csv-{suffix}".encode()).hexdigest()
    filters_json = "{}"
    fingerprint = hashlib.sha256(filters_json.encode()).hexdigest()
    payload = {
        "audit_export_id": export_id,
        "content_sha256": content_sha,
        "content_size_bytes": 128,
        "filter_fingerprint": fingerprint,
        "format": "csv",
        "row_count": 0,
        "schema_version": 1,
        "status": "completed",
    }
    return {
        "audit_export_id": export_id,
        "tenant_id": tenant_id,
        "requested_by": actor_id,
        "audit_log_id": audit_id,
        "status": "completed",
        "format": "csv",
        "schema_version": 1,
        "filters_json": filters_json,
        "filter_fingerprint": fingerprint,
        "snapshot_sequence": snapshot_sequence,
        "snapshot_audit_log_id": snapshot_audit_log_id,
        "snapshot_created_at": snapshot_created_at,
        "row_count": 0,
        "content_size_bytes": 128,
        "content_sha256": content_sha,
        "file_name": f"metahub-audit-{suffix}.csv",
        "request_id": f"req_prd058_export_{suffix}",
        "trace_id": f"trc_prd058_export_{suffix}",
        "event_id": event_id,
        "event_payload_json": json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }


def _insert_export_graph(
    connection: sqlite3.Connection,
    *,
    actor: sqlite3.Row,
    values: dict[str, object],
) -> None:
    """按冻结事务顺序写导出、待派发 source event 与指定 ID 审计日志。

    参数：数据库连接、同租户管理员和完整导出字段；返回：无返回值。
    约束：三个事实必须处于调用方同一事务，任一 owner 或延迟外键失败都由调用方回滚。
    """

    columns = tuple(values)
    connection.execute(
        f"INSERT INTO audit_exports({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    connection.execute(
        """
        INSERT INTO audit_export_events(
          audit_export_event_id,tenant_id,audit_export_id,status,attempt_count,
          next_attempt_at,claim_token,claim_owner,lease_until,last_error_code,
          dispatched_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
        """,
        (
            values["event_id"],
            values["tenant_id"],
            values["audit_export_id"],
            values["created_at"],
            values["created_at"],
            values["created_at"],
        ),
    )
    summary = {
        "content_sha256": values["content_sha256"],
        "content_size_bytes": values["content_size_bytes"],
        "filter_fingerprint": values["filter_fingerprint"],
        "format": values["format"],
        "row_count": values["row_count"],
        "schema_version": values["schema_version"],
        "status": values["status"],
    }
    changed_fields = sorted(summary)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
          object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
          client_ip,user_agent,request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            values["audit_log_id"],
            values["tenant_id"],
            values["requested_by"],
            actor["email"],
            actor["role"],
            "audit.exported",
            "audit_export",
            values["audit_export_id"],
            None,
            "{}",
            json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            json.dumps(changed_fields, ensure_ascii=True, separators=(",", ":")),
            None,
            "prd058-migration-test",
            values["request_id"],
            values["trace_id"],
            values["created_at"],
        ),
    )


def _assert_healthy(connection: sqlite3.Connection) -> None:
    """验证迁移后 SQLite 结构和复合外键均无损坏。"""

    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_prd058_history_backfill_autoincrement_and_empty_roundtrip(tmp_path: Path) -> None:
    """0048 历史行保真升级、稳定序列回填及无导出事实 down/up 可完整往返。"""

    database_url = _sqlite_url(tmp_path / "prd058-history-roundtrip.db")
    first = apply_migrations(database_url)
    assert first.applied == EXPECTED_VERSIONS
    assert rollback_last_migration(database_url).rolled_back == "0049"

    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    legacy = _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd058_legacy",
    )
    connection.commit()
    connection.close()

    assert apply_migrations(database_url).applied == ["0049"]
    connection = connect_database(database_url)
    copied = connection.execute(
        "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd058_legacy'"
    ).fetchone()
    assert copied is not None and tuple(copied) == legacy
    sequence = connection.execute(
        "SELECT audit_log_sequence FROM audit_log_export_sequences "
        "WHERE tenant_id=? AND audit_log_id='aud_prd058_legacy'",
        (seed.tenant_id,),
    ).fetchone()
    assert sequence is not None and sequence[0] > 0

    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd058_after_upgrade",
        created_at=_utc_after(NOW, 1),
    )
    later_sequence = connection.execute(
        "SELECT audit_log_sequence FROM audit_log_export_sequences "
        "WHERE audit_log_id='aud_prd058_after_upgrade'"
    ).fetchone()[0]
    assert later_sequence > sequence[0]
    connection.commit()
    _assert_healthy(connection)
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0049"
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE name='audit_exports'"
    ).fetchone() is None
    assert tuple(
        connection.execute(
            "SELECT rowid,* FROM audit_logs WHERE audit_log_id='aud_prd058_legacy'"
        ).fetchone()
    ) == legacy
    _assert_healthy(connection)
    connection.close()
    assert apply_migrations(database_url).applied == ["0049"]


def test_prd058_export_owner_immutability_and_source_lease_cas(tmp_path: Path) -> None:
    """导出图一一绑定且不可改写，source outbox 只允许有效 lease 的 CAS 状态转换。"""

    database_url = _sqlite_url(tmp_path / "prd058-export-owner-lease.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    values = _export_values(tenant_id=seed.tenant_id, actor_id=actor["user_id"], suffix="1")
    _insert_export_graph(connection, actor=actor, values=values)
    connection.commit()

    mapping = connection.execute(
        "SELECT audit_log_sequence,tenant_id FROM audit_log_export_sequences "
        "WHERE audit_log_id=?",
        (values["audit_log_id"],),
    ).fetchone()
    assert mapping is not None and mapping["tenant_id"] == seed.tenant_id
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            "UPDATE audit_exports SET row_count=row_count WHERE audit_export_id=?",
            (values["audit_export_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            "INSERT OR REPLACE INTO audit_exports SELECT * FROM audit_exports "
            "WHERE audit_export_id=?",
            (values["audit_export_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_export_sequence_immutable"):
        connection.execute(
            "DELETE FROM audit_log_export_sequences WHERE audit_log_id=?",
            (values["audit_log_id"],),
        )
    connection.rollback()

    claimed_at = NOW
    lease_until = _utc_after(claimed_at, 30)
    changed = connection.execute(
        """
        UPDATE audit_export_events
        SET status='processing',attempt_count=attempt_count+1,next_attempt_at=NULL,
            claim_token=?,claim_owner=?,lease_until=?,last_error_code=NULL,updated_at=?
        WHERE audit_export_event_id=? AND status='pending' AND next_attempt_at<=?
        """,
        (
            "a" * 32,
            "worker-prd058-a",
            lease_until,
            claimed_at,
            values["event_id"],
            claimed_at,
        ),
    )
    assert changed.rowcount == 1
    # 旧 token 的 CAS 必须零命中，不能覆盖当前 lease owner。
    stale = connection.execute(
        """
        UPDATE audit_export_events SET updated_at=?
        WHERE audit_export_event_id=? AND claim_token=? AND lease_until>?
        """,
        (_utc_after(claimed_at, 1), values["event_id"], "b" * 32, claimed_at),
    )
    assert stale.rowcount == 0
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_event_state_invalid"):
        connection.execute(
            """
            UPDATE audit_export_events
            SET status='dispatched',claim_token=NULL,claim_owner=NULL,lease_until=NULL,
                dispatched_at=?,updated_at=?
            WHERE audit_export_event_id=?
            """,
            (
                _utc_after(lease_until, 1),
                _utc_after(lease_until, 1),
                values["event_id"],
            ),
        )
    connection.rollback()

    # rollback 恢复 pending；重新 claim 后在有效 lease 内按固定 5 秒退避。
    connection.execute(
        """
        UPDATE audit_export_events
        SET status='processing',attempt_count=1,next_attempt_at=NULL,
            claim_token=?,claim_owner=?,lease_until=?,last_error_code=NULL,updated_at=?
        WHERE audit_export_event_id=?
        """,
        ("c" * 32, "worker-prd058-c", lease_until, claimed_at, values["event_id"]),
    )
    retry_at = _utc_after(claimed_at, 1)
    connection.execute(
        """
        UPDATE audit_export_events
        SET status='pending',next_attempt_at=?,claim_token=NULL,claim_owner=NULL,
            lease_until=NULL,last_error_code='webhook.delivery_unavailable',updated_at=?
        WHERE audit_export_event_id=? AND claim_token=? AND lease_until>?
        """,
        (
            _utc_after(retry_at, 5),
            retry_at,
            values["event_id"],
            "c" * 32,
            retry_at,
        ),
    )
    state = connection.execute(
        "SELECT status,attempt_count,last_error_code FROM audit_export_events "
        "WHERE audit_export_event_id=?",
        (values["event_id"],),
    ).fetchone()
    assert tuple(state) == ("pending", 1, "webhook.delivery_unavailable")
    connection.commit()
    _assert_healthy(connection)
    connection.close()


def test_prd058_cross_tenant_fk_conflict_and_negative_rowid_close(tmp_path: Path) -> None:
    """跨租户 owner/FK、保留 ID 冲突和显式负 rowid 都必须在提交前失败关闭。"""

    database_url = _sqlite_url(tmp_path / "prd058-cross-tenant.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    connection.execute(
        "INSERT INTO tenants VALUES(?,?,?,?,?,?,?)",
        ("tenant_prd058_other", "prd058-other", "Other", "internal_only", "active", NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            "usr_prd058_other",
            "tenant_prd058_other",
            "other-prd058@example.invalid",
            "Other Admin",
            "test-hash",
            "admin",
            "active",
            NOW,
            NOW,
        ),
    )
    other = _admin(connection, "tenant_prd058_other")
    # owner 图是后续多个独立失败事务的共同前置条件，先提交避免一次预期 rollback
    # 连带撤销第二租户，掩盖负 rowid 真正应命中的不可变门禁。
    connection.commit()

    forged = _export_values(
        tenant_id="tenant_prd058_other", actor_id=actor["user_id"], suffix="2"
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_owner_mismatch"):
        columns = tuple(forged)
        connection.execute(
            f"INSERT INTO audit_exports({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            tuple(forged[column] for column in columns),
        )
    connection.rollback()

    valid = _export_values(
        tenant_id="tenant_prd058_other", actor_id=other["user_id"], suffix="3"
    )
    columns = tuple(valid)
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_immutable"):
        connection.execute(
            f"INSERT INTO audit_exports(rowid,{','.join(columns)}) "
            f"VALUES(-1,{','.join('?' for _ in columns)})",
            tuple(valid[column] for column in columns),
        )
    connection.rollback()

    # 先只写延迟 FK 的 Export，模拟同一事务中 AuditLog ID 已预留但自身审计尚未插入；
    # 另一租户不能用普通 action 抢占该 ID 来伪造满足 commit 时的引用。
    connection.execute(
        f"INSERT INTO audit_exports({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(valid[column] for column in columns),
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit_export_event_immutable"):
        connection.execute(
            """
            INSERT INTO audit_export_events(
              rowid,audit_export_event_id,tenant_id,audit_export_id,status,attempt_count,
              next_attempt_at,created_at,updated_at
            ) VALUES(-1,?,?,?,'pending',0,?,?,?)
            """,
            (
                valid["event_id"],
                valid["tenant_id"],
                valid["audit_export_id"],
                NOW,
                NOW,
                NOW,
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
              object_id,before_summary_json,after_summary_json,changed_fields_json,
              request_id,trace_id,created_at
            ) VALUES(?,?,?,?,?,'provider.updated','provider',?,'{}','{}','[]',?,?,?)
            """,
            (
                valid["audit_log_id"],
                seed.tenant_id,
                actor["user_id"],
                actor["email"],
                actor["role"],
                "provider_cross_tenant",
                "req_cross_tenant",
                "trc_cross_tenant",
                NOW,
            ),
        )
    connection.rollback()

    # 显式负 rowid 的 AuditLog 与稳定序列都必须失败；临时移除自动映射 trigger 只在
    # 当前未提交测试事务内制造“合法 AuditLog 尚未映射”的损坏前状态，rollback 会完整恢复 DDL。
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_immutable"):
        connection.execute(
            """
            INSERT INTO audit_logs(
              rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
              object_id,before_summary_json,after_summary_json,changed_fields_json,
              request_id,trace_id,created_at
            ) VALUES(-1,?,?,?,?,?,'provider.updated','provider',?,'{}','{}','[]',?,?,?)
            """,
            (
                "aud_prd058_negative",
                seed.tenant_id,
                actor["user_id"],
                actor["email"],
                actor["role"],
                "provider_negative",
                "req_negative_audit",
                "trc_negative_audit",
                NOW,
            ),
        )
    connection.rollback()

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("DROP TRIGGER trg_audit_logs_export_sequence_insert_prd058")
    _insert_plain_audit(
        connection,
        tenant_id=seed.tenant_id,
        actor=actor,
        audit_log_id="aud_prd058_unmapped_probe",
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_export_sequence_immutable"):
        connection.execute(
            """
            INSERT INTO audit_log_export_sequences(rowid,tenant_id,audit_log_id)
            VALUES(-1,?,?)
            """,
            (seed.tenant_id, "aud_prd058_unmapped_probe"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit_log_export_sequence_immutable"):
        connection.execute(
            """
            INSERT INTO audit_log_export_sequences(rowid,tenant_id,audit_log_id)
            VALUES(0,?,?)
            """,
            (seed.tenant_id, "aud_prd058_unmapped_probe"),
        )
    connection.rollback()
    assert connection.execute(
        "SELECT 1 FROM sqlite_schema "
        "WHERE type='trigger' AND name='trg_audit_logs_export_sequence_insert_prd058'"
    ).fetchone() is not None

    _insert_export_graph(connection, actor=other, values=valid)
    connection.commit()
    _assert_healthy(connection)
    connection.close()


@pytest.mark.parametrize("dependency_kind", ["export", "subscription", "unknown_view"])
def test_prd058_down_guard_preserves_facts_and_unknown_dependencies(
    tmp_path: Path,
    dependency_kind: str,
) -> None:
    """导出事实、audit.exported 订阅或未知后续依赖均使 0049 down 原子拒绝。"""

    database_url = _sqlite_url(tmp_path / f"prd058-down-{dependency_kind}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    actor = _admin(connection, seed.tenant_id)
    if dependency_kind == "export":
        values = _export_values(
            tenant_id=seed.tenant_id, actor_id=actor["user_id"], suffix="4"
        )
        _insert_export_graph(connection, actor=actor, values=values)
        evidence_name = str(values["audit_export_id"])
    elif dependency_kind == "subscription":
        evidence_name = "wh_prd058_subscription"
        connection.execute(
            """
            INSERT INTO webhook_endpoints(
              webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
              encrypted_secret,secret_prefix,secret_fingerprint,status,
              created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,'https://example.invalid/prd058','["audit.exported"]',
                     'test-ciphertext','whsec_test','fingerprint-prd058','active',?,?,?,?)
            """,
            (
                evidence_name,
                seed.tenant_id,
                "PRD058 Subscription",
                actor["user_id"],
                actor["user_id"],
                NOW,
                NOW,
            ),
        )
    else:
        evidence_name = "later_prd058_export_view"
        connection.execute(
            "CREATE VIEW later_prd058_export_view AS "
            "SELECT audit_export_id FROM audit_exports"
        )
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as error:
        rollback_last_migration(database_url)
    assert error.value.code == "db.migration_failed"
    assert "prd058_down_blocked" in error.value.message
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0049'"
    ).fetchone()[0] == "applied"
    if dependency_kind == "export":
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_exports WHERE audit_export_id=?", (evidence_name,)
        ).fetchone()[0] == 1
    elif dependency_kind == "subscription":
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_endpoints WHERE webhook_endpoint_id=?",
            (evidence_name,),
        ).fetchone()[0] == 1
    else:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name=?", (evidence_name,)
        ).fetchone()[0] == 1
    _assert_healthy(connection)
    connection.close()
