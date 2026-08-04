"""PRD-054 请求解释中心数据库迁移、租户边界与事件 outbox 专项测试。

测试只在 pytest 独占 SQLite 文件中构造最小 owner 图，并使用 ``example.invalid``、
摘要占位符和安全 ID。任何夹具都不保存 Provider Key、Token、Prompt、Response、真实
Webhook 地址或上游错误原文。专项同时验证升级/回滚原子性，避免迁移只在新空库可用。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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

# 版本断言覆盖从空库到 PRD-054 的连续迁移链，防止缺号或越级升级掩盖部署风险。
EXPECTED_VERSIONS = [f"{version:04d}" for version in range(1, 45)]
"""PRD-054 空库必须按顺序登记的 44 个迁移版本。"""

# 当前完整链必须真实执行后再逐版回退，历史专项才能同时证明新旧迁移可串行共存。
FROZEN_VERSION = "0044"
"""PRD-054 历史断言必须恢复到的迁移栈顶。"""

# 历史专项会真实执行并回滚后续迁移，但迁移框架保留每个曾执行版本的审计行；这里从
# 仓库真实 up SQL 计算版本集合，新增迁移时仍校验精确总数，避免再次写死当前栈顶。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
"""仓库正式迁移目录；测试不得从复制的旧目录推导当前完整迁移数。"""
# 只收集正式 up SQL 的版本前缀；down 文件与同版 up 共用前缀，必须排除以免重复计数。
CURRENT_UP_MIGRATION_VERSIONS = frozenset(
    path.name.split("_", 1)[0]
    for path in MIGRATIONS_DIR.glob("*.sql")
    if not path.name.endswith(".down.sql")
)
"""当前正式 up 迁移的精确版本集合，用于核对只改变状态的迁移审计历史。"""

# 区块顺序是迁移回填 JSON 与 API 冻结合同的共同边界，测试不得按夹具内容动态调整。
SECTION_NAMES = (
    "authentication",
    "rate_limit",
    "budget",
    "dlp",
    "policy",
    "routing",
    "keypool",
    "provider_response",
    "usage_and_finops",
    "logs",
    "webhooks",
)
"""RequestExplanation 冻结的 11 个详情区块，防止测试漏掉 rate_limit。"""


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占文件转换为迁移入口可解析的 SQLite URL。

    参数：``tmp_path`` 下的数据库文件。返回：项目数据库模块可识别的 URL。
    关键约束：不回落到共享开发库，确保异常和回滚用例不会污染实际验收证据。
    """

    return f"sqlite:///{path.as_posix()}"


def apply_migrations(database_url: str) -> migration_module.MigrationResult:
    """执行当前完整迁移链并逐版回退，使 PRD-054 专项停在 0044 栈顶。

    参数：pytest 独占数据库 URL。返回：只包含截至 0044 的迁移结果。
    关键约束：必须真实执行后续迁移的 up/down guard，不能复制旧迁移目录规避兼容性。
    """

    result = apply_current_migrations(database_url)
    # 依据迁移器实际报告计算回退链，确保未来新版本也先经过正式 down 验证。
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
        # 回滚顺序必须严格匹配真实栈顶，防止后续迁移加入后误测旧 down 合同。
        rolled_back = rollback_last_migration(database_url)
        assert rolled_back.rolled_back == version
    excluded = set(later_migrations)
    return migration_module.MigrationResult(
        run_id=result.run_id,
        applied=[version for version in result.applied if version not in excluded],
        skipped=[version for version in result.skipped if version not in excluded],
        rolled_back=result.rolled_back,
    )


