"""PRD-045 策略快照发布、回滚、历史与内部拉取服务。

控制面把 PRD-044 的 active Policy 视为“下一次发布候选”。发布必须在同一
``BEGIN IMMEDIATE`` 事务中重新读取并编译候选，随后写入 building header/items、
一次性封印、推进 head，并追加持久事件和审计。任何提交前失败都保留旧 head；
Webhook 是提交后的外部副作用，失败不能回滚已经激活的事实。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.responses import Response
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.policy_dsl import PolicyDslError, compile_policy_expression
from metahub_shared.runtime.policy_snapshot import (
    POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
    PolicySnapshotArtifact,
    PolicySnapshotItem,
    PolicySnapshotValidationError,
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
    compute_policy_snapshot_content_hash,
    policy_snapshot_to_wire_dict,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    PolicySnapshotActorInfo,
    PolicySnapshotCurrentResponse,
    PolicySnapshotInfo,
    PolicySnapshotListResponse,
    PolicySnapshotMutationResponse,
    PublishPolicySnapshotRequest,
)

logger = logging.getLogger("metahub.control.policy_snapshots")

# HTTP、分页与文本边界在服务层集中定义，数据库 CHECK 是第二道防线而非唯一校验。
CURRENT_QUERY_KEYS = frozenset()
HISTORY_QUERY_KEYS = frozenset({"limit", "offset"})
INTERNAL_QUERY_KEYS = frozenset()
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100
MAX_HISTORY_OFFSET = 10_000
MAX_REASON_LENGTH = 256
MAX_REQUEST_ID_LENGTH = 128
MAX_SNAPSHOT_VERSION = 2_147_483_647
MAX_INTERNAL_PAYLOAD_BYTES = 4_194_304
# 数据库 header schema 与内部 wire schema 分开演进；前者解释持久表，后者解释 Gateway JSON。
POLICY_SNAPSHOT_DB_SCHEMA_VERSION = "policy-snapshot-v1"
HEAD_ETAG_RE = re.compile(r'"(0|[1-9][0-9]{0,9})"\Z')
CANDIDATE_ETAG_RE = re.compile(r'"([0-9a-f]{64})"\Z')
INTERNAL_ETAG_RE = re.compile(r'"([0-9a-f]{64})"\Z')
SNAPSHOT_ID_RE = re.compile(r"psnap_[0-9a-f]{32}\Z")
TENANT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class _Candidate:
    """保存锁内权威重编后的不可变候选制品。"""

    items: tuple[PolicySnapshotItem, ...]
    content_hash: str

    @property
    def policy_count(self) -> int:
        """返回候选项数量，避免响应与封印分别计算后发生漂移。"""

        return len(self.items)


@dataclass(frozen=True, slots=True)
class _SnapshotEvent:
    """提交后投递 Webhook 所需的最小持久事件事实。"""

    event_id: str
    tenant_id: str
    snapshot_id: str
    snapshot_version: int
    actor_user_id: str
    operation: str
    payload: dict[str, object]
    request_id: str
    trace_id: str
    created_at: datetime


def get_current_policy_snapshot(
    config: AppConfig,
    context: AuthContext,
    *,
    query_keys: tuple[str, ...],
    request_id: str,
    trace_id: str,
) -> tuple[PolicySnapshotCurrentResponse, str, str | None]:
    """读取同一数据库视图中的当前 head 和 active 候选摘要。

    候选图损坏时仍允许管理员查看已经发布的 head，但只返回 ``invalid`` 固定状态，
    不返回表达式、诊断细节或可用于发布的 candidate ETag。
    """

    _reject_unknown_query(query_keys, CURRENT_QUERY_KEYS)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        current_row = _load_current_row(connection, context.user.tenant_id)
        try:
            candidate = _compile_active_candidate(connection, context.user.tenant_id)
        # 候选领域错误只降级候选状态，不能遮蔽仍可安全查看的已发布 head。
        except ApiError as exc:
            if exc.code not in {
                "policy_snapshot.action_unsupported",
                "policy_snapshot.variable_unsupported",
                "policy_snapshot.route_target_invalid",
                "policy_snapshot.graph_invalid",
            }:
                raise
            connection.rollback()
            return (
                PolicySnapshotCurrentResponse(
                    current_snapshot=(
                        _snapshot_info(current_row) if current_row is not None else None
                    ),
                    candidate_status="invalid",
                    request_id=request_id,
                    trace_id=trace_id,
                ),
                _head_etag(current_row),
                None,
            )
        connection.commit()
    # 已规范化领域错误保持原错误码，同时释放可能仍然持有的只读事务。
    except ApiError:
        _safe_rollback(connection)
        raise
    # 数据库或制品解析失败统一折叠，禁止把 SQL、表达式或底层异常返回给管理员。
    except (sqlite3.Error, TypeError, ValueError, PolicySnapshotValidationError) as exc:
        _safe_rollback(connection)
        raise ApiError("policy_snapshot.unavailable") from exc
    finally:
        connection.close()

    current_info = _snapshot_info(current_row) if current_row is not None else None
    matches = bool(
        current_info is not None and current_info.content_hash == candidate.content_hash
    )
    return (
        PolicySnapshotCurrentResponse(
            current_snapshot=current_info,
            candidate_status="valid",
            candidate_content_hash=candidate.content_hash,
            candidate_policy_count=candidate.policy_count,
            candidate_matches_current=matches,
            request_id=request_id,
            trace_id=trace_id,
        ),
        _head_etag(current_row),
        f'"{candidate.content_hash}"',
    )


def list_policy_snapshots(
    config: AppConfig,
    context: AuthContext,
    *,
    limit: str | None,
    offset: str | None,
    query_keys: tuple[str, ...],
    request_id: str,
    trace_id: str,
) -> PolicySnapshotListResponse:
    """按当前认证 tenant 返回不可变快照历史，不接受客户端 tenant 过滤。"""

    _reject_unknown_query(query_keys, HISTORY_QUERY_KEYS)
    normalized_limit = _normalize_pagination_value(
        limit,
        default=DEFAULT_HISTORY_LIMIT,
        minimum=1,
        maximum=MAX_HISTORY_LIMIT,
    )
    normalized_offset = _normalize_pagination_value(
        offset,
        default=0,
        minimum=0,
        maximum=MAX_HISTORY_OFFSET,
    )
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            {_SNAPSHOT_SELECT}
            WHERE s.tenant_id=? AND s.state='published'
            ORDER BY s.snapshot_version DESC
            LIMIT ? OFFSET ?
            """,
            (context.user.tenant_id, normalized_limit, normalized_offset),
        ).fetchall()
        total_row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM policy_snapshots
            WHERE tenant_id=? AND state='published'
            """,
            (context.user.tenant_id,),
        ).fetchone()
        total = int(total_row["total"] if total_row is not None else 0)
        infos = [_snapshot_info(row) for row in rows]
    # 历史读取与 DTO 转换故障不能返回部分租户数据，统一按快照服务不可用处理。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("policy_snapshot.unavailable") from exc
    finally:
        connection.close()
    return PolicySnapshotListResponse(
        policy_snapshots=infos,
        total=total,
        limit=normalized_limit,
        offset=normalized_offset,
        request_id=request_id,
        trace_id=trace_id,
    )


def publish_policy_snapshot(
    config: AppConfig,
    context: AuthContext,
    payload: PublishPolicySnapshotRequest,
    *,
    if_match: str | None,
    candidate_match: str | None,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicySnapshotMutationResponse:
    """编译 active 候选并以 head+candidate 双 CAS 发布一个新版本。"""

    reason = _normalize_mutation_payload(payload)
    expected_head = _normalize_head_if_match(if_match)
    expected_candidate = _normalize_candidate_match(candidate_match)
    normalized_request_id = _normalize_request_id(request_id)
    fingerprint = _request_fingerprint(
        actor_id=context.user.user_id,
        operation="publish",
        expected_head=expected_head,
        expected_candidate=expected_candidate,
        target_snapshot_id=None,
        reason=reason,
    )
    connection = _connect(config)
    event: _SnapshotEvent | None = None
    audit_record: AuditRecord | None = None
    replayed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_idempotency_row(connection, context.user.tenant_id, normalized_request_id)
        if existing is not None:
            result = _replay_idempotent_result(
                existing,
                actor_id=context.user.user_id,
                fingerprint=fingerprint,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )
            # 原请求可能在 COMMIT 后、首次 Webhook intent 前退出；回放不能只返回业务结果，
            # 必须从同事务持久事实恢复两个稳定事件，让既有派发器按 event_id 补齐漏投并去重。
            event, audit_record = _load_replay_dispatch_facts(connection, existing)
            connection.rollback()
            dispatch_audit_recorded(
                config,
                context,
                audit_record,
                request_id=event.request_id,
                trace_id=event.trace_id,
            )
            _dispatch_snapshot_event(config, event)
            return result

        current_row = _load_current_row(connection, context.user.tenant_id)
        _require_head_version(current_row, expected_head)
        candidate = _compile_active_candidate(connection, context.user.tenant_id)
        # candidate hash 必须在写锁内与客户端读取值一致，避免无感发布被并发修改的候选。
        if candidate.content_hash != expected_candidate:
            raise ApiError("policy_snapshot.candidate_conflict")
        _validate_active_template_invariants(connection, context.user.tenant_id, candidate)
        # 执行内容未变化时不制造无意义版本，也不占用这次请求的持久幂等键。
        if current_row is not None and current_row["content_hash"] == candidate.content_hash:
            raise ApiError("policy_snapshot.no_changes")

        artifact, snapshot_id, created_at = _create_published_snapshot(
            connection,
            context,
            operation="publish",
            reason=reason,
            request_id=normalized_request_id,
            trace_id=trace_id,
            fingerprint=fingerprint,
            previous_row=current_row,
            rollback_source_row=None,
            items=candidate.items,
        )
        event = _insert_snapshot_event(
            connection,
            context,
            artifact,
            request_id=normalized_request_id,
            trace_id=trace_id,
            created_at=created_at,
        )
        _advance_head(connection, context, artifact, updated_at=created_at)
        after_summary = _artifact_audit_summary(artifact, reason)
        audit_record = record_audit_log(
            connection,
            context,
            action="policy_snapshot.published",
            object_type="policy_snapshot",
            object_id=snapshot_id,
            object_name=None,
            before_summary=(
                _snapshot_row_audit_summary(current_row) if current_row is not None else {}
            ),
            after_summary=after_summary,
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
        result_row = _load_snapshot_row(connection, context.user.tenant_id, snapshot_id)
        # 提交后的权威回读缺失表示存储不可确认，调用方可用同 request_id 安全重试回放。
        if result_row is None:
            raise ApiError("policy_snapshot.unavailable")
        result = PolicySnapshotMutationResponse(
            policy_snapshot=_snapshot_info(result_row),
            idempotent_replay=replayed,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
    # 所有提交前领域失败都必须释放事务；审计基础设施故障统一映射到快照可重试错误。
    except ApiError as exc:
        _safe_rollback(connection)
        # 审计与业务事实必须共同成功，不能把底层审计模块错误直接暴露为部分成功。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("policy_snapshot.unavailable") from exc
        raise
    # SQL、编译或 shared 制品异常均可能含内部细节，只能回滚并返回固定服务错误。
    except (
        sqlite3.Error,
        TypeError,
        ValueError,
        PolicyDslError,
        PolicySnapshotValidationError,
    ) as exc:
        _safe_rollback(connection)
        # 发布构建失败只暴露稳定的快照服务错误，避免 SQL、DSL 或制品校验细节越过管理面边界。
        raise ApiError("policy_snapshot.unavailable") from exc

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_snapshot_event(config, event)
    return result


def rollback_policy_snapshot(
    config: AppConfig,
    context: AuthContext,
    policy_snapshot_id: str,
    payload: PublishPolicySnapshotRequest,
    *,
    if_match: str | None,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicySnapshotMutationResponse:
    """只允许复制当前上一版，并创建 head+1 的新 rollback 快照。"""

    target_id = _normalize_snapshot_id(policy_snapshot_id)
    reason = _normalize_mutation_payload(payload)
    expected_head = _normalize_head_if_match(if_match)
    normalized_request_id = _normalize_request_id(request_id)
    fingerprint = _request_fingerprint(
        actor_id=context.user.user_id,
        operation="rollback",
        expected_head=expected_head,
        expected_candidate=None,
        target_snapshot_id=target_id,
        reason=reason,
    )
    connection = _connect(config)
    event: _SnapshotEvent | None = None
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 模板补偿撤销必须先于普通上一版复制识别。立即撤销时 target 恰好也是 current.previous，
        # 若先走 PRD-045 分支只会复制旧快照，却会把生成 Policy 留在 active candidate 中重新上线。
        from metahub_control.policy_templates.service import (
            _try_rollback_policy_template_application,
        )

        template_result = _try_rollback_policy_template_application(
            config,
            connection,
            context,
            target_snapshot_id=target_id,
            reason=reason,
            expected_head=expected_head,
            request_fingerprint=fingerprint,
            audit_request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        if template_result is not None:
            return template_result
        existing = _load_idempotency_row(connection, context.user.tenant_id, normalized_request_id)
        if existing is not None:
            result = _replay_idempotent_result(
                existing,
                actor_id=context.user.user_id,
                fingerprint=fingerprint,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )
            # rollback 与 publish 共用相同崩溃恢复合同；只从已核验的持久 event/audit
            # 重建外部事件，不能根据本次请求 body 重新推导可能漂移的 Webhook 内容。
            event, audit_record = _load_replay_dispatch_facts(connection, existing)
            connection.rollback()
            dispatch_audit_recorded(
                config,
                context,
                audit_record,
                request_id=event.request_id,
                trace_id=event.trace_id,
            )
            _dispatch_snapshot_event(config, event)
            return result

        current_row = _load_current_row(connection, context.user.tenant_id)
        _require_head_version(current_row, expected_head)
        target_row = _load_snapshot_row(connection, context.user.tenant_id, target_id)
        # snapshot ID 必须在认证租户内存在；跨租户与不存在统一处理以阻止对象枚举。
        if target_row is None:
            raise ApiError("policy_snapshot.not_found")
        # M1 只允许回滚当前 head 的上一版，禁止任意历史跳转破坏单调发布图。
        if current_row is None or current_row["previous_snapshot_id"] != target_id:
            raise ApiError("policy_snapshot.rollback_target_invalid")

        source_artifact = _load_verified_artifact(connection, target_row)
        # 模板补偿撤销会把 application 和生成 Policy 一并置为终态；普通上一版回滚不得
        # 仅复制历史制品而绕过该生命周期，否则会在不恢复权限、依赖和 application 的情况下
        # 把已禁用 route_to/deny/mask 重新送入 Gateway。
        _require_no_rolled_back_template_policies(
            connection,
            context.user.tenant_id,
            source_artifact.items,
        )
        _require_active_route_targets(
            config,
            connection,
            context.user.tenant_id,
            source_artifact.items,
        )
        artifact, snapshot_id, created_at = _create_published_snapshot(
            connection,
            context,
            operation="rollback",
            reason=reason,
            request_id=normalized_request_id,
            trace_id=trace_id,
            fingerprint=fingerprint,
            previous_row=current_row,
            rollback_source_row=target_row,
            items=source_artifact.items,
        )
        # 回滚只允许复制来源执行内容，任何 hash 漂移都必须在推进 head 前整体失败。
        if artifact.content_hash != source_artifact.content_hash:
            raise ApiError("policy_snapshot.unavailable")
        event = _insert_snapshot_event(
            connection,
            context,
            artifact,
            request_id=normalized_request_id,
            trace_id=trace_id,
            created_at=created_at,
        )
        _advance_head(connection, context, artifact, updated_at=created_at)
        audit_record = record_audit_log(
            connection,
            context,
            action="policy_snapshot.rolled_back",
            object_type="policy_snapshot",
            object_id=snapshot_id,
            object_name=None,
            before_summary=_snapshot_row_audit_summary(current_row),
            after_summary=_artifact_audit_summary(artifact, reason),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
        result_row = _load_snapshot_row(connection, context.user.tenant_id, snapshot_id)
        # 提交后的回读缺失不能伪造成功响应，原请求仍可依赖幂等记录重新确认结果。
        if result_row is None:
            raise ApiError("policy_snapshot.unavailable")
        result = PolicySnapshotMutationResponse(
            policy_snapshot=_snapshot_info(result_row),
            idempotent_replay=False,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
    # 回滚领域错误保持稳定语义并撤销所有提交前 header、items、事件与审计写入。
    except ApiError as exc:
        _safe_rollback(connection)
        # 审计不可用意味着回滚事实不可验收，必须对外表现为可重试的快照故障。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("policy_snapshot.unavailable") from exc
        raise
    # 底层 SQL、编译和 hash 异常一律脱敏折叠，旧 head 由事务回滚继续保留。
    except (
        sqlite3.Error,
        TypeError,
        ValueError,
        PolicyDslError,
        PolicySnapshotValidationError,
    ) as exc:
        _safe_rollback(connection)
        # 回滚重建失败必须保留旧 head，并把底层存储或哈希异常统一收敛为可重试服务错误。
        raise ApiError("policy_snapshot.unavailable") from exc
    finally:
        # 幂等早返回和模板补偿分支也必须释放连接，避免长时间运行的 Control 进程积累句柄。
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_snapshot_event(config, event)
    return result


def build_internal_current_response(
    config: AppConfig,
    tenant_id: str,
    *,
    if_none_match: str | None,
    query_keys: tuple[str, ...],
    authorization: str | None,
    request_id: str,
    trace_id: str,
) -> Response:
    """生成单 tenant 内部 wire 响应，支持 schema-bound 强 ETag 与 304。

    响应先在内存中完整构造并检查字节上限，再一次性返回；绝不发送截断 JSON，
    也不把 canonical expression 放入结构化日志或错误正文。
    """

    _reject_unknown_query(query_keys, INTERNAL_QUERY_KEYS)
    normalized_tenant = _normalize_internal_tenant_id(tenant_id)
    # 内部拉取与浏览器登录态是两条认证边界，拒绝混用可避免代理错误转发 Bearer。
    if authorization is not None:
        raise ApiError("policy_snapshot.payload_invalid")
    normalized_if_none_match = _normalize_internal_if_none_match(if_none_match)
    connection = _connect(config)
    try:
        tenant = connection.execute(
            "SELECT tenant_id, status FROM tenants WHERE tenant_id=?",
            (normalized_tenant,),
        ).fetchone()
        # 未知与 disabled tenant 不能伪装成空策略基线，必须统一失败以保护租户隔离。
        if tenant is None or tenant["status"] != "active":
            raise ApiError("policy_snapshot.tenant_unavailable")
        current_row = _load_current_row(connection, normalized_tenant)
        artifact: PolicySnapshotArtifact | None = None
        if current_row is not None:
            artifact = _load_verified_artifact(connection, current_row)
        etag = _internal_etag(normalized_tenant, artifact)
        if normalized_if_none_match is not None and normalized_if_none_match == etag:
            return Response(
                status_code=304,
                headers={"Cache-Control": "no-store", "ETag": etag},
            )
        envelope: dict[str, object] = {
            "wire_schema_version": POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
            "tenant_id": normalized_tenant,
            "snapshot": (
                policy_snapshot_to_wire_dict(artifact) if artifact is not None else None
            ),
            "request_id": request_id,
            "trace_id": trace_id,
        }
        body = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        max_bytes = int(
            getattr(config, "policy_snapshot_max_payload_bytes", MAX_INTERNAL_PAYLOAD_BYTES)
        )
        # 完整响应超过安全上限时整体拒绝，绝不能发送可被误当成合法快照的截断 JSON。
        if len(body) > max_bytes:
            raise ApiError("policy_snapshot.unavailable")
    # 已归类领域错误直接交给统一响应中间件，不能被兜底重新标成内部未知异常。
    except ApiError:
        raise
    # DB、序列化或 shared 校验故障不得泄露 canonical expression 与底层异常文本。
    except (sqlite3.Error, TypeError, ValueError, PolicySnapshotValidationError) as exc:
        raise ApiError("policy_snapshot.unavailable") from exc
    return Response(
        content=body,
        status_code=200,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "ETag": etag},
    )


def _compile_active_candidate(connection: sqlite3.Connection, tenant_id: str) -> _Candidate:
    """从锁内 active Policy 图重新编译 canonical items，并验证持久编译事实。

    前端 validation、数据库 AST 和历史 hash 都不是发布授权凭证；这里必须以原始受限
    expression 调用共享编译器，并逐字段核对后再生成快照项。
    """

    rows = connection.execute(
        """
        SELECT
          p.policy_id, p.version AS policy_version, p.priority, p.dsl_version,
          r.policy_rule_id, r.condition_expr, r.condition_ast_json,
          r.condition_hash, r.variables_json,
          a.policy_action_id, a.action_type, a.target_model_id
        FROM policies p
        LEFT JOIN policy_rules r
          ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
        LEFT JOIN policy_actions a
          ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.status='active'
        ORDER BY p.priority ASC, p.policy_id ASC
        """,
        (tenant_id,),
    ).fetchall()
    items: list[PolicySnapshotItem] = []
    for evaluation_order, row in enumerate(rows):
        required = (
            "policy_rule_id",
            "condition_expr",
            "condition_ast_json",
            "condition_hash",
            "variables_json",
            "policy_action_id",
            "action_type",
        )
        # active Policy 缺 Rule/Action 或关键编译事实时，整个候选图必须失败关闭。
        if any(row[key] is None for key in required):
            raise ApiError("policy_snapshot.graph_invalid")
        try:
            compiled = compile_policy_expression(row["condition_expr"])
            persisted_ast = json.loads(row["condition_ast_json"])
            persisted_variables = json.loads(row["variables_json"])
        # 原始表达式或持久 JSON 损坏时不回显具体内容，只报告候选图不可用。
        except (PolicyDslError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ApiError("policy_snapshot.graph_invalid") from exc
        # 持久 AST/hash/变量必须与本次权威重编完全一致，防止绕过编译器伪造制品。
        if (
            row["dsl_version"] != compiled.dsl_version
            or persisted_ast != compiled.canonical_ast
            or row["condition_hash"] != compiled.condition_hash
            or not isinstance(persisted_variables, list)
            or tuple(persisted_variables) != compiled.variables
        ):
            # 任一编译事实漂移都意味着候选不可证明可信，不能只采用其中看似正常的字段。
            raise ApiError("policy_snapshot.graph_invalid")

        action_type = str(row["action_type"])
        if "data_class" in compiled.variables:
            # 请求 Header/Body 不能充当可信数据分类来源，避免调用方伪造治理上下文。
            raise ApiError("policy_snapshot.variable_unsupported")
        if action_type == "route_to":
            _require_active_route_target(connection, tenant_id, row["target_model_id"])
        # 非路由动作携带 target 表示数据库图已腐损，不能静默忽略悬空字段。
        elif row["target_model_id"] is not None:
            raise ApiError("policy_snapshot.graph_invalid")
        # 未知动作不能按 allow 兜底，否则会把控制面损坏转化为安全策略绕过。
        if action_type not in {"allow", "deny", "mask", "log_only", "route_to"}:
            raise ApiError("policy_snapshot.graph_invalid")
        try:
            item = build_policy_snapshot_item(
                evaluation_order=evaluation_order,
                policy_id=str(row["policy_id"]),
                policy_version=int(row["policy_version"]),
                policy_rule_id=str(row["policy_rule_id"]),
                policy_action_id=str(row["policy_action_id"]),
                priority=int(row["priority"]),
                dsl_version=str(row["dsl_version"]),
                canonical_expression=compiled.canonical_expression,
                condition_hash=str(row["condition_hash"]),
                variables=tuple(str(value) for value in persisted_variables),
                action_type=action_type,
                target_model_id=(
                    str(row["target_model_id"])
                    if row["target_model_id"] is not None
                    else None
                ),
            )
        # shared 拒绝任何字段时说明持久候选无法形成可信 wire，统一按图损坏处理。
        except PolicySnapshotValidationError as exc:
            raise ApiError("policy_snapshot.graph_invalid") from exc
        items.append(item)
    try:
        content_hash = compute_policy_snapshot_content_hash(tenant_id, tuple(items))
    # content hash 计算失败表示候选顺序或租户字段不可信，禁止进入发布事务后半段。
    except PolicySnapshotValidationError as exc:
        raise ApiError("policy_snapshot.graph_invalid") from exc
    return _Candidate(items=tuple(items), content_hash=content_hash)


def _validate_active_template_invariants(
    connection: sqlite3.Connection,
    tenant_id: str,
    candidate: _Candidate,
) -> None:
    """证明全部 active template application 在候选中仍不可变且不会被绕过。

    参数：当前写事务连接、认证 tenant 与已经通过 shared compiler 的完整候选。返回值：无。
    关键约束：generated Policy 必须按 application item 的 wire hash 精确出现一次；排序中新加入
    的决定性动作若可能绕过模板即固定 409。查询始终以 tenant 开头，rolled_back application
    不再参与约束，因其 Policy 已由补偿事务停用。
    """

    applications = connection.execute(
        """
        SELECT application.application_id,application.generated_policy_id,
               template.template_code
        FROM policy_template_applications application
        JOIN policy_templates template
          ON template.tenant_id=application.tenant_id
         AND template.policy_template_id=application.policy_template_id
        WHERE application.tenant_id=? AND application.status='applied'
        ORDER BY application.application_id
        """,
        (tenant_id,),
    ).fetchall()
    for application in applications:
        item_rows = connection.execute(
            """
            SELECT after_hash FROM policy_template_application_items
            WHERE tenant_id=? AND application_id=?
              AND resource_type='policy' AND operation='create'
            """,
            (tenant_id, application["application_id"]),
        ).fetchall()
        generated = [
            (index, item)
            for index, item in enumerate(candidate.items)
            if item.policy_id == application["generated_policy_id"]
        ]
        # 应用事实与候选图必须保持一对一，否则无法证明正在约束的是原模板生成策略。
        if len(item_rows) != 1 or len(generated) != 1:
            raise ApiError("policy_snapshot.template_invariant_conflict")
        generated_index, generated_item = generated[0]
        generated_hash = _policy_item_graph_hash(generated_item)
        # wire 图发生漂移表示模板生成资源已被旁路修改，必须先走显式回滚或重新应用流程。
        if generated_hash != item_rows[0]["after_hash"]:
            raise ApiError("policy_snapshot.template_invariant_conflict")
        # 即使生成资源自身未变，完整排序中的其他决定性策略也不能遮蔽模板承诺的治理效果。
        if _template_shadowing_conflicts(
            candidate.items,
            generated_index,
            str(application["template_code"]),
        ):
            # 候选排序已破坏模板持续承诺，固定返回冲突而不能静默发布一个名义有效的快照。
            raise ApiError("policy_snapshot.template_invariant_conflict")


def _policy_item_graph_hash(item: PolicySnapshotItem) -> str:
    """计算单条 Policy 的稳定图哈希，不把候选集合中的相对序号当成策略内容。

    ``evaluation_order`` 会在其他 Policy 插入或移除时合法变化；模板 application 冻结的是
    生成 Policy 自身的版本、规则、动作和优先级图。若把相对序号纳入哈希，不同 Project
    中语义互斥的策略也会造成假漂移，进而错误阻断后续发布和模板补偿撤销。
    """

    graph = item.as_wire_dict()
    graph.pop("evaluation_order", None)
    return hashlib.sha256(_canonical_json_bytes(graph)).hexdigest()


def _template_shadowing_conflicts(
    items: tuple[PolicySnapshotItem, ...],
    generated_index: int,
    template_code: str,
) -> bool:
    """按 PRD-068 决策矩阵保守判断完整排序是否会绕过某条模板。"""

    generated_item = items[generated_index]
    generated_projects = _required_project_values(generated_item)
    for index, item in enumerate(items):
        if index == generated_index:
            continue
        # 若两个条件都能证明只命中不同 Project，则它们不可能在同一请求上互相遮蔽。
        item_projects = _required_project_values(item)
        if (
            generated_projects is not None
            and item_projects is not None
            and generated_projects.isdisjoint(item_projects)
        ):
            continue
        decisive = item.action_type in {"allow", "deny", "route_to"}
        # route_to 会直接结束 Shared 求值；低成本模板后的重叠 mask 虽是修饰动作，仍会被
        # 永久跳过并泄露未脱敏输入，因此持续发布不变量必须把它视为安全冲突。
        if (
            template_code == "low_cost_mode"
            and index > generated_index
            and item.action_type == "mask"
        ):
            return True
        if not decisive:
            continue
        # 更早的放行或路由会提前结束决策，使后续模板策略永远无法对同一请求生效。
        if index < generated_index and item.action_type in {"allow", "route_to"}:
            return True
        if template_code == "low_cost_mode" and (
            index > generated_index and item.action_type == "deny"
        ):
            return True
        if template_code in {"high_security_mode", "domestic_models_only"} and (
            index > generated_index and item.action_type == "route_to"
        ):
            return True
        if template_code == "customer_service_redaction" and (
            index < generated_index and item.action_type in {"allow", "route_to"}
        ):
            return True
    return False


def _required_project_values(item: PolicySnapshotItem) -> frozenset[str] | None:
    """从 canonical AST 提取必需 Project 等值约束；无法证明时返回未知。"""

    def visit(node: object) -> frozenset[str] | None:
        """递归处理 AND 的交集语义；OR/NOT 与非等值比较保持保守未知。"""

        if type(node) is not dict:
            return None
        kind = node.get("kind")
        if kind == "comparison" and node.get("variable") == "project":
            literal = node.get("value")
            if type(literal) is not dict:
                return None
            if node.get("operator") == "eq" and literal.get("kind") == "string":
                value = literal.get("value")
                return frozenset({value}) if type(value) is str else None
            if node.get("operator") == "in" and literal.get("kind") == "list":
                items = literal.get("items")
                if type(items) is not list:
                    return None
                values = [
                    item.get("value")
                    for item in items
                    if type(item) is dict and item.get("kind") == "string"
                ]
                if len(values) != len(items) or any(type(value) is not str for value in values):
                    return None
                return frozenset(values)
            return None
        if (
            kind == "logical"
            and node.get("operator") == "and"
            and type(node.get("children")) is list
        ):
            known = [
                value
                for child in node["children"]
                if (value := visit(child)) is not None
            ]
            if not known:
                return None
            intersection = set(known[0])
            for value in known[1:]:
                intersection.intersection_update(value)
            return frozenset(intersection)
        return None

    return visit(item.compiled_expression.ast)


def _create_published_snapshot(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    operation: str,
    reason: str,
    request_id: str,
    trace_id: str,
    fingerprint: str,
    previous_row: sqlite3.Row | None,
    rollback_source_row: sqlite3.Row | None,
    items: tuple[PolicySnapshotItem, ...],
    created_at: datetime | None = None,
    snapshot_id: str | None = None,
) -> tuple[PolicySnapshotArtifact, str, datetime]:
    """写 building/items 后构造 shared artifact，并执行唯一一次封印 UPDATE。

    ``created_at`` 与 ``snapshot_id`` 仅供同一事务内编排多个领域事实的复合命令使用；
    普通发布继续由本 helper 生成当前 UTC 时间和随机 ID。显式时间必须带时区，预生成
    ID 必须仍满足内部强格式，避免延迟外键把非法身份写进不可变发布链。
    """

    previous_version = int(previous_row["snapshot_version"]) if previous_row is not None else None
    snapshot_version = 1 if previous_version is None else previous_version + 1
    # 版本达到数据库和 HTTP 合同上限时拒绝继续增长，避免整数边界产生不一致。
    if snapshot_version > MAX_SNAPSHOT_VERSION:
        raise ApiError("policy_snapshot.unavailable")
    effective_snapshot_id = snapshot_id or f"psnap_{uuid4().hex}"
    # 复合事务可预生成主键，但仍须遵守快照命名空间，避免非法标识进入不可变引用链。
    if not SNAPSHOT_ID_RE.fullmatch(effective_snapshot_id):
        raise ApiError("policy_snapshot.unavailable")
    effective_created_at = created_at or _utcnow()
    # 无时区时间无法稳定排序跨服务事实，写库前拒绝以免各节点按本地时区产生不同结果。
    if effective_created_at.tzinfo is None:
        raise ApiError("policy_snapshot.unavailable")
    effective_created_at = effective_created_at.astimezone(UTC)
    created_text = _to_db_time(effective_created_at)
    connection.execute(
        """
        INSERT INTO policy_snapshots(
          policy_snapshot_id, tenant_id, snapshot_version, schema_version, state,
          operation, previous_snapshot_id, previous_snapshot_version,
          rollback_source_snapshot_id, rollback_source_snapshot_version,
          request_id, trace_id, request_fingerprint, content_hash, artifact_hash,
          policy_count, change_reason, approval_status, created_by, created_at, published_at
        ) VALUES(?, ?, ?, ?, 'building', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                 NULL, ?, 'not_required', ?, ?, NULL)
        """,
        (
            effective_snapshot_id,
            context.user.tenant_id,
            snapshot_version,
            POLICY_SNAPSHOT_DB_SCHEMA_VERSION,
            operation,
            previous_row["policy_snapshot_id"] if previous_row is not None else None,
            previous_version,
            (
                rollback_source_row["policy_snapshot_id"]
                if rollback_source_row is not None
                else None
            ),
            (
                int(rollback_source_row["snapshot_version"])
                if rollback_source_row is not None
                else None
            ),
            request_id,
            trace_id,
            fingerprint,
            reason,
            context.user.user_id,
            created_text,
        ),
    )
    for item in items:
        _insert_snapshot_item(
            connection,
            context.user.tenant_id,
            effective_snapshot_id,
            item,
            created_text,
        )

    artifact = build_policy_snapshot_artifact(
        tenant_id=context.user.tenant_id,
        policy_snapshot_id=effective_snapshot_id,
        snapshot_version=snapshot_version,
        operation=operation,
        previous_snapshot_id=(
            str(previous_row["policy_snapshot_id"]) if previous_row is not None else None
        ),
        previous_snapshot_version=previous_version,
        rollback_source_snapshot_id=(
            str(rollback_source_row["policy_snapshot_id"])
            if rollback_source_row is not None
            else None
        ),
        rollback_source_snapshot_version=(
            int(rollback_source_row["snapshot_version"])
            if rollback_source_row is not None
            else None
        ),
        items=items,
    )
    cursor = connection.execute(
        """
        UPDATE policy_snapshots
        SET state='published', content_hash=?, artifact_hash=?, policy_count=?, published_at=?
        WHERE tenant_id=? AND policy_snapshot_id=? AND state='building'
        """,
        (
            artifact.content_hash,
            artifact.artifact_hash,
            len(artifact.items),
            created_text,
            context.user.tenant_id,
            effective_snapshot_id,
        ),
    )
    # 封印必须精确影响刚创建的一条 building header，否则不得继续事件与 head 写入。
    if cursor.rowcount != 1:
        raise ApiError("policy_snapshot.unavailable")
    return artifact, effective_snapshot_id, effective_created_at


def _insert_snapshot_item(
    connection: sqlite3.Connection,
    tenant_id: str,
    snapshot_id: str,
    item: PolicySnapshotItem,
    created_at: str,
) -> None:
    """把一个 shared canonical item 写入 building 快照，字段不做二次解释。"""

    connection.execute(
        """
        INSERT INTO policy_snapshot_items(
          policy_snapshot_item_id, tenant_id, policy_snapshot_id, evaluation_order,
          source_policy_id, source_policy_version, policy_rule_id, policy_action_id,
          priority, dsl_version, canonical_expression, condition_hash, variables_json,
          action_type, target_model_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"psitem_{uuid4().hex}",
            tenant_id,
            snapshot_id,
            item.evaluation_order,
            item.policy_id,
            item.policy_version,
            item.policy_rule_id,
            item.policy_action_id,
            item.priority,
            item.dsl_version,
            item.canonical_expression,
            item.condition_hash,
            _json_text(list(item.variables)),
            item.action_type,
            item.target_model_id,
            created_at,
        ),
    )


