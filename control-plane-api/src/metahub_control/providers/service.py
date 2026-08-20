"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/providers/service.py
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import encrypt_provider_key, secret_fingerprint, secret_prefix
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    ProviderAdapterSpec,
    ProviderAdapterSpecError,
    provider_types,
    validate_provider_auth,
)

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    CreateProviderKeyRequest,
    CreateProviderRequest,
    ProviderInfo,
    ProviderKeyInfo,
    ProviderKeyListResponse,
    ProviderKeyResponse,
    ProviderListResponse,
    ProviderResponse,
    ProviderTestResponse,
    ProviderTestResult,
    TestProviderRequest,
    UpdateProviderKeyRequest,
    UpdateProviderRequest,
)

logger = logging.getLogger("metahub.providers")

# Provider 类型、认证方式、状态和编码规则共同定义上游接入白名单，密钥明文只允许一次性写入。
PROVIDER_TYPES = provider_types()
AUTH_TYPES = {"none", "bearer", "custom_header"}
STATUSES = {"active", "disabled"}
PROVIDER_KEY_STATUSES = {"active", "disabled"}
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _reject_local_endpoint_provider(provider: ProviderInfo) -> None:
    if provider.provider_type == "local_endpoint":
        raise ApiError("provider.local_endpoint_requires_local_api")