def _tenant_context(connection: sqlite3.Connection, tenant_id: str) -> dict[str, str]:
    """读取同租户 admin、Virtual Key、Provider、Model 和 dev Environment 安全 ID。

    参数：数据库连接和可信 tenant_id。返回：构造最小请求 owner 图所需 ID。
    租户约束：每个 JOIN 都显式携带 tenant_id，夹具自身不能掩盖横向引用缺陷。
    """

    row = connection.execute(
        """
        SELECT actor.user_id, actor.email, actor.role,
               key.virtual_key_id, provider.provider_id, model.model_id,
               environment.environment_id, environment.code AS environment_code
        FROM users actor
        JOIN virtual_keys key ON key.tenant_id=actor.tenant_id
        JOIN providers provider ON provider.tenant_id=actor.tenant_id
        JOIN models model
          ON model.tenant_id=provider.tenant_id
         AND model.provider_id=provider.provider_id
        JOIN environments environment
          ON environment.tenant_id=actor.tenant_id AND environment.code='dev'
        WHERE actor.tenant_id=? AND actor.role='admin'
        ORDER BY actor.user_id, key.virtual_key_id, provider.provider_id, model.model_id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    assert row is not None
    return {column: str(row[column]) for column in row.keys()}


def _insert_request(
    connection: sqlite3.Connection,
    tenant_id: str,
    context: dict[str, str],
    suffix: str,
    *,
    status: str = "success",
) -> dict[str, str]:
    """通过 Gateway claim owner 图写入一条不含请求正文的 RequestLog。

    参数：连接、租户上下文、唯一后缀和终态。返回：request/trace/时间事实。
    安全约束：只保存追踪 ID 与归属字段，不构造 Prompt、Response 或原始错误信息。
    """

    request_id = f"req_prd054_{suffix}"
    trace_id = f"trc_prd054_{suffix}"
    created_at = "2026-07-28T01:00:00Z"
    updated_at = "2026-07-28T01:00:01Z"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        ) VALUES(?, ?, ?, ?, NULL, 'disabled', ?)
        """,
        (tenant_id, request_id, trace_id, context["virtual_key_id"], created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          status, created_at, updated_at, environment_id, environment_code
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            context["virtual_key_id"],
            context["provider_id"],
            context["model_id"],
            status,
            created_at,
            updated_at,
            context["environment_id"],
            context["environment_code"],
        ),
    )
    return {
        "request_id": request_id,
        "trace_id": trace_id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _insert_session(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    suffix: str,
) -> str:
    """为访问审计写入同租户会话，只使用测试摘要而不保存登录 Token。

    参数：连接、tenant/user owner 与唯一后缀。返回：session_id。
    安全约束：token_hash 是不可逆测试摘要占位值，不能被当作可登录凭据使用。
    """

    session_id = f"sess_prd054_{suffix}"
    connection.execute(
        """
        INSERT INTO sessions(
          session_id, tenant_id, user_id, token_jti, token_hash, status,
          expires_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, 'active',
                 '2026-07-29T01:00:00Z', '2026-07-28T00:59:00Z',
                 '2026-07-28T00:59:00Z')
        """,
        (
            session_id,
            tenant_id,
            user_id,
            f"jti-prd054-{suffix}",
            f"sha256-prd054-{suffix}",
        ),
    )
    # 会话 ID 作为后续认证归属证据返回，调用方不得改用跨租户查询补齐该关联。
    return session_id


def _explanation_values(
    tenant_id: str,
    context: dict[str, str],
    request: dict[str, str],
    session_id: str,
    hex_char: str,
) -> dict[str, Any]:
    """生成一条合法 pending RequestExplanation 的命名参数。

    参数：完整 owner 图和单个小写十六进制字符。返回：参数字典。
    关键约束：11 个区块全部出现；访问操作 ID 与源 Gateway request/trace 明确分离。
    """

    viewed_at = "2026-07-28T01:01:00Z"
    return {
        "explanation_id": f"rxe_{hex_char * 32}",
        "event_id": f"rxevt_{hex_char * 32}",
        "tenant_id": tenant_id,
        "source_request_id": request["request_id"],
        "source_trace_id": request["trace_id"],
        "actor_user_id": context["user_id"],
        "actor_session_id": session_id,
        "source_status": request["status"],
        "section_status_json": json.dumps(
            {name: "complete" for name in SECTION_NAMES},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "source_created_at": request["created_at"],
        "source_updated_at": request["updated_at"],
        "operation_request_id": f"req_control_prd054_{hex_char}",
        "operation_trace_id": f"trc_control_prd054_{hex_char}",
        "viewed_at": viewed_at,
    }


def _insert_explanation(
    connection: sqlite3.Connection,
    values: dict[str, Any],
) -> str:
    """写入一条完整访问审计兼 Webhook source outbox 事实。

    参数：``_explanation_values`` 生成或攻击用例覆盖后的参数。返回：解释 ID。
    事务约束：调用者控制提交，便于同一事务验证 owner 触发器和状态机原子性。
    """

    connection.execute(
        """
        INSERT INTO request_explanations(
          request_explanation_id, event_id, tenant_id,
          source_request_id, source_trace_id, actor_user_id, actor_session_id,
          source_status, status, section_status_json,
          missing_sections_json, truncated_sections_json,
          source_created_at, source_updated_at,
          operation_request_id, operation_trace_id,
          event_dispatch_status, event_dispatch_attempts, event_next_attempt_at,
          viewed_at, created_at, updated_at
        ) VALUES(
          :explanation_id, :event_id, :tenant_id,
          :source_request_id, :source_trace_id, :actor_user_id, :actor_session_id,
          :source_status, 'complete', :section_status_json,
          '[]', '[]', :source_created_at, :source_updated_at,
          :operation_request_id, :operation_trace_id,
          'pending', 0, :viewed_at, :viewed_at, :viewed_at, :viewed_at
        )
        """,
        values,
    )
    return str(values["explanation_id"])


def _prepare_request_graph(
    tmp_path: Path,
    name: str,
) -> tuple[
    str,
    sqlite3.Connection,
    str,
    dict[str, str],
    dict[str, str],
    str,
]:
    """建立 44 版本、seed、RequestLog 与访问会话的最小 owner 图。

    参数：pytest 临时目录和数据库名称。返回：URL、连接及可信 owner 上下文。
    资源边界：连接由调用测试关闭；数据库文件始终位于 pytest 独占目录。
    """

    database_url = _sqlite_url(tmp_path / f"{name}.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    request = _insert_request(connection, seed.tenant_id, context, name)
    session_id = _insert_session(connection, seed.tenant_id, context["user_id"], name)
    # 聚合返回同一租户内的真实连接和证据对象，确保迁移测试覆盖完整 owner 图而非孤立表。
    return database_url, connection, seed.tenant_id, context, request, session_id


def _create_other_tenant(
    connection: sqlite3.Connection,
    suffix: str,
) -> tuple[str, str, str]:
    """创建第二租户及其 admin/session，用于构造真实横向引用攻击。

    参数：连接和唯一后缀。返回：tenant_id、user_id、session_id。
    隔离约束：返回 ID 只用于负向测试，不与当前租户资源建立合法业务关系。
    """

    tenant_id = f"tenant_prd054_{suffix}"
    user_id = f"usr_prd054_{suffix}"
    now = "2026-07-28T00:30:00Z"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, ?, 'PRD054 隔离租户', 'internal_only', 'active', ?, ?)
        """,
        (tenant_id, f"prd054-{suffix}", now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(?, ?, ?, 'PRD054 Other Admin', 'sha256-test-only',
                 'admin', 'active', ?, ?)
        """,
        (user_id, tenant_id, f"prd054-{suffix}@example.invalid", now, now),
    )
    session_id = _insert_session(connection, tenant_id, user_id, f"other_{suffix}")
    # 返回另一租户的完整 owner 键，仅用于证明触发器拒绝跨租户引用，不能参与正常读取。
    return tenant_id, user_id, session_id


def _insert_webhook_endpoint(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_user_id: str,
    suffix: str,
) -> str:
    """创建仅用于落库测试的 Webhook Endpoint。

    参数：连接、owner、actor 与唯一后缀。返回：Endpoint ID。
    安全约束：URL 使用保留域，密文和指纹均为无效占位摘要，测试不会真实出网。
    """

    endpoint_id = f"wh_prd054_{suffix}"
    now = "2026-07-28T01:02:00Z"
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id, tenant_id, name, endpoint_url, event_types_json,
          encrypted_secret, secret_prefix, secret_fingerprint, status,
          created_by, updated_by, created_at, updated_at
        ) VALUES(?, ?, 'PRD054', 'https://example.invalid/webhook',
                 '["gateway.request_failed","request.explanation_viewed"]',
                 'ciphertext-test-only', 'whsec_test', ?, 'active', ?, ?, ?, ?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"fingerprint-prd054-{suffix}",
            actor_user_id,
            actor_user_id,
            now,
            now,
        ),
    )
    return endpoint_id


