"""PRD-078 审批目标注册表与同事务回写适配器。

本模块只接受服务端恢复的 tenant、任务和认证上下文。目标版本、指纹材料、权限和回写
类型全部由固定注册表派生；调用方输入不能选择任意 SQL、函数、URL 或目标正文。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from metahub_shared.runtime.budget_blocks import release_active_budget_blocks
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext
from metahub_control.session_risks.service import (
    SessionRiskStoreSyncIntent,
    _action_digest_candidates,
    _build_store_sync_intent,
    _execute_action,
    _load_score,
)

TargetType = Literal["budget_block_release", "session_risk_action", "mcp_tool_invocation"]

# Session Risk 回写使用独立 HMAC 域和固定理由矩阵，避免摘要跨用途复用或客户端自造动作。
_SESSION_APPROVAL_ACTION_KEY_DOMAIN = b"metahub.session-risk.approval-action-idempotency.v1"
_SESSION_APPROVAL_ACTION_INTENT_DOMAIN = b"metahub.session-risk.approval-action-intent.v1"
_SESSION_ACTION_REASON_MATRIX = {
    "freeze_session": frozenset({"confirmed_attack"}),
    "freeze_virtual_key": frozenset({"confirmed_attack"}),
    "dismiss": frozenset({"false_positive", "verified_safe"}),
    "release_session": frozenset({"false_positive", "incident_contained"}),
    "release_virtual_key": frozenset({"false_positive", "incident_contained"}),
}


@dataclass(frozen=True, slots=True)
class ApprovalTarget:
    """服务端冻结的一条审批目标，不包含可公开业务正文。"""

    target_type: TargetType
    target_id: str
    target_action: str
    target_reason_code: str | None
    target_version: str
    fingerprint_parts: tuple[str, ...]
    risk_level: Literal["standard", "high"]
    read_permission_code: str
    write_permission_code: str


@dataclass(frozen=True, slots=True)
class AppliedTarget:
    """批准事务产生的持久目标坐标和可选 Redis 对账意图。"""

    adapter_kind: Literal[
        "budget_block_release", "session_risk_store_sync", "mcp_invocation_enqueue"
    ]
    target_fact_id: str | None
    target_revision: int | None
    sync_intent: SessionRiskStoreSyncIntent | None = None


def load_target(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    target_action: str,
    target_reason_code: str | None,
) -> ApprovalTarget:
    """按固定注册表读取目标并生成 canonical 指纹材料。"""

    if target_type == "budget_block_release":
        return _load_budget_target(
            connection,
            tenant_id=tenant_id,
            source_type=source_type,
            source_id=source_id,
            target_id=target_id,
            target_action=target_action,
            target_reason_code=target_reason_code,
        )
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    if target_type == "session_risk_action":
        # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
        return _load_session_risk_target(
            connection,
            tenant_id=tenant_id,
            source_type=source_type,
            source_id=source_id,
            target_id=target_id,
            target_action=target_action,
            target_reason_code=target_reason_code,
        )
    if target_type == "mcp_tool_invocation":
        return _load_mcp_target(
            connection,
            tenant_id=tenant_id,
            source_type=source_type,
            source_id=source_id,
            target_id=target_id,
            target_action=target_action,
            target_reason_code=target_reason_code,
        )
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    raise ApiError("approval.input_invalid")


def load_task_target(connection: sqlite3.Connection, task: sqlite3.Row) -> ApprovalTarget:
    """只从持久 ApprovalTask owner 图重建目标，供读取、step-up 与决策复核。"""

    return load_target(
        connection,
        tenant_id=_text(task["tenant_id"]),
        source_type=_text(task["source_type"]),
        source_id=_text(task["source_id"]),
        target_type=_text(task["target_type"]),
        target_id=_text(task["target_id"]),
        target_action=_text(task["target_action"]),
        target_reason_code=(
            _text(task["target_reason_code"]) if task["target_reason_code"] is not None else None
        ),
    )


def apply_approved_target(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> AppliedTarget:
    """在调用方现有写事务中执行固定目标回写，不提交也不访问网络。"""

    target_type = _text(task["target_type"])
    if target_type == "budget_block_release":
        released = release_active_budget_blocks(
            connection,
            tenant_id=context.user.tenant_id,
            budget_rule_id=_text(task["target_id"]),
            reason="approval_approved",
            actor_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            now=_to_db_time(now),
        )
        if not released:
            # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
            raise ApiError("approval.target_conflict")
        # 预算没有异步投影；writeback 坐标只保留给可恢复的会话风险投影。
        return AppliedTarget(
            adapter_kind="budget_block_release",
            target_fact_id=None,
            target_revision=None,
        )
    if target_type == "mcp_tool_invocation":
        return _apply_mcp_target(
            connection,
            context,
            task,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    if target_type != "session_risk_action":
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    return _apply_session_risk_target(
        connection,
        config,
        context,
        task,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def apply_rejected_target(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    timeout: bool,
    now: datetime,
) -> None:
    """让 MCP 审批拒绝/超时与 Invocation 终态在同一事务收敛。"""

    if _text(task["target_type"]) != "mcp_tool_invocation":
        return
    error_code = "mcp.approval_timeout" if timeout else "mcp.approval_rejected"
    timestamp = _to_db_time(now)
    changed = connection.execute(
        """
        UPDATE mcp_invocations
        SET status='rejected',execution_status='not_called',invocation_count=1,
            tool_call_count=0,cost_status='not_applicable',cost_micros=NULL,
            error_code=?,completed_at=?,updated_at=?,state_revision=state_revision+1,
            lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=NULL
        WHERE tenant_id=? AND mcp_invocation_id=? AND status='approval_pending'
          AND approval_task_id=?
        """,
        (
            error_code,
            timestamp,
            timestamp,
            _text(task["tenant_id"]),
            _text(task["target_id"]),
            _text(task["approval_task_id"]),
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("approval.target_conflict")


def _load_mcp_target(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_type: str,
    source_id: str,
    target_id: str,
    target_action: str,
    target_reason_code: str | None,
) -> ApprovalTarget:
    """从 tenant-qualified Invocation 重建高危 MCP 审批指纹。"""

    if (
        source_type != "mcp"
        or source_id != target_id
        or target_action != "invoke"
        or target_reason_code is not None
    ):
        raise ApiError("approval.input_invalid")
    row = connection.execute(
        """
        SELECT invocation.*,server.state_revision AS current_server_revision,
               server.active_credential_revision AS current_credential_revision,
               server.status AS current_server_status,
               tool.schema_revision AS current_schema_revision,
               tool.policy_revision AS current_policy_revision,
               tool.status AS current_tool_status
        FROM mcp_invocations invocation
        JOIN mcp_servers server
          ON server.tenant_id=invocation.tenant_id
         AND server.mcp_server_id=invocation.mcp_server_id
        JOIN mcp_tools tool
          ON tool.tenant_id=invocation.tenant_id
         AND tool.mcp_server_id=invocation.mcp_server_id
         AND tool.mcp_tool_id=invocation.mcp_tool_id
        WHERE invocation.tenant_id=? AND invocation.mcp_invocation_id=?
        """,
        (tenant_id, target_id),
    ).fetchone()
    if row is None:
        raise ApiError("approval.not_found")
    if (
        row["status"] != "approval_pending"
        or row["risk_level"] != "high"
        or row["approval_required"] != 1
        or row["current_server_status"] != "active"
        or row["current_tool_status"] != "active"
        or row["server_state_revision"] != row["current_server_revision"]
        or row["credential_revision"] != row["current_credential_revision"]
        or row["schema_revision"] != row["current_schema_revision"]
        or row["policy_revision"] != row["current_policy_revision"]
    ):
        raise ApiError("approval.target_conflict")
    return build_mcp_approval_target(
        invocation_id=_text(row["mcp_invocation_id"]),
        tenant_id=_text(row["tenant_id"]),
        server_id=_text(row["mcp_server_id"]),
        tool_id=_text(row["mcp_tool_id"]),
        requested_by=_text(row["requested_by"]),
        server_state_revision=_positive_integer(row["server_state_revision"]),
        credential_revision=_positive_integer(row["credential_revision"]),
        schema_revision=_positive_integer(row["schema_revision"]),
        policy_revision=_positive_integer(row["policy_revision"]),
        input_schema_original_sha256=_text(row["input_schema_original_sha256"]),
        input_schema_effective_sha256=_text(row["input_schema_effective_sha256"]),
        output_schema_original_sha256=(
            _text(row["output_schema_original_sha256"])
            if row["output_schema_original_sha256"] is not None
            else None
        ),
        output_schema_effective_sha256=(
            _text(row["output_schema_effective_sha256"])
            if row["output_schema_effective_sha256"] is not None
            else None
        ),
        required_permissions_sha256=_text(row["required_permissions_sha256"]),
        grant_sha256=_text(row["grant_sha256"]),
        arguments_hmac=_text(row["arguments_hmac"]),
        arguments_hmac_key_version=_positive_integer(row["arguments_hmac_key_version"]),
        approval_deadline_at=_text(row["approval_deadline_at"]),
    )


def build_mcp_approval_target(
    *,
    invocation_id: str,
    tenant_id: str,
    server_id: str,
    tool_id: str,
    requested_by: str,
    server_state_revision: int,
    credential_revision: int,
    schema_revision: int,
    policy_revision: int,
    input_schema_original_sha256: str,
    input_schema_effective_sha256: str,
    output_schema_original_sha256: str | None,
    output_schema_effective_sha256: str | None,
    required_permissions_sha256: str,
    grant_sha256: str,
    arguments_hmac: str,
    arguments_hmac_key_version: int,
    approval_deadline_at: str,
) -> ApprovalTarget:
    """Build the one canonical approval target used before and after persistence."""

    version = ":".join(
        str(value)
        for value in (
            server_state_revision,
            credential_revision,
            schema_revision,
            policy_revision,
        )
    )
    fields = (
        invocation_id,
        tenant_id,
        server_id,
        tool_id,
        requested_by,
        input_schema_original_sha256,
        input_schema_effective_sha256,
        output_schema_original_sha256 or "",
        output_schema_effective_sha256 or "",
        required_permissions_sha256,
        grant_sha256,
        arguments_hmac,
        str(arguments_hmac_key_version),
        approval_deadline_at,
    )
    return ApprovalTarget(
        target_type="mcp_tool_invocation",
        target_id=invocation_id,
        target_action="invoke",
        target_reason_code=None,
        target_version=version,
        fingerprint_parts=("mcp_tool_invocation", version, *fields),
        risk_level="high",
        read_permission_code="mcp.read",
        write_permission_code="mcp.approve",
    )


def _apply_mcp_target(
    connection: sqlite3.Connection,
    context: AuthContext,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> AppliedTarget:
    """审批事务只排队 Invocation；不得解密参数或访问 MCP Endpoint。"""

    del request_id, trace_id
    tenant_id = context.user.tenant_id
    invocation_id = _text(task["target_id"])
    row = connection.execute(
        """
        SELECT requested_by,policy_revision,status,approval_task_id
        FROM mcp_invocations
        WHERE tenant_id=? AND mcp_invocation_id=?
        """,
        (tenant_id, invocation_id),
    ).fetchone()
    if row is None or row["status"] != "approval_pending":
        raise ApiError("approval.target_conflict")
    if row["requested_by"] == context.user.user_id:
        raise ApiError("mcp.approval_self_denied")
    if row["approval_task_id"] != task["approval_task_id"]:
        raise ApiError("approval.target_conflict")
    timestamp = _to_db_time(now)
    changed = connection.execute(
        """
        UPDATE mcp_invocations
        SET status='queued',queued_at=?,next_attempt_at=?,updated_at=?,
            state_revision=state_revision+1
        WHERE tenant_id=? AND mcp_invocation_id=? AND status='approval_pending'
          AND approval_task_id=? AND requested_by!=?
        """,
        (
            timestamp,
            timestamp,
            timestamp,
            tenant_id,
            invocation_id,
            task["approval_task_id"],
            context.user.user_id,
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("approval.target_conflict")
    return AppliedTarget(
        adapter_kind="mcp_invocation_enqueue",
        target_fact_id=invocation_id,
        target_revision=_positive_integer(row["policy_revision"]),
    )


def _load_budget_target(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_type: str,
    source_id: str,
    target_id: str,
    target_action: str,
    target_reason_code: str | None,
) -> ApprovalTarget:
    """``_load_budget_target`` 解析或应用审批目标，必须校验当前租户、目标版本和服务端权限映射。"""
    if source_type != "budget" or target_action != "release" or target_reason_code is not None:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    rule = connection.execute(
        """
        SELECT budget_rule_id,tenant_id,environment_id,scope_type,scope_id,
               exhausted_action,status,updated_at
        FROM budget_rules
        WHERE tenant_id=? AND budget_rule_id=?
        """,
        (tenant_id, target_id),
    ).fetchone()
    if rule is None:
        # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
        raise ApiError("approval.not_found")
    blocks = connection.execute(
        """
        SELECT budget_block_id,budget_date,blocked_at,status
        FROM budget_blocks
        WHERE tenant_id=? AND budget_rule_id=? AND status='active'
        ORDER BY budget_block_id
        """,
        (tenant_id, target_id),
    ).fetchall()
    if not blocks or not any(_text(row["budget_block_id"]) == source_id for row in blocks):
        # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
        raise ApiError("approval.target_conflict")
    version = _text(rule["updated_at"])
    block_material = json.dumps(
        [
            [
                _text(row["budget_block_id"]),
                _text(row["budget_date"]),
                _text(row["blocked_at"]),
                _text(row["status"]),
            ]
            for row in blocks
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return ApprovalTarget(
        target_type="budget_block_release",
        target_id=_text(rule["budget_rule_id"]),
        target_action="release",
        target_reason_code=None,
        target_version=version,
        fingerprint_parts=(
            "budget_block_release",
            _text(rule["budget_rule_id"]),
            _text(rule["tenant_id"]),
            _text(rule["environment_id"]),
            _text(rule["scope_type"]),
            _text(rule["scope_id"]),
            _text(rule["exhausted_action"]),
            _text(rule["status"]),
            version,
            block_material,
        ),
        risk_level="standard",
        read_permission_code="billing.budget.read",
        write_permission_code="billing.budget.write",
    )


def _load_session_risk_target(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_type: str,
    source_id: str,
    target_id: str,
    target_action: str,
    target_reason_code: str | None,
) -> ApprovalTarget:
    """按当前租户解析并冻结会话风险目标。"""
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    if source_type != "session_risk" or source_id != target_id:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    reasons = _SESSION_ACTION_REASON_MATRIX.get(target_action)
    if reasons is None or target_reason_code not in reasons:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    score = _load_score(connection, tenant_id, target_id)
    if score is None:
        # 租户内不可见、目标域无权或真实缺失统一返回不存在，防止通过差异响应探测跨租户资源。
        raise ApiError("approval.not_found")
    freezes = connection.execute(
        """
        SELECT session_risk_freeze_id,target_type,status,state_revision,created_at,released_at
        FROM session_risk_freezes
        WHERE tenant_id=? AND source_session_risk_score_id=?
        ORDER BY session_risk_freeze_id
        """,
        (tenant_id, target_id),
    ).fetchall()
    _validate_session_action(score, freezes, target_action)
    revision = _positive_integer(score["state_revision"])
    freeze_material = json.dumps(
        [
            [
                _text(row["session_risk_freeze_id"]),
                _text(row["target_type"]),
                _text(row["status"]),
                _positive_integer(row["state_revision"]),
                _text(row["created_at"]),
                _text(row["released_at"]) if row["released_at"] is not None else None,
            ]
            for row in freezes
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return ApprovalTarget(
        target_type="session_risk_action",
        target_id=_text(score["session_risk_score_id"]),
        target_action=target_action,
        target_reason_code=target_reason_code,
        target_version=str(revision),
        fingerprint_parts=(
            "session_risk_action",
            _text(score["session_risk_score_id"]),
            _text(score["tenant_id"]),
            _text(score["scope_type"]),
            _text(score["scope_key"]),
            _text(score["virtual_key_id"]),
            _text(score["status"]),
            str(revision),
            str(_positive_integer(score["hmac_key_version"])),
            target_action,
            target_reason_code,
            freeze_material,
        ),
        risk_level="high",
        read_permission_code="session_risk.read",
        write_permission_code="session_risk.manage",
    )


def _apply_session_risk_target(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    task: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> AppliedTarget:
    """在审批事务内应用已复核的会话风险目标。"""
    tenant_id = context.user.tenant_id
    score_id = _text(task["target_id"])
    score = _load_score(connection, tenant_id, score_id)
    if score is None:
        # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
        raise ApiError("approval.target_conflict")
    expected_target_revision = _version_integer(task["target_version"])
    canonical = {
        "action": _text(task["target_action"]),
        "reason_code": _text(task["target_reason_code"]),
        "expected_revision": expected_target_revision,
    }
    action_owner = f"approval:{_text(task['approval_task_id'])}"
    key_candidates = _action_digest_candidates(
        config,
        domain=_SESSION_APPROVAL_ACTION_KEY_DOMAIN,
        parts=(tenant_id, context.user.user_id, action_owner),
    )
    intent_candidates = dict(
        _action_digest_candidates(
            config,
            domain=_SESSION_APPROVAL_ACTION_INTENT_DOMAIN,
            parts=(
                tenant_id,
                context.user.user_id,
                score_id,
                json.dumps(canonical, sort_keys=True, separators=(",", ":")),
            ),
        )
    )
    if not key_candidates:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    digest_version, key_digest = key_candidates[0]
    intent_digest = intent_candidates.get(digest_version)
    if intent_digest is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    try:
        result = _execute_action(
            connection,
            context,
            score,
            canonical,
            digest_key_version=digest_version,
            idempotency_key_digest=key_digest,
            intent_digest=intent_digest,
            expires_at=now + timedelta(hours=24),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError as exc:
        # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
        if exc.code.startswith("session_risk."):
            # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
            raise ApiError("approval.target_conflict") from exc
        raise
    action_row = result[0]
    refreshed = _load_score(connection, tenant_id, score_id)
    if refreshed is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    try:
        sync_intent = _build_store_sync_intent(config, refreshed)
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError as exc:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    return AppliedTarget(
        adapter_kind="session_risk_store_sync",
        target_fact_id=_text(action_row["session_risk_action_id"]),
        target_revision=_positive_integer(refreshed["state_revision"]),
        sync_intent=sync_intent,
    )


def _validate_session_action(
    score: sqlite3.Row,
    freezes: list[sqlite3.Row],
    action: str,
) -> None:
    """验证动作与当前会话风险状态是否兼容。"""
    status = _text(score["status"])
    active_targets = {
        _text(row["target_type"]) for row in freezes if _text(row["status"]) == "active"
    }
    valid = False
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    if action == "freeze_session":
        valid = status in {"high_risk", "frozen"} and "session" not in active_targets
    elif action == "freeze_virtual_key":
        valid = status in {"high_risk", "frozen"} and "virtual_key" not in active_targets
    elif action == "dismiss":
        valid = status == "high_risk" and not active_targets
    # Session Risk 目标必须由当前租户的服务端适配器解析，不能接受客户端指定的跨域对象。
    elif action == "release_session":
        valid = status == "frozen" and "session" in active_targets
    elif action == "release_virtual_key":
        valid = status == "frozen" and "virtual_key" in active_targets
    if not valid:
        # 目标版本、指纹或允许动作发生变化时拒绝执行，避免把旧审批应用到新的业务对象。
        raise ApiError("approval.target_conflict")


def _version_integer(value: object) -> int:
    """``_version_integer`` 解析或应用审批目标，必须校验当前租户、目标版本和服务端权限映射。"""
    try:
        parsed = int(str(value))
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (TypeError, ValueError):
        # 预算 updated_at 不是数值；为 writeback 坐标生成稳定正整数秒版本。
        try:
            parsed = int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, OverflowError):
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable") from None
    if parsed < 1:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return parsed


def _positive_integer(value: object) -> int:
    """``_positive_integer`` 解析或应用审批目标，必须校验当前租户、目标版本和服务端权限映射。"""
    if type(value) is not int or value < 1:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _text(value: object) -> str:
    """``_text`` 解析或应用审批目标，必须校验当前租户、目标版本和服务端权限映射。"""
    if type(value) is not str or not value:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _to_db_time(value: datetime) -> str:
    """``_to_db_time`` 解析或应用审批目标，必须校验当前租户、目标版本和服务端权限映射。"""
    if value.tzinfo is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "AppliedTarget",
    "ApprovalTarget",
    "apply_approved_target",
    "apply_rejected_target",
    "load_target",
    "load_task_target",
]
