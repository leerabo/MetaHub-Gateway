"""PRD-056 Provider 错误归一规则的租户服务、版本事务与内部快照。

所有公共读写都只使用认证上下文中的 tenant_id/actor。真实变更在一个 ``BEGIN IMMEDIATE``
事务内追加不可变规则版本、推进稳定资源指针、重建完整 active overlay、追加 head history、
Source Outbox、Audit 与幂等 operation；任一步失败都整体回滚。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.responses import Response as FastAPIResponse
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_error_map import (
    BUILTIN_CATALOG_VERSION,
    EMPTY_OVERLAY_CONTENT_HASH,
    PROVIDER_ERROR_OUTPUT_CODES,
    ProviderErrorMapArtifact,
    ProviderErrorRule,
    ProviderErrorRuleValidationError,
    builtin_catalog_registry_rows,
    compile_provider_error_rule,
    provider_error_map_etag,
    provider_error_output_spec,
    provider_error_overlay_content_hash,
    provider_error_rule_to_wire,
    serialize_provider_error_map_snapshot,
    tenant_rule_condition_hash,
)
from pydantic import ValidationError

from metahub_control.audit import (
    AuditRequestMeta,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    CreateProviderErrorMapRequest,
    ProviderErrorMapHeadInfo,
    ProviderErrorMapListResponse,
    ProviderErrorMapMutationResponse,
    ProviderErrorMapProviderOption,
    ProviderErrorMapRuleInfo,
    UpdateProviderErrorMapRequest,
)

# 租户资源上限、revision 边界、分页、资源 ID、强 ETag 与可重放业务错误共同定义控制面合同；
# 所有计数与查询都必须带认证 tenant_id，错误映射不得回显 pattern 或底层数据库信息。
MAX_ACTIVE_RULES = 128
MAX_STABLE_RULES = 512
MAX_HEAD_REVISION = 2_147_483_647
DEFAULT_LIMIT = 100
MAX_LIMIT = 100
MAX_OFFSET = 10_000
_MAP_ID = re.compile(r"^pem_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STRONG_ETAG = re.compile(r'^"[0-9a-f]{64}"$')
_BUSINESS_STATUS = {
    "error_map.not_found": 404,
    "error_map.input_invalid": 422,
    "error_map.update_empty": 422,
    "error_map.output_code_invalid": 422,
    "error_map.pattern_invalid": 422,
    "error_map.pattern_sensitive": 422,
    "error_map.conflict": 409,
    "error_map.version_conflict": 409,
    "error_map.limit_exceeded": 409,
}


def list_provider_error_maps(
    config: AppConfig,
    context: AuthContext,
    *,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> tuple[ProviderErrorMapListResponse, str]:
    """返回当前租户合并规则、Provider 选项、分页和同一读取事务内的 head。"""

    parsed = _parse_list_query(query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        # 精确资源模式仍只按认证 tenant 查询，不能受分页筛选影响或返回其他租户同 ID 行。
        if parsed["error_map_id"] is not None:
            row = _load_current_rule_row(
                connection,
                context.user.tenant_id,
                str(parsed["error_map_id"]),
                required=False,
            )
            # 不存在与跨租户保持同形 404，避免通过资源 ID 枚举其他租户规则。
            if row is None:
                raise ApiError("error_map.not_found")
            rules = [_tenant_rule_info(row)]
            total, limit, offset = 1, 1, 0
        else:
            provider_id = parsed["provider_id"]
            # Provider 筛选必须先验证同租户 owner，跨租户 ID 不得退化为空列表泄露差异。
            if provider_id is not None:
                _require_provider(connection, context.user.tenant_id, str(provider_id))
            rules = [
                _tenant_rule_info(row)
                for row in _load_current_rule_rows(connection, context.user.tenant_id)
            ]
            rules.extend(_builtin_rule_infos())
            rules = _filter_rules(
                rules,
                provider_id=str(provider_id) if provider_id is not None else None,
                source=parsed["source"],
                status=parsed["status"],
                output_error_code=parsed["output_error_code"],
            )
            total = len(rules)
            limit = int(parsed["limit"])
            offset = int(parsed["offset"])
            rules = rules[offset : offset + limit]
        head = _load_head(connection, context.user.tenant_id)
        providers = _provider_options(connection, context.user.tenant_id)
        connection.commit()
    # 已知业务错误回滚只读事务并保留统一码，不转换成瞬时服务不可用。
    except ApiError:
        connection.rollback()
        raise
    # 持久坏值、数据库和 DTO 校验异常统一折叠，底层 SQL 与规则 pattern 不得进入响应。
    except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
        connection.rollback()
        # 固定服务错误码保留 request_id 外层包络，同时阻止异常正文泄露内部状态。
        raise ApiError("error_map.unavailable") from exc
    finally:
        connection.close()
    response = _list_response(
        rules,
        providers,
        head,
        total=total,
        limit=limit,
        offset=offset,
        request_id=request_id,
        trace_id=trace_id,
    )
    return response, provider_error_map_etag(
        context.user.tenant_id,
        head.revision,
        head.content_hash,
        head.builtin_catalog_version,
    )


def create_provider_error_map(
    config: AppConfig,
    context: AuthContext,
    payload: CreateProviderErrorMapRequest | UpdateProviderErrorMapRequest,
    *,
    audit_request_meta: AuditRequestMeta | None,
    request_id: str,
    trace_id: str,
) -> ProviderErrorMapMutationResponse:
    """创建租户规则，并以 tenant+actor+request_id 提供结果级幂等。"""

    # 路由层虽已做 strict model 校验，服务边界仍拒绝错误 DTO，避免内部调用绕过合同。
    if not isinstance(payload, CreateProviderErrorMapRequest):
        raise ApiError("error_map.input_invalid")
    intent = {
        "body": payload.model_dump(mode="json"),
        "kind": "create",
        "schema_version": 1,
    }
    fingerprint = _fingerprint(intent)
    connection = _connect(config)
    audit_record = None
    business_error: ApiError | None = None
    result: ProviderErrorMapMutationResponse | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _replay_operation(
            connection,
            context,
            request_id=request_id,
            fingerprint=fingerprint,
            trace_id=trace_id,
        )
        if replay is not None:
            connection.commit()
            return replay
        # 稳定资源达到租户上限后记录可重放业务结果，不创建 ID、版本或 Source Event。
        if _stable_rule_count(connection, context.user.tenant_id) >= MAX_STABLE_RULES:
            business_error = _record_business_error(
                connection,
                context,
                request_id=request_id,
                fingerprint=fingerprint,
                operation="create",
                target_id=None,
                code="error_map.limit_exceeded",
            )
        else:
            error_map_id = f"pem_{uuid4().hex}"
            candidate, error = _compile_candidate(
                payload.model_dump(mode="python"),
                tenant_id=context.user.tenant_id,
                error_map_id=error_map_id,
                version=1,
                connection=connection,
            )
            # Shared 编译错误转换为固定业务码并写 operation，绝不保存或回显非法 pattern。
            if error is not None:
                business_error = _record_business_error(
                    connection,
                    context,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    operation="create",
                    target_id=None,
                    code=error,
                )
            elif candidate is not None and _activation_rejected(
                connection,
                context.user.tenant_id,
                candidate,
                exclude_map_id=None,
            ):
                code = (
                    "error_map.limit_exceeded"
                    if _active_rule_count(connection, context.user.tenant_id) >= MAX_ACTIVE_RULES
                    else "error_map.conflict"
                )
                business_error = _record_business_error(
                    connection,
                    context,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    operation="create",
                    target_id=None,
                    code=code,
                )
            elif candidate is not None:
                try:
                    next_revision, changed_at = _next_head(connection, context.user.tenant_id)
                # head 上限属于同一 canonical 意图的可重放业务失败，其他领域错误继续传播。
                except ApiError as exc:
                    # 仅限额码可以写业务 operation，未知错误不能伪装成确定性结果。
                    if exc.code != "error_map.limit_exceeded":
                        raise
                    # canonical 意图已形成，head 限额属于可重放业务结果而不是瞬时 503。
                    business_error = _record_business_error(
                        connection,
                        context,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        operation="create",
                        target_id=None,
                        code=exc.code,
                    )
                else:
                    result, audit_record = _commit_create(
                        connection,
                        context,
                        candidate,
                        next_revision=next_revision,
                        changed_at=changed_at,
                        fingerprint=fingerprint,
                        audit_request_meta=audit_request_meta,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
        connection.commit()
    # 触发器和唯一约束失败必须整体回滚版本、head、事件、Audit 与 operation。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 仅根据批准的约束类别映射固定码，不把 SQLite 详情或持久值回显。
        raise _integrity_error(exc) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    # 提交后再抛已持久化业务错误，使同 request_id 重试可以回放完全相同结果。
    if business_error is not None:
        raise business_error
    # 无业务错误却没有结果表示事务状态损坏，统一 503 而不能构造虚假成功。
    if result is None:
        raise ApiError("error_map.unavailable")
    _dispatch_after_commit(config, context, audit_record, request_id, trace_id)
    return result


def update_provider_error_map(
    config: AppConfig,
    context: AuthContext,
    error_map_id: str,
    payload: CreateProviderErrorMapRequest | UpdateProviderErrorMapRequest,
    *,
    audit_request_meta: AuditRequestMeta | None,
    request_id: str,
    trace_id: str,
) -> ProviderErrorMapMutationResponse:
    """CAS 更新稳定规则；同值 no-op 不推进版本、head、Audit 或 Source Event。"""

    # 服务层重复校验 DTO 类型，防止内部调用绕过 expected_version 与 exclude_unset 语义。
    if not isinstance(payload, UpdateProviderErrorMapRequest):
        raise ApiError("error_map.input_invalid")
    intent = {
        "body": payload.model_dump(mode="json", exclude_unset=True),
        "error_map_id": error_map_id,
        "kind": "update",
        "schema_version": 1,
    }
    fingerprint = _fingerprint(intent)
    connection = _connect(config)
    audit_record = None
    business_error: ApiError | None = None
    result: ProviderErrorMapMutationResponse | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _replay_operation(
            connection,
            context,
            request_id=request_id,
            fingerprint=fingerprint,
            trace_id=trace_id,
        )
        if replay is not None:
            connection.commit()
            return replay
        existing = (
            _load_current_rule_row(
                connection,
                context.user.tenant_id,
                error_map_id,
                required=False,
            )
            if _MAP_ID.fullmatch(error_map_id)
            else None
        )
        if existing is None:
            business_error = _record_business_error(
                connection,
                context,
                request_id=request_id,
                fingerprint=fingerprint,
                operation="update",
                target_id=None,
                code="error_map.not_found",
            )
        elif type(payload.expected_version) is not int or payload.expected_version < 1:
            business_error = _record_business_error(
                connection,
                context,
                request_id=request_id,
                fingerprint=fingerprint,
                operation="update",
                target_id=error_map_id,
                code="error_map.input_invalid",
            )
        elif payload.expected_version != int(existing["version"]):
            business_error = _record_business_error(
                connection,
                context,
                request_id=request_id,
                fingerprint=fingerprint,
                operation="update",
                target_id=error_map_id,
                code="error_map.version_conflict",
            )
        else:
            merged = _editable_from_row(existing)
            for field in payload.model_fields_set - {"expected_version"}:
                merged[field] = getattr(payload, field)
            candidate, error = _compile_candidate(
                merged,
                tenant_id=context.user.tenant_id,
                error_map_id=error_map_id,
                version=int(existing["version"]) + 1,
                connection=connection,
            )
            operation = _update_operation(existing, candidate)
            # 编译失败只记录安全业务码，非法 pattern 不进入版本、Audit 或事件载荷。
            if error is not None:
                business_error = _record_business_error(
                    connection,
                    context,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    operation=operation,
                    target_id=error_map_id,
                    code=error,
                )
            elif candidate is not None and _same_editable(existing, candidate):
                head = _load_head(connection, context.user.tenant_id)
                current_info = _tenant_rule_info(existing)
                result = _mutation_response(
                    current_info,
                    head,
                    changed=False,
                    replay=False,
                    request_id=request_id,
                    trace_id=trace_id,
                )
                _insert_operation(
                    connection,
                    context,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    operation=operation,
                    target_id=error_map_id,
                    outcome="noop",
                    http_status=200,
                    error_code=None,
                    changed=False,
                    result_version=current_info.version,
                    result_head_revision=head.revision,
                    source_event_id=None,
                    snapshot=_mutation_snapshot(result),
                )
            elif candidate is not None and _activation_rejected(
                connection,
                context.user.tenant_id,
                candidate,
                exclude_map_id=error_map_id,
            ):
                code = (
                    "error_map.limit_exceeded"
                    if candidate.status == "active"
                    and str(existing["status"]) != "active"
                    and _active_rule_count(connection, context.user.tenant_id) >= MAX_ACTIVE_RULES
                    else "error_map.conflict"
                )
                business_error = _record_business_error(
                    connection,
                    context,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    operation=operation,
                    target_id=error_map_id,
                    code=code,
                )
            elif candidate is not None:
                try:
                    next_revision, changed_at = _next_head(connection, context.user.tenant_id)
                # revision 上限可作为可重放业务结果，其他错误必须让整个 update 回滚。
                except ApiError as exc:
                    # 未登记领域错误不能写入 operation 快照，避免重试永久回放错误分类。
                    if exc.code != "error_map.limit_exceeded":
                        raise
                    business_error = _record_business_error(
                        connection,
                        context,
                        request_id=request_id,
                        fingerprint=fingerprint,
                        operation=operation,
                        target_id=error_map_id,
                        code=exc.code,
                    )
                else:
                    result, audit_record = _commit_update(
                        connection,
                        context,
                        existing,
                        candidate,
                        operation=operation,
                        next_revision=next_revision,
                        changed_at=changed_at,
                        fingerprint=fingerprint,
                        audit_request_meta=audit_request_meta,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
        connection.commit()
    # CAS 相关唯一约束或 owner trigger 失败必须整体回滚，不留下半推进 head。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 约束异常折叠为固定 conflict/limit/unavailable，禁止 SQL 和字段值外泄。
        raise _integrity_error(exc) from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    # 确定性业务失败已与 operation 同事务提交，响应阶段抛出后仍可按幂等键回放。
    if business_error is not None:
        raise business_error
    # 事务未产生结果且无业务错误属于内部不变量破坏，只返回统一不可用码。
    if result is None:
        raise ApiError("error_map.unavailable")
    _dispatch_after_commit(config, context, audit_record, request_id, trace_id)
    return result


def build_internal_provider_error_map_response(
    config: AppConfig,
    tenant_id: str,
    *,
    if_none_match: str | None,
    request_id: str,
    trace_id: str,
) -> FastAPIResponse:
    """构造 Gateway 内部 active overlay 的 exact-schema 200/304 响应，并执行租户与强 ETag 校验。"""

    # 内部 path tenant 必须是有界字符串，坏值与不存在租户统一为不可枚举结果。
    if type(tenant_id) is not str or not tenant_id or len(tenant_id) > 128:
        raise ApiError("error_map.tenant_unavailable")
    # 条件请求只接受固定强 ETag 语法，弱标签和列表形式不能由服务端猜测。
    if if_none_match is not None and _STRONG_ETAG.fullmatch(if_none_match) is None:
        raise ApiError("error_map.snapshot_unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        tenant = connection.execute(
            "SELECT status FROM tenants WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        # 只有当前 active 租户可取得快照，停用与不存在保持同形且不返回规则元数据。
        if tenant is None or tenant["status"] != "active":
            raise ApiError("error_map.tenant_unavailable")
        rules = _load_active_rules(connection, tenant_id)
        head = _load_head(connection, tenant_id)
        calculated = provider_error_overlay_content_hash(rules)
        # 当前规则必须与同租户 head 摘要一致，revision 0 绝不允许携带租户覆盖。
        if calculated != head.content_hash or (head.revision == 0 and rules):
            raise ApiError("error_map.snapshot_unavailable")
        connection.commit()
    # 已知租户/快照错误保持固定码，但必须回滚一致性读取事务。
    except ApiError:
        connection.rollback()
        raise
    # 数据库、持久 wire 与 Shared 编译异常统一转为快照不可用，不泄露规则内容。
    except (sqlite3.Error, ProviderErrorRuleValidationError, TypeError, ValueError) as exc:
        connection.rollback()
        # 原始异常可能包含 SQL、pattern 或持久值，只保留固定内部接口错误码。
        raise ApiError("error_map.snapshot_unavailable") from exc
    finally:
        connection.close()
    etag = provider_error_map_etag(
        tenant_id,
        head.revision,
        head.content_hash,
        head.builtin_catalog_version,
    )
    headers = {"Cache-Control": "no-store", "ETag": etag}
    if if_none_match == etag:
        return FastAPIResponse(status_code=304, headers=headers)
    artifact = ProviderErrorMapArtifact(
        tenant_id=tenant_id,
        revision=head.revision,
        overlay_content_hash=head.content_hash,
        builtin_catalog_version=head.builtin_catalog_version,
        rules=tuple(rules),
    )
    payload = serialize_provider_error_map_snapshot(
        artifact,
        request_id=request_id,
        trace_id=trace_id,
    )
    body = _canonical_json(payload).encode("utf-8")
    # 序列化后再次执行硬上限，防止规则数量边界内仍由文本字段放大内部响应。
    if len(body) > 262_144:
        raise ApiError("error_map.snapshot_unavailable")
    return FastAPIResponse(
        content=body,
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


def _commit_create(
    connection: sqlite3.Connection,
    context: AuthContext,
    candidate: ProviderErrorRule,
    *,
    next_revision: int,
    changed_at: str,
    fingerprint: str,
    audit_request_meta: AuditRequestMeta | None,
    request_id: str,
    trace_id: str,
) -> tuple[ProviderErrorMapMutationResponse, object | None]:
    """在调用方事务中写入创建版本、稳定资源、head、事件、Audit 与 ledger。"""

    version_id = f"pemv_{uuid4().hex}"
    _insert_version(
        connection,
        context,
        candidate,
        version_id=version_id,
        head_revision=next_revision,
        created_at=changed_at,
    )
    connection.execute(
        """
        INSERT INTO provider_error_maps(
          provider_error_map_id, tenant_id, current_version, created_by, updated_by,
          created_at, updated_at
        ) VALUES(?, ?, 1, ?, ?, ?, ?)
        """,
        (
            candidate.error_map_id,
            context.user.tenant_id,
            context.user.user_id,
            context.user.user_id,
            changed_at,
            changed_at,
        ),
    )
    head, head_version_id = _advance_head(
        connection,
        context.user.tenant_id,
        revision=next_revision,
        changed_at=changed_at,
    )
    info = _load_rule_info(connection, context.user.tenant_id, candidate.error_map_id)
    event_id = _insert_event(
        connection,
        context,
        info,
        operation="create",
        version_id=version_id,
        head_version_id=head_version_id,
        head=head,
        fingerprint=fingerprint,
        request_id=request_id,
        trace_id=trace_id,
        created_at=changed_at,
    )
    audit_record = record_audit_log(
        connection,
        context,
        action="error_map.created",
        object_type="provider_error_map",
        object_id=info.error_map_id,
        object_name=info.error_map_id,
        before_summary={},
        after_summary=_audit_summary(info),
        request_meta=audit_request_meta or AuditRequestMeta(None, None),
        request_id=request_id,
        trace_id=trace_id,
    )
    result = _mutation_response(
        info,
        head,
        changed=True,
        replay=False,
        request_id=request_id,
        trace_id=trace_id,
    )
    _insert_operation(
        connection,
        context,
        request_id=request_id,
        fingerprint=fingerprint,
        operation="create",
        target_id=info.error_map_id,
        outcome="success",
        http_status=201,
        error_code=None,
        changed=True,
        result_version=info.version,
        result_head_revision=head.revision,
        source_event_id=event_id,
        snapshot=_mutation_snapshot(result),
    )
    return result, audit_record


def _commit_update(
    connection: sqlite3.Connection,
    context: AuthContext,
    existing: sqlite3.Row,
    candidate: ProviderErrorRule,
    *,
    operation: str,
    next_revision: int,
    changed_at: str,
    fingerprint: str,
    audit_request_meta: AuditRequestMeta | None,
    request_id: str,
    trace_id: str,
) -> tuple[ProviderErrorMapMutationResponse, object | None]:
    """在调用方事务中追加更新版本并推进全部权威事实。"""

    before = _tenant_rule_info(existing)
    version_id = f"pemv_{uuid4().hex}"
    _insert_version(
        connection,
        context,
        candidate,
        version_id=version_id,
        head_revision=next_revision,
        created_at=changed_at,
    )
    updated = connection.execute(
        """
        UPDATE provider_error_maps
        SET current_version=?, updated_by=?, updated_at=?
        WHERE tenant_id=? AND provider_error_map_id=? AND current_version=?
        """,
        (
            candidate.version,
            context.user.user_id,
            changed_at,
            context.user.tenant_id,
            candidate.error_map_id,
            int(existing["version"]),
        ),
    )
    # 稳定资源 current_version 使用旧版本 CAS，并发写入丢失时必须返回版本冲突并整体回滚。
    if updated.rowcount != 1:
        raise ApiError("error_map.version_conflict")
    head, head_version_id = _advance_head(
        connection,
        context.user.tenant_id,
        revision=next_revision,
        changed_at=changed_at,
    )
    info = _load_rule_info(connection, context.user.tenant_id, candidate.error_map_id)
    event_id = _insert_event(
        connection,
        context,
        info,
        operation=operation,
        version_id=version_id,
        head_version_id=head_version_id,
        head=head,
        fingerprint=fingerprint,
        request_id=request_id,
        trace_id=trace_id,
        created_at=changed_at,
    )
    audit_record = record_audit_log(
        connection,
        context,
        action="error_map.updated",
        object_type="provider_error_map",
        object_id=info.error_map_id,
        object_name=info.error_map_id,
        before_summary=_audit_summary(before),
        after_summary=_audit_summary(info),
        request_meta=audit_request_meta or AuditRequestMeta(None, None),
        request_id=request_id,
        trace_id=trace_id,
    )
    result = _mutation_response(
        info,
        head,
        changed=True,
        replay=False,
        request_id=request_id,
        trace_id=trace_id,
    )
    _insert_operation(
        connection,
        context,
        request_id=request_id,
        fingerprint=fingerprint,
        operation=operation,
        target_id=info.error_map_id,
        outcome="success",
        http_status=200,
        error_code=None,
        changed=True,
        result_version=info.version,
        result_head_revision=head.revision,
        source_event_id=event_id,
        snapshot=_mutation_snapshot(result),
    )
    return result, audit_record


def _insert_version(
    connection: sqlite3.Connection,
    context: AuthContext,
    rule: ProviderErrorRule,
    *,
    version_id: str,
    head_revision: int,
    created_at: str,
) -> None:
    """追加一条完整不可变规则版本；循环外键在事务提交时统一验证。"""

    wire = provider_error_rule_to_wire(rule)
    connection.execute(
        """
        INSERT INTO provider_error_map_versions(
          provider_error_map_version_id, tenant_id, provider_error_map_id, version,
          provider_id, priority, http_statuses_json, http_status_class, error_field,
          field_operator, field_pattern, message_operator, message_pattern,
          output_error_code, status, pattern_hash, condition_hash, head_revision,
          created_by, updated_by, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            context.user.tenant_id,
            rule.error_map_id,
            rule.version,
            rule.provider_id,
            rule.priority,
            _canonical_json(list(rule.http_statuses)),
            rule.http_status_class,
            rule.error_field,
            rule.field_operator,
            rule.field_pattern,
            rule.message_operator,
            rule.message_pattern,
            rule.output_error_code,
            rule.status,
            rule.pattern_hash,
            tenant_rule_condition_hash(wire),
            head_revision,
            context.user.user_id,
            context.user.user_id,
            created_at,
        ),
    )