def _event_payload(
    tenant_id: str,
    event_id: str,
    event_type: str,
    request_id: str,
) -> str:
    """生成只含白名单追踪 ID 的 canonical Webhook 信封。

    参数：owner、事件身份与源 request_id。返回：稳定 JSON 文本。
    数据最小化：payload 不包含请求/响应正文、URL、凭据或 Provider 原始错误。
    """

    return json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "payload": {"request_id": request_id},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _insert_delivery(
    connection: sqlite3.Connection,
    tenant_id: str,
    endpoint_id: str,
    suffix: str,
    *,
    event_id: str,
    event_type: str,
    payload_json: str,
    source_delivery_id: str | None = None,
    replayed_by: str | None = None,
) -> str:
    """插入 original 或 replay delivery，并返回 delivery ID。

    参数：owner、事件信封及可选 replay 来源/actor。返回：delivery 主键。
    关键约束：是否传入 source 决定 delivery_kind；replay 四个归属字段必须成组出现。
    """

    delivery_kind = "replay" if source_delivery_id is not None else "original"
    delivery_id = f"whd_prd054_{suffix}"
    created_at = "2026-07-28T01:03:00Z"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id, tenant_id, webhook_endpoint_id,
          event_id, event_type, event_timestamp, payload_json,
          status, attempt_count, max_attempts, delivery_kind,
          replayed_from_delivery_id, replay_idempotency_key,
          replayed_by, replayed_at, secret_version, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            delivery_id,
            tenant_id,
            endpoint_id,
            event_id,
            event_type,
            created_at,
            payload_json,
            delivery_kind,
            source_delivery_id,
            f"idem-prd054-{suffix}" if source_delivery_id is not None else None,
            replayed_by,
            created_at if source_delivery_id is not None else None,
            created_at,
            created_at,
        ),
    )
    return delivery_id