def _insert_snapshot_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    artifact: PolicySnapshotArtifact,
    *,
    request_id: str,
    trace_id: str,
    created_at: datetime,
    event_id: str | None = None,
) -> _SnapshotEvent:
    """在发布事务中追加持久事件；可预生成 ID 只服务延迟复合外键。"""

    payload: dict[str, object] = {
        "version": artifact.snapshot_version,
        "content_hash": artifact.content_hash,
        "policy_count": len(artifact.items),
        "operation": artifact.operation,
        "previous_version": artifact.previous_snapshot_version,
        "rollback_source_version": artifact.rollback_source_snapshot_version,
    }
    effective_event_id = event_id or f"psevt_{uuid4().hex}"
    # 预生成事件 ID 也必须限定在快照事件命名空间，确保延迟外键不会接受任意外部标识。
    if not re.fullmatch(r"psevt_[0-9a-f]{32}", effective_event_id):
        raise ApiError("policy_snapshot.unavailable")
    connection.execute(
        """
        INSERT INTO policy_snapshot_events(
          policy_snapshot_event_id, tenant_id, policy_snapshot_id, snapshot_version,
          event_type, operation, actor_user_id, request_id, trace_id, payload_json, created_at
        ) VALUES(?, ?, ?, ?, 'policy_snapshot.published', ?, ?, ?, ?, ?, ?)
        """,
        (
            effective_event_id,
            context.user.tenant_id,
            artifact.policy_snapshot_id,
            artifact.snapshot_version,
            artifact.operation,
            context.user.user_id,
            request_id,
            trace_id,
            _json_text(payload),
            _to_db_time(created_at),
        ),
    )
    return _SnapshotEvent(
        event_id=effective_event_id,
        tenant_id=context.user.tenant_id,
        snapshot_id=artifact.policy_snapshot_id,
        snapshot_version=artifact.snapshot_version,
        actor_user_id=context.user.user_id,
        operation=artifact.operation,
        payload=payload,
        request_id=request_id,
        trace_id=trace_id,
        created_at=created_at,
    )