def _advance_head(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    revision: int,
    changed_at: str,
) -> tuple[ProviderErrorMapHeadInfo, str]:
    """从当前 map 指针权威重编译 active overlay，并追加 history/current head。"""

    rules = _load_active_rules(connection, tenant_id)
    content_hash = provider_error_overlay_content_hash(rules)
    refs = [
        {"error_map_id": rule.error_map_id, "version": rule.version}
        for rule in sorted(rules, key=lambda item: item.error_map_id)
    ]
    head_version_id = f"pemhv_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO provider_error_map_head_versions(
          provider_error_map_head_version_id, tenant_id, revision, content_hash,
          builtin_catalog_version, active_rule_refs_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            head_version_id,
            tenant_id,
            revision,
            content_hash,
            BUILTIN_CATALOG_VERSION,
            _canonical_json(refs),
            changed_at,
        ),
    )
    current = connection.execute(
        "SELECT revision FROM provider_error_map_heads WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    if current is None:
        connection.execute(
            """
            INSERT INTO provider_error_map_heads(
              tenant_id, revision, content_hash, updated_at
            ) VALUES(?, ?, ?, ?)
            """,
            (tenant_id, revision, content_hash, changed_at),
        )
    else:
        updated = connection.execute(
            """
            UPDATE provider_error_map_heads
            SET revision=?, content_hash=?, updated_at=?
            WHERE tenant_id=? AND revision=?
            """,
            (revision, content_hash, changed_at, tenant_id, revision - 1),
        )
        # head 只能从相邻 revision CAS 推进，迟到事务不得覆盖更新后的租户快照。
        if updated.rowcount != 1:
            raise ApiError("error_map.version_conflict")
    return (
        ProviderErrorMapHeadInfo(
            revision=revision,
            content_hash=content_hash,
            builtin_catalog_version=BUILTIN_CATALOG_VERSION,
            updated_at=changed_at,
        ),
        head_version_id,
    )


def _insert_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    rule: ProviderErrorMapRuleInfo,
    *,
    operation: str,
    version_id: str,
    head_version_id: str,
    head: ProviderErrorMapHeadInfo,
    fingerprint: str,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> str:
    """写入 immutable 业务事件和待派发 Source Outbox，payload 不含 pattern。"""

    event_id = f"pemevt_{uuid4().hex}"
    payload = {
        "head_revision": head.revision,
        "operation": operation,
        "output_error_code": rule.output_error_code,
        "pattern_hash": rule.pattern_hash,
        "priority": rule.priority,
        "provider_error_map_id": rule.error_map_id,
        "provider_scope": rule.provider_id or "all",
        "request_id": request_id,
        "rule_version": rule.version,
        "schema_version": 1,
        "status": rule.status,
    }
    connection.execute(
        """
        INSERT INTO provider_error_map_events(
          provider_error_map_event_id, tenant_id, provider_error_map_id, operation,
          rule_version, provider_error_map_version_id, provider_error_map_head_version_id,
          head_revision, overlay_content_hash, builtin_catalog_version, pattern_hash,
          status, provider_id, actor_user_id, request_id, trace_id, request_fingerprint,
          payload_json, dispatch_status, dispatch_attempts, next_attempt_at,
          dispatched_at, claim_token, lease_owner_id, lease_expires_at, last_error_code,
          created_at, updated_at
        ) VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          'pending', 0, ?, NULL, NULL, NULL, NULL, NULL, ?, ?
        )
        """,
        (
            event_id,
            context.user.tenant_id,
            rule.error_map_id,
            operation,
            rule.version,
            version_id,
            head_version_id,
            head.revision,
            head.content_hash,
            head.builtin_catalog_version,
            rule.pattern_hash,
            rule.status,
            rule.provider_id,
            context.user.user_id,
            request_id,
            trace_id,
            fingerprint,
            _canonical_json(payload),
            created_at,
            created_at,
            created_at,
        ),
    )
    return event_id


def _insert_operation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
    operation: str,
    target_id: str | None,
    outcome: str,
    http_status: int,
    error_code: str | None,
    changed: bool,
    result_version: int | None,
    result_head_revision: int | None,
    source_event_id: str | None,
    snapshot: Mapping[str, object],
) -> None:
    """追加不可变 operation ledger；snapshot 只保存安全结果标量。"""

    connection.execute(
        """
        INSERT INTO provider_error_map_operations(
          provider_error_map_operation_id, tenant_id, actor_user_id, request_id,
          request_fingerprint, operation, target_provider_error_map_id, outcome,
          http_status, error_code, changed, result_version, result_head_revision,
          source_event_id, response_snapshot_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"pemo_{uuid4().hex}",
            context.user.tenant_id,
            context.user.user_id,
            request_id,
            fingerprint,
            operation,
            target_id,
            outcome,
            http_status,
            error_code,
            int(changed),
            result_version,
            result_head_revision,
            source_event_id,
            _canonical_json(dict(snapshot)),
            _to_db_time(datetime.now(UTC)),
        ),
    )


def _record_business_error(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
    operation: str,
    target_id: str | None,
    code: str,
) -> ApiError:
    """记录确定性 404/409/422 结果，响应丢失后可用同一意图安全重放。"""

    status_code = _BUSINESS_STATUS.get(code)
    # 未登记错误不具备确定性重放资格，禁止把瞬时或内部错误永久写入 operation。
    if status_code is None:
        raise ApiError("error_map.unavailable")
    _insert_operation(
        connection,
        context,
        request_id=request_id,
        fingerprint=fingerprint,
        operation=operation,
        target_id=target_id,
        outcome="business_error",
        http_status=status_code,
        error_code=code,
        changed=False,
        result_version=None,
        result_head_revision=None,
        source_event_id=None,
        snapshot={"error_code": code, "http_status": status_code},
    )
    return ApiError(code)


def _replay_operation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
    trace_id: str,
) -> ProviderErrorMapMutationResponse | None:
    """在读取任何目标资源前重放同意图 ledger；不同意图固定冲突。"""

    row = connection.execute(
        """
        SELECT * FROM provider_error_map_operations
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        """,
        (context.user.tenant_id, context.user.user_id, request_id),
    ).fetchone()
    if row is None:
        return None
    # 同 request_id 只有 canonical 指纹完全一致才可回放，不同意图固定冲突。
    if row["request_fingerprint"] != fingerprint:
        raise ApiError("error_map.request_conflict")
    try:
        snapshot = json.loads(str(row["response_snapshot_json"]))
    # 持久快照损坏时不能猜测成功结果，原始 JSON 与解析异常也不得回显。
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ApiError("error_map.unavailable") from exc
    # 业务失败只重放已批准 code，并核对 snapshot 与独立列完全一致。
    if row["outcome"] == "business_error":
        code = snapshot.get("error_code") if type(snapshot) is dict else None
        # 任一字段漂移都说明 operation 被破坏，不能按当前错误表重新解释历史结果。
        if code != row["error_code"] or code not in _BUSINESS_STATUS:
            raise ApiError("error_map.unavailable")
        raise ApiError(str(code))
    # 成功/no-op 快照采用 exact schema，未知字段不能未经评审进入公开响应。
    if type(snapshot) is not dict or set(snapshot) != {"changed", "error_map", "head"}:
        raise ApiError("error_map.unavailable")
    # changed 必须是真实布尔值，整数或字符串会改变前端 mutation 状态语义。
    if type(snapshot["changed"]) is not bool:
        raise ApiError("error_map.unavailable")
    raw_head = snapshot["head"]
    # head 使用当前 DTO 精确字段集，防止租户、revision 或摘要字段缺失后仍被回放。
    if type(raw_head) is not dict or set(raw_head) != set(ProviderErrorMapHeadInfo.model_fields):
        raise ApiError("error_map.unavailable")
    head_payload = dict(raw_head)
    updated_at = head_payload.get("updated_at")
    if updated_at is not None:
        # 持久时间只允许 canonical 字符串，任意对象不能通过 str/repr 进入 DTO。
        if type(updated_at) is not str:
            raise ApiError("error_map.unavailable")
        try:
            head_payload["updated_at"] = _parse_time(updated_at)
        # 时间损坏会破坏 revision 审计顺序，只返回固定服务错误。
        except (TypeError, ValueError) as exc:
            raise ApiError("error_map.unavailable") from exc
    try:
        rule = ProviderErrorMapRuleInfo.model_validate(snapshot["error_map"], strict=True)
        head = ProviderErrorMapHeadInfo.model_validate(head_payload, strict=True)
    # DTO 严格校验失败时不回显快照中的 pattern 或持久值，只视为 operation 损坏。
    except ValidationError as exc:
        raise ApiError("error_map.unavailable") from exc
    return _mutation_response(
        rule,
        head,
        changed=snapshot["changed"],
        replay=True,
        request_id=request_id,
        trace_id=trace_id,
    )


def _compile_candidate(
    editable: Mapping[str, object],
    *,
    tenant_id: str,
    error_map_id: str,
    version: int,
    connection: sqlite3.Connection,
) -> tuple[ProviderErrorRule | None, str | None]:
    """验证 Provider owner 后复用 Shared 编译器执行 pattern、guard 和 output 矩阵校验。"""

    provider_id = editable.get("provider_id")
    # Provider scope 若存在必须属于当前认证租户，跨租户与不存在统一业务 404。
    if provider_id is not None:
        if (
            type(provider_id) is not str
            or _provider_row(connection, tenant_id, provider_id) is None
        ):
            return None, "error_map.not_found"
    raw = {
        "error_map_id": error_map_id,
        "tenant_id": tenant_id,
        "provider_id": provider_id,
        "priority": editable.get("priority"),
        "http_statuses": editable.get("http_statuses"),
        "http_status_class": editable.get("http_status_class"),
        "error_field": editable.get("error_field"),
        "field_operator": editable.get("field_operator"),
        "field_pattern": editable.get("field_pattern"),
        "message_operator": editable.get("message_operator"),
        "message_pattern": editable.get("message_pattern"),
        "output_error_code": editable.get("output_error_code"),
        "status": editable.get("status"),
        "version": version,
        "pattern_hash": None,
    }
    # Shared 编译器以显式 expected_tenant_id 复核租户、guard、pattern 与输出矩阵。
    try:
        return compile_provider_error_rule(raw, expected_tenant_id=tenant_id), None
    # 编译异常只映射批准业务码，异常消息可能含输入细节，不能进入 operation 或响应。
    except ProviderErrorRuleValidationError as exc:
        code = exc.code if exc.code in _BUSINESS_STATUS else "error_map.input_invalid"
        return None, code


def _activation_rejected(
    connection: sqlite3.Connection,
    tenant_id: str,
    candidate: ProviderErrorRule,
    *,
    exclude_map_id: str | None,
) -> bool:
    """检查 active 上限和同优先级/条件的重叠 Provider scope。"""

    if candidate.status != "active":
        return False
    # 新建 active 规则在扫描冲突前执行 tenant-qualified 上限，历史版本不计入。
    if exclude_map_id is None and _active_rule_count(connection, tenant_id) >= MAX_ACTIVE_RULES:
        return True
    rows = _load_current_rule_rows(connection, tenant_id)
    for row in rows:
        # 更新目标自身和所有 disabled 规则不参与 active 冲突判定。
        if row["provider_error_map_id"] == exclude_map_id or row["status"] != "active":
            continue
        if (
            int(row["priority"]) != candidate.priority
            or row["pattern_hash"] != candidate.pattern_hash
        ):
            continue
        other_provider = row["provider_id"]
        if (
            other_provider is None
            or candidate.provider_id is None
            or other_provider == candidate.provider_id
        ):
            return True
    return False


def _load_active_rules(connection: sqlite3.Connection, tenant_id: str) -> list[ProviderErrorRule]:
    """从稳定 map 指针重新编译全部 active 规则，任何持久漂移都失败关闭。"""

    rules: list[ProviderErrorRule] = []
    for row in _load_current_rule_rows(connection, tenant_id):
        if row["status"] != "active":
            continue
        rules.append(
            compile_provider_error_rule(
                _wire_from_row(row),
                expected_tenant_id=tenant_id,
            )
        )
    return rules


def _load_current_rule_rows(connection: sqlite3.Connection, tenant_id: str) -> list[sqlite3.Row]:
    """读取当前租户所有稳定资源的 current version 与安全 Provider 名称。"""

    return connection.execute(
        """
        SELECT version.*, provider.provider_code, provider.name AS provider_name
        FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        LEFT JOIN providers provider
          ON provider.tenant_id=version.tenant_id AND provider.provider_id=version.provider_id
        WHERE map.tenant_id=?
        ORDER BY map.provider_error_map_id
        """,
        (tenant_id,),
    ).fetchall()


def _load_current_rule_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    error_map_id: str,
    *,
    required: bool,
) -> sqlite3.Row | None:
    """按 tenant+稳定 ID 读取 current version，跨租户与不存在保持同形。"""

    row = connection.execute(
        """
        SELECT version.*, provider.provider_code, provider.name AS provider_name
        FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        LEFT JOIN providers provider
          ON provider.tenant_id=version.tenant_id AND provider.provider_id=version.provider_id
        WHERE map.tenant_id=? AND map.provider_error_map_id=?
        """,
        (tenant_id, error_map_id),
    ).fetchone()
    # tenant-qualified 查询无结果时，跨租户与不存在根据调用合同保持同形 404。
    if row is None and required:
        raise ApiError("error_map.not_found")
    return row


def _load_rule_info(
    connection: sqlite3.Connection, tenant_id: str, error_map_id: str
) -> ProviderErrorMapRuleInfo:
    """权威回读刚提交到事务内的 current version 并转换公开 DTO。"""

    row = _load_current_rule_row(connection, tenant_id, error_map_id, required=True)
    assert row is not None
    # 公开 DTO 只从同事务权威回读行构造，不能复用调用方候选或跨租户缓存。
    return _tenant_rule_info(row)


def _load_head(connection: sqlite3.Connection, tenant_id: str) -> ProviderErrorMapHeadInfo:
    """读取持久 head；不存在时返回固定 revision-0 空 overlay，不写数据库。"""

    row = connection.execute(
        "SELECT revision, content_hash, updated_at FROM provider_error_map_heads WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    if row is None:
        return ProviderErrorMapHeadInfo(
            revision=0,
            content_hash=EMPTY_OVERLAY_CONTENT_HASH,
            builtin_catalog_version=BUILTIN_CATALOG_VERSION,
            updated_at=None,
        )
    return ProviderErrorMapHeadInfo(
        revision=int(row["revision"]),
        content_hash=str(row["content_hash"]),
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        updated_at=row["updated_at"],
    )


def _next_head(connection: sqlite3.Connection, tenant_id: str) -> tuple[int, str]:
    """生成下一 revision 与严格单调秒级时间；达到上限返回业务限额错误。"""

    row = connection.execute(
        "SELECT revision, updated_at FROM provider_error_map_heads WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    if row is None:
        return 1, _to_db_time(datetime.now(UTC))
    revision = int(row["revision"])
    # 达到冻结上限时作为可重放业务限额失败，不能整数溢出或绕回旧 revision。
    if revision >= MAX_HEAD_REVISION:
        raise ApiError("error_map.limit_exceeded")
    previous = _parse_time(row["updated_at"])
    changed = max(datetime.now(UTC), previous + timedelta(seconds=1))
    return revision + 1, _to_db_time(changed)


def _tenant_rule_info(row: sqlite3.Row) -> ProviderErrorMapRuleInfo:
    """把持久 current version 转为 PAGE-081 安全 DTO，只暴露批准字段与规则 pattern。"""

    spec = provider_error_output_spec(str(row["output_error_code"]))
    statuses = _status_array(row["http_statuses_json"])
    return ProviderErrorMapRuleInfo(
        error_map_id=str(row["provider_error_map_id"]),
        source="tenant",
        readonly=False,
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        provider_name=row["provider_name"],
        priority=int(row["priority"]),
        ordinal=None,
        http_statuses=statuses,
        http_status_class=row["http_status_class"],
        error_field=row["error_field"],
        field_operator=row["field_operator"],
        field_pattern=row["field_pattern"],
        message_operator=row["message_operator"],
        message_pattern=row["message_pattern"],
        output_error_code=spec.code,
        retryable=spec.retryable,
        user_action=spec.user_action,
        status=str(row["status"]),
        version=int(row["version"]),
        pattern_hash=str(row["pattern_hash"]),
    )


def _builtin_rule_infos() -> list[ProviderErrorMapRuleInfo]:
    """从 Shared 不可变 registry 构造 guard/builtin 只读展示行，不读取数据库租户覆盖。"""

    result: list[ProviderErrorMapRuleInfo] = []
    for row in builtin_catalog_registry_rows():
        metadata = json.loads(str(row["rule_metadata_json"]))
        if "rule" in metadata:
            metadata = metadata["rule"]
        spec = provider_error_output_spec(str(row["output_error_code"]))
        result.append(
            ProviderErrorMapRuleInfo(
                error_map_id=str(row["error_map_id"]),
                source=str(row["source"]),
                readonly=True,
                priority=int(row["ordinal"]),
                ordinal=int(row["ordinal"]),
                http_statuses=list(metadata.get("http_statuses") or []),
                http_status_class=metadata.get("http_status_class"),
                output_error_code=spec.code,
                retryable=spec.retryable,
                user_action=spec.user_action,
                status="active",
                version=1,
                pattern_hash=str(row["pattern_hash"]),
            )
        )
    return result


def _provider_options(
    connection: sqlite3.Connection, tenant_id: str
) -> list[ProviderErrorMapProviderOption]:
    """读取当前租户 Provider 安全选项，不选择 Endpoint、认证或 KeyPool 字段。"""

    rows = connection.execute(
        """
        SELECT provider_id, provider_code, name, status
        FROM providers WHERE tenant_id=?
        ORDER BY status, name, provider_id
        """,
        (tenant_id,),
    ).fetchall()
    return [ProviderErrorMapProviderOption.model_validate(dict(row)) for row in rows]


def _parse_list_query(query: Mapping[str, str]) -> dict[str, object]:
    """验证列表筛选、分页和精确资源模式，不接受宽松数字或空值。"""

    error_map_id = query.get("error_map_id")
    # 精确资源模式只接受稳定 pem_ ID，并与其他筛选分页分支完全分离。
    if error_map_id is not None:
        # 非法 ID 在访问数据库前返回固定输入错误，避免宽松路径解释。
        if _MAP_ID.fullmatch(error_map_id) is None:
            raise ApiError("error_map.input_invalid")
        return {
            "error_map_id": error_map_id,
            "provider_id": None,
            "source": None,
            "status": None,
            "output_error_code": None,
            "limit": 1,
            "offset": 0,
        }
    provider_id = _optional_text(query.get("provider_id"))
    source = _optional_enum(query.get("source"), {"tenant", "guard", "builtin"})
    status = _optional_enum(query.get("status"), {"active", "disabled"})
    output = _optional_enum(query.get("output_error_code"), PROVIDER_ERROR_OUTPUT_CODES)
    return {
        "error_map_id": None,
        "provider_id": provider_id,
        "source": source,
        "status": status,
        "output_error_code": output,
        "limit": _bounded_query_int(query.get("limit"), 1, MAX_LIMIT, DEFAULT_LIMIT),
        "offset": _bounded_query_int(query.get("offset"), 0, MAX_OFFSET, 0),
    }


def _filter_rules(
    rules: list[ProviderErrorMapRuleInfo],
    *,
    provider_id: str | None,
    source: object,
    status: object,
    output_error_code: object,
) -> list[ProviderErrorMapRuleInfo]:
    """应用内存筛选；builtin/guard 是全 Provider 规则，因此 provider 筛选仍保留。"""

    filtered = [
        rule
        for rule in rules
        if (provider_id is None or rule.provider_id in {None, provider_id})
        and (source is None or rule.source == source)
        and (status is None or rule.status == status)
        and (output_error_code is None or rule.output_error_code == output_error_code)
    ]
    return sorted(
        filtered, key=lambda item: (item.source != "tenant", item.priority * -1, item.error_map_id)
    )


def _list_response(
    rules: list[ProviderErrorMapRuleInfo],
    providers: list[ProviderErrorMapProviderOption],
    head: ProviderErrorMapHeadInfo,
    *,
    total: int,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> ProviderErrorMapListResponse:
    """构造同时包含 head 对象与兼容扁平字段的列表响应。"""

    return ProviderErrorMapListResponse(
        error_maps=rules,
        provider_options=providers,
        head=head,
        head_revision=head.revision,
        content_hash=head.content_hash,
        builtin_catalog_version=head.builtin_catalog_version,
        updated_at=head.updated_at,
        total=total,
        limit=limit,
        offset=offset,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mutation_response(
    rule: ProviderErrorMapRuleInfo,
    head: ProviderErrorMapHeadInfo,
    *,
    changed: bool,
    replay: bool,
    request_id: str,
    trace_id: str,
) -> ProviderErrorMapMutationResponse:
    """构造创建、更新、no-op 和 replay 共享响应，并保留当前请求的链路标识。"""

    return ProviderErrorMapMutationResponse(
        error_map=rule,
        head=head,
        head_revision=head.revision,
        content_hash=head.content_hash,
        builtin_catalog_version=head.builtin_catalog_version,
        updated_at=head.updated_at,
        changed=changed,
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mutation_snapshot(result: ProviderErrorMapMutationResponse) -> dict[str, object]:
    """移除链路标识后保存安全结果，replay 使用当前 trace_id 重新包络。"""

    return {
        "changed": result.changed,
        "error_map": result.error_map.model_dump(mode="json"),
        "head": result.head.model_dump(mode="json"),
    }


def _audit_summary(rule: ProviderErrorMapRuleInfo) -> dict[str, object]:
    """生成 Audit 字段白名单摘要；pattern 原文永不进入审计库。"""

    return {
        "provider_id": rule.provider_id,
        "priority": rule.priority,
        "http_statuses_json": _canonical_json(rule.http_statuses),
        "http_status_class": rule.http_status_class,
        "error_field": rule.error_field,
        "field_operator": rule.field_operator,
        "message_operator": rule.message_operator,
        "output_error_code": rule.output_error_code,
        "status": rule.status,
        "version": rule.version,
        "pattern_hash": rule.pattern_hash,
    }


def _wire_from_row(row: sqlite3.Row) -> dict[str, object]:
    """把持久不可变版本恢复为 Shared exact-schema wire，供重新编译与内容摘要复核。"""

    return {
        "error_map_id": row["provider_error_map_id"],
        "tenant_id": row["tenant_id"],
        "provider_id": row["provider_id"],
        "priority": row["priority"],
        "http_statuses": _status_array(row["http_statuses_json"]),
        "http_status_class": row["http_status_class"],
        "error_field": row["error_field"],
        "field_operator": row["field_operator"],
        "field_pattern": row["field_pattern"],
        "message_operator": row["message_operator"],
        "message_pattern": row["message_pattern"],
        "output_error_code": row["output_error_code"],
        "status": row["status"],
        "version": row["version"],
        "pattern_hash": row["pattern_hash"],
    }


def _editable_from_row(row: sqlite3.Row) -> dict[str, object]:
    """提取 PATCH 可编辑字段，不复制版本、actor、hash 或 tenant owner。"""

    wire = _wire_from_row(row)
    return {
        key: value
        for key, value in wire.items()
        if key
        in {
            "provider_id",
            "priority",
            "http_statuses",
            "http_status_class",
            "error_field",
            "field_operator",
            "field_pattern",
            "message_operator",
            "message_pattern",
            "output_error_code",
            "status",
        }
    }


def _same_editable(row: sqlite3.Row, candidate: ProviderErrorRule) -> bool:
    """比较规范化后全部可编辑字段，确保真正 no-op 不推进任何业务版本。"""

    current = _wire_from_row(row)
    candidate_wire = provider_error_rule_to_wire(candidate)
    fields = set(_editable_from_row(row))
    return all(current[field] == candidate_wire[field] for field in fields)


def _update_operation(existing: sqlite3.Row, candidate: ProviderErrorRule | None) -> str:
    """根据冻结前后状态生成 create/update/enable/disable 事件枚举。"""

    if candidate is None:
        return "update"
    before = str(existing["status"])
    if before == "active" and candidate.status == "disabled":
        return "disable"
    if before == "disabled" and candidate.status == "active":
        return "enable"
    return "update"


def _status_array(value: object) -> list[int]:
    """解析 canonical HTTP status JSON；损坏值不得被静默修复。"""

    parsed = json.loads(str(value))
    # 持久状态数组必须是严格整数列表，字符串、bool 与容器均视为数据库坏值。
    if type(parsed) is not list or any(type(item) is not int for item in parsed):
        raise ValueError("invalid provider error status array")
    # canonical 数组必须排序去重且有界，不能静默修复后改变规则摘要。
    if parsed != sorted(set(parsed)) or len(parsed) > 16:
        raise ValueError("invalid provider error status order")
    return parsed


def _stable_rule_count(connection: sqlite3.Connection, tenant_id: str) -> int:
    """返回租户稳定资源数，历史 version 不计入 512 上限。"""

    row = connection.execute(
        "SELECT COUNT(*) AS count FROM provider_error_maps WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    return int(row["count"])


def _active_rule_count(connection: sqlite3.Connection, tenant_id: str) -> int:
    """按 tenant-qualified current version 返回当前 active 稳定资源数，用于执行租户上限。"""

    row = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        WHERE map.tenant_id=? AND version.status='active'
        """,
        (tenant_id,),
    ).fetchone()
    return int(row["count"])