def _plan_text(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> str:
    """执行 EXPLAIN QUERY PLAN 并返回可断言的计划文本。

    参数：连接、固定查询和绑定参数。返回：所有 planner detail 的拼接文本。
    安全约束：调用方只传测试内固定 SQL，不允许业务输入参与语句结构拼接。
    """

    rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
    return " ".join(str(row[3]) for row in rows)


def test_prd054_empty_database_repeat_and_empty_roundtrip(tmp_path: Path) -> None:
    """空库完整应用 44 版、重复 apply 幂等，且空事实 0044 可原子 down/up。"""

    database_url = _sqlite_url(tmp_path / "prd054-empty-roundtrip.db")
    first = apply_migrations(database_url)
    second = apply_migrations(database_url)
    assert first.applied == EXPECTED_VERSIONS
    assert second.applied == []
    assert second.skipped == EXPECTED_VERSIONS

    connection = connect_database(database_url)
    # 回滚只改变状态、不删除审计行，因此审计行数必须等于真实 up 迁移版本总数。
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
        CURRENT_UP_MIGRATION_VERSIONS
    )
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE type='table' AND name IN (
          'request_explanations', 'request_webhook_delivery_links'
        )
        """
    ).fetchone()[0] == 2
    connection.close()

    assert rollback_last_migration(database_url).rolled_back == "0044"
    connection = connect_database(database_url)
    assert connection.execute(
        """
        SELECT COUNT(*) FROM sqlite_master
        WHERE name LIKE '%prd054' OR name IN (
          'request_explanations', 'request_webhook_delivery_links'
        )
        """
    ).fetchone()[0] == 0
    # 迁移框架保留已回滚版本的审计行并标记 rolled_back，不能把历史执行事实物理删除。
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(
        CURRENT_UP_MIGRATION_VERSIONS
    )
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0044'"
    ).fetchone()[0] == "rolled_back"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()

    assert apply_migrations(database_url).applied == ["0044"]
    assert apply_migrations(database_url).applied == []


def test_prd054_owner_sections_and_append_only_contract(tmp_path: Path) -> None:
    """跨租户 actor/source、伪造区块及解释事实 UPDATE/DELETE/REPLACE 全部拒绝。"""

    _, connection, tenant_id, context, request, session_id = _prepare_request_graph(
        tmp_path,
        "owner",
    )
    values = _explanation_values(tenant_id, context, request, session_id, "a")
    explanation_id = _insert_explanation(connection, values)
    original = tuple(
        connection.execute(
            "SELECT rowid, * FROM request_explanations WHERE request_explanation_id=?",
            (explanation_id,),
        ).fetchone()
    )
    other_tenant, other_user, other_session = _create_other_tenant(connection, "owner")

    for overrides in (
        {"actor_user_id": other_user, "actor_session_id": other_session},
        {
            "tenant_id": other_tenant,
            "actor_user_id": other_user,
            "actor_session_id": other_session,
        },
        {"source_trace_id": "trc_prd054_forged"},
        {
            "section_status_json": json.dumps(
                {name: "complete" for name in SECTION_NAMES if name != "rate_limit"},
                separators=(",", ":"),
                sort_keys=True,
            )
        },
    ):
        attack = {
            **_explanation_values(tenant_id, context, request, session_id, "b"),
            **overrides,
        }
        with pytest.raises(sqlite3.IntegrityError, match="request_explanation_owner_mismatch"):
            _insert_explanation(connection, attack)

    with pytest.raises(sqlite3.IntegrityError, match="request_explanation_state_invalid"):
        connection.execute(
            "UPDATE request_explanations SET actor_user_id=? WHERE request_explanation_id=?",
            (other_user, explanation_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_explanation_immutable"):
        connection.execute(
            "DELETE FROM request_explanations WHERE request_explanation_id=?",
            (explanation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_explanation_immutable"):
        connection.execute(
            """
            INSERT OR REPLACE INTO request_explanations
            SELECT * FROM request_explanations WHERE request_explanation_id=?
            """,
            (explanation_id,),
        )
    assert tuple(
        connection.execute(
            "SELECT rowid, * FROM request_explanations WHERE request_explanation_id=?",
            (explanation_id,),
        ).fetchone()
    ) == original
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd054_outbox_cas_lease_retry_and_dispatch(tmp_path: Path) -> None:
    """pending 只可到期 claim，processing 只可过期接管、重试或完成，审计字段始终冻结。"""

    _, connection, tenant_id, context, request, session_id = _prepare_request_graph(
        tmp_path,
        "outbox",
    )
    explanation_id = _insert_explanation(
        connection,
        _explanation_values(tenant_id, context, request, session_id, "c"),
    )

    claim_sql = """
        UPDATE request_explanations
        SET event_dispatch_status='processing',
            event_dispatch_attempts=event_dispatch_attempts+1,
            event_next_attempt_at=NULL, event_claim_token=?,
            event_lease_owner_id=?, event_lease_expires_at=?, updated_at=?
        WHERE request_explanation_id=?
    """
    with pytest.raises(sqlite3.IntegrityError, match="request_explanation_state_invalid"):
        connection.execute(
            claim_sql,
            (
                "claim-prd054-early",
                "worker-early",
                "2026-07-28T01:04:00Z",
                "2026-07-28T01:00:59Z",
                explanation_id,
            ),
        )
    connection.execute(
        claim_sql,
        (
            "claim-prd054-first",
            "worker-first",
            "2026-07-28T01:05:00Z",
            "2026-07-28T01:01:00Z",
            explanation_id,
        ),
    )
    assert tuple(
        connection.execute(
            """
            SELECT event_dispatch_status, event_dispatch_attempts,
                   event_claim_token, event_lease_owner_id
            FROM request_explanations WHERE request_explanation_id=?
            """,
            (explanation_id,),
        ).fetchone()
    ) == ("processing", 1, "claim-prd054-first", "worker-first")

    takeover_sql = """
        UPDATE request_explanations
        SET event_dispatch_attempts=event_dispatch_attempts+1,
            event_claim_token=?, event_lease_owner_id=?,
            event_lease_expires_at=?, updated_at=?
        WHERE request_explanation_id=?
    """
    with pytest.raises(sqlite3.IntegrityError, match="request_explanation_state_invalid"):
        connection.execute(
            takeover_sql,
            (
                "claim-prd054-too-soon",
                "worker-second",
                "2026-07-28T01:09:00Z",
                "2026-07-28T01:04:59Z",
                explanation_id,
            ),
        )
    connection.execute(
        takeover_sql,
        (
            "claim-prd054-second",
            "worker-second",
            "2026-07-28T01:10:00Z",
            "2026-07-28T01:05:00Z",
            explanation_id,
        ),
    )
    connection.execute(
        """
        UPDATE request_explanations
        SET event_dispatch_status='pending',
            event_next_attempt_at='2026-07-28T01:07:00Z',
            event_claim_token=NULL, event_lease_owner_id=NULL,
            event_lease_expires_at=NULL,
            event_last_error_code='webhook.delivery_unavailable',
            updated_at='2026-07-28T01:06:00Z'
        WHERE request_explanation_id=?
        """,
        (explanation_id,),
    )
    connection.execute(
        claim_sql,
        (
            "claim-prd054-third",
            "worker-third",
            "2026-07-28T01:12:00Z",
            "2026-07-28T01:07:00Z",
            explanation_id,
        ),
    )
    connection.execute(
        """
        UPDATE request_explanations
        SET event_dispatch_status='dispatched',
            event_dispatched_at='2026-07-28T01:07:30Z',
            event_claim_token=NULL, event_lease_owner_id=NULL,
            event_lease_expires_at=NULL, event_last_error_code=NULL,
            updated_at='2026-07-28T01:07:30Z'
        WHERE request_explanation_id=?
        """,
        (explanation_id,),
    )
    row = connection.execute(
        """
        SELECT event_dispatch_status, event_dispatch_attempts,
               event_dispatched_at, source_request_id, actor_user_id
        FROM request_explanations WHERE request_explanation_id=?
        """,
        (explanation_id,),
    ).fetchone()
    assert tuple(row) == (
        "dispatched",
        3,
        "2026-07-28T01:07:30Z",
        request["request_id"],
        context["user_id"],
    )
    connection.close()


def test_prd054_backfill_and_live_original_replay_links(tmp_path: Path) -> None:
    """0043 历史和 0044 实时 original/replay 均建立同租户请求 link，link 本身不可变。"""

    database_url = _sqlite_url(tmp_path / "prd054-links.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    assert rollback_last_migration(database_url).rolled_back == "0044"
    connection = connect_database(database_url)
    context = _tenant_context(connection, seed.tenant_id)
    request = _insert_request(connection, seed.tenant_id, context, "links")
    endpoint_id = _insert_webhook_endpoint(
        connection,
        seed.tenant_id,
        context["user_id"],
        "links",
    )
    event_type = "gateway.request_failed"
    old_event_id = "evt_prd054_backfill"
    old_payload = _event_payload(
        seed.tenant_id,
        old_event_id,
        event_type,
        request["request_id"],
    )
    old_original = _insert_delivery(
        connection,
        seed.tenant_id,
        endpoint_id,
        "backfill_original",
        event_id=old_event_id,
        event_type=event_type,
        payload_json=old_payload,
    )
    old_replay = _insert_delivery(
        connection,
        seed.tenant_id,
        endpoint_id,
        "backfill_replay",
        event_id=old_event_id,
        event_type=event_type,
        payload_json=old_payload,
        source_delivery_id=old_original,
        replayed_by=context["user_id"],
    )
    connection.commit()
    connection.close()

    assert apply_migrations(database_url).applied == ["0044"]
    connection = connect_database(database_url)
    assert [
        tuple(row)
        for row in connection.execute(
            """
            SELECT webhook_delivery_id, source_request_id, delivery_kind
            FROM request_webhook_delivery_links
            WHERE tenant_id=? ORDER BY webhook_delivery_id
            """,
            (seed.tenant_id,),
        ).fetchall()
    ] == [
        (old_original, request["request_id"], "original"),
        (old_replay, request["request_id"], "replay"),
    ]

    live_event_id = "evt_prd054_live"
    live_payload = _event_payload(
        seed.tenant_id,
        live_event_id,
        event_type,
        request["request_id"],
    )
    live_original = _insert_delivery(
        connection,
        seed.tenant_id,
        endpoint_id,
        "live_original",
        event_id=live_event_id,
        event_type=event_type,
        payload_json=live_payload,
    )
    live_replay = _insert_delivery(
        connection,
        seed.tenant_id,
        endpoint_id,
        "live_replay",
        event_id=live_event_id,
        event_type=event_type,
        payload_json=live_payload,
        source_delivery_id=live_original,
        replayed_by=context["user_id"],
    )
    links = connection.execute(
        """
        SELECT request_webhook_delivery_link_id, webhook_delivery_id,
               source_request_id, delivery_kind
        FROM request_webhook_delivery_links
        WHERE webhook_delivery_id IN (?, ?) ORDER BY webhook_delivery_id
        """,
        (live_original, live_replay),
    ).fetchall()
    assert [(row[1], row[2], row[3]) for row in links] == [
        (live_original, request["request_id"], "original"),
        (live_replay, request["request_id"], "replay"),
    ]
    link_id = str(links[0][0])
    with pytest.raises(sqlite3.IntegrityError, match="request_webhook_link_immutable"):
        connection.execute(
            "UPDATE request_webhook_delivery_links SET event_type='usage.normalized' "
            "WHERE request_webhook_delivery_link_id=?",
            (link_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="request_webhook_link_immutable"):
        connection.execute(
            "DELETE FROM request_webhook_delivery_links "
            "WHERE request_webhook_delivery_link_id=?",
            (link_id,),
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd054_invalid_json_does_not_block_existing_delivery_write(tmp_path: Path) -> None:
    """畸形历史 payload 只是不建 request link，不能破坏既有 Webhook delivery 写入语义。"""

    _, connection, tenant_id, context, _, _ = _prepare_request_graph(
        tmp_path,
        "invalid-json",
    )
    endpoint_id = _insert_webhook_endpoint(
        connection,
        tenant_id,
        context["user_id"],
        "invalid_json",
    )
    delivery_id = _insert_delivery(
        connection,
        tenant_id,
        endpoint_id,
        "invalid_json",
        event_id="evt_prd054_invalid_json",
        event_type="gateway.request_failed",
        payload_json="{invalid-json",
    )
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_delivery_id=?",
        (delivery_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM request_webhook_delivery_links WHERE webhook_delivery_id=?",
        (delivery_id,),
    ).fetchone()[0] == 0
    connection.close()


def test_prd054_request_scoped_queries_use_tenant_first_indexes(tmp_path: Path) -> None:
    """解释、outbox、Webhook、预算与 Usage 六条主查询均命中冻结复合索引。"""

    _, connection, tenant_id, context, request, session_id = _prepare_request_graph(
        tmp_path,
        "plans",
    )
    _insert_explanation(
        connection,
        _explanation_values(tenant_id, context, request, session_id, "d"),
    )
    queries = (
        (
            "idx_request_explanations_request_time_prd054",
            """
            SELECT request_explanation_id FROM request_explanations
            WHERE tenant_id=? AND source_request_id=?
            ORDER BY viewed_at DESC, request_explanation_id DESC LIMIT 101
            """,
            (tenant_id, request["request_id"]),
        ),
        (
            "idx_request_explanations_actor_time_prd054",
            """
            SELECT request_explanation_id FROM request_explanations
            WHERE tenant_id=? AND actor_user_id=?
            ORDER BY viewed_at DESC, request_explanation_id DESC LIMIT 101
            """,
            (tenant_id, context["user_id"]),
        ),
        (
            "idx_request_explanations_pending_event_prd054",
            """
            SELECT request_explanation_id FROM request_explanations
            WHERE event_dispatch_status IN ('pending','processing')
              AND (
                (event_dispatch_status='pending' AND event_next_attempt_at<=?)
                OR
                (event_dispatch_status='processing' AND event_lease_expires_at<=?)
              )
            ORDER BY event_dispatch_status, event_next_attempt_at,
                     event_lease_expires_at, created_at, request_explanation_id
            LIMIT 101
            """,
            ("2026-07-28T01:02:00Z", "2026-07-28T01:02:00Z"),
        ),
        (
            "idx_request_webhook_links_request_time_prd054",
            """
            SELECT webhook_delivery_id FROM request_webhook_delivery_links
            WHERE tenant_id=? AND source_request_id=?
            ORDER BY created_at DESC, request_webhook_delivery_link_id DESC LIMIT 101
            """,
            (tenant_id, request["request_id"]),
        ),
        (
            "idx_budget_scope_events_request_prd054",
            """
            SELECT budget_scope_event_id FROM budget_scope_events
            WHERE tenant_id=? AND request_id=?
            ORDER BY created_at, budget_scope_event_id LIMIT 101
            """,
            (tenant_id, request["request_id"]),
        ),
        (
            "idx_usage_ledger_request_prd054",
            """
            SELECT usage_ledger_id FROM usage_ledger
            WHERE tenant_id=? AND request_id=?
            ORDER BY created_at, usage_ledger_id LIMIT 101
            """,
            (tenant_id, request["request_id"]),
        ),
    )
    for index_name, sql, parameters in queries:
        assert index_name in _plan_text(connection, sql, parameters)
    connection.close()


@pytest.mark.parametrize("fact_kind", ["explanation", "webhook"])
def test_prd054_down_guard_preserves_business_facts_atomically(
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """存在访问审计或已投递 explanation 事件时，0044 回滚原子拒绝且证据保留。"""

    database_url, connection, tenant_id, context, request, session_id = (
        _prepare_request_graph(tmp_path, f"down-{fact_kind}")
    )
    if fact_kind == "explanation":
        _insert_explanation(
            connection,
            _explanation_values(tenant_id, context, request, session_id, "e"),
        )
    else:
        endpoint_id = _insert_webhook_endpoint(
            connection,
            tenant_id,
            context["user_id"],
            "down_webhook",
        )
        _insert_delivery(
            connection,
            tenant_id,
            endpoint_id,
            "down_webhook",
            event_id="rxevt_down_prd054",
            event_type="request.explanation_viewed",
            payload_json="{}",
        )
    connection.commit()
    connection.close()

    with pytest.raises(DbError) as exc:
        rollback_last_migration(database_url)
    assert exc.value.code == "db.migration_failed"
    assert "prd054_down_has_business_facts" in exc.value.message

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM schema_migrations WHERE version='0044'"
    ).fetchone()[0] == "applied"
    if fact_kind == "explanation":
        assert connection.execute("SELECT COUNT(*) FROM request_explanations").fetchone()[0] == 1
    else:
        assert connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries "
            "WHERE event_type='request.explanation_viewed'"
        ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='request_explanations'"
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_prd054_source_request_link_requires_whitelisted_persisted_binding(
    tmp_path: Path,
) -> None:
    """source_request_id 仅能由白名单事件及其持久 source 事实证明。"""

    _, connection, tenant_id, context, request, session_id = _prepare_request_graph(
        tmp_path,
        "source-binding",
    )
    other_request = _insert_request(
        connection,
        tenant_id,
        context,
        "source-binding-other",
    )
    endpoint_id = _insert_webhook_endpoint(
        connection,
        tenant_id,
        context["user_id"],
        "source_binding",
    )

    # 即使任意其他事件夹带合法 request ID，也不能进入请求解释的 Webhook 索引。
    unrelated_event_id = "evt_prd054_unrelated_source"
    unrelated_payload = json.dumps(
        {
            "event_id": unrelated_event_id,
            "event_type": "cost.anomaly_detected",
            "tenant_id": tenant_id,
            "payload": {"source_request_id": request["request_id"]},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    unrelated_delivery = _insert_delivery(
        connection,
        tenant_id,
        endpoint_id,
        "unrelated_source",
        event_id=unrelated_event_id,
        event_type="cost.anomaly_detected",
        payload_json=unrelated_payload,
    )

    # 白名单事件也必须绑定同一条 request_explanations source，不能借对象 ID 指向另一请求。
    mismatched = _explanation_values(
        tenant_id,
        context,
        request,
        session_id,
        "c",
    )
    _insert_explanation(connection, mismatched)
    mismatched_payload = json.dumps(
        {
            "event_id": mismatched["event_id"],
            "event_type": "request.explanation_viewed",
            "tenant_id": tenant_id,
            "object_id": mismatched["explanation_id"],
            "payload": {
                "request_explanation_id": mismatched["explanation_id"],
                "source_request_id": other_request["request_id"],
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    mismatched_delivery = _insert_delivery(
        connection,
        tenant_id,
        endpoint_id,
        "mismatched_source",
        event_id=mismatched["event_id"],
        event_type="request.explanation_viewed",
        payload_json=mismatched_payload,
    )

    valid = _explanation_values(tenant_id, context, request, session_id, "d")
    _insert_explanation(connection, valid)
    valid_payload = json.dumps(
        {
            "event_id": valid["event_id"],
            "event_type": "request.explanation_viewed",
            "tenant_id": tenant_id,
            "object_id": valid["explanation_id"],
            "payload": {
                "request_explanation_id": valid["explanation_id"],
                "source_request_id": request["request_id"],
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    valid_delivery = _insert_delivery(
        connection,
        tenant_id,
        endpoint_id,
        "valid_source",
        event_id=valid["event_id"],
        event_type="request.explanation_viewed",
        payload_json=valid_payload,
    )

    # 手工写 link 也必须复用自动路径的证明规则，未知事件和 payload 错绑不能绕过 owner 触发器。
    manual_attacks = (
        (
            unrelated_delivery,
            unrelated_event_id,
            "cost.anomaly_detected",
            request["request_id"],
        ),
        (
            mismatched_delivery,
            mismatched["event_id"],
            "request.explanation_viewed",
            request["request_id"],
        ),
    )
    for index, (delivery_id, event_id, event_type, source_request_id) in enumerate(
        manual_attacks,
        start=1,
    ):
        with pytest.raises(
            sqlite3.IntegrityError,
            match="request_webhook_link_owner_mismatch",
        ):
            connection.execute(
                """
                INSERT INTO request_webhook_delivery_links(
                  request_webhook_delivery_link_id, tenant_id, source_request_id,
                  webhook_delivery_id, event_id, event_type, delivery_kind, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'original', '2026-07-28T01:04:00Z')
                """,
                (
                    f"rwdl_{index:032x}",
                    tenant_id,
                    source_request_id,
                    delivery_id,
                    event_id,
                    event_type,
                ),
            )
    connection.commit()

    assert connection.execute(
        "SELECT COUNT(*) FROM request_webhook_delivery_links "
        "WHERE webhook_delivery_id IN (?, ?)",
        (unrelated_delivery, mismatched_delivery),
    ).fetchone()[0] == 0
    linked = connection.execute(
        "SELECT source_request_id, event_id, event_type "
        "FROM request_webhook_delivery_links WHERE webhook_delivery_id=?",
        (valid_delivery,),
    ).fetchone()
    assert linked is not None
    assert tuple(linked) == (
        request["request_id"],
        valid["event_id"],
        "request.explanation_viewed",
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