def list_providers(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    provider_type: str | None,
    request_id: str,
    trace_id: str,
) -> ProviderListResponse:
    """列出当前租户 Provider。

    Provider 是租户级高风险配置，只展示 Endpoint 元数据，不返回任何 Provider Key。
    """

    normalized_status = _normalize_optional_status(status)
    normalized_type = _normalize_optional_provider_type(provider_type)
    if normalized_type == "local_endpoint":
        raise ApiError("provider.local_endpoint_requires_local_api")
    params: list[str] = [context.user.tenant_id]
    filters: list[str] = ["provider_type!='local_endpoint'"]
    if normalized_status:
        filters.append("status=?")
        params.append(normalized_status)
    if normalized_type:
        filters.append("provider_type=?")
        params.append(normalized_type)
    where_extra = f" AND {' AND '.join(filters)}" if filters else ""
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT
              provider_id, tenant_id, provider_code, name, provider_type, base_url, auth_type,
              status, description, maintenance_note, health_status, last_tested_at,
              last_test_error, created_at, updated_at, disabled_at
            FROM providers
            WHERE tenant_id=? {where_extra}
            ORDER BY created_at DESC, provider_code ASC
            """,
            params,
        ).fetchall()
        return ProviderListResponse(
            providers=[_provider_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_provider(
    config: AppConfig,
    context: AuthContext,
    payload: CreateProviderRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> ProviderResponse:
    """创建 Provider 与 KeyPool 管理 资源，负责输入归一化、唯一性校验和审计所需追踪字段。
    
    安全：写入数据必须绑定认证上下文中的 tenant_id，不能接受客户端伪造租户。
    """


    provider_code = _normalize_code(payload.provider_code)
    name = _normalize_name(payload.name)
    provider_type = _normalize_provider_type(payload.provider_type)
    if provider_type == "local_endpoint":
        raise ApiError("provider.local_endpoint_requires_local_api")
    base_url = _normalize_base_url(payload.base_url)
    auth_type = _normalize_auth_type(payload.auth_type)
    # 类型与鉴权必须作为一个可执行合同校验；分别命中两个枚举并不代表组合可联网。
    _validate_provider_auth_combination(provider_type, auth_type)
    description = _normalize_description(payload.description)
    maintenance_note = _normalize_maintenance_note(payload.maintenance_note)
    provider_id = str(uuid4())
    now = _to_db_time(_utcnow())
    connection = _connect(config)
    try:
        connection.execute(
            """
            INSERT INTO providers(
              provider_id, tenant_id, provider_code, name, base_url, status,
              provider_type, auth_type, description, maintenance_note, health_status,
              created_by, created_at, updated_at,
              next_probe_at
            )
            VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 'unknown', ?, ?, ?, ?)
            """,
            (
                provider_id,
                context.user.tenant_id,
                provider_code,
                name,
                base_url,
                provider_type,
                auth_type,
                description,
                maintenance_note,
                context.user.user_id,
                now,
                now,
                now,
            ),
        )
        # Provider 与审计事实必须使用同一连接、同一事务；任一写入失败都不能留下无审计的配置变更。
        provider = _load_provider(connection, context.user.tenant_id, provider_id)
        audit_record = record_audit_log(
            connection,
            context,
            action="provider.created",
            object_type="provider",
            object_id=provider.provider_id,
            object_name=provider.name,
            before_summary={},
            after_summary=_provider_audit_summary(provider),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 租户边界必须来自认证上下文或已校验记录，不能相信客户端传入的 tenant_id。
        if "providers.tenant_id, providers.provider_code" in str(exc) or "UNIQUE" in str(exc):
            raise ApiError("provider.code_exists") from exc
        raise ApiError("provider.create_invalid") from exc
    except Exception:
        # 审计校验、审计落库或事务提交失败时必须显式回滚，禁止依赖连接析构的隐式行为。
        connection.rollback()
        raise
    # Webhook 是提交后的外部副作用，投递失败只能记录安全错误，不能撤销已提交 Provider。
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _log_created(context, provider, request_id, trace_id)
    return ProviderResponse(provider=provider, request_id=request_id, trace_id=trace_id)


def update_provider(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    payload: UpdateProviderRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> ProviderResponse:
    """更新 Provider 与 KeyPool 管理 资源，区分未传字段和显式清空，避免覆盖不相关配置。
    
    边界：只允许修改该资源暴露的业务字段，敏感字段通过专门流程处理。
    """


    updates: dict[str, object] = {}
    if payload.name is not None:
        updates["name"] = _normalize_name(payload.name)
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    if payload.provider_type is not None:
        updates["provider_type"] = _normalize_provider_type(payload.provider_type)
        if updates["provider_type"] == "local_endpoint":
            raise ApiError("provider.local_endpoint_requires_local_api")
    if payload.base_url is not None:
        updates["base_url"] = _normalize_base_url(payload.base_url)
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if payload.auth_type is not None:
        updates["auth_type"] = _normalize_auth_type(payload.auth_type)
    if payload.description is not None:
        updates["description"] = _normalize_description(payload.description)
    # 只有 maintenance_note 明确出现在 PATCH 中才更新；显式 null/空串都表示清空。
    if "maintenance_note" in payload.model_fields_set:
        updates["maintenance_note"] = _normalize_maintenance_note(payload.maintenance_note)
    if payload.status is not None:
        updates["status"] = _normalize_status(payload.status)
    if not updates:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.update_empty")

    connection = _connect(config)
    before = _load_provider(connection, context.user.tenant_id, provider_id)
    if before.provider_type == "local_endpoint":
        raise ApiError("provider.local_endpoint_requires_local_api")
    # PATCH 可能只修改 type 或 auth，必须用修改后的值与未修改的持久值组成完整候选再校验。
    _validate_provider_auth_combination(
        str(updates.get("provider_type", before.provider_type)),
        str(updates.get("auth_type", before.auth_type)),
    )
    now = _to_db_time(_utcnow())
    set_clauses = [f"{column}=?" for column in updates]
    values = list(updates.values())
    disabled_event = before.status != "disabled" and updates.get("status") == "disabled"
    reactivated = before.status == "disabled" and updates.get("status") == "active"
    if disabled_event:
        set_clauses.extend(["disabled_at=?", "disabled_by=?"])
        values.extend([now, context.user.user_id])
    elif reactivated:
        set_clauses.extend(["disabled_at=NULL", "disabled_by=NULL"])
    set_clauses.append("updated_at=?")
    values.append(now)
    values.extend([context.user.tenant_id, provider_id])
    try:
        connection.execute(
            f"""
            UPDATE providers
            SET {', '.join(set_clauses)}
            WHERE tenant_id=? AND provider_id=?
            """,
            values,
        )
        # Endpoint、认证、类型或启停变化会使在途探测失效，必须在同一事务递增代际。
        if {"provider_type", "base_url", "auth_type", "status"}.intersection(updates):
            _reset_provider_health(
                connection,
                context.user.tenant_id,
                provider_id,
                active=updates.get("status", before.status) == "active",
                now=now,
            )
        provider = _load_provider(connection, context.user.tenant_id, provider_id)
        audit_record = record_audit_log(
            connection,
            context,
            action="provider.updated",
            object_type="provider",
            object_id=provider.provider_id,
            object_name=provider.name,
            before_summary=_provider_audit_summary(before),
            after_summary=_provider_audit_summary(provider),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    except Exception:
        # Provider 更新与审计记录是一个原子管理操作，审计不可用时业务状态也必须回滚。
        connection.rollback()
        raise
    if audit_record is not None:
        dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    return ProviderResponse(provider=provider, request_id=request_id, trace_id=trace_id)


def list_provider_keys(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> ProviderKeyListResponse:
    """分页列出 Provider 与 KeyPool 管理 资源，只返回当前租户可见的脱敏字段。
    
    边界：列表接口不返回明文密钥、Token、Prompt 或上游完整错误体。
    """


    connection = _connect(config)
    try:
        provider = _load_provider(connection, context.user.tenant_id, provider_id)
        _reject_local_endpoint_provider(provider)
        rows = connection.execute(
            """
            SELECT
              provider_credential_id, tenant_id, provider_id, key_fingerprint, name, key_prefix,
              weight, status, last_used_at, last_error_at, last_error_code, last_error_message,
              rate_limited_until, created_at, updated_at, disabled_at
            FROM provider_credentials
            WHERE tenant_id=? AND provider_id=?
            ORDER BY created_at DESC, provider_credential_id ASC
            """,
            (context.user.tenant_id, provider_id),
        ).fetchall()
        return ProviderKeyListResponse(
            provider_keys=[_provider_key_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_provider_key(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    payload: CreateProviderKeyRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> ProviderKeyResponse:
    """创建 Provider 与 KeyPool 管理 资源，负责输入归一化、唯一性校验和审计所需追踪字段。
    
    安全：写入数据必须绑定认证上下文中的 tenant_id，不能接受客户端伪造租户。
    """


    connection = _connect(config)
    provider = _load_provider(connection, context.user.tenant_id, provider_id)
    _reject_local_endpoint_provider(provider)
    plain_value = _normalize_provider_key_plain_value(payload.plain_value)
    key_prefix = secret_prefix(plain_value)
    name = _normalize_provider_key_name(payload.name, default=f"{provider.provider_code}-{key_prefix}")
    weight = _normalize_provider_key_weight(payload.weight)
    provider_key_id = str(uuid4())
    now = _to_db_time(_utcnow())
    try:
        encrypted_key = encrypt_provider_key(plain_value)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except ValueError as exc:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.encryption_failed") from exc

    try:
        connection.execute(
            """
            INSERT INTO provider_credentials(
              provider_credential_id, tenant_id, provider_id, key_fingerprint, encrypted_key,
              name, key_prefix, weight, status, created_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                provider_key_id,
                context.user.tenant_id,
                provider_id,
                secret_fingerprint(plain_value),
                encrypted_key,
                name,
                key_prefix,
                weight,
                context.user.user_id,
                now,
                now,
            ),
        )
        # KeyPool 新增会改变认证可用性，健康失效必须与 Key 和审计记录同事务提交。
        _reset_provider_health(
            connection,
            context.user.tenant_id,
            provider_id,
            active=provider.status == "active",
            now=now,
        )
        # 审计摘要只接收脱敏 DTO；plain_value、encrypted_key 和完整指纹都不进入审计函数参数。
        provider_key = _load_provider_key(connection, context.user.tenant_id, provider_key_id)
        audit_record = record_audit_log(
            connection,
            context,
            action="provider_key.created",
            object_type="provider_key",
            object_id=provider_key.provider_key_id,
            object_name=f"{provider_key.name} / {provider_key.key_prefix}",
            before_summary={},
            after_summary=_provider_key_audit_summary(provider_key),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "provider_credentials" in str(exc) or "UNIQUE" in str(exc):
            # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
            raise ApiError("provider_key.duplicate") from exc
        raise ApiError("provider_key.create_invalid") from exc
    except Exception:
        # 加密已在内存完成，但任何数据库或审计异常都必须回滚，不允许产生无法追溯的 Key。
        connection.rollback()
        raise

    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _log_provider_key_created(context, provider, provider_key, request_id, trace_id)
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    return ProviderKeyResponse(provider_key=provider_key, request_id=request_id, trace_id=trace_id)


def update_provider_key(
    config: AppConfig,
    context: AuthContext,
    provider_key_id: str,
    payload: UpdateProviderKeyRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> ProviderKeyResponse:
    """更新 Provider 与 KeyPool 管理 资源，区分未传字段和显式清空，避免覆盖不相关配置。
    
    边界：只允许修改该资源暴露的业务字段，敏感字段通过专门流程处理。
    """


    updates: dict[str, object] = {}
    if payload.name is not None:
        updates["name"] = _normalize_provider_key_name(payload.name)
    if payload.weight is not None:
        updates["weight"] = _normalize_provider_key_weight(payload.weight)
    if payload.status is not None:
        updates["status"] = _normalize_provider_key_status(payload.status)
    if not updates:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.update_empty")

    connection = _connect(config)
    before = _load_provider_key(connection, context.user.tenant_id, provider_key_id)
    provider = _load_provider(connection, context.user.tenant_id, before.provider_id)
    _reject_local_endpoint_provider(provider)
    now = _to_db_time(_utcnow())
    set_clauses = [f"{column}=?" for column in updates]
    values = list(updates.values())
    disabled_event = before.status != "disabled" and updates.get("status") == "disabled"
    reactivated = before.status != "active" and updates.get("status") == "active"
    if disabled_event:
        set_clauses.extend(["disabled_at=?", "disabled_by=?"])
        values.extend([now, context.user.user_id])
    elif reactivated:
        set_clauses.extend(
            [
                "disabled_at=NULL",
                "disabled_by=NULL",
                "rate_limited_until=NULL",
                "last_error_code=NULL",
                "last_error_message=NULL",
            ]
        )
    set_clauses.append("updated_at=?")
    values.append(now)
    values.extend([context.user.tenant_id, provider_key_id])
    try:
        connection.execute(
            f"""
            UPDATE provider_credentials
            SET {', '.join(set_clauses)}
            WHERE tenant_id=? AND provider_credential_id=?
            """,
            values,
        )
        # 只有启停影响候选集合；名称和权重变化不应制造无意义健康失效。
        if "status" in updates:
            _reset_provider_health(
                connection,
                context.user.tenant_id,
                before.provider_id,
                active=provider.status == "active",
                now=now,
            )
        provider_key = _load_provider_key(connection, context.user.tenant_id, provider_key_id)
        audit_record = record_audit_log(
            connection,
            context,
            action="provider_key.updated",
            object_type="provider_key",
            object_id=provider_key.provider_key_id,
            object_name=f"{provider_key.name} / {provider_key.key_prefix}",
            before_summary=_provider_key_audit_summary(before),
            after_summary=_provider_key_audit_summary(provider_key),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    except Exception:
        # 状态/权重修改与审计必须一起成功，尤其不能出现“Key 已禁用但审计缺失”。
        connection.rollback()
        raise
    if audit_record is not None:
        dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    if disabled_event:
        _log_provider_key_disabled(context, provider, provider_key, request_id, trace_id)
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    return ProviderKeyResponse(provider_key=provider_key, request_id=request_id, trace_id=trace_id)


def test_provider_connection(
    config: AppConfig,
    context: AuthContext,
    provider_id: str,
    payload: TestProviderRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ProviderTestResponse:
    """测试 Provider Endpoint 可达性。

    M0 不读取或发送 Provider Key 明文，只做 URL 可达性与基础认证状态探测。PRD-063 起，
    该旧入口也必须在任何 DNS/HTTP 前复用 Shared spec 的 type/auth 门禁和健康探测安全策略。
    """

    connection = _connect(config)
    provider = _load_provider(connection, context.user.tenant_id, provider_id)
    _reject_local_endpoint_provider(provider)
    # 历史 custom_header 或损坏组合必须在 import 健康模块、DNS、lease 和网络 I/O 前失败关闭。
    adapter_spec = _validate_provider_auth_combination(
        provider.provider_type,
        provider.auth_type,
    )
    timeout_seconds = _normalize_timeout(payload.timeout_seconds)
    # 本地导入避免 provider_health 在模块加载时复用本模块校验函数形成循环依赖。
    from metahub_control.provider_health.service import probe_provider_reachability

    attempt = probe_provider_reachability(
        config,
        provider.base_url,
        probe_profile=adapter_spec.probe_profile,
        timeout_seconds=timeout_seconds,
    )
    status = "success" if attempt.result_status == "success" else "failed"
    if status == "success":
        message = "Endpoint is reachable."
    # 认证拒绝只证明 Endpoint 可达，不回显上游响应或推断某把具体 Key 的状态。
    elif attempt.error_code == "authentication_failed":
        message = "Endpoint is reachable but authentication failed."
    # 超时单独保留稳定运维语义，但底层目标和异常文本不能进入健康事实。
    elif attempt.error_code == "timeout":
        message = "Provider test timed out."
    # SSRF/DNS 策略拒绝统一为固定摘要，不能泄露命中的地址或私网规则。
    elif attempt.error_code == "endpoint_blocked":
        message = "Provider endpoint is blocked by probe policy."
    elif attempt.http_status is not None:
        message = f"Endpoint returned HTTP {attempt.http_status}."
    else:
        # 传输层自由异常不得进入 API、数据库健康摘要或结构化日志。
        message = "Provider endpoint is unavailable."
    result = ProviderTestResult(
        status=status,
        http_status=attempt.http_status,
        latency_ms=attempt.latency_ms,
        message=message,
        checked_at=attempt.checked_at,
    )
    _record_health(connection, context, provider.provider_id, result)
    provider = _load_provider(connection, context.user.tenant_id, provider_id)
    _log_health_checked(context, provider, result, request_id, trace_id)
    return ProviderTestResponse(
        provider=provider,
        test_result=result,
        request_id=request_id,
        trace_id=trace_id,
    )


def _record_health(
    connection: sqlite3.Connection,
    context: AuthContext,
    provider_id: str,
    result: ProviderTestResult,
) -> None:
    """在 Provider 与 KeyPool 管理 内部复用 record health 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    health_status = "healthy" if result.status == "success" else "unhealthy"
    last_error = None if result.status == "success" else result.message[:240]
    now = _to_db_time(result.checked_at)
    connection.execute(
        """
        UPDATE providers
        SET health_status=?, last_tested_at=?, last_test_error=?, updated_at=?
        WHERE tenant_id=? AND provider_id=?
        """,
        (health_status, now, last_error, now, context.user.tenant_id, provider_id),
    )
    connection.commit()


def _reset_provider_health(
    connection: sqlite3.Connection,
    tenant_id: str,
    provider_id: str,
    *,
    active: bool,
    now: str,
) -> None:
    """健康相关配置变化时递增代际并清除旧聚合和租约。

    调用方负责在原管理事务中提交，保证配置、审计和健康失效事实原子一致。
    """

    connection.execute(
        """
        UPDATE providers
        SET health_generation=health_generation+1, health_status='unknown',
            consecutive_probe_failures=0, health_updated_at=NULL,
            last_probe_error_code=NULL, last_test_error=NULL,
            probe_lease_token=NULL, probe_lease_until=NULL, next_probe_at=?
        WHERE tenant_id=? AND provider_id=?
        """,
        (now if active else None, tenant_id, provider_id),
    )


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    """为服务层直调补齐空来源元数据。

    HTTP 路由会传入已清洗的直接对端 IP 和 User-Agent；集成测试或内部任务没有 Request 时使用空值，
    但仍保留同事务审计，不能因为缺少网络元数据而跳过审计事实。
    """

    return value or AuditRequestMeta(client_ip=None, user_agent=None)


def _provider_audit_summary(provider: ProviderInfo) -> dict[str, object]:
    """构造 Provider 审计白名单摘要，并把完整 Endpoint 收敛为不可逆哈希。

    摘要不接收数据库整行，避免未来表新增认证头、密钥或上游错误正文后被自动扩散到审计库。
    Endpoint 的 host/path 也可能包含客户内网拓扑或短期令牌，因此不能保留任何可逆 URL 片段。
    """

    return {
        "name": provider.name,
        "provider_code": provider.provider_code,
        "provider_type": provider.provider_type,
        "base_url_hash": _audit_base_url_hash(provider.base_url),
        "auth_type": provider.auth_type,
        "description_hash": _audit_text_hash(provider.description),
        # 维护备注对状态页可见，但审计/Webhook 仍只允许不可逆摘要，避免原文扩散。
        "maintenance_note_hash": _audit_text_hash(provider.maintenance_note),
        "status": provider.status,
    }


def _provider_key_audit_summary(provider_key: ProviderKeyInfo) -> dict[str, object]:
    """构造 Provider Key 审计白名单摘要。

    这里只允许可展示的前缀、名称、权重和状态；完整指纹、密文、明文及最近上游错误均被排除。
    """

    return {
        "provider_id": provider_key.provider_id,
        "name": provider_key.name,
        "key_prefix": provider_key.key_prefix,
        "weight": provider_key.weight,
        "status": provider_key.status,
    }


def _audit_base_url_hash(value: str) -> str:
    """对规范化 Provider Endpoint 生成 SHA-256，支持变更审计但不暴露 URL 组成。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _audit_text_hash(value: str | None) -> str | None:
    """对自由文本生成不可逆摘要，使 description 变化可审计但原文不扩散到审计库。"""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 Provider 与 KeyPool 管理 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _load_provider(connection: sqlite3.Connection, tenant_id: str, provider_id: str) -> ProviderInfo:
    """按当前租户加载 Provider 与 KeyPool 管理 的 load provider 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT
          provider_id, tenant_id, provider_code, name, provider_type, base_url, auth_type,
          status, description, maintenance_note, health_status, last_tested_at, last_test_error,
          created_at, updated_at, disabled_at
        FROM providers
        WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.not_found")
    return _provider_info(row)


def _load_provider_key(connection: sqlite3.Connection, tenant_id: str, provider_key_id: str) -> ProviderKeyInfo:
    """按当前租户加载 Provider 与 KeyPool 管理 的 load provider key 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT
          provider_credential_id, tenant_id, provider_id, key_fingerprint, name, key_prefix,
          weight, status, last_used_at, last_error_at, last_error_code, last_error_message,
          rate_limited_until, created_at, updated_at, disabled_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_credential_id=?
        """,
        (tenant_id, provider_key_id),
    ).fetchone()
    if not row:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.not_found")
    return _provider_key_info(row)


def _provider_info(row: sqlite3.Row) -> ProviderInfo:
    """在 Provider 与 KeyPool 管理 内部复用 provider info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return ProviderInfo(
        provider_id=row["provider_id"],
        tenant_id=row["tenant_id"],
        provider_code=row["provider_code"],
        name=row["name"],
        provider_type=row["provider_type"],
        base_url=row["base_url"],
        auth_type=row["auth_type"],
        status=row["status"],
        description=row["description"],
        maintenance_note=row["maintenance_note"],
        health_status=row["health_status"],
        last_tested_at=_parse_optional_db_time(row["last_tested_at"]),
        last_test_error=row["last_test_error"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        disabled_at=_parse_optional_db_time(row["disabled_at"]),
    )


def _provider_key_info(row: sqlite3.Row) -> ProviderKeyInfo:
    """在 Provider 与 KeyPool 管理 内部复用 provider key info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return ProviderKeyInfo(
        provider_key_id=row["provider_credential_id"],
        tenant_id=row["tenant_id"],
        provider_id=row["provider_id"],
        name=row["name"] or row["key_prefix"] or "Provider Key",
        key_prefix=row["key_prefix"] or "",
        key_fingerprint=row["key_fingerprint"],
        weight=int(row["weight"] or 100),
        status=row["status"],
        last_used_at=_parse_optional_db_time(row["last_used_at"]),
        last_error_at=_parse_optional_db_time(row["last_error_at"]),
        last_error_code=row["last_error_code"],
        last_error_message=row["last_error_message"],
        rate_limited_until=_parse_optional_db_time(row["rate_limited_until"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        disabled_at=_parse_optional_db_time(row["disabled_at"]),
    )


def _normalize_code(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize code 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    code = (value or "").strip().lower()
    if not CODE_PATTERN.match(code):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.code_invalid")
    return code


def _normalize_name(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize name 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    name = (value or "").strip()
    if not name or len(name) > 120:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.name_invalid")
    return name


def _normalize_provider_type(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize provider type 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    provider_type = (value or "openai_compatible").strip().lower()
    # Provider 判断会影响上游归因和密钥健康，错误路径必须保持脱敏。
    if provider_type not in PROVIDER_TYPES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.type_invalid")
    return provider_type


def _normalize_optional_provider_type(value: str | None) -> str | None:
    """规范化 Provider 与 KeyPool 管理 的 normalize optional provider type 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_provider_type(value)


def _normalize_auth_type(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize auth type 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    auth_type = (value or "bearer").strip().lower()
    # 关键控制流影响权限、审计或错误归因，必须保留统一错误语义。
    if auth_type not in AUTH_TYPES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.auth_type_invalid")
    return auth_type


def _validate_provider_auth_combination(
    provider_type: object,
    auth_type: object,
) -> ProviderAdapterSpec:
    """按 Shared SDK 冻结 spec 校验 Provider 类型与鉴权组合。

    参数：已经归一化或来自历史数据库的 provider_type/auth_type；返回：合法组合对应的不可变
    spec。关键约束：``custom_header`` 只保留历史读取兼容，管理、导入、探测和 Gateway 联网
    前都必须失败关闭，不能猜测 Header 或回退 Bearer。
    """

    try:
        # 返回值只能来自冻结目录验证器，未知类型和历史 custom_header 不允许本地猜测。
        return validate_provider_auth(provider_type, auth_type)
    # Shared 错误转换为 Control 统一错误码，原始字段和内部 reason 不进入响应。
    except ProviderAdapterSpecError as exc:
        # Shared 异常只携带固定 code/reason；对外响应不得拼接原始类型、Header 或异常正文。
        raise ApiError(exc.code) from None


def _normalize_base_url(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize base url 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    base_url = (value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    try:
        parsed_port = parsed.port
    # urllib 会在读取越界或非数字端口时抛异常；这里统一折叠为对外稳定错误码。
    except ValueError as exc:
        # 非法端口必须映射为稳定业务错误，不能让 urllib 的异常正文穿透 API。
        raise ApiError("provider.base_url_invalid") from exc
    if (
        len(base_url) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in base_url)
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or parsed_port is not None
        and not 1 <= parsed_port <= 65535
    ):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.base_url_invalid")
    return base_url


def _normalize_description(value: str | None) -> str | None:
    """规范化 Provider 与 KeyPool 管理 的 normalize description 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    description = value.strip()
    if len(description) > 240:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.create_invalid")
    return description or None


def _normalize_maintenance_note(value: str | None) -> str | None:
    """规范化状态页维护备注。

    参数：客户端提交的可空短文本。返回：去除首尾空白后的文本或 ``None``。
    关键约束：备注会展示给拥有状态读取权限的用户，因此拒绝 C0/C1 控制字符并限制为
    240 个 Unicode 字符；日志和审计只能使用哈希，不能记录这里的原文。
    """

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 240 or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in normalized
    ):
        # 控制字符会污染状态页和审计边界，超长文本也不能依赖数据库截断后继续写入。
        raise ApiError("provider.maintenance_note_invalid")
    return normalized


def _normalize_status(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    status = (value or "").strip().lower()
    if status not in STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("provider.status_invalid")
    return status


def _normalize_optional_status(value: str | None) -> str | None:
    """规范化 Provider 与 KeyPool 管理 的 normalize optional status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_status(value)


def _normalize_provider_key_name(value: str | None, *, default: str | None = None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize provider key name 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    name = (value or default or "").strip()
    if not name or len(name) > 80:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.name_invalid")
    return name


def _normalize_provider_key_plain_value(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize provider key plain value 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    plain_value = (value or "").strip()
    if not plain_value or len(plain_value) > 4096:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.plain_value_invalid")
    return plain_value


def _normalize_provider_key_weight(value: int | None) -> int:
    """规范化 Provider 与 KeyPool 管理 的 normalize provider key weight 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    try:
        weight = int(value if value is not None else 100)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except (TypeError, ValueError):
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        raise ApiError("provider_key.weight_invalid") from None
    if weight <= 0 or weight > 10000:
        raise ApiError("provider_key.weight_invalid")
    return weight


def _normalize_provider_key_status(value: str | None) -> str:
    """规范化 Provider 与 KeyPool 管理 的 normalize provider key status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    status = (value or "").strip().lower()
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    if status not in PROVIDER_KEY_STATUSES:
        raise ApiError("provider_key.status_invalid")
    return status


def _normalize_timeout(value: float | None) -> float:
    """规范化 Provider 与 KeyPool 管理 的 normalize timeout 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    try:
        timeout = float(value if value is not None else 5)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 5.0
    return min(max(timeout, 0.5), 15.0)


def _join_url(base_url: str, path: str) -> str:
    """在 Provider 与 KeyPool 管理 内部复用 join url 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 Provider 与 KeyPool 管理 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 Provider 与 KeyPool 管理 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 Provider 与 KeyPool 管理 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 Provider 与 KeyPool 管理 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)


def _log_created(context: AuthContext, provider: ProviderInfo, request_id: str, trace_id: str) -> None:
    """在 Provider 与 KeyPool 管理 内部复用 log created 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "provider.created",
        extra={
            "event": "provider.created",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": provider.provider_id,
            # Provider Endpoint 可能是客户内网地址；即使不含凭据，host、端口和 path 也会
            # 暴露网络拓扑，因此运行日志完全不记录 URL 或其摘要。
            "payload": {
                "provider_code": provider.provider_code,
                "provider_type": provider.provider_type,
                "auth_type": provider.auth_type,
            },
        },
    )


def _log_health_checked(
    context: AuthContext,
    provider: ProviderInfo,
    result: ProviderTestResult,
    request_id: str,
    trace_id: str,
) -> None:
    """在 Provider 与 KeyPool 管理 内部复用 log health checked 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "provider.health_checked",
        extra={
            "event": "provider.health_checked",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": provider.provider_id,
            "payload": {
                "provider_code": provider.provider_code,
                "health_status": provider.health_status,
                "test_status": result.status,
                "http_status": result.http_status,
                "latency_ms": result.latency_ms,
            },
        },
    )


def _log_provider_key_created(
    context: AuthContext,
    provider: ProviderInfo,
    provider_key: ProviderKeyInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """在 Provider 与 KeyPool 管理 内部复用 log provider key created 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "provider_key.created",
        extra={
            "event": "provider_key.created",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": provider_key.provider_key_id,
            "payload": {
                "provider_id": provider.provider_id,
                "provider_code": provider.provider_code,
                "key_prefix": provider_key.key_prefix,
                "weight": provider_key.weight,
            },
        },
    )


def _log_provider_key_disabled(
    context: AuthContext,
    provider: ProviderInfo,
    provider_key: ProviderKeyInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """在 Provider 与 KeyPool 管理 内部复用 log provider key disabled 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "provider_key.disabled",
        extra={
            "event": "provider_key.disabled",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": provider_key.provider_key_id,
            "payload": {
                "provider_id": provider.provider_id,
                "provider_code": provider.provider_code,
                "key_prefix": provider_key.key_prefix,
            },
        },
    )