def _provider_options_row(
    connection: sqlite3.Connection, tenant_id: str, provider_id: str
) -> sqlite3.Row | None:
    """按 tenant-qualified owner 读取 Provider 安全标量，跨租户与不存在保持同形空结果。"""

    return connection.execute(
        """
        SELECT provider_id, provider_code, name, status
        FROM providers WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchone()


def _provider_row(
    connection: sqlite3.Connection, tenant_id: str, provider_id: str
) -> sqlite3.Row | None:
    """兼容内部调用命名，始终使用 tenant-qualified Provider owner。"""

    # 委托仍显式传递 tenant_id，兼容别名不得退化为只按 provider_id 查询。
    return _provider_options_row(connection, tenant_id, provider_id)


def _require_provider(connection: sqlite3.Connection, tenant_id: str, provider_id: str) -> None:
    """要求 Provider 属于认证租户；跨租户与不存在统一 404。"""

    # 空 ID、跨租户和不存在统一 404，避免筛选接口成为 Provider 枚举侧信道。
    if not provider_id or _provider_row(connection, tenant_id, provider_id) is None:
        raise ApiError("error_map.not_found")


def _optional_text(value: str | None) -> str | None:
    """验证可选 query 文本非空且不含控制字符。"""

    if value is None:
        return None
    # 可选文本必须有界且无控制字符，不做 strip 以免幂等或查询语义发生隐式改变。
    if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise ApiError("error_map.input_invalid")
    return value


def _optional_enum(value: str | None, allowed: set[str] | frozenset[str]) -> str | None:
    """验证可选 query 枚举，不做 strip 或大小写猜测。"""

    if value is None:
        return None
    # 枚举只接受 exact 值，不做大小写或空白猜测，保持 Control/Console 合同确定。
    if value not in allowed:
        raise ApiError("error_map.input_invalid")
    return value


def _bounded_query_int(value: str | None, minimum: int, maximum: int, default: int) -> int:
    """解析无符号十进制 query，拒绝符号、空白、前导零和超大整数。"""

    if value is None:
        return default
    # 先限制 ASCII、十进制与文本长度，再执行整数转换，避免大整数解析放大。
    if not value or not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ApiError("error_map.input_invalid")
    # 多位前导零不属于 canonical query，拒绝同一数值的多种缓存与审计表示。
    if len(value) > 1 and value.startswith("0"):
        raise ApiError("error_map.input_invalid")
    parsed = int(value)
    # 分页值必须落在调用方冻结区间，越界统一返回输入错误而不截断。
    if not minimum <= parsed <= maximum:
        raise ApiError("error_map.input_invalid")
    return parsed


def _fingerprint(value: object) -> str:
    """计算 canonical mutation 意图 SHA-256，用于结果级幂等且不把请求正文写入日志。"""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    """生成 sorted-key、无空白、ASCII 稳定 JSON，统一幂等摘要、快照和安全审计字段。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _integrity_error(exc: sqlite3.IntegrityError) -> ApiError:
    """把数据库 trigger/约束失败折叠成固定规则错误，不泄露 SQL 或持久值。"""

    text = str(exc)
    if "conflict" in text:
        return ApiError("error_map.conflict")
    if "limit" in text or "state_invalid" in text:
        return ApiError("error_map.limit_exceeded")
    return ApiError("error_map.unavailable")


def _dispatch_after_commit(
    config: AppConfig,
    context: AuthContext,
    audit_record: object | None,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递 Audit；Source Event 由持久 scheduler 独立恢复。"""

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开启用外键的短生命周期数据库连接。"""

    return connect_database(config.database_url)


def _parse_time(value: object) -> datetime:
    """解析 SQLite UTC 时间并返回 aware datetime，供 revision 与事件时间保持严格单调。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """生成秒级 UTC canonical 数据库时间，确保 SQLite 文本比较与跨服务序列化一致。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "build_internal_provider_error_map_response",
    "create_provider_error_map",
    "list_provider_error_maps",
    "update_provider_error_map",
]
