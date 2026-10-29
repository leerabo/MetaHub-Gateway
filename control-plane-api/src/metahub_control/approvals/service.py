"""PRD-078 审批任务、step-up、决策与 timeout 的权威事务服务。"""

from __future__ import annotations

import base64
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict
from uuid import uuid4

from metahub_shared.approvals import (
    CREATE_INTENT_DOMAIN,
    CREATE_KEY_DOMAIN,
    CURSOR_DOMAIN,
    DECISION_INTENT_DOMAIN,
    DECISION_KEY_DOMAIN,
    STEP_UP_DOMAIN,
    TARGET_FINGERPRINT_DOMAIN,
    ApprovalContractError,
    ApprovalKeyring,
    VersionedApprovalSecret,
    canonical_approval_bytes,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import verify_password
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import (
    AuthContext,
    refresh_transaction_authentication,
    require_permission,
)

from .models import (
    ApprovalDecisionInfo,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalDetailResponse,
    ApprovalListResponse,
    ApprovalStepUpRequest,
    ApprovalStepUpResponse,
    ApprovalSummary,
    ApprovalTargetSummary,
    CreateApprovalRequest,
    CreateApprovalResponse,
)
from .targets import (
    ApprovalTarget,
    apply_approved_target,
    apply_rejected_target,
    build_mcp_approval_target,
    load_target,
    load_task_target,
)

# 审批标识、分页、容量、票据时效及目标权限映射构成服务端输入与授权边界。
APPROVAL_ID_PATTERN = re.compile(r"^apr_[0-9a-f]{32}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
VALID_STATUSES = frozenset({"pending", "approved", "rejected", "all"})
VALID_TARGET_TYPES = frozenset(
    {"budget_block_release", "session_risk_action", "mcp_tool_invocation"}
)
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_PENDING_PER_TENANT = 1_000
CURSOR_VERSION = 2
STEP_UP_TTL = timedelta(seconds=120)
STEP_UP_LOCK = timedelta(minutes=15)
REPLAY_TTL = timedelta(hours=24)
INTERNAL_PRODUCER_ID = "approval-internal-producer"
TIMEOUT_ACTOR_ID = "approval-timeout-scheduler"

_DECISION_REASON_MATRIX = {
    "approve": frozenset({"approved_business_need"}),
    "reject": frozenset({"risk_unacceptable", "budget_not_approved", "no_longer_needed"}),
}
_READ_PERMISSION = {
    "budget_block_release": "billing.budget.read",
    "session_risk_action": "session_risk.read",
    "mcp_tool_invocation": "mcp.read",
}


@dataclass(frozen=True, slots=True)
class TimeoutResult:
    """一次 scheduler timeout 的安全坐标。
    ``TimeoutResult`` 维护审批权威事务状态，所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。
    """

    tenant_id: str
    approval_id: str
    event_id: str
    request_id: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class SchedulerTenantLease:
    """Scheduler 交给 timeout 事务的完整 tenant lease 坐标。

    所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。
    """

    tenant_id: str
    lease_generation: int
    lease_owner_id: str
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class _StepUpSnapshot:
    """PBKDF2 前冻结的可信 owner 图；不保存到日志或响应。"""

    tenant_id: str
    user_id: str
    session_id: str
    approval_id: str
    action: Literal["approve", "reject"]
    task_revision: int
    authorization_fingerprint: str
    password_hash: str = field(repr=False)


class _CursorState(TypedDict):
    """签名游标内经严格校验的分页快照。"""

    snapshot: str
    snapshot_rowid: int
    before_time: str
    before_id: str


def create_approval(
    config: AppConfig,
    tenant_id: str,
    payload: CreateApprovalRequest,
    *,
    request_id: str,
    trace_id: str,
) -> CreateApprovalResponse:
    """由内部服务身份创建 tenant-qualified 审批及 created source outbox。"""

    owner_tenant = _bounded_text(tenant_id, maximum=160)
    canonical = _canonical_create(payload)
    keyring = _keyring(config)
    key_payload = {
        "tenant_id": owner_tenant,
        "producer_id": INTERNAL_PRODUCER_ID,
        "idempotency_key": canonical.pop("idempotency_key"),
    }
    key_candidates = keyring.digest_candidates(CREATE_KEY_DOMAIN, key_payload)
    intent_payload: dict[str, object] = {"tenant_id": owner_tenant, **canonical}
    intent_candidates = {
        item.version: item.digest
        for item in keyring.digest_candidates(CREATE_INTENT_DOMAIN, intent_payload)
    }
    current_key = key_candidates[0]
    current_intent = intent_candidates.get(current_key.version)
    if current_intent is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        tenant = connection.execute(
            "SELECT tenant_id,status FROM tenants WHERE tenant_id=?",
            (owner_tenant,),
        ).fetchone()
        # tenant_id 必须来自可信所有者关系并参与每次读写条件；缺失或不一致时立即失败关闭。
        if tenant is None or tenant["status"] != "active":
            # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
            raise ApiError("approval.not_found")
        replay = _find_create_replay(connection, owner_tenant, key_candidates)
        if replay is not None:
            expected = intent_candidates.get(_positive_int(replay["create_hmac_key_version"]))
            if expected is None or not hmac.compare_digest(
                _text(replay["create_intent_hmac"]), expected
            ):
                # 同一幂等摘要只能恢复完全相同的审批意图；摘要版本或内容不一致时拒绝覆盖既有事实。
                raise ApiError("approval.idempotency_conflict")
            response = CreateApprovalResponse(
                approval=_summary(replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response

        pending_count = connection.execute(
            "SELECT COUNT(*) AS value FROM approval_tasks WHERE tenant_id=? AND status='pending'",
            (owner_tenant,),
        ).fetchone()
        maximum = int(
            getattr(config, "approval_max_pending_tasks_per_tenant", MAX_PENDING_PER_TENANT)
        )
        if pending_count is None or type(pending_count["value"]) is not int:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        if pending_count["value"] >= maximum:
            # 待审批容量按可信 tenant_id 单独计数，达到租户上限后拒绝创建并保留失败可追踪性。
            raise ApiError("approval.capacity_full")

        target = load_target(
            connection,
            tenant_id=owner_tenant,
            source_type=str(canonical["source_type"]),
            source_id=str(canonical["source_id"]),
            target_type=str(canonical["target_type"]),
            target_id=str(canonical["target_id"]),
            target_action=str(canonical["target_action"]),
            target_reason_code=(
                str(canonical["target_reason_code"])
                if canonical["target_reason_code"] is not None
                else None
            ),
        )
        fingerprint = keyring.digest(
            TARGET_FINGERPRINT_DOMAIN,
            _target_fingerprint_payload(owner_tenant, target),
        )
        approval_id = f"apr_{uuid4().hex}"
        expires_at = now + timedelta(seconds=int(canonical["expires_in_seconds"]))
        timestamp = _to_db_time(now)
        connection.execute(
            """
            INSERT INTO approval_tasks(
              approval_task_id,tenant_id,source_type,source_id,target_type,target_id,
              target_action,target_reason_code,target_version,target_fingerprint,risk_level,
              status,resolution_reason,state_revision,writeback_status,
              required_permission_code,created_by_type,created_by_id,decided_by_user_id,
              decided_at,expires_at,create_hmac_key_version,create_idempotency_key_hmac,
              create_intent_hmac,source_request_id,source_trace_id,decision_request_id,
              decision_trace_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending',NULL,1,'pending',?,
                     'service',?,NULL,NULL,?,?,?,?,?,?,NULL,NULL,?,?)
            """,
            (
                approval_id,
                owner_tenant,
                canonical["source_type"],
                canonical["source_id"],
                target.target_type,
                target.target_id,
                target.target_action,
                target.target_reason_code,
                target.target_version,
                fingerprint.digest,
                target.risk_level,
                target.write_permission_code,
                INTERNAL_PRODUCER_ID,
                _to_db_time(expires_at),
                current_key.version,
                current_key.digest,
                current_intent,
                request_id,
                trace_id,
                timestamp,
                timestamp,
            ),
        )
        task = _load_task(connection, owner_tenant, approval_id)
        if task is None:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        _insert_created_event(
            connection,
            task,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        connection.execute(
            """
            INSERT INTO approval_scheduler_tenants(
              tenant_id,status,lease_generation,lease_owner_id,lease_token,
              lease_expires_at,next_scan_at,created_at,updated_at
            ) VALUES(?,'idle',0,NULL,NULL,NULL,?,?,?) ON CONFLICT DO NOTHING
            """,
            (owner_tenant, timestamp, timestamp, timestamp),
        )
        response = CreateApprovalResponse(
            approval=_summary(task),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库唯一性或外键冲突表示并发状态已变化；事务回滚后仅返回固定状态冲突错误。
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
        raise ApiError("approval.state_conflict") from exc
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def create_mcp_approval_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    tenant_id: str,
    invocation_id: str,
    approval_id: str,
    created_by_id: str,
    expires_at: datetime,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> sqlite3.Row:
    """在 Invocation 的现有事务内原子创建高危 MCP ApprovalTask。

    调用方必须先插入使用 deferred FK 的 approval_pending Invocation。此函数不提交、不解密参数、
    不访问 MCP 网络；任一 Task/Event/Outbox 写入失败都由调用方整体回滚。
    """

    if not connection.in_transaction:
        raise ApiError("approval.unavailable")
    pending = connection.execute(
        "SELECT COUNT(*) AS value FROM approval_tasks WHERE tenant_id=? AND status='pending'",
        (tenant_id,),
    ).fetchone()
    maximum = int(getattr(config, "approval_max_pending_tasks_per_tenant", MAX_PENDING_PER_TENANT))
    if pending is None or type(pending["value"]) is not int:
        raise ApiError("approval.unavailable")
    if pending["value"] >= maximum:
        raise ApiError("approval.capacity_full")
    target = load_target(
        connection,
        tenant_id=tenant_id,
        source_type="mcp",
        source_id=invocation_id,
        target_type="mcp_tool_invocation",
        target_id=invocation_id,
        target_action="invoke",
        target_reason_code=None,
    )
    keyring = _keyring(config)
    key_digest = keyring.digest(
        CREATE_KEY_DOMAIN,
        {
            "tenant_id": tenant_id,
            "producer_id": "mcp-governance",
            "idempotency_key": invocation_id,
        },
    )
    intent_digest = keyring.digest(
        CREATE_INTENT_DOMAIN,
        {
            "tenant_id": tenant_id,
            "source_type": "mcp",
            "source_id": invocation_id,
            "target_type": target.target_type,
            "target_id": target.target_id,
            "target_action": target.target_action,
            "target_version": target.target_version,
            "expires_at": _to_db_time(expires_at),
        },
    )
    fingerprint = keyring.digest(
        TARGET_FINGERPRINT_DOMAIN,
        _target_fingerprint_payload(tenant_id, target),
    )
    timestamp = _to_db_time(now)
    connection.execute(
        """
        INSERT INTO approval_tasks(
          approval_task_id,tenant_id,source_type,source_id,target_type,target_id,
          target_action,target_reason_code,target_version,target_fingerprint,risk_level,
          status,resolution_reason,state_revision,writeback_status,
          required_permission_code,created_by_type,created_by_id,decided_by_user_id,
          decided_at,expires_at,create_hmac_key_version,create_idempotency_key_hmac,
          create_intent_hmac,source_request_id,source_trace_id,decision_request_id,
          decision_trace_id,created_at,updated_at,mcp_invocation_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,1,'pending',?,
                 'user',?,NULL,NULL,?,?,?,?,?,?,NULL,NULL,?,?,?)
        """,
        (
            approval_id,
            tenant_id,
            "mcp",
            invocation_id,
            target.target_type,
            target.target_id,
            target.target_action,
            target.target_reason_code,
            target.target_version,
            fingerprint.digest,
            target.risk_level,
            target.write_permission_code,
            created_by_id,
            _to_db_time(expires_at),
            key_digest.version,
            key_digest.digest,
            intent_digest.digest,
            request_id,
            trace_id,
            timestamp,
            timestamp,
            invocation_id,
        ),
    )
    task = _load_task(connection, tenant_id, approval_id)
    if task is None:
        raise ApiError("approval.unavailable")
    _insert_created_event(
        connection,
        task,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO approval_scheduler_tenants(
          tenant_id,status,lease_generation,lease_owner_id,lease_token,
          lease_expires_at,next_scan_at,created_at,updated_at
        ) VALUES(?,'idle',0,NULL,NULL,NULL,?,?,?) ON CONFLICT DO NOTHING
        """,
        (tenant_id, timestamp, timestamp, timestamp),
    )
    return task


def mcp_approval_target_coordinates(
    config: AppConfig,
    *,
    fingerprint_key_version: int | None = None,
    **values: object,
) -> tuple[str, str]:
    """Return the canonical MCP target version and Approval-domain fingerprint."""

    try:
        target = build_mcp_approval_target(**values)  # type: ignore[arg-type]
        keyring = _keyring(config)
        payload = _target_fingerprint_payload(str(values["tenant_id"]), target)
        if fingerprint_key_version is None:
            fingerprint = keyring.digest(TARGET_FINGERPRINT_DOMAIN, payload).digest
        else:
            fingerprint = _digest_for_version(
                keyring,
                TARGET_FINGERPRINT_DOMAIN,
                payload,
                _positive_int(fingerprint_key_version),
            )
            if fingerprint is None:
                raise ApprovalContractError("approval.hmac_key_invalid")
    except (ApprovalContractError, KeyError, TypeError, ValueError) as exc:
        raise ApiError("approval.unavailable") from exc
    return target.target_version, fingerprint


def list_approvals(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    target_type: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> ApprovalListResponse:
    """返回当前租户和目标域权限交集后的稳定 keyset 页。"""

    normalized_status = _query_status(status)
    normalized_target = _query_target_type(target_type)
    page_size = _query_limit(limit)
    keyring = _keyring(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _refresh_reader(connection, context)
        allowed_types = tuple(
            item
            for item in sorted(VALID_TARGET_TYPES)
            if _READ_PERMISSION[item] in live.permission_codes
        )
        if normalized_target is not None:
            allowed_types = tuple(item for item in allowed_types if item == normalized_target)
        now = _database_now(connection)
        if cursor is None:
            snapshot_row = connection.execute(
                """
                SELECT COALESCE(MAX(rowid),0) AS value
                FROM approval_tasks
                WHERE tenant_id=?
                """,
                (live.user.tenant_id,),
            ).fetchone()
            # 缺少同租户快照水位表示数据库读取不完整，固定失败且不得生成可漂移游标。
            if snapshot_row is None:
                raise ApiError("approval.unavailable")
            cursor_state: _CursorState = {
                "snapshot": _to_db_time(now),
                # 时间精度只有秒；rowid 水位阻止同秒新建任务混入后续页。
                "snapshot_rowid": _nonnegative_int(snapshot_row["value"]),
                "before_time": "9999-12-31T23:59:59Z",
                "before_id": "apr_" + "f" * 32,
            }
        else:
            cursor_state = _decode_cursor(
                keyring,
                cursor,
                tenant_id=live.user.tenant_id,
                authorization_fingerprint=live.authorization_fingerprint,
                status=normalized_status,
                target_type=normalized_target,
                allowed_types=allowed_types,
            )
        rows: list[sqlite3.Row] = []
        if allowed_types:
            placeholders = ",".join("?" for _ in allowed_types)
            clauses = [
                "tenant_id=?",
                "rowid<=?",
                f"target_type IN ({placeholders})",
                "unixepoch(created_at)<=unixepoch(?)",
                "(unixepoch(created_at)<unixepoch(?) OR (created_at=? AND approval_task_id<?))",
            ]
            params: list[object] = [
                live.user.tenant_id,
                cursor_state["snapshot_rowid"],
                *allowed_types,
                cursor_state["snapshot"],
                cursor_state["before_time"],
                cursor_state["before_time"],
                cursor_state["before_id"],
            ]
            if normalized_status != "all":
                clauses.append("status=?")
                params.append(normalized_status)
            rows = connection.execute(
                f"""
                SELECT * FROM approval_tasks
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC,approval_task_id DESC
                LIMIT ?
                """,
                (*params, page_size + 1),
            ).fetchall()
        visible = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size and visible:
            next_cursor = _encode_cursor(
                keyring,
                tenant_id=live.user.tenant_id,
                authorization_fingerprint=live.authorization_fingerprint,
                status=normalized_status,
                target_type=normalized_target,
                allowed_types=allowed_types,
                snapshot=str(cursor_state["snapshot"]),
                snapshot_rowid=cursor_state["snapshot_rowid"],
                before_time=_text(visible[-1]["created_at"]),
                before_id=_text(visible[-1]["approval_task_id"]),
            )
        response = ApprovalListResponse(
            approvals=[_summary(row) for row in visible],
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def get_approval(
    config: AppConfig,
    context: AuthContext,
    approval_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> ApprovalDetailResponse:
    """读取一条当前租户可见任务与不可变决策时间线。"""

    normalized_id = _approval_id(approval_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _refresh_reader(connection, context)
        task = _load_task(connection, live.user.tenant_id, normalized_id)
        if task is None or not _is_target_visible(live, task):
            # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
            raise ApiError("approval.not_found")
        decisions = connection.execute(
            """
            SELECT * FROM approval_decisions
            WHERE tenant_id=? AND approval_task_id=?
            ORDER BY decided_at ASC,approval_decision_id ASC
            """,
            (live.user.tenant_id, normalized_id),
        ).fetchall()
        allowed: list[Literal["approve", "reject"]] = []
        if (
            task["status"] == "pending"
            and "approval.decide" in live.permission_codes
            and _text(task["required_permission_code"]) in live.permission_codes
            and _parse_time(task["expires_at"]) > _database_now(connection)
        ):
            allowed = ["approve", "reject"]
        response = ApprovalDetailResponse(
            approval=_summary(task),
            target_summary=ApprovalTargetSummary(
                target_type=_text(task["target_type"]),
                target_id=_text(task["target_id"]),
                target_action=_text(task["target_action"]),
                target_version=_text(task["target_version"]),
            ),
            allowed_actions=allowed,
            decisions=[_decision_info(row) for row in decisions],
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def issue_step_up(
    config: AppConfig,
    context: AuthContext,
    approval_id: str,
    payload: ApprovalStepUpRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ApprovalStepUpResponse:
    """在无写锁阶段校验密码，并用短事务签发一次性高风险票据。"""

    normalized_id = _approval_id(approval_id)
    action = _decision_action(payload.action)
    snapshot = _read_step_up_snapshot(config, context, normalized_id, action)
    try:
        password_valid = verify_password(payload.password, snapshot.password_hash)
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (TypeError, ValueError):
        password_valid = False
    if not password_valid:
        _record_step_up_failure(config, context, snapshot)
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    return _sign_step_up(
        config,
        context,
        snapshot,
        request_id=request_id,
        trace_id=trace_id,
    )


def _read_step_up_snapshot(
    config: AppConfig,
    context: AuthContext,
    approval_id: str,
    action: Literal["approve", "reject"],
) -> _StepUpSnapshot:
    """读取绑定租户、会话、权限和任务修订的 step-up 快照。"""
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _refresh_decider(connection, context)
        task = _load_task(connection, live.user.tenant_id, approval_id)
        if task is None or not _is_target_visible(live, task):
            # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
            raise ApiError("approval.not_found")
        _require_task_writer(live, task)
        now = _database_now(connection)
        _require_pending_high_task(task, now)
        _require_target_unchanged(connection, config, task)
        guard = _load_step_up_guard(
            connection,
            tenant_id=live.user.tenant_id,
            approval_id=approval_id,
            user_id=live.user.user_id,
        )
        if guard is not None and _guard_is_locked(guard, now):
            # 二次验证失败次数按用户与租户持久化限流，锁定窗口内不再校验密码或签发票据。
            raise ApiError("approval.step_up_rate_limited")
        actor = connection.execute(
            """
            SELECT password_hash FROM users
            WHERE tenant_id=? AND user_id=? AND status='active'
            """,
            (live.user.tenant_id, live.user.user_id),
        ).fetchone()
        if actor is None:
            # 二次验证票据绑定租户、用户、会话、动作、修订和时效；原文不得落库或回显。
            raise ApiError("approval.step_up_invalid")
        snapshot = _StepUpSnapshot(
            tenant_id=live.user.tenant_id,
            user_id=live.user.user_id,
            session_id=live.session.session_id,
            approval_id=approval_id,
            action=action,
            task_revision=_positive_int(task["state_revision"]),
            authorization_fingerprint=_authorization_fingerprint(live),
            password_hash=_text(actor["password_hash"]),
        )
        connection.commit()
        return snapshot
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def _record_step_up_failure(
    config: AppConfig,
    context: AuthContext,
    snapshot: _StepUpSnapshot,
) -> None:
    """在重新确认 step-up 所有者后记录一次密码失败。"""
    connection = _connect(config)
    locked = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        live, task, now = _reload_step_up_owner(connection, config, context, snapshot)
        guard = _load_step_up_guard(
            connection,
            tenant_id=snapshot.tenant_id,
            approval_id=snapshot.approval_id,
            user_id=snapshot.user_id,
        )
        timestamp = _to_db_time(now)
        if guard is None:
            connection.execute(
                """
                INSERT INTO approval_step_up_guards(
                  tenant_id,approval_task_id,user_id,failed_attempts,locked_until,
                  created_at,updated_at
                ) VALUES(?,?,?,1,NULL,?,?)
                """,
                (snapshot.tenant_id, snapshot.approval_id, snapshot.user_id, timestamp, timestamp),
            )
        else:
            attempts = _nonnegative_int(guard["failed_attempts"])
            if attempts == 5:
                if _guard_is_locked(guard, now):
                    locked = True
                else:
                    connection.execute(
                        """
                        UPDATE approval_step_up_guards
                        SET failed_attempts=0,locked_until=NULL,updated_at=?
                        WHERE tenant_id=? AND approval_task_id=? AND user_id=?
                          AND failed_attempts=5
                        """,
                        (timestamp, snapshot.tenant_id, snapshot.approval_id, snapshot.user_id),
                    )
                    attempts = 0
            if not locked:
                next_attempt = attempts + 1
                locked_until = _to_db_time(now + STEP_UP_LOCK) if next_attempt == 5 else None
                cursor = connection.execute(
                    """
                    UPDATE approval_step_up_guards
                    SET failed_attempts=?,locked_until=?,updated_at=?
                    WHERE tenant_id=? AND approval_task_id=? AND user_id=?
                      AND failed_attempts=?
                    """,
                    (
                        next_attempt,
                        locked_until,
                        timestamp,
                        snapshot.tenant_id,
                        snapshot.approval_id,
                        snapshot.user_id,
                        attempts,
                    ),
                )
                if cursor.rowcount != 1:
                    # 并发推进失败时关闭事务，只暴露固定错误码。
                    raise ApiError("approval.unavailable")
                locked = next_attempt == 5
        _ = live, task
        connection.commit()
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()
    if locked:
        # 二次验证失败次数按用户与租户持久化限流，锁定窗口内不再校验密码或签发票据。
        raise ApiError("approval.step_up_rate_limited")


def _sign_step_up(
    config: AppConfig,
    context: AuthContext,
    snapshot: _StepUpSnapshot,
    *,
    request_id: str,
    trace_id: str,
) -> ApprovalStepUpResponse:
    """在写事务中重验所有者并签发一次性 step-up 票据。"""
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live, task, now = _reload_step_up_owner(connection, config, context, snapshot)
        guard = _load_step_up_guard(
            connection,
            tenant_id=snapshot.tenant_id,
            approval_id=snapshot.approval_id,
            user_id=snapshot.user_id,
        )
        if guard is not None and _guard_is_locked(guard, now):
            # 二次验证失败次数按用户与租户持久化限流，锁定窗口内不再校验密码或签发票据。
            raise ApiError("approval.step_up_rate_limited")
        timestamp = _to_db_time(now)
        if guard is not None and _nonnegative_int(guard["failed_attempts"]) > 0:
            cursor = connection.execute(
                """
                UPDATE approval_step_up_guards
                SET failed_attempts=0,locked_until=NULL,updated_at=?
                WHERE tenant_id=? AND approval_task_id=? AND user_id=?
                """,
                (timestamp, snapshot.tenant_id, snapshot.approval_id, snapshot.user_id),
            )
            if cursor.rowcount != 1:
                # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
                raise ApiError("approval.unavailable")
        connection.execute(
            """
            UPDATE approval_step_up_challenges
            SET status='revoked',revoked_at=?,updated_at=?
            WHERE tenant_id=? AND approval_task_id=? AND user_id=? AND action=?
              AND status='active'
            """,
            (
                timestamp,
                timestamp,
                snapshot.tenant_id,
                snapshot.approval_id,
                snapshot.user_id,
                snapshot.action,
            ),
        )
        token = secrets.token_urlsafe(32)
        token_digest = _keyring(config).digest(
            STEP_UP_DOMAIN,
            _step_up_payload(
                token,
                tenant_id=snapshot.tenant_id,
                user_id=snapshot.user_id,
                session_id=snapshot.session_id,
                approval_id=snapshot.approval_id,
                action=snapshot.action,
                task_revision=snapshot.task_revision,
                authorization_fingerprint=snapshot.authorization_fingerprint,
            ),
        )
        expires_at = now + STEP_UP_TTL
        connection.execute(
            """
            INSERT INTO approval_step_up_challenges(
              approval_challenge_id,tenant_id,approval_task_id,user_id,session_id,
              action,task_revision,authorization_fingerprint,hmac_key_version,token_hmac,
              status,expires_at,consumed_at,revoked_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,NULL,NULL,?,?)
            """,
            (
                f"apch_{uuid4().hex}",
                snapshot.tenant_id,
                snapshot.approval_id,
                snapshot.user_id,
                snapshot.session_id,
                snapshot.action,
                snapshot.task_revision,
                snapshot.authorization_fingerprint,
                token_digest.version,
                token_digest.digest,
                _to_db_time(expires_at),
                timestamp,
                timestamp,
            ),
        )
        _ = live, task
        response = ApprovalStepUpResponse(
            step_up_token=token,
            expires_at=expires_at,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def _reload_step_up_owner(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    snapshot: _StepUpSnapshot,
) -> tuple[AuthContext, sqlite3.Row, datetime]:
    """在写事务中重验 step-up 所有者、密码版本和任务状态。"""
    actor = connection.execute(
        """
        SELECT password_hash FROM users
        WHERE tenant_id=? AND user_id=? AND status='active'
        """,
        (snapshot.tenant_id, snapshot.user_id),
    ).fetchone()
    current_password_hash = actor["password_hash"] if actor is not None else None
    if (
        type(current_password_hash) is not str
        or not current_password_hash
        or not hmac.compare_digest(current_password_hash, snapshot.password_hash)
    ):
        # PBKDF2 运行期间若账号失效或改密，旧密码结果不得签票或累计失败次数。
        raise ApiError("approval.step_up_invalid")
    live = _refresh_decider(connection, context)
    if (
        live.user.tenant_id != snapshot.tenant_id
        or live.user.user_id != snapshot.user_id
        or live.session.session_id != snapshot.session_id
        or _authorization_fingerprint(live) != snapshot.authorization_fingerprint
    ):
        # 二次验证票据绑定租户、用户、会话、动作、修订和时效；原文不得落库或回显。
        raise ApiError("approval.step_up_invalid")
    task = _load_task(connection, snapshot.tenant_id, snapshot.approval_id)
    if task is None or not _is_target_visible(live, task):
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    _require_task_writer(live, task)
    now = _database_now(connection)
    _require_pending_high_task(task, now)
    if _positive_int(task["state_revision"]) != snapshot.task_revision:
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    _require_target_unchanged(connection, config, task)
    return live, task, now


def _refresh_reader(connection: sqlite3.Connection, context: AuthContext) -> AuthContext:
    """刷新认证事实并要求当前租户具备审批读取权限。"""
    live = refresh_transaction_authentication(
        connection,
        context,
        unavailable_error_code="approval.unavailable",
    )
    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return require_permission(live, "approval.read")


def _refresh_decider(connection: sqlite3.Connection, context: AuthContext) -> AuthContext:
    """刷新认证事实并要求当前租户具备审批决策权限。"""
    live = _refresh_reader(connection, context)
    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return require_permission(live, "approval.decide")


def _require_task_writer(context: AuthContext, task: sqlite3.Row) -> None:
    """要求决策者具备目标任务声明的写权限。"""
    require_permission(context, _text(task["required_permission_code"]))


def _require_pending_high_task(task: sqlite3.Row, now: datetime) -> None:
    """要求任务仍为未过期的高风险 pending 状态。"""
    if task["status"] != "pending" or task["risk_level"] != "high":
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    if _parse_time(task["expires_at"]) <= now:
        # 到期判断只信任数据库时钟与持久化终态；过期任务不得继续换票或提交用户决策。
        raise ApiError("approval.expired")


def _load_step_up_guard(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    approval_id: str,
    user_id: str,
) -> sqlite3.Row | None:
    """按租户、审批和用户读取 step-up 限流状态。"""
    return connection.execute(
        """
        SELECT * FROM approval_step_up_guards
        WHERE tenant_id=? AND approval_task_id=? AND user_id=?
        """,
        (tenant_id, approval_id, user_id),
    ).fetchone()


def _guard_is_locked(guard: sqlite3.Row, now: datetime) -> bool:
    """按数据库权威时间判断 step-up 限流锁是否仍有效，避免客户端时钟绕过失败次数。"""
    return (
        _nonnegative_int(guard["failed_attempts"]) == 5
        and guard["locked_until"] is not None
        and _parse_time(guard["locked_until"]) > now
    )


def _require_target_unchanged(
    connection: sqlite3.Connection,
    config: AppConfig,
    task: sqlite3.Row,
) -> ApprovalTarget:
    """确认审批目标版本和安全指纹均未变化。"""
    target = load_task_target(connection, task)
    if target.target_version != _text(task["target_version"]):
        # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
        raise ApiError("approval.target_conflict")
    version = _positive_int(task["create_hmac_key_version"])
    digest = _digest_for_version(
        _keyring(config),
        TARGET_FINGERPRINT_DOMAIN,
        _target_fingerprint_payload(_text(task["tenant_id"]), target),
        version,
    )
    if digest is None or not hmac.compare_digest(
        digest,
        _text(task["target_fingerprint"]),
    ):
        # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
        raise ApiError("approval.target_conflict")
    return target


def _digest_for_version(
    keyring: ApprovalKeyring,
    domain: str | bytes,
    payload: Mapping[str, object],
    version: int,
) -> str | None:
    """读取指定密钥版本对应的摘要。"""
    for candidate in keyring.digest_candidates(domain, payload):
        if candidate.version == version:
            return candidate.digest
    return None


def _step_up_payload(
    token: str,
    *,
    tenant_id: str,
    user_id: str,
    session_id: str,
    approval_id: str,
    action: str,
    task_revision: int,
    authorization_fingerprint: str,
) -> dict[str, object]:
    """构造完整绑定安全坐标的 step-up 摘要载荷。"""
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "session_id": session_id,
        "approval_id": approval_id,
        "action": action,
        "task_revision": task_revision,
        "authorization_fingerprint": authorization_fingerprint,
        "token": token,
    }


def decide_approval(
    config: AppConfig,
    context: AuthContext,
    approval_id: str,
    *,
    action: str,
    payload: ApprovalDecisionRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ApprovalDecisionResponse:
    """以一次写事务完成实时授权、幂等、票据、目标回写与终态审计。"""

    normalized_id = _approval_id(approval_id)
    normalized_action = _decision_action(action)
    reason_code = _decision_reason(normalized_action, payload.reason_code)
    normalized_key = _idempotency_key(idempotency_key)
    keyring = _keyring(config)
    key_payload = {
        "tenant_id": context.user.tenant_id,
        "actor_user_id": context.user.user_id,
        "idempotency_key": normalized_key,
    }
    key_candidates = keyring.digest_candidates(DECISION_KEY_DOMAIN, key_payload)
    intent_payload = _decision_intent_payload(
        tenant_id=context.user.tenant_id,
        actor_user_id=context.user.user_id,
        approval_id=normalized_id,
        action=normalized_action,
        expected_revision=payload.expected_revision,
        reason_code=reason_code,
    )
    intent_candidates = {
        item.version: item.digest
        for item in keyring.digest_candidates(DECISION_INTENT_DOMAIN, intent_payload)
    }
    current_key = key_candidates[0]
    current_intent = intent_candidates.get(current_key.version)
    if current_intent is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")

    connection = _connect(config)
    sync_intent: object | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _refresh_decider(connection, context)
        task = _load_task(connection, live.user.tenant_id, normalized_id)
        if task is None or not _is_target_visible(live, task):
            # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
            raise ApiError("approval.not_found")
        _require_task_writer(live, task)

        now = _database_now(connection)
        replay = _find_decision_replay(
            connection,
            tenant_id=live.user.tenant_id,
            actor_user_id=live.user.user_id,
            key_candidates=key_candidates,
            now=now,
        )
        if replay is not None:
            expected = intent_candidates.get(_positive_int(replay["hmac_key_version"]))
            if expected is None or not hmac.compare_digest(
                expected,
                _text(replay["intent_hmac"]),
            ):
                # 同一幂等摘要只能恢复完全相同的审批意图；摘要版本或内容不一致时拒绝覆盖既有事实。
                raise ApiError("approval.idempotency_conflict")
            decision = _load_decision(
                connection,
                live.user.tenant_id,
                _text(replay["approval_decision_id"]),
            )
            replay_task = _load_task(connection, live.user.tenant_id, normalized_id)
            if decision is None or replay_task is None:
                # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
                raise ApiError("approval.unavailable")
            response = _decision_response(
                replay_task,
                decision,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response

        if task["status"] != "pending":
            # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
            raise ApiError("approval.state_conflict")
        if _parse_time(task["expires_at"]) <= now:
            _timeout_task_in_transaction(
                connection,
                task,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            connection.commit()
            # 到期判断只信任数据库时钟与持久化终态；过期任务不得继续换票或提交用户决策。
            raise ApiError("approval.expired")
        if _positive_int(task["state_revision"]) != payload.expected_revision:
            # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
            raise ApiError("approval.state_conflict")
        _require_target_unchanged(connection, config, task)
        step_up_used = _consume_step_up(
            connection,
            config,
            live,
            task,
            action=normalized_action,
            token=payload.step_up_token,
            now=now,
        )

        applied = None
        if normalized_action == "approve":
            applied = apply_approved_target(
                connection,
                config,
                live,
                task,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
            sync_intent = applied.sync_intent
            writeback_status = (
                "pending" if applied.adapter_kind == "session_risk_store_sync" else "completed"
            )
            task_status = "approved"
            resolution_reason = "approved"
        else:
            apply_rejected_target(connection, task, timeout=False, now=now)
            writeback_status = "completed"
            task_status = "rejected"
            resolution_reason = "user_rejected"
        timestamp = _to_db_time(now)
        revision_before = _positive_int(task["state_revision"])
        cursor = connection.execute(
            """
            UPDATE approval_tasks
            SET status=?,resolution_reason=?,state_revision=state_revision+1,
                writeback_status=?,decided_by_user_id=?,decided_at=?,
                decision_request_id=?,decision_trace_id=?,updated_at=?
            WHERE tenant_id=? AND approval_task_id=? AND status='pending'
              AND state_revision=? AND unixepoch(expires_at)>unixepoch(?)
            """,
            (
                task_status,
                resolution_reason,
                writeback_status,
                live.user.user_id,
                timestamp,
                request_id,
                trace_id,
                timestamp,
                live.user.tenant_id,
                normalized_id,
                revision_before,
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
            raise ApiError("approval.state_conflict")
        decision_id = f"apd_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO approval_decisions(
              approval_decision_id,tenant_id,approval_task_id,decision,reason_code,
              actor_type,actor_id,revision_before,revision_after,step_up_used,
              writeback_status,request_id,trace_id,decided_at
            ) VALUES(?,?,?,?,?,'user',?,?,?,?,?,?,?,?)
            """,
            (
                decision_id,
                live.user.tenant_id,
                normalized_id,
                normalized_action,
                reason_code,
                live.user.user_id,
                revision_before,
                revision_before + 1,
                int(step_up_used),
                writeback_status,
                request_id,
                trace_id,
                timestamp,
            ),
        )
        _insert_writeback(
            connection,
            tenant_id=live.user.tenant_id,
            approval_id=normalized_id,
            action=normalized_action,
            applied=applied,
            now=now,
        )
        connection.execute(
            """
            INSERT INTO approval_action_replays(
              approval_action_replay_id,tenant_id,approval_task_id,actor_user_id,
              action,expected_revision,reason_code,hmac_key_version,
              idempotency_key_hmac,intent_hmac,approval_decision_id,expires_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"aprep_{uuid4().hex}",
                live.user.tenant_id,
                normalized_id,
                live.user.user_id,
                normalized_action,
                revision_before,
                reason_code,
                current_key.version,
                current_key.digest,
                current_intent,
                decision_id,
                _to_db_time(now + REPLAY_TTL),
                timestamp,
            ),
        )
        final_task = _load_task(connection, live.user.tenant_id, normalized_id)
        decision = _load_decision(connection, live.user.tenant_id, decision_id)
        if final_task is None or decision is None:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        response = _decision_response(
            final_task,
            decision,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError as exc:
        if connection.in_transaction:
            _rollback(connection)
        raise exc
    # 数据库唯一性或外键冲突表示并发状态已变化；事务回滚后仅返回固定状态冲突错误。
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
        raise ApiError("approval.state_conflict") from exc
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError, ApprovalContractError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()
    if sync_intent is not None:
        _apply_sync_intent_best_effort(sync_intent)
    return response


def timeout_one_due_approval(
    config: AppConfig,
    lease: SchedulerTenantLease,
    *,
    request_id: str,
    trace_id: str,
    next_scan_delay_seconds: int,
) -> TimeoutResult | None:
    """在同一事务复核 tenant lease、终结一条到期任务并释放 lease。"""

    owner_tenant = _bounded_text(lease.tenant_id, maximum=160)
    if (
        type(lease.lease_generation) is not int
        or lease.lease_generation < 1
        or not lease.lease_owner_id
        or not lease.lease_token
        or lease.lease_expires_at.tzinfo is None
        or type(next_scan_delay_seconds) is not int
        or not 1 <= next_scan_delay_seconds <= 60
    ):
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        lease_row = connection.execute(
            """
            SELECT 1 FROM approval_scheduler_tenants
            WHERE tenant_id=? AND status='leased' AND lease_generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                owner_tenant,
                lease.lease_generation,
                lease.lease_owner_id,
                lease.lease_token,
                _to_db_time(lease.lease_expires_at),
                _to_db_time(now),
            ),
        ).fetchone()
        if lease_row is None:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        task = connection.execute(
            """
            SELECT * FROM approval_tasks
            WHERE tenant_id=? AND status='pending'
              AND unixepoch(expires_at)<=unixepoch(?)
            ORDER BY expires_at,approval_task_id LIMIT 1
            """,
            (owner_tenant, _to_db_time(now)),
        ).fetchone()
        if task is None:
            _release_scheduler_lease(
                connection,
                lease,
                now=now,
                next_scan_delay_seconds=next_scan_delay_seconds,
            )
            connection.commit()
            return None
        event_id = _timeout_task_in_transaction(
            connection,
            task,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _release_scheduler_lease(
            connection,
            lease,
            now=now,
            next_scan_delay_seconds=next_scan_delay_seconds,
        )
        result = TimeoutResult(
            tenant_id=owner_tenant,
            approval_id=_text(task["approval_task_id"]),
            event_id=event_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库唯一性或外键冲突表示并发状态已变化；事务回滚后仅返回固定状态冲突错误。
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
        raise ApiError("approval.state_conflict") from exc
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def _release_scheduler_lease(
    connection: sqlite3.Connection,
    lease: SchedulerTenantLease,
    *,
    now: datetime,
    next_scan_delay_seconds: int,
) -> None:
    """只有当前且未过期的 owner 能在 timeout 事务末尾释放 tenant lease。"""

    timestamp = _to_db_time(now)
    next_scan = _to_db_time(now + timedelta(seconds=next_scan_delay_seconds))
    cursor = connection.execute(
        """
        UPDATE approval_scheduler_tenants
        SET status='idle',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_scan_at=?,updated_at=?
        WHERE tenant_id=? AND status='leased' AND lease_generation=?
          AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            next_scan,
            timestamp,
            lease.tenant_id,
            lease.lease_generation,
            lease.lease_owner_id,
            lease.lease_token,
            _to_db_time(lease.lease_expires_at),
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")


def _consume_step_up(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    task: sqlite3.Row,
    *,
    action: Literal["approve", "reject"],
    token: str | None,
    now: datetime,
) -> bool:
    """校验并原子消费当前决策绑定的 step-up 票据。"""
    high_risk = task["risk_level"] == "high"
    if not high_risk:
        # 令牌只允许在短生命周期内做摘要和所有权校验；缺失、额外出现或不匹配均固定失败。
        if token is not None:
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ApiError("approval.input_invalid")
        return False
    # 令牌只允许在短生命周期内做摘要和所有权校验；缺失、额外出现或不匹配均固定失败。
    if token is None:
        # 高风险审批必须提交服务端签发的一次性二次验证票据，前端可见按钮不能绕过该要求。
        raise ApiError("approval.step_up_required")
    versioned_rows = connection.execute(
        """
        SELECT * FROM approval_step_up_challenges
        WHERE tenant_id=? AND approval_task_id=? AND user_id=? AND session_id=?
          AND action=? AND status='active'
        ORDER BY created_at DESC,approval_challenge_id DESC
        """,
        (
            context.user.tenant_id,
            _text(task["approval_task_id"]),
            context.user.user_id,
            context.session.session_id,
            action,
        ),
    ).fetchall()
    matched: sqlite3.Row | None = None
    keyring = _keyring(config)
    binding = _step_up_payload(
        token,
        tenant_id=context.user.tenant_id,
        user_id=context.user.user_id,
        session_id=context.session.session_id,
        approval_id=_text(task["approval_task_id"]),
        action=action,
        task_revision=_positive_int(task["state_revision"]),
        authorization_fingerprint=_authorization_fingerprint(context),
    )
    for row in versioned_rows:
        expected = _digest_for_version(
            keyring,
            STEP_UP_DOMAIN,
            binding,
            _positive_int(row["hmac_key_version"]),
        )
        # 令牌只允许在短生命周期内做摘要和所有权校验；缺失、额外出现或不匹配均固定失败。
        if expected is not None and hmac.compare_digest(expected, _text(row["token_hmac"])):
            matched = row
            break
    if (
        matched is None
        or _positive_int(matched["task_revision"]) != _positive_int(task["state_revision"])
        or _text(matched["authorization_fingerprint"]) != _authorization_fingerprint(context)
        or _parse_time(matched["expires_at"]) <= now
    ):
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    timestamp = _to_db_time(now)
    cursor = connection.execute(
        """
        UPDATE approval_step_up_challenges
        SET status='consumed',consumed_at=?,updated_at=?
        WHERE tenant_id=? AND approval_challenge_id=? AND status='active'
          AND unixepoch(expires_at)>unixepoch(?)
        """,
        (
            timestamp,
            timestamp,
            context.user.tenant_id,
            _text(matched["approval_challenge_id"]),
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        # 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
        raise ApiError("approval.step_up_invalid")
    return True


def _timeout_task_in_transaction(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    """在当前事务内将到期任务推进为唯一 timeout 终态。"""
    tenant_id = _text(task["tenant_id"])
    approval_id = _text(task["approval_task_id"])
    revision_before = _positive_int(task["state_revision"])
    timestamp = _to_db_time(now)
    apply_rejected_target(connection, task, timeout=True, now=now)
    cursor = connection.execute(
        """
        UPDATE approval_tasks
        SET status='rejected',resolution_reason='timeout',
            state_revision=state_revision+1,writeback_status='completed',
            decided_by_user_id=NULL,decided_at=?,decision_request_id=?,
            decision_trace_id=?,updated_at=?
        WHERE tenant_id=? AND approval_task_id=? AND status='pending'
          AND state_revision=? AND unixepoch(expires_at)<=unixepoch(?)
        """,
        (
            timestamp,
            request_id,
            trace_id,
            timestamp,
            tenant_id,
            approval_id,
            revision_before,
            timestamp,
        ),
    )
    if cursor.rowcount != 1:
        # 审批修订或租约状态已被并发事务推进时必须失败，禁止用旧快照覆盖新的权威状态。
        raise ApiError("approval.state_conflict")
    connection.execute(
        """
        INSERT INTO approval_decisions(
          approval_decision_id,tenant_id,approval_task_id,decision,reason_code,
          actor_type,actor_id,revision_before,revision_after,step_up_used,
          writeback_status,request_id,trace_id,decided_at
        ) VALUES(?,?,?,'timeout','timeout','system',?,?,?,0,'completed',?,?,?)
        """,
        (
            f"apd_{uuid4().hex}",
            tenant_id,
            approval_id,
            TIMEOUT_ACTOR_ID,
            revision_before,
            revision_before + 1,
            request_id,
            trace_id,
            timestamp,
        ),
    )
    _insert_writeback(
        connection,
        tenant_id=tenant_id,
        approval_id=approval_id,
        action="timeout",
        applied=None,
        now=now,
    )
    final_task = _load_task(connection, tenant_id, approval_id)
    if final_task is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return _insert_timeout_event(
        connection,
        final_task,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _insert_writeback(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    approval_id: str,
    action: str,
    applied: object | None,
    now: datetime,
) -> None:
    """为批准决策写入租户限定的目标回写任务。"""
    timestamp = _to_db_time(now)
    if action != "approve":
        adapter_kind = "no_effect"
        target_fact_id = None
        target_revision = None
        status = "completed"
    else:
        if applied is None:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        adapter_kind = str(getattr(applied, "adapter_kind", ""))
        target_fact_id = getattr(applied, "target_fact_id", None)
        target_revision = getattr(applied, "target_revision", None)
        status = "pending" if adapter_kind == "session_risk_store_sync" else "completed"
    connection.execute(
        """
        INSERT INTO approval_writebacks(
          approval_writeback_id,tenant_id,approval_task_id,adapter_kind,
          target_fact_id,target_revision,status,attempt_count,lease_generation,
          lease_owner_id,lease_token,lease_expires_at,next_attempt_at,last_error_code,
          completed_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,0,0,NULL,NULL,NULL,?,NULL,?,?,?)
        """,
        (
            f"apwb_{uuid4().hex}",
            tenant_id,
            approval_id,
            adapter_kind,
            target_fact_id,
            target_revision,
            status,
            timestamp if status == "pending" else None,
            timestamp if status == "completed" else None,
            timestamp,
            timestamp,
        ),
    )


def _apply_sync_intent_best_effort(sync_intent: object) -> None:
    """SQLite 已提交后才尝试 Redis 投影；worker 仍以持久 writeback 为恢复根。"""

    apply_method = getattr(sync_intent, "apply", None)
    if callable(apply_method):
        try:
            apply_method()
        except Exception:
            return


def _canonical_create(payload: CreateApprovalRequest) -> dict[str, object]:
    """严格规范化内部创建请求，只保留固定字段并限制标识、原因、时限和幂等键。"""
    values = payload.model_dump(mode="python")
    expected = {
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "target_action",
        "target_reason_code",
        "expires_in_seconds",
        "idempotency_key",
    }
    if set(values) != expected:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    for name in {"source_type", "source_id", "target_type", "target_id", "target_action"}:
        values[name] = _bounded_text(values[name], maximum=160)
    reason = values["target_reason_code"]
    if reason is not None:
        values["target_reason_code"] = _bounded_text(reason, maximum=64)
    ttl = values["expires_in_seconds"]
    if type(ttl) is not int or not 300 <= ttl <= 86_400:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    values["idempotency_key"] = _idempotency_key(values["idempotency_key"])
    return values


def _decision_intent_payload(
    *,
    tenant_id: str,
    actor_user_id: str,
    approval_id: str,
    action: str,
    expected_revision: int,
    reason_code: str,
) -> dict[str, object]:
    """构造租户限定的决策意图摘要载荷。"""
    return {
        "tenant_id": tenant_id,
        "actor_user_id": actor_user_id,
        "approval_id": approval_id,
        "action": action,
        "expected_revision": expected_revision,
        "reason_code": reason_code,
    }


def _target_fingerprint_payload(
    tenant_id: str,
    target: ApprovalTarget,
) -> dict[str, object]:
    """构造租户限定的审批目标安全指纹载荷。"""
    return {
        "tenant_id": tenant_id,
        "target_type": target.target_type,
        "target_id": target.target_id,
        "target_action": target.target_action,
        "target_reason_code": target.target_reason_code,
        "target_version": target.target_version,
        "fingerprint_parts": list(target.fingerprint_parts),
    }


def _keyring(config: AppConfig) -> ApprovalKeyring:
    """``_keyring`` 维护审批权威事务状态，所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。"""
    try:
        current_secret = config.approval_hmac_secret
        # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
        if current_secret is None:
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid")
        previous = None
        if (
            config.approval_previous_hmac_version is not None
            and config.approval_previous_hmac_secret is not None
        ):
            previous = VersionedApprovalSecret(
                config.approval_previous_hmac_version,
                config.approval_previous_hmac_secret,
            )
        return ApprovalKeyring(
            current=VersionedApprovalSecret(config.approval_hmac_version, current_secret),
            previous=previous,
        )
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (AttributeError, TypeError, ValueError, ApprovalContractError) as exc:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc


def _find_create_replay(
    connection: sqlite3.Connection,
    tenant_id: str,
    key_candidates: tuple[object, ...],
) -> sqlite3.Row | None:
    """按轮换密钥摘要查找创建幂等回放。"""
    for candidate in key_candidates:
        version = getattr(candidate, "version", None)
        digest = getattr(candidate, "digest", None)
        if type(version) is not int or type(digest) is not str:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        row = connection.execute(
            """
            SELECT * FROM approval_tasks
            WHERE tenant_id=? AND create_hmac_key_version=?
              AND create_idempotency_key_hmac=?
            """,
            (tenant_id, version, digest),
        ).fetchone()
        if row is not None:
            return row
    return None


def _find_decision_replay(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    key_candidates: tuple[object, ...],
    now: datetime,
) -> sqlite3.Row | None:
    """按数据库时钟查找仍在 24 小时窗口内的决策幂等回放。"""
    timestamp = _to_db_time(now)
    for candidate in key_candidates:
        version = getattr(candidate, "version", None)
        digest = getattr(candidate, "digest", None)
        if type(version) is not int or type(digest) is not str:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        row = connection.execute(
            """
            SELECT * FROM approval_action_replays
            WHERE tenant_id=? AND actor_user_id=? AND hmac_key_version=?
              AND idempotency_key_hmac=?
              AND unixepoch(expires_at)>unixepoch(?)
            ORDER BY created_at DESC,approval_action_replay_id DESC
            LIMIT 1
            """,
            (tenant_id, actor_user_id, version, digest, timestamp),
        ).fetchone()
        if row is not None:
            return row
    return None


def _load_task(
    connection: sqlite3.Connection,
    tenant_id: str,
    approval_id: str,
) -> sqlite3.Row | None:
    """按后端可信 tenant_id 与审批标识读取单条任务，禁止跨租户枚举或回退查询。"""
    return connection.execute(
        "SELECT * FROM approval_tasks WHERE tenant_id=? AND approval_task_id=?",
        (tenant_id, approval_id),
    ).fetchone()


def _load_decision(
    connection: sqlite3.Connection,
    tenant_id: str,
    decision_id: str,
) -> sqlite3.Row | None:
    """按后端可信 tenant_id 与决策标识读取单条事实，禁止跨租户回放或探测。"""
    return connection.execute(
        "SELECT * FROM approval_decisions WHERE tenant_id=? AND approval_decision_id=?",
        (tenant_id, decision_id),
    ).fetchone()


def _insert_created_event(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    """在任务事务内写入脱敏审批创建事件及对应 outbox，并绑定原请求链路标识。"""
    # 请求链路标识必须与持久化来源事实一致；不一致视为数据污染并以固定错误终止处理。
    if request_id != task["source_request_id"] or trace_id != task["source_trace_id"]:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    payload = {
        "schema_version": 1,
        "approval_id": _text(task["approval_task_id"]),
        "status": "pending",
        "target_type": _text(task["target_type"]),
        "target_action": _text(task["target_action"]),
        "risk_level": _text(task["risk_level"]),
        "requires_step_up": task["risk_level"] == "high",
        "expires_at": _text(task["expires_at"]),
        "entry": "approval-card",
    }
    return _insert_event(
        connection,
        task,
        event_type="approval.created",
        payload=payload,
        actor_type=_text(task["created_by_type"]),
        actor_id=_text(task["created_by_id"]),
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _insert_timeout_event(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    """在超时收敛事务内写入脱敏终态事件及对应 outbox，保持任务与通知原子一致。"""
    payload = {
        "schema_version": 1,
        "approval_id": _text(task["approval_task_id"]),
        "status": "rejected",
        "target_type": _text(task["target_type"]),
        "target_action": _text(task["target_action"]),
        "resolution_reason": "timeout",
        "decided_at": _text(task["decided_at"]),
    }
    return _insert_event(
        connection,
        task,
        event_type="approval.timeout",
        payload=payload,
        actor_type="system",
        actor_id=TIMEOUT_ACTOR_ID,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _insert_event(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    event_type: str,
    payload: dict[str, object],
    actor_type: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str:
    """写入租户限定的不可变审批事件与投递快照。"""
    from metahub_shared.runtime.webhooks import build_webhook_event

    event_id = f"apevt_{uuid4().hex}"
    event = build_webhook_event(
        tenant_id=_text(task["tenant_id"]),
        event_type=event_type,
        payload=payload,
        actor_id=actor_id,
        object_id=_text(task["approval_task_id"]),
        trace_id=trace_id,
        source_request_id=request_id,
        event_id=event_id,
        timestamp=now,
    )
    timestamp = _to_db_time(now)
    payload_json = json.dumps(
        event.payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO approval_events(
          approval_event_id,tenant_id,approval_task_id,event_type,payload_json,
          actor_type,actor_id,source_request_id,source_trace_id,occurred_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            event.tenant_id,
            _text(task["approval_task_id"]),
            event_type,
            payload_json,
            actor_type,
            actor_id,
            request_id,
            trace_id,
            timestamp,
            timestamp,
        ),
    )
    from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

    freeze_webhook_event_delivery_snapshot(
        connection,
        event,
        frozen_at=timestamp,
    )
    delivery_rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY created_at,webhook_delivery_id
        """,
        (event.tenant_id, event_id),
    ).fetchall()
    snapshot_json = json.dumps(
        [_text(row["webhook_delivery_id"]) for row in delivery_rows],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO approval_event_outbox(
          approval_outbox_id,tenant_id,approval_event_id,status,attempt_count,
          lease_generation,lease_owner_id,lease_token,lease_expires_at,next_attempt_at,
          subscription_snapshot_json,snapshot_frozen_at,last_error_code,delivered_at,
          created_at,updated_at
        ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,NULL,NULL,?,?)
        """,
        (
            f"apob_{uuid4().hex}",
            event.tenant_id,
            event_id,
            timestamp,
            snapshot_json,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return event_id


def _summary(row: sqlite3.Row) -> ApprovalSummary:
    """``_summary`` 维护审批权威事务状态，所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。"""
    risk = _text(row["risk_level"])
    return ApprovalSummary(
        approval_id=_text(row["approval_task_id"]),
        target_type=_text(row["target_type"]),
        target_action=_text(row["target_action"]),
        risk_level=risk,
        status=_text(row["status"]),
        resolution_reason=(
            _text(row["resolution_reason"]) if row["resolution_reason"] is not None else None
        ),
        state_revision=_positive_int(row["state_revision"]),
        writeback_status=_text(row["writeback_status"]),
        required_permission_code=_text(row["required_permission_code"]),
        requires_step_up=risk == "high",
        created_at=_parse_time(row["created_at"]),
        expires_at=_parse_time(row["expires_at"]),
        decided_at=_optional_time(row["decided_at"]),
    )


def _decision_info(row: sqlite3.Row) -> ApprovalDecisionInfo:
    """将持久化决策映射为公开响应。"""
    return ApprovalDecisionInfo(
        decision_id=_text(row["approval_decision_id"]),
        decision=_text(row["decision"]),
        reason_code=_text(row["reason_code"]),
        actor_type=_text(row["actor_type"]),
        actor_id=_text(row["actor_id"]),
        revision_before=_positive_int(row["revision_before"]),
        revision_after=_positive_int(row["revision_after"]),
        step_up_used=row["step_up_used"] == 1,
        writeback_status=_text(row["writeback_status"]),
        created_at=_parse_time(row["decided_at"]),
    )


def _decision_response(
    task: sqlite3.Row,
    decision: sqlite3.Row,
    *,
    idempotent_replay: bool,
    request_id: str,
    trace_id: str,
) -> ApprovalDecisionResponse:
    """构造带链路标识的审批决策响应。"""
    return ApprovalDecisionResponse(
        approval=_summary(task),
        decision=_decision_info(decision),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _is_target_visible(context: AuthContext, task: sqlite3.Row) -> bool:
    """按后端实时权限判断目标域是否可见。"""
    permission = _READ_PERMISSION.get(str(task["target_type"]))
    # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    return permission is not None and permission in context.permission_codes


def _query_status(value: str | None) -> str:
    """规范化并校验审批状态筛选。"""
    normalized = "pending" if value is None else value
    if normalized not in VALID_STATUSES:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return normalized


def _query_target_type(value: str | None) -> str | None:
    """规范化并校验审批目标类型筛选。"""
    if value is None:
        return None
    if value not in VALID_TARGET_TYPES:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return value


def _query_limit(value: str | None) -> int:
    """规范化并约束审批列表页大小，拒绝符号、非 ASCII 数字和超出服务预算的值。"""
    if value is None:
        return DEFAULT_PAGE_SIZE
    if not value.isascii() or not value.isdigit():
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    parsed = int(value)
    if not 1 <= parsed <= MAX_PAGE_SIZE:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return parsed


def _encode_cursor(
    keyring: ApprovalKeyring,
    *,
    tenant_id: str,
    authorization_fingerprint: str,
    status: str,
    target_type: str | None,
    allowed_types: tuple[str, ...],
    snapshot: str,
    snapshot_rowid: int,
    before_time: str,
    before_id: str,
) -> str:
    """签名编码绑定授权与快照水位的分页游标。"""
    payload: dict[str, object] = {
        "v": CURSOR_VERSION,
        "tenant_id": tenant_id,
        "authorization_fingerprint": authorization_fingerprint,
        "status": status,
        "target_type": target_type,
        "allowed_types": list(allowed_types),
        "snapshot": snapshot,
        "snapshot_rowid": _nonnegative_int(snapshot_rowid),
        "before_time": before_time,
        "before_id": before_id,
    }
    digest = keyring.digest(CURSOR_DOMAIN, payload)
    envelope = {"key_version": digest.version, "payload": payload, "signature": digest.digest}
    encoded = canonical_approval_bytes(envelope)
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_cursor(
    keyring: ApprovalKeyring,
    value: str,
    *,
    tenant_id: str,
    authorization_fingerprint: str,
    status: str,
    target_type: str | None,
    allowed_types: tuple[str, ...],
) -> _CursorState:
    """严格验签并解析绑定当前授权的分页游标。"""
    try:
        if not 32 <= len(value) <= 4096 or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("utf-8"))
        if type(decoded) is not dict or set(decoded) != {"key_version", "payload", "signature"}:
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError
        payload = decoded["payload"]
        expected_fields = {
            "v",
            "tenant_id",
            "authorization_fingerprint",
            "status",
            "target_type",
            "allowed_types",
            "snapshot",
            "snapshot_rowid",
            "before_time",
            "before_id",
        }
        if type(payload) is not dict or set(payload) != expected_fields:
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError
        version = decoded["key_version"]
        expected = _digest_for_version(keyring, CURSOR_DOMAIN, payload, version)
        if (
            type(decoded["signature"]) is not str
            or expected is None
            or not hmac.compare_digest(decoded["signature"], expected)
        ):
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError
        if (
            payload["v"] != CURSOR_VERSION
            or payload["tenant_id"] != tenant_id
            or payload["authorization_fingerprint"] != authorization_fingerprint
            or payload["status"] != status
            or payload["target_type"] != target_type
            or payload["allowed_types"] != list(allowed_types)
            or type(payload["snapshot_rowid"]) is not int
            or payload["snapshot_rowid"] < 0
            or APPROVAL_ID_PATTERN.fullmatch(str(payload["before_id"])) is None
        ):
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError
        snapshot = _to_db_time(_parse_time(payload["snapshot"]))
        before_time = _to_db_time(_parse_time(payload["before_time"]))
        return {
            "snapshot": snapshot,
            "snapshot_rowid": payload["snapshot_rowid"],
            "before_time": before_time,
            "before_id": str(payload["before_id"]),
        }
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, ApprovalContractError):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid") from None


def _approval_id(value: object) -> str:
    """校验不泄露存在性的审批标识。"""
    normalized = str(value or "")
    if APPROVAL_ID_PATTERN.fullmatch(normalized) is None:
        # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
        raise ApiError("approval.not_found")
    return normalized


def _decision_action(value: object) -> Literal["approve", "reject"]:
    """校验审批决策动作只属于固定 approve/reject 集合，拒绝任意目标方法名。"""
    if value == "approve":
        return "approve"
    if value == "reject":
        return "reject"
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    raise ApiError("approval.input_invalid")


def _decision_reason(action: str, value: object) -> str:
    """按批准或拒绝动作校验固定原因码矩阵，禁止客户端提交任意审计正文。"""
    reason = str(value or "")
    if reason not in _DECISION_REASON_MATRIX.get(action, frozenset()):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return reason


def _idempotency_key(value: object) -> str:
    """严格校验决策幂等键格式和长度，原值只用于派生摘要且不得持久化或记录。"""
    normalized = str(value or "")
    if IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized) is None:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return normalized


def _bounded_text(value: object, *, maximum: int) -> str:
    """严格校验内部标识为有界非空文本，并拒绝可改变底层解析语义的 NUL 字节。"""
    if type(value) is not str or not 1 <= len(value) <= maximum or "\x00" in value:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return value


def _authorization_fingerprint(context: AuthContext) -> str:
    """读取并校验后端生成的授权指纹。"""
    value = context.authorization_fingerprint
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _text(value: object) -> str:
    """``_text`` 维护审批权威事务状态，所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。"""
    if type(value) is not str or not value:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _positive_int(value: object) -> int:
    """读取持久化正整数并拒绝布尔值、零和负数，防止损坏版本进入治理计算。"""
    if type(value) is not int or value < 1:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _nonnegative_int(value: object) -> int:
    """读取持久化非负整数并拒绝布尔值与负数，防止损坏计数进入状态判断。"""
    if type(value) is not int or value < 0:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _parse_time(value: object) -> datetime:
    """解析持久化的带时区时间并统一转为 UTC，拒绝依赖主机本地时区的歧义值。"""
    if type(value) is not str:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("time invalid")
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    """解析可空的持久化时间；非空值继续遵守带时区和 UTC 规范化约束。"""
    return None if value is None else _parse_time(value)


def _to_db_time(value: datetime) -> str:
    """将时区时间规范化为数据库 UTC 秒精度格式。"""
    if value.tzinfo is None:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("time invalid")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _database_now(connection: sqlite3.Connection) -> datetime:
    """从当前事务连接读取数据库权威 UTC 时钟，避免客户端或应用时钟决定任务终态。"""
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return _parse_time(row[0])


def _connect(config: AppConfig) -> sqlite3.Connection:
    """``_connect`` 维护审批权威事务状态，所有查询和写入都必须绑定可信 tenant_id 与当前权限快照。"""
    try:
        return connect_database(config.database_url)
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except DbError as exc:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前数据库事务；回滚自身失败不得覆盖对外固定的审批错误语义。"""
    try:
        if connection.in_transaction:
            connection.rollback()
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except sqlite3.Error:
        return


__all__ = [
    "SchedulerTenantLease",
    "TimeoutResult",
    "create_approval",
    "decide_approval",
    "get_approval",
    "issue_step_up",
    "list_approvals",
    "timeout_one_due_approval",
]