def _advance_head(
    connection: sqlite3.Connection,
    context: AuthContext,
    artifact: PolicySnapshotArtifact,
    *,
    updated_at: datetime,
) -> None:
    """首次插入或精确推进 head；数据库触发器继续阻止跳版、降版和跨租户引用。"""

    if artifact.previous_snapshot_id is None:
        connection.execute(
            """
            INSERT INTO policy_snapshot_heads(
              tenant_id, current_snapshot_id, current_snapshot_version, updated_by, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                context.user.tenant_id,
                artifact.policy_snapshot_id,
                artifact.snapshot_version,
                context.user.user_id,
                _to_db_time(updated_at),
            ),
        )
        return
    cursor = connection.execute(
        """
        UPDATE policy_snapshot_heads
        SET current_snapshot_id=?, current_snapshot_version=?, updated_by=?, updated_at=?
        WHERE tenant_id=? AND current_snapshot_id=? AND current_snapshot_version=?
        """,
        (
            artifact.policy_snapshot_id,
            artifact.snapshot_version,
            context.user.user_id,
            _to_db_time(updated_at),
            context.user.tenant_id,
            artifact.previous_snapshot_id,
            artifact.previous_snapshot_version,
        ),
    )
    # CAS 更新未命中表示 head 已漂移或数据库触发器拒绝，必须返回明确版本冲突。
    if cursor.rowcount != 1:
        raise ApiError("policy_snapshot.version_conflict")


def _load_verified_artifact(
    connection: sqlite3.Connection,
    snapshot_row: sqlite3.Row,
) -> PolicySnapshotArtifact:
    """从已发布数据库行重建共享制品，并核对内容与信封双 hash 的完整一致性。"""

    # 内部拉取和回滚永远不能消费 building 半快照，状态异常必须立即失败关闭。
    if snapshot_row["state"] != "published":
        raise ApiError("policy_snapshot.unavailable")
    item_rows = connection.execute(
        """
        SELECT evaluation_order, source_policy_id, source_policy_version,
               policy_rule_id, policy_action_id, priority, dsl_version,
               canonical_expression, condition_hash, variables_json,
               action_type, target_model_id
        FROM policy_snapshot_items
        WHERE tenant_id=? AND policy_snapshot_id=?
        ORDER BY evaluation_order ASC
        """,
        (snapshot_row["tenant_id"], snapshot_row["policy_snapshot_id"]),
    ).fetchall()
    items: list[PolicySnapshotItem] = []
    for row in item_rows:
        try:
            variables = json.loads(row["variables_json"])
            # 变量摘要必须保持 JSON 数组形状，标量或对象都不能进入 shared 重编入口。
            if not isinstance(variables, list):
                raise ValueError("variables shape")
            item = build_policy_snapshot_item(
                evaluation_order=int(row["evaluation_order"]),
                policy_id=str(row["source_policy_id"]),
                policy_version=int(row["source_policy_version"]),
                policy_rule_id=str(row["policy_rule_id"]),
                policy_action_id=str(row["policy_action_id"]),
                priority=int(row["priority"]),
                dsl_version=str(row["dsl_version"]),
                canonical_expression=str(row["canonical_expression"]),
                condition_hash=str(row["condition_hash"]),
                variables=tuple(str(value) for value in variables),
                action_type=str(row["action_type"]),
                target_model_id=(
                    str(row["target_model_id"])
                    if row["target_model_id"] is not None
                    else None
                ),
            )
        # 任一 item 类型、表达式或 hash 校验失败都使整个快照不可用，不能部分缓存。
        except (TypeError, ValueError, PolicySnapshotValidationError) as exc:
            raise ApiError("policy_snapshot.unavailable") from exc
        items.append(item)
    try:
        artifact = build_policy_snapshot_artifact(
            tenant_id=str(snapshot_row["tenant_id"]),
            policy_snapshot_id=str(snapshot_row["policy_snapshot_id"]),
            snapshot_version=int(snapshot_row["snapshot_version"]),
            operation=str(snapshot_row["operation"]),
            previous_snapshot_id=(
                str(snapshot_row["previous_snapshot_id"])
                if snapshot_row["previous_snapshot_id"] is not None
                else None
            ),
            previous_snapshot_version=(
                int(snapshot_row["previous_snapshot_version"])
                if snapshot_row["previous_snapshot_version"] is not None
                else None
            ),
            rollback_source_snapshot_id=(
                str(snapshot_row["rollback_source_snapshot_id"])
                if snapshot_row["rollback_source_snapshot_id"] is not None
                else None
            ),
            rollback_source_snapshot_version=(
                int(snapshot_row["rollback_source_snapshot_version"])
                if snapshot_row["rollback_source_snapshot_version"] is not None
                else None
            ),
            items=tuple(items),
        )
    # envelope/provenance 不满足 shared 合同时阻断回滚和内部下发，并隐藏具体字段值。
    except PolicySnapshotValidationError as exc:
        raise ApiError("policy_snapshot.unavailable") from exc
    # 数据库双 hash 与重算结果必须完全一致，任何篡改都不能降级为仅告警继续使用。
    if (
        artifact.content_hash != snapshot_row["content_hash"]
        or artifact.artifact_hash != snapshot_row["artifact_hash"]
        or len(artifact.items) != int(snapshot_row["policy_count"])
    ):
        # 双 hash 或数量任一不一致都阻断整个制品，防止 Gateway 接收部分篡改快照。
        raise ApiError("policy_snapshot.unavailable")
    return artifact


def _require_no_rolled_back_template_policies(
    connection: sqlite3.Connection,
    tenant_id: str,
    items: Iterable[PolicySnapshotItem],
) -> None:
    """拒绝通过普通 Snapshot 回滚复活已经补偿撤销的模板生成策略。

    参数：当前写事务、认证 tenant 与已验证来源制品。返回值：无。关键约束：只读取同租户
    rolled_back application 的稳定 generated_policy_id；命中时使用普通回滚的统一 409，既不
    暴露模板类别，也不尝试隐式恢复 application、权限或已经允许漂移的依赖。
    """

    source_policy_ids = {item.policy_id for item in items}
    if not source_policy_ids:
        return
    rows = connection.execute(
        """
        SELECT generated_policy_id
        FROM policy_template_applications
        WHERE tenant_id=? AND status='rolled_back'
        """,
        (tenant_id,),
    ).fetchall()
    # application 终态优先于历史 Snapshot 内容；两者相交即表示来源制品已失去再次上线授权。
    if any(str(row["generated_policy_id"]) in source_policy_ids for row in rows):
        raise ApiError("policy_snapshot.rollback_target_invalid")


def _require_active_route_targets(
    config: AppConfig,
    connection: sqlite3.Connection,
    tenant_id: str,
    items: Iterable[PolicySnapshotItem],
) -> None:
    """回滚前重新校验历史运行能力和 route target 仍然安全可用。"""

    for item in items:
        # mask 已由 PRD-068 正式实现，但旧部署和混合版本必须继续由默认关闭的运维门禁
        # 失败关闭；不能再用 PRD-045 的“永远不支持”前提阻断已经证明具备能力的部署。
        if item.action_type == "mask" and not config.policy_mask_runtime_enabled:
            raise ApiError("policy_snapshot.action_unsupported")
        # data_class 至今没有可信来源，回滚旧制品时必须执行与发布相同的安全限制。
        if "data_class" in item.variables:
            raise ApiError("policy_snapshot.variable_unsupported")
        # route_to 的历史目标可能已被禁用，回滚前必须按当前租户状态重新验证。
        if item.action_type == "route_to":
            _require_active_route_target(connection, tenant_id, item.target_model_id)


def _require_active_route_target(
    connection: sqlite3.Connection,
    tenant_id: str,
    target_model_id: object,
) -> None:
    """确认 route_to 目标在当前 tenant 内存在且 active，跨租户与禁用使用同一错误。"""

    # 缺失或非字符串目标无法形成可信路由，统一返回目标非法而不暴露数据库信息。
    if not isinstance(target_model_id, str) or not target_model_id:
        raise ApiError("policy_snapshot.route_target_invalid")
    row = connection.execute(
        """
        SELECT 1 FROM models
        WHERE tenant_id=? AND model_id=? AND status='active'
        """,
        (tenant_id, target_model_id),
    ).fetchone()
    # 跨租户、不存在和 disabled 模型使用同一错误，防止通过发布接口枚举模型目录。
    if row is None:
        raise ApiError("policy_snapshot.route_target_invalid")


def _load_current_row(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row | None:
    """通过 tenant+ID+version 三列关系读取当前已发布 head。"""

    return connection.execute(
        f"""
        {_SNAPSHOT_SELECT}
        JOIN policy_snapshot_heads h
          ON h.tenant_id=s.tenant_id
         AND h.current_snapshot_id=s.policy_snapshot_id
         AND h.current_snapshot_version=s.snapshot_version
        WHERE h.tenant_id=? AND s.state='published'
        """,
        (tenant_id,),
    ).fetchone()


def _load_snapshot_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    snapshot_id: str,
) -> sqlite3.Row | None:
    """按 tenant 与 snapshot ID 联合读取历史，避免全局 ID 被误当成租户授权。"""

    return connection.execute(
        f"""
        {_SNAPSHOT_SELECT}
        WHERE s.tenant_id=? AND s.policy_snapshot_id=? AND s.state='published'
        """,
        (tenant_id, snapshot_id),
    ).fetchone()


def _load_idempotency_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> sqlite3.Row | None:
    """在任何 head/candidate CAS 前读取租户级幂等事实。"""

    return connection.execute(
        f"""
        {_SNAPSHOT_SELECT}
        WHERE s.tenant_id=? AND s.request_id=? AND s.state='published'
        """,
        (tenant_id, request_id),
    ).fetchone()


def _replay_idempotent_result(
    row: sqlite3.Row,
    *,
    actor_id: str,
    fingerprint: str,
    request_id: str,
    trace_id: str,
) -> PolicySnapshotMutationResponse:
    """只向原 actor 回放完全相同 fingerprint 的成功结果。"""

    if row["created_by"] != actor_id or row["request_fingerprint"] != fingerprint:
        # 不同管理员只获得通用冲突，不能借 request_id 探测原快照版本、原因或 hash。
        raise ApiError("policy_snapshot.idempotency_conflict")
    return PolicySnapshotMutationResponse(
        policy_snapshot=_snapshot_info(row),
        idempotent_replay=True,
        request_id=request_id,
        trace_id=trace_id,
    )


def _require_head_version(current_row: sqlite3.Row | None, expected_head: int) -> None:
    """比较事务内权威 head；从未发布按版本 0 表达而不是伪造 v0 快照。"""

    current_version = int(current_row["snapshot_version"]) if current_row is not None else 0
    # 客户端看到的版本与锁内 head 不一致时必须冲突，不能覆盖其他管理员的发布。
    if current_version != expected_head:
        raise ApiError("policy_snapshot.version_conflict")


def _snapshot_info(row: sqlite3.Row) -> PolicySnapshotInfo:
    """把已发布数据库行转换为不含 DSL 的 admin 摘要。"""

    content_hash = str(row["content_hash"])
    artifact_hash = str(row["artifact_hash"])
    # 公共历史只返回标准小写 SHA-256，损坏摘要不能作为可信 ETag 或回滚依据。
    if not HASH_RE.fullmatch(content_hash) or not HASH_RE.fullmatch(artifact_hash):
        raise ApiError("policy_snapshot.unavailable")
    return PolicySnapshotInfo(
        policy_snapshot_id=str(row["policy_snapshot_id"]),
        snapshot_version=int(row["snapshot_version"]),
        schema_version=str(row["schema_version"]),
        operation=str(row["operation"]),  # type: ignore[arg-type]
        content_hash=content_hash,
        artifact_hash=artifact_hash,
        policy_count=int(row["policy_count"]),
        change_reason=str(row["change_reason"]),
        approval_status=str(row["approval_status"]),  # type: ignore[arg-type]
        previous_snapshot_version=(
            int(row["previous_snapshot_version"])
            if row["previous_snapshot_version"] is not None
            else None
        ),
        rollback_source_snapshot_version=(
            int(row["rollback_source_snapshot_version"])
            if row["rollback_source_snapshot_version"] is not None
            else None
        ),
        created_by=PolicySnapshotActorInfo(
            user_id=str(row["created_by"]),
            email=str(row["actor_email"]),
            display_name=str(row["actor_display_name"]),
        ),
        created_at=_parse_db_time(row["created_at"]),
        published_at=_parse_db_time(row["published_at"]),
    )


def _artifact_audit_summary(
    artifact: PolicySnapshotArtifact,
    reason: str,
) -> dict[str, object]:
    """构造快照审计安全摘要；原因正文只以不可逆 hash 保存。"""

    return {
        "snapshot_version": artifact.snapshot_version,
        "content_hash": artifact.content_hash,
        "artifact_hash": artifact.artifact_hash,
        "policy_count": len(artifact.items),
        "operation": artifact.operation,
        "previous_snapshot_version": artifact.previous_snapshot_version,
        "rollback_source_snapshot_version": artifact.rollback_source_snapshot_version,
        "approval_status": "not_required",
        "change_reason_hash": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }


def _snapshot_row_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """从当前快照行构造审计 before 摘要，不读取或传播 DSL items。"""

    return {
        "snapshot_version": int(row["snapshot_version"]),
        "content_hash": str(row["content_hash"]),
        "artifact_hash": str(row["artifact_hash"]),
        "policy_count": int(row["policy_count"]),
        "operation": str(row["operation"]),
        "previous_snapshot_version": (
            int(row["previous_snapshot_version"])
            if row["previous_snapshot_version"] is not None
            else None
        ),
        "rollback_source_snapshot_version": (
            int(row["rollback_source_snapshot_version"])
            if row["rollback_source_snapshot_version"] is not None
            else None
        ),
        "approval_status": str(row["approval_status"]),
        "change_reason_hash": hashlib.sha256(
            str(row["change_reason"]).encode("utf-8")
        ).hexdigest(),
    }


def _load_replay_dispatch_facts(
    connection: sqlite3.Connection,
    snapshot_row: sqlite3.Row,
) -> tuple[_SnapshotEvent, AuditRecord]:
    """读取并核验幂等回放需要重派的快照事件与审计事实。

    参数：当前租户写事务连接和已通过 actor/fingerprint 校验的 published 快照行。
    返回：只含 Webhook 白名单字段的持久快照事件、审计事件。
    约束：任何缺失、重复或字段漂移都说明提交事实不可确认，必须拒绝派发而不是从
    本次请求重新拼装事件；这样既能修复 COMMIT 后崩溃漏投，也不会放大数据库篡改。
    """

    tenant_id = str(snapshot_row["tenant_id"])
    snapshot_id = str(snapshot_row["policy_snapshot_id"])
    snapshot_version = int(snapshot_row["snapshot_version"])
    request_id = str(snapshot_row["request_id"])
    trace_id = str(snapshot_row["trace_id"])
    actor_user_id = str(snapshot_row["created_by"])
    operation = str(snapshot_row["operation"])

    event_rows = connection.execute(
        """
        SELECT policy_snapshot_event_id, tenant_id, policy_snapshot_id, snapshot_version,
               event_type, operation, actor_user_id, request_id, trace_id, payload_json, created_at
        FROM policy_snapshot_events
        WHERE tenant_id=? AND policy_snapshot_id=? AND snapshot_version=?
          AND event_type='policy_snapshot.published'
        LIMIT 2
        """,
        (tenant_id, snapshot_id, snapshot_version),
    ).fetchall()
    # 事务合同要求每个 published 快照恰好一个持久事件；LIMIT 2 足以识别重复且避免无界读取。
    if len(event_rows) != 1:
        raise ApiError("policy_snapshot.unavailable")
    event_row = event_rows[0]
    expected_payload: dict[str, object] = {
        "version": snapshot_version,
        "content_hash": str(snapshot_row["content_hash"]),
        "policy_count": int(snapshot_row["policy_count"]),
        "operation": operation,
        "previous_version": (
            int(snapshot_row["previous_snapshot_version"])
            if snapshot_row["previous_snapshot_version"] is not None
            else None
        ),
        "rollback_source_version": (
            int(snapshot_row["rollback_source_snapshot_version"])
            if snapshot_row["rollback_source_snapshot_version"] is not None
            else None
        ),
    }
    event_payload = json.loads(str(event_row["payload_json"]))
    event_scalars_match = (
        str(event_row["tenant_id"]) == tenant_id
        and str(event_row["policy_snapshot_id"]) == snapshot_id
        and int(event_row["snapshot_version"]) == snapshot_version
        and str(event_row["event_type"]) == "policy_snapshot.published"
        and str(event_row["operation"]) == operation
        and str(event_row["actor_user_id"]) == actor_user_id
        and str(event_row["request_id"]) == request_id
        and str(event_row["trace_id"]) == trace_id
    )
    # payload 必须与快照权威摘要逐字段完全相等，拒绝额外字段、DSL 或原因正文被带入 Webhook。
    if not event_scalars_match or event_payload != expected_payload:
        raise ApiError("policy_snapshot.unavailable")

    audit_action = (
        "policy_snapshot.published" if operation == "publish" else "policy_snapshot.rolled_back"
    )
    audit_rows = connection.execute(
        """
        SELECT audit_log_id, tenant_id, actor_user_id, action, object_type, object_id,
               before_summary_json, after_summary_json, changed_fields_json,
               request_id, trace_id, created_at
        FROM audit_logs
        WHERE tenant_id=? AND object_type='policy_snapshot' AND object_id=?
          AND request_id=? AND action=?
        LIMIT 2
        """,
        (tenant_id, snapshot_id, request_id, audit_action),
    ).fetchall()
    if len(audit_rows) != 1:
        # 缺失或重复审计都无法确定唯一稳定 audit event_id，禁止猜测后产生重复或伪造投递。
        raise ApiError("policy_snapshot.unavailable")
    audit_row = audit_rows[0]

    previous_summary: dict[str, object] = {}
    if snapshot_row["previous_snapshot_id"] is not None:
        previous_row = _load_snapshot_row(
            connection,
            tenant_id,
            str(snapshot_row["previous_snapshot_id"]),
        )
        # 审计 before 必须能够回溯到同租户上一版；缺失时不能安全恢复 audit.recorded。
        if previous_row is None:
            raise ApiError("policy_snapshot.unavailable")
        previous_summary = _snapshot_row_audit_summary(previous_row)
    current_summary = _snapshot_row_audit_summary(snapshot_row)
    expected_changed_fields = tuple(
        sorted(
            key
            for key in set(previous_summary) | set(current_summary)
            if previous_summary.get(key) != current_summary.get(key)
        )
    )
    audit_before = json.loads(str(audit_row["before_summary_json"]))
    audit_after = json.loads(str(audit_row["after_summary_json"]))
    audit_changed = json.loads(str(audit_row["changed_fields_json"]))
    audit_scalars_match = (
        str(audit_row["tenant_id"]) == tenant_id
        and str(audit_row["actor_user_id"]) == actor_user_id
        and str(audit_row["action"]) == audit_action
        and str(audit_row["object_type"]) == "policy_snapshot"
        and str(audit_row["object_id"]) == snapshot_id
        and str(audit_row["request_id"]) == request_id
        and str(audit_row["trace_id"]) == trace_id
    )
    if (
        not audit_scalars_match
        or audit_before != previous_summary
        or audit_after != current_summary
        or audit_changed != list(expected_changed_fields)
    ):
        # 审计摘要漂移可能把未批准字段带出控制面，必须在构造 Webhook 信封前失败关闭。
        raise ApiError("policy_snapshot.unavailable")

    event = _SnapshotEvent(
        event_id=str(event_row["policy_snapshot_event_id"]),
        tenant_id=tenant_id,
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_version,
        actor_user_id=actor_user_id,
        operation=operation,
        payload=expected_payload,
        request_id=request_id,
        trace_id=trace_id,
        created_at=_parse_db_time(event_row["created_at"]),
    )
    audit_record = AuditRecord(
        audit_log_id=str(audit_row["audit_log_id"]),
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=audit_action,
        object_type="policy_snapshot",
        object_id=snapshot_id,
        changed_fields=expected_changed_fields,
        trace_id=trace_id,
        created_at=_parse_db_time(audit_row["created_at"]),
    )
    return event, audit_record


def _dispatch_snapshot_event(config: AppConfig, event: _SnapshotEvent) -> None:
    """提交后尽力投递持久发布事件，失败只写固定脱敏日志。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="policy_snapshot.published",
            # 由持久 psevt ID 确定性派生外部事件 ID，与既有 policy/audit 事件模式一致。
            event_id=f"evt_{event.event_id}",
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.snapshot_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # 异常文本可能含 Endpoint URL 或响应正文，结构化日志只能记录固定码与安全 ID。
        logger.error(
            "policy_snapshot.webhook_dispatch_failed",
            extra={
                "event": "policy_snapshot.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.snapshot_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _request_fingerprint(
    *,
    actor_id: str,
    operation: str,
    expected_head: int,
    expected_candidate: str | None,
    target_snapshot_id: str | None,
    reason: str,
) -> str:
    """计算租户内幂等请求指纹，覆盖 actor、CAS、目标与规范化原因。"""

    payload = {
        "actor_id": actor_id,
        "operation": operation,
        "expected_head": expected_head,
        "expected_candidate": expected_candidate,
        "target_snapshot_id": target_snapshot_id,
        "reason": reason,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _internal_etag(
    tenant_id: str,
    artifact: PolicySnapshotArtifact | None,
) -> str:
    """生成同时绑定 wire schema、租户、版本与 artifact hash 的内部强缓存标识。"""

    payload = {
        "wire_schema_version": POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "snapshot_version": artifact.snapshot_version if artifact is not None else None,
        "artifact_hash": artifact.artifact_hash if artifact is not None else None,
    }
    # tenant 必须进入 ETag，避免相同版本与内容摘要在不同租户之间错误复用 304。
    return f'"{hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()}"'


def _head_etag(row: sqlite3.Row | None) -> str:
    """把空 head 表达为强 ETag ``\"0\"``，但不对外伪造 v0 快照。"""

    return f'"{int(row["snapshot_version"]) if row is not None else 0}"'


def _normalize_reason(value: object) -> str:
    """规范化必填变更原因，拒绝控制字符和超长正文。"""

    # 原因正文只能是明确字符串，其他 JSON 类型不能通过隐式转换进入审计元数据。
    if not isinstance(value, str):
        raise ApiError("policy_snapshot.payload_invalid")
    normalized = unicodedata.normalize("NFC", value).strip()
    # 空原因、超长内容和控制字符都会破坏审计展示或日志边界，必须统一拒绝。
    if (
        not normalized
        or len(normalized) > MAX_REASON_LENGTH
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        # 非法原因不进入快照、审计或页面，统一返回不含原文的固定参数错误。
        raise ApiError("policy_snapshot.payload_invalid")
    return normalized


def _normalize_mutation_payload(payload: PublishPolicySnapshotRequest) -> str:
    """拒绝任何未声明写字段，再规范化唯一允许的 reason。

    tenant、actor、approval、version 或 items 即使能被 Pydantic 暂存，也绝不会进入事务；
    该分层只为把框架校验统一收敛到 ``policy_snapshot.payload_invalid``。
    """

    # 任何额外字段都可能试图覆盖 tenant、actor、版本或制品，必须在事务前拒绝。
    if payload.model_extra:
        raise ApiError("policy_snapshot.payload_invalid")
    return _normalize_reason(payload.reason)


def _normalize_head_if_match(value: str | None) -> int:
    """解析发布/回滚强 If-Match，拒绝弱值、通配符、多值与超界版本。"""

    # If-Match 是发布并发控制必需条件，缺失时不能猜测客户端所见 head。
    if value is None:
        raise ApiError("policy_snapshot.if_match_required")
    matched = HEAD_ETAG_RE.fullmatch(value)
    # 弱 ETag、通配符、多值和非规范数字都会产生含糊 CAS，统一按 payload 非法拒绝。
    if matched is None:
        raise ApiError("policy_snapshot.payload_invalid")
    version = int(matched.group(1))
    # 超过数据库版本上限的客户端值不可能合法命中，不能退化为普通版本冲突。
    if version > MAX_SNAPSHOT_VERSION:
        raise ApiError("policy_snapshot.payload_invalid")
    return version


def _normalize_candidate_match(value: str | None) -> str:
    """解析 publish 专用强 candidate ETag；缺失也属于条件请求前置失败。"""

    # 发布还必须携带 candidate 条件头，缺失时禁止只凭 head 发布未知候选内容。
    if value is None:
        raise ApiError("policy_snapshot.if_match_required")
    matched = CANDIDATE_ETAG_RE.fullmatch(value)
    # candidate 只接受单个带引号小写 SHA-256，拒绝弱值、列表和任意文本。
    if matched is None:
        raise ApiError("policy_snapshot.payload_invalid")
    return matched.group(1)


def _normalize_internal_if_none_match(value: str | None) -> str | None:
    """只接受单个强内部 ETag；弱、多值和通配符不会被含糊降级为缓存 miss。"""

    if value is None:
        return None
    # 内部缓存协商也只接受单个强 ETag，避免代理通配符隐藏租户快照变化。
    if INTERNAL_ETAG_RE.fullmatch(value) is None:
        raise ApiError("policy_snapshot.payload_invalid")
    return value


def _normalize_request_id(value: object) -> str:
    """验证共享中间件 request_id 可安全作为租户幂等键持久化。"""

    # request_id 必须是共享中间件生成的字符串，禁止把其他类型持久化为幂等键。
    if not isinstance(value, str):
        raise ApiError("policy_snapshot.payload_invalid")
    normalized = value.strip()
    # 空值、控制字符和超长 ID 会破坏事件关联与索引边界，必须在写事务前拒绝。
    if (
        not normalized
        or len(normalized) > MAX_REQUEST_ID_LENGTH
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        # 非法 request_id 不能进入租户幂等索引，错误响应也不回显客户端原始值。
        raise ApiError("policy_snapshot.payload_invalid")
    return normalized


def _normalize_snapshot_id(value: object) -> str:
    """验证 rollback 路径 ID；非法与不存在统一返回 not_found，避免对象探测。"""

    # 非规范路径 ID 与不存在统一使用 not_found，避免暴露内部 ID 格式的探测差异。
    if not isinstance(value, str) or SNAPSHOT_ID_RE.fullmatch(value) is None:
        raise ApiError("policy_snapshot.not_found")
    return value


def _normalize_internal_tenant_id(value: object) -> str:
    """验证内部路径 tenant；非法、未知与 disabled 使用相同不可探测错误。"""

    # 非规范 tenant 路径与未知租户使用同一错误，内部 Token 也不能跨边界枚举租户。
    if not isinstance(value, str) or TENANT_ID_RE.fullmatch(value) is None:
        raise ApiError("policy_snapshot.tenant_unavailable")
    return value


def _normalize_pagination_value(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """解析十进制分页参数，拒绝符号、空白、bool 语义和超界值。"""

    if value is None:
        return default
    # 分页只接受无符号规范十进制，拒绝空白、正号、负号和前导零等含糊表示。
    if not re.fullmatch(r"0|[1-9][0-9]{0,10}", value):
        raise ApiError("policy_snapshot.payload_invalid")
    parsed = int(value)
    # 越界分页可能造成无界扫描或含糊空页，必须在执行租户查询前直接拒绝。
    if parsed < minimum or parsed > maximum:
        raise ApiError("policy_snapshot.payload_invalid")
    return parsed


def _reject_unknown_query(query_keys: tuple[str, ...], allowed: frozenset[str]) -> None:
    """拒绝未知 query，包括任何试图覆盖 tenant 或 actor 的参数。"""

    # 未知 query 可能试图注入 tenant 或 actor 过滤，不能被框架静默忽略。
    if any(key not in allowed for key in query_keys):
        raise ApiError("policy_snapshot.payload_invalid")


def _canonical_json_bytes(value: object) -> bytes:
    """生成平台无关 UTF-8 canonical JSON，供指纹和 ETag 使用。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_text(value: object) -> str:
    """把安全事件或变量摘要编码为稳定紧凑 JSON。"""

    return _canonical_json_bytes(value).decode("utf-8")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并折叠底层 URL/驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 连接错误可能携带数据库路径或驱动文本，统一转换为不泄露细节的快照错误。
    except DbError as exc:
        raise ApiError("policy_snapshot.unavailable") from exc


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """尽力释放写事务；rollback 的底层异常不得覆盖原统一错误。"""

    try:
        connection.rollback()
    # rollback 自身失败不能覆盖原始安全错误，也不能把 SQLite 异常文本返回客户端。
    except sqlite3.Error:
        pass


def _utcnow() -> datetime:
    """返回 UTC 当前时间，统一快照、head、事件和审计时间口径。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把带时区 datetime 编码为 SQLite 使用的 UTC ISO 文本。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """把数据库 UTC ISO 文本恢复为有时区 datetime，损坏值由调用方统一映射。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# 公共快照读取始终按 tenant 追加 WHERE，并联接同租户 actor 生成安全管理摘要。
_SNAPSHOT_SELECT = """
SELECT
  s.policy_snapshot_id, s.tenant_id, s.snapshot_version, s.schema_version, s.state,
  s.operation, s.previous_snapshot_id, s.previous_snapshot_version,
  s.rollback_source_snapshot_id, s.rollback_source_snapshot_version,
  s.request_id, s.trace_id, s.request_fingerprint, s.content_hash, s.artifact_hash,
  s.policy_count, s.change_reason, s.approval_status, s.created_by,
  s.created_at, s.published_at,
  u.email AS actor_email, u.display_name AS actor_display_name
FROM policy_snapshots s
JOIN users u ON u.tenant_id=s.tenant_id AND u.user_id=s.created_by
"""
