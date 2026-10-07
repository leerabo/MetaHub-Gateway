"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/virtual_keys/service.py
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from secrets import token_urlsafe
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import table_columns
from metahub_shared.db.security import decrypt_provider_key, encrypt_provider_key, hash_secret, secret_prefix
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    AuditActor,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
    record_audit_log_for_actor,
)
from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.consumers.service import require_active_consumer
from metahub_control.environments.service import resolve_environment_binding

from .models import (
    CreateVirtualKeyRequest,
    CreateVirtualKeyResponse,
    RateLimitRuleInfo,
    RevealVirtualKeyResponse,
    UpdateVirtualKeyLimitsRequest,
    UpdateVirtualKeyRequest,
    VirtualKeyInfo,
    VirtualKeyListResponse,
    VirtualKeyResponse,
)

logger = logging.getLogger("metahub.virtual_keys")

# Virtual Key 环境、状态和限额上限集中定义，确保密钥治理字段在创建和更新时一致校验。
VALID_ENVIRONMENTS = {"dev", "test", "prod"}
VALID_STATUSES = {"active", "disabled"}
MAX_NAME_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 240
MAX_LIMIT_VALUE = 100_000_000


def disable_virtual_keys_created_by(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    created_by: str,
    member_account_id: str | None = None,
    member_display_name: str | None = None,
    member_email: str | None = None,
    actor: AuditActor,
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    now: str,
) -> int:
    """Disable every key that belongs to a removed workspace member.

    A member can have either a self-created key or a legacy key created by an
    administrator and assigned through ``virtual_key_consumers``. The caller
    owns the surrounding transaction. Rows remain in place so usage, billing
    and audit history keep their original references; changing the key and
    consumer status makes every previously issued secret fail immediately.
    """

    owner_ids = [created_by]
    if member_account_id and member_account_id != created_by:
        owner_ids.append(member_account_id)
    owner_placeholders = ",".join("?" for _ in owner_ids)
    consumer_columns = set(table_columns(connection, "workspace_consumers"))
    employee_code_clause = ""
    employee_code_params: list[str] = []
    if "employee_code" in consumer_columns:
        # Older employee directories used either the login email or the global
        # account id as employee_code. Match both forms during removal so a
        # previously issued assignment cannot survive a membership deletion.
        employee_codes = [value for value in (member_email, member_account_id) if value]
        if employee_codes:
            employee_code_clause = " OR " + " OR ".join(
                "lower(trim(COALESCE(consumer.employee_code,'')))=lower(trim(?))"
                for _ in employee_codes
            )
            employee_code_params = employee_codes
    rows = connection.execute(
        f"""
        SELECT DISTINCT vk.virtual_key_id
        FROM virtual_keys vk
        LEFT JOIN virtual_key_consumers assignment
          ON assignment.tenant_id=vk.tenant_id
         AND assignment.virtual_key_id=vk.virtual_key_id
        LEFT JOIN workspace_consumers consumer
          ON consumer.tenant_id=assignment.tenant_id
         AND consumer.consumer_id=assignment.consumer_id
        WHERE vk.tenant_id=? AND vk.status='active'
          AND (
            vk.created_by IN ({owner_placeholders})
            OR consumer.created_by IN ({owner_placeholders})
            OR (
              ? IS NOT NULL
              AND lower(trim(consumer.display_name))=lower(trim(?))
              AND (
                SELECT COUNT(*)
                FROM workspace_consumers same_name
                WHERE same_name.tenant_id=consumer.tenant_id
                  AND same_name.status='active'
                  AND lower(trim(same_name.display_name))=lower(trim(?))
              )=1
            )
            {employee_code_clause}
          )
        ORDER BY vk.virtual_key_id
        """,
        (
            tenant_id,
            *owner_ids,
            *owner_ids,
            member_display_name,
            member_display_name,
            member_display_name,
            *employee_code_params,
        ),
    ).fetchall()

    disabled = 0
    for row in rows:
        virtual_key_id = str(row["virtual_key_id"])
        before = _load_virtual_key(connection, tenant_id, virtual_key_id)
        changed = connection.execute(
            """
            UPDATE virtual_keys
            SET status='disabled', credential_revision=credential_revision+1,
                disabled_at=?, disabled_by=?, disabled_reason='member_removed', updated_at=?
            WHERE tenant_id=? AND virtual_key_id=? AND status='active'
            """,
            (now, actor.user_id, now, tenant_id, virtual_key_id),
        ).rowcount
        if changed != 1:
            continue
        after = _load_virtual_key(connection, tenant_id, virtual_key_id)
        record_audit_log_for_actor(
            connection,
            actor,
            action="virtual_key.updated",
            object_type="virtual_key",
            object_id=virtual_key_id,
            object_name=f"{after.name} / {after.key_prefix}",
            before_summary=_virtual_key_audit_summary(before),
            after_summary=_virtual_key_audit_summary(after),
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        disabled += 1

    # Disable the historical assignment as well. Gateway authentication checks
    # this status on every request, while the key row remains for accounting.
    consumer_owner_clause = f"created_by IN ({owner_placeholders})"
    consumer_name_clause = (
        "lower(trim(display_name))=lower(trim(?))"
        " AND (SELECT COUNT(*) FROM workspace_consumers same_name "
        "WHERE same_name.tenant_id=workspace_consumers.tenant_id "
        "AND same_name.status='active' "
        "AND lower(trim(same_name.display_name))=lower(trim(?)))=1"
        if member_display_name
        else "0"
    )
    consumer_employee_clause = ""
    consumer_employee_params: list[str] = []
    if "employee_code" in consumer_columns:
        employee_codes = [value for value in (member_email, member_account_id) if value]
        if employee_codes:
            consumer_employee_clause = " OR " + " OR ".join(
                "lower(trim(COALESCE(employee_code,'')))=lower(trim(?))"
                for _ in employee_codes
            )
            consumer_employee_params = employee_codes
    consumer_params = [now, tenant_id, *owner_ids]
    if member_display_name:
        consumer_params.append(member_display_name)
        consumer_params.append(member_display_name)
    consumer_params.extend(consumer_employee_params)
    connection.execute(
        f"""
            UPDATE workspace_consumers
            SET status='disabled', revision=revision+1, updated_at=?
            WHERE tenant_id=? AND status='active'
              AND (
                {consumer_owner_clause}
                OR {consumer_name_clause}
                {consumer_employee_clause}
              )
        """,
        consumer_params,
    )
    return disabled


def sync_project_key_lifecycle(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    project_id: str,
    project_status: str,
    actor_user_id: str,
    now: str,
) -> None:
    """Apply project lifecycle to its bound keys without rewriting billing history."""

    if project_status in {"disabled", "archived"}:
        connection.execute(
            """
            UPDATE virtual_keys
            SET status='disabled', credential_revision=credential_revision+1,
                disabled_at=?, disabled_by=?, disabled_reason='project_inactive', updated_at=?
            WHERE tenant_id=? AND project_id=? AND status='active'
            """,
            (now, actor_user_id, now, tenant_id, project_id),
        )
    elif project_status == "active":
        connection.execute(
            """
            UPDATE virtual_keys
            SET status='active', credential_revision=credential_revision+1,
                disabled_at=NULL, disabled_by=NULL, disabled_reason='manual', updated_at=?
            WHERE tenant_id=? AND project_id=? AND status='disabled'
              AND disabled_reason='project_inactive'
              AND application_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM applications a
                WHERE a.tenant_id=virtual_keys.tenant_id
                  AND a.application_id=virtual_keys.application_id
                  AND a.status='active'
              )
            """,
            (now, tenant_id, project_id),
        )


def sync_application_key_lifecycle(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    application_id: str,
    application_status: str,
    actor_user_id: str,
    now: str,
) -> None:
    """Keep assigned personal keys unusable while their application is inactive."""

    if application_status in {"disabled", "archived"}:
        connection.execute(
            """
            UPDATE virtual_keys
            SET status='disabled', credential_revision=credential_revision+1,
                disabled_at=?, disabled_by=?, disabled_reason='project_inactive', updated_at=?
            WHERE tenant_id=? AND application_id=? AND status='active' AND deleted_at IS NULL
            """,
            (now, actor_user_id, now, tenant_id, application_id),
        )
    elif application_status == "active":
        connection.execute(
            """
            UPDATE virtual_keys
            SET status='active', credential_revision=credential_revision+1,
                disabled_at=NULL, disabled_by=NULL, disabled_reason='manual', updated_at=?
            WHERE tenant_id=? AND application_id=? AND status='disabled'
              AND disabled_reason='project_inactive' AND deleted_at IS NULL
              AND EXISTS (
                SELECT 1 FROM projects p
                JOIN applications a ON a.tenant_id=p.tenant_id AND a.project_id=p.project_id
                WHERE a.tenant_id=virtual_keys.tenant_id
                  AND a.application_id=virtual_keys.application_id
                  AND p.status='active'
              )
            """,
            (now, tenant_id, application_id),
        )


def record_virtual_key_binding_change(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    project_id: str | None,
    application_id: str | None,
    effective_at: str,
) -> None:
    """Close the old interval and open the new one for an administrative rebind."""

    _close_virtual_key_binding_history(
        connection,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        effective_to=effective_at,
    )
    if project_id is not None:
        _open_virtual_key_binding_history(
            connection,
            tenant_id=tenant_id,
            virtual_key_id=virtual_key_id,
            project_id=project_id,
            application_id=application_id,
            effective_from=effective_at,
            reason="reassigned",
        )


def list_virtual_keys(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    environment_id: str | None,
    environment: str | None,
    personal_only: bool = False,
    user_id: str | None = None,
    request_id: str,
    trace_id: str,
) -> VirtualKeyListResponse:
    """列出当前租户 Virtual Key。

    Virtual Key 是模型调用入口凭证；列表只展示安全元数据，绝不返回 key_hash 或明文 Key。
    """

    # Disabled keys remain in immutable billing/audit history but stay out of
    # the normal organization directory after a member is removed.
    requested_status = _normalize_optional_status(status)
    normalized_status = requested_status
    normalized_environment = _normalize_optional_environment(environment)
    params: list[str] = [context.user.tenant_id]
    filters: list[str] = []
    if not requested_status:
        filters.append("vk.deleted_at IS NULL")
    if personal_only:
        if not user_id:
            raise ApiError("workspace_session.permission_denied")
        filters.append("vk.created_by=?")
        params.append(user_id)
        filters.append("vk.deleted_at IS NULL")
    if normalized_status:
        filters.append("vk.status=?")
        params.append(normalized_status)
    if normalized_environment:
        filters.append("vk.environment=?")
        params.append(normalized_environment)
    connection = _connect(config)
    try:
        if environment_id:
            # Resolve and list on one tenant-scoped, short-lived database lease.
            binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=environment_id,
                environment_code=normalized_environment,
                require_active=False,
                allow_default=False,
            )
            filters.append("vk.environment_id=?")
            params.append(binding.environment_id)
        where_extra = f" AND {' AND '.join(filters)}" if filters else ""
        rows = connection.execute(
        f"""
        SELECT
          vk.virtual_key_id, vk.tenant_id, vk.name, vk.key_prefix, vk.environment,
          e.environment_id AS environment_id, e.name AS environment_name, e.status AS environment_status, vk.description,
          vk.project_id, p.name AS project_name, vk.application_id, a.name AS application_name,
          vk.created_by AS owner_user_id, creator.display_name AS owner_display_name,
          vk.status, vk.disabled_reason, vk.last_used_at, vk.created_at, vk.updated_at, vk.disabled_at,
          kc.consumer_id, consumer.status AS consumer_status, vk.credential_revision,
          rlr.rate_limit_rule_id, rlr.rpm_limit, rlr.tpm_limit, rlr.concurrency_limit,
          rlr.status AS rate_limit_status, rlr.updated_at AS rate_limit_updated_at,
          CASE WHEN vk.encrypted_key IS NOT NULL THEN 1 ELSE 0 END AS has_recoverable_secret
        FROM virtual_keys vk
        LEFT JOIN virtual_key_consumers kc
          ON kc.tenant_id=vk.tenant_id AND kc.virtual_key_id=vk.virtual_key_id
        LEFT JOIN workspace_consumers consumer
          ON consumer.tenant_id=kc.tenant_id AND consumer.consumer_id=kc.consumer_id
        LEFT JOIN virtual_key_rate_limit_rules rlr
          ON rlr.tenant_id = vk.tenant_id AND rlr.virtual_key_id = vk.virtual_key_id
        LEFT JOIN projects p ON p.tenant_id = vk.tenant_id AND p.project_id = vk.project_id
        LEFT JOIN applications a ON a.tenant_id = vk.tenant_id AND a.application_id = vk.application_id
        LEFT JOIN users creator ON creator.tenant_id = vk.tenant_id AND creator.user_id = vk.created_by
        JOIN environments e
          ON e.tenant_id=vk.tenant_id
         AND (e.environment_id=vk.environment_id OR (vk.environment_id IS NULL AND e.code=vk.environment))
        WHERE vk.tenant_id=? {where_extra}
        ORDER BY vk.created_at DESC, vk.name ASC
        """,
        params,
        ).fetchall()
        return VirtualKeyListResponse(
            virtual_keys=[_virtual_key_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def reveal_virtual_key(
    config: AppConfig,
    context: AuthContext,
    virtual_key_id: str,
    *,
    request_id: str,
    trace_id: str,
    personal_only: bool = False,
) -> RevealVirtualKeyResponse:
    """Return a saved key only for an explicit, authorized copy action."""

    with ResourceWriteTransaction(
        _connect(config), config, context, "virtual_key.read", read_only=True
    ) as transaction:
        connection = transaction.connection
        context = transaction.context
        _require_personal_key_owner(connection, context, virtual_key_id, personal_only)
        row = connection.execute(
            """
            SELECT encrypted_key, status, disabled_reason, project_id, application_id, deleted_at
            FROM virtual_keys
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (context.user.tenant_id, virtual_key_id),
        ).fetchone()
        if row is None:
            raise ApiError("virtual_key.not_found")
        if row["deleted_at"] is not None or row["disabled_reason"] == "member_removed" or not row["encrypted_key"]:
            raise ApiError("virtual_key.reveal_unavailable")
        try:
            plain_value = decrypt_provider_key(
                str(row["encrypted_key"]),
                encryption_secret=config.provider_key_encryption_secret,
            )
        except (TypeError, ValueError):
            raise ApiError("virtual_key.reveal_unavailable") from None
        key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        return RevealVirtualKeyResponse(
            virtual_key=key,
            plain_value=plain_value,
            request_id=request_id,
            trace_id=trace_id,
        )


def create_virtual_key(
    config: AppConfig,
    context: AuthContext,
    payload: CreateVirtualKeyRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
    personal_only: bool = False,
) -> CreateVirtualKeyResponse:
    """创建 Virtual Key。

    明文 Key 只在本函数返回值中出现一次；落库前立即转换为不可逆哈希和短前缀。
    """

    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    if personal_only and (payload.project_id is not None or payload.application_id is not None):
        # 项目和应用归属由组织管理员治理；成员创建自己的 KEY 时不应自行选择成本归属。
        raise ApiError("auth.permission_denied")
    with ResourceWriteTransaction(_connect(config), config, context, "virtual_key.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        existing_personal_key = connection.execute(
            """
            SELECT 1
            FROM virtual_keys
            WHERE tenant_id=? AND created_by=? AND deleted_at IS NULL
            LIMIT 1
            """,
            (context.user.tenant_id, context.user.user_id),
        ).fetchone()
        if existing_personal_key is not None:
            raise ApiError("virtual_key.personal_limit_reached")
        for _attempt in range(5):
            # A collision rolls back the transaction, including its authorization snapshot.
            if _attempt:
                transaction.begin()
                context = transaction.context
            environment_binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=payload.environment_id,
                environment_code=payload.environment,
                invalid_code_error="virtual_key.environment_invalid",
                mismatch_error="virtual_key.environment_mismatch",
            )
            project_id, application_id = _resolve_project_binding(
                connection, context.user.tenant_id, payload.project_id, payload.application_id
            )
            personal_project_status = None
            if personal_only:
                # If an administrator assigned the member before the member
                # created a key, inherit that current application automatically.
                project_id, application_id, personal_project_status = _resolve_personal_application_binding(
                    connection, context.user.tenant_id, context.user.user_id
                )
            if personal_only and payload.consumer_id is not None:
                # Members may create their own key, but cannot issue one for
                # another employee. Reuse the standard 403 contract so the
                # boundary does not surface an internal-only error code.
                raise ApiError("auth.permission_denied")
            if payload.consumer_id is not None:
                require_active_consumer(connection, context, payload.consumer_id)
            now = _to_db_time(_utcnow())
            plain_value = _generate_virtual_key(environment_binding.code)
            virtual_key_id = str(uuid4())
            # A personal key becomes usable only after the administrator assigns
            # an active application in an active project.
            key_status = "active" if project_id is not None and application_id is not None else "disabled"
            disabled_reason = "manual"
            if personal_project_status and personal_project_status != "active":
                key_status = "disabled"
                disabled_reason = "project_inactive"
            try:
                connection.execute(
                    """
                    INSERT INTO virtual_keys(
                      virtual_key_id, tenant_id, name, key_prefix, key_hash, status, disabled_reason,
                      encrypted_key, environment, environment_id, description, project_id, application_id,
                      created_by, created_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        virtual_key_id,
                        context.user.tenant_id,
                        name,
                        secret_prefix(plain_value),
                        hash_secret(plain_value),
                        key_status,
                        disabled_reason,
                        encrypt_provider_key(
                            plain_value, encryption_secret=config.provider_key_encryption_secret
                        ),
                        environment_binding.code,
                        environment_binding.environment_id,
                        description,
                        project_id,
                        application_id,
                        context.user.user_id,
                        now,
                        now,
                    ),
                )
                if project_id is not None:
                    _open_virtual_key_binding_history(
                        connection,
                        tenant_id=context.user.tenant_id,
                        virtual_key_id=virtual_key_id,
                        project_id=project_id,
                        application_id=application_id,
                        effective_from=now,
                        reason="assigned",
                    )
                # 先用同一连接回读脱敏 DTO，再把业务行和审计行作为一个事务提交；明文只保留在响应变量中。
                if payload.consumer_id is not None:
                    connection.execute(
                        """INSERT INTO virtual_key_consumers(
                             virtual_key_id,tenant_id,consumer_id,created_at)
                           VALUES(?,?,?,?)""",
                        (virtual_key_id, context.user.tenant_id, payload.consumer_id, now),
                    )
                key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
                audit_record = record_audit_log(
                    connection,
                    context,
                    action="virtual_key.created",
                    object_type="virtual_key",
                    object_id=key.virtual_key_id,
                    object_name=f"{key.name} / {key.key_prefix}",
                    before_summary={},
                    after_summary=_virtual_key_audit_summary(key),
                    request_meta=_audit_request_meta(audit_request_meta),
                    request_id=request_id,
                    trace_id=trace_id,
                )
                if payload.consumer_id is not None:
                    require_active_consumer(connection, transaction.verify(), payload.consumer_id)
                transaction.commit(commit_audited_transaction)
                dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
                _log_created(context, key, request_id, trace_id)
                return CreateVirtualKeyResponse(
                    virtual_key=key,
                    plain_value=plain_value,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
            except sqlite3.IntegrityError:
                connection.rollback()
                continue
            except Exception:
                # 审计或 commit 失败不能被当作随机 Key 冲突重试，否则可能掩盖持久化故障。
                connection.rollback()
                raise
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.create_invalid")


def rotate_virtual_key(
    config, context, virtual_key_id, payload, *, audit_request_meta, request_id, trace_id, personal_only=False
):
    with ResourceWriteTransaction(
        _connect(config), config, context, "virtual_key.write"
    ) as transaction:
        connection, context = transaction.connection, transaction.context
        _require_personal_key_owner(connection, context, virtual_key_id, personal_only)
        before = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        if before.status != "active" or before.credential_revision != payload.expected_revision:
            raise ApiError("virtual_key.rotate_conflict")
        if before.consumer_id is not None:
            require_active_consumer(connection, context, before.consumer_id)
        plain_value = _generate_virtual_key(before.environment)
        changed = connection.execute(
            """UPDATE virtual_keys SET key_hash=?,key_prefix=?,encrypted_key=?,updated_at=?,
               credential_revision=credential_revision+1
               WHERE tenant_id=? AND virtual_key_id=?
                 AND credential_revision=? AND status='active'""",
            (
                hash_secret(plain_value), secret_prefix(plain_value),
                encrypt_provider_key(plain_value, encryption_secret=config.provider_key_encryption_secret),
                _to_db_time(_utcnow()),
                context.user.tenant_id, virtual_key_id, payload.expected_revision,
            ),
        )
        if changed.rowcount != 1:
            raise ApiError("virtual_key.rotate_conflict")
        key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        record = record_audit_log(
            connection, context, action="virtual_key.rotated", object_type="virtual_key",
            object_id=virtual_key_id, object_name=virtual_key_id,
            before_summary=_virtual_key_audit_summary(before),
            after_summary=_virtual_key_audit_summary(key),
            request_meta=audit_request_meta, request_id=request_id, trace_id=trace_id,
        )
        if before.consumer_id is not None:
            require_active_consumer(connection, transaction.verify(), before.consumer_id)
        transaction.commit(commit_audited_transaction)
        dispatch_audit_recorded(config, context, record, request_id=request_id, trace_id=trace_id)
        return CreateVirtualKeyResponse(
            virtual_key=key, plain_value=plain_value, request_id=request_id, trace_id=trace_id
        )


def update_virtual_key(
    config: AppConfig,
    context: AuthContext,
    virtual_key_id: str,
    payload: UpdateVirtualKeyRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
    personal_only: bool = False,
) -> VirtualKeyResponse:
    """更新当前租户 Virtual Key 的安全元数据和状态。"""

    updates: dict[str, object] = {}
    if payload.name is not None:
        updates["name"] = _normalize_name(payload.name)
    if payload.description is not None:
        updates["description"] = _normalize_description(payload.description)
    if payload.status is not None:
        if personal_only:
            # 成员只能使用自己的 KEY，不能自行启停；项目生命周期由组织管理员控制。
            raise ApiError("auth.permission_denied")
        updates["status"] = _normalize_status(payload.status)
    binding_fields = _provided_fields(payload, {"project_id", "application_id"})
    if personal_only and binding_fields:
        raise ApiError("auth.permission_denied")
    environment_fields = _provided_fields(payload, {"environment_id", "environment"})
    if not updates and not binding_fields and not environment_fields:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.update_empty")

    with ResourceWriteTransaction(_connect(config), config, context, "virtual_key.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        _require_personal_key_owner(connection, context, virtual_key_id, personal_only)
        before = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        if environment_fields:
            # 环境变更必须同时更新权威 ID 和兼容代码，二者冲突时拒绝静默覆盖。
            environment_binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=payload.environment_id,
                environment_code=payload.environment,
                allow_default=False,
                invalid_code_error="virtual_key.environment_invalid",
                mismatch_error="virtual_key.environment_mismatch",
            )
            updates["environment_id"] = environment_binding.environment_id
            updates["environment"] = environment_binding.code
        if binding_fields:
            target_project_id = before.project_id
            target_application_id = before.application_id
            if "project_id" in binding_fields:
                target_project_id = _normalize_optional_text(payload.project_id)
                if target_project_id is None:
                    target_application_id = None
            if "application_id" in binding_fields:
                target_application_id = _normalize_optional_text(payload.application_id)
            target_project_id, target_application_id = _resolve_project_binding(
                connection, context.user.tenant_id, target_project_id, target_application_id
            )
            updates["project_id"] = target_project_id
            updates["application_id"] = target_application_id
            binding_changed = (
                target_project_id != before.project_id
                or target_application_id != before.application_id
            )
        else:
            binding_changed = False
        if (
            binding_changed
            and "status" not in updates
            and before.status == "disabled"
            and target_project_id is not None
            and target_application_id is not None
            and (
                before.disabled_reason == "project_inactive"
                or (before.disabled_reason == "manual" and before.disabled_at is None)
            )
        ):
            # First assignment activates a pending key; an explicitly disabled key stays disabled.
            updates["status"] = "active"
        if updates.get("status") == "active":
            _ensure_virtual_key_project_active(
                connection,
                context.user.tenant_id,
                target_project_id if binding_fields else before.project_id,
                target_application_id if binding_fields else before.application_id,
            )
        now = _to_db_time(_utcnow())
        set_clauses = [f"{column}=?" for column in updates]
        values = list(updates.values())
        disabled_event = before.status != "disabled" and updates.get("status") == "disabled"
        reactivated = before.status == "disabled" and updates.get("status") == "active"
        if disabled_event:
            set_clauses.extend(["disabled_at=?", "disabled_by=?", "disabled_reason=?"])
            values.extend([now, context.user.user_id])
            values.append("manual")
        elif reactivated:
            set_clauses.extend(["disabled_at=NULL", "disabled_by=NULL", "disabled_reason='manual'"])
        set_clauses.append("updated_at=?")
        values.append(now)
        values.extend([context.user.tenant_id, virtual_key_id])
        try:
            connection.execute(
                f"""
                UPDATE virtual_keys
                SET {', '.join(set_clauses)}
                WHERE tenant_id=? AND virtual_key_id=?
                """,
                values,
            )
            if binding_changed:
                _close_virtual_key_binding_history(
                    connection,
                    tenant_id=context.user.tenant_id,
                    virtual_key_id=virtual_key_id,
                    effective_to=now,
                )
                if target_project_id is not None:
                    _open_virtual_key_binding_history(
                        connection,
                        tenant_id=context.user.tenant_id,
                        virtual_key_id=virtual_key_id,
                        project_id=target_project_id,
                        application_id=target_application_id,
                        effective_from=now,
                        reason="reassigned" if before.project_id is not None else "assigned",
                    )
            key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
            audit_record = record_audit_log(
                connection,
                context,
                action="virtual_key.updated",
                object_type="virtual_key",
                object_id=key.virtual_key_id,
                object_name=f"{key.name} / {key.key_prefix}",
                before_summary=_virtual_key_audit_summary(before),
                after_summary=_virtual_key_audit_summary(key),
                request_meta=_audit_request_meta(audit_request_meta),
                request_id=request_id,
                trace_id=trace_id,
            )
            transaction.commit(commit_audited_transaction)
        except Exception:
            # Key 元数据、状态与审计事实必须原子提交，审计不可用时不能留下已生效的 Key 变更。
            connection.rollback()
            raise
        dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
        if disabled_event:
            _log_disabled(context, key, request_id, trace_id)
        return VirtualKeyResponse(virtual_key=key, request_id=request_id, trace_id=trace_id)


def update_virtual_key_limits(
    config: AppConfig,
    context: AuthContext,
    virtual_key_id: str,
    payload: UpdateVirtualKeyLimitsRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
    personal_only: bool = False,
) -> VirtualKeyResponse:
    """更新当前租户某把 Virtual Key 的 RPM、TPM 和并发限制。

    限制设置是治理配置，不接收 tenant_id，也不读取或返回明文 Key；所有写入都先按
    登录态 tenant_id 确认目标 Key 存在，避免跨租户修改。
    """

    if personal_only:
        raise ApiError("auth.permission_denied")
    provided = _provided_fields(payload, {"rpm_limit", "tpm_limit", "concurrency_limit", "status"})
    if not provided:
        # 空 Patch 无法表达“修改”还是“误提交”，必须拒绝，避免控制台误判限流已更新。
        raise ApiError("virtual_key.limit_update_empty")

    updates: dict[str, object | None] = {}
    if "rpm_limit" in provided:
        updates["rpm_limit"] = _normalize_limit_value(payload.rpm_limit)
    if "tpm_limit" in provided:
        updates["tpm_limit"] = _normalize_limit_value(payload.tpm_limit)
    # 用量成本是审计账本字段，缺失或异常必须进入可追踪分支。
    if "concurrency_limit" in provided:
        # 并发限制和 RPM/TPM 使用相同的正整数校验，但含义是“同时在途请求数”。
        updates["concurrency_limit"] = _normalize_limit_value(payload.concurrency_limit)
    if "status" in provided:
        updates["status"] = _normalize_limit_status(payload.status)

    with ResourceWriteTransaction(_connect(config), config, context, "virtual_key.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        _require_personal_key_owner(connection, context, virtual_key_id, personal_only)
        before = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        now = _to_db_time(_utcnow())
        existing = connection.execute(
            """
            SELECT rate_limit_rule_id
            FROM virtual_key_rate_limit_rules
            WHERE tenant_id=? AND virtual_key_id=?
            """,
            (context.user.tenant_id, virtual_key_id),
        ).fetchone()

        if existing is None:
            connection.execute(
                """
                INSERT INTO virtual_key_rate_limit_rules(
                  rate_limit_rule_id, tenant_id, virtual_key_id,
                  rpm_limit, tpm_limit, concurrency_limit, status,
                  created_by, updated_by, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rlr_{uuid4().hex}",
                    context.user.tenant_id,
                    virtual_key_id,
                    updates.get("rpm_limit"),
                    updates.get("tpm_limit"),
                    updates.get("concurrency_limit"),
                    updates.get("status", "active"),
                    context.user.user_id,
                    context.user.user_id,
                    now,
                    now,
                ),
            )
        else:
            set_clauses = [f"{column}=?" for column in updates]
            values = list(updates.values())
            set_clauses.extend(["updated_by=?", "updated_at=?"])
            values.extend([context.user.user_id, now, context.user.tenant_id, virtual_key_id])
            connection.execute(
                f"""
                UPDATE virtual_key_rate_limit_rules
                SET {', '.join(set_clauses)}
                WHERE tenant_id=? AND virtual_key_id=?
                """,
                values,
            )

        try:
            key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
            audit_record = record_audit_log(
                connection,
                context,
                action="virtual_key.updated",
                object_type="virtual_key",
                object_id=key.virtual_key_id,
                object_name=f"{key.name} / {key.key_prefix}",
                before_summary=_virtual_key_audit_summary(before),
                after_summary=_virtual_key_audit_summary(key),
                request_meta=_audit_request_meta(audit_request_meta),
                request_id=request_id,
                trace_id=trace_id,
            )
            transaction.commit(commit_audited_transaction)
        except Exception:
            # 限流规则直接影响调用准入，必须与审计事实原子提交，不能形成无记录的治理变更。
            connection.rollback()
            raise
        dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
        _log_limits_updated(context, key, request_id, trace_id)
        return VirtualKeyResponse(virtual_key=key, request_id=request_id, trace_id=trace_id)


def disable_virtual_key(
    config: AppConfig,
    context: AuthContext,
    virtual_key_id: str,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
    personal_only: bool = False,
) -> VirtualKeyResponse:
    """软禁用 Virtual Key。

    不做物理删除，因为历史 request_logs/usage_ledger 需要继续追溯到这把 Key。
    """

    with ResourceWriteTransaction(_connect(config), config, context, "virtual_key.write") as transaction:
        connection = transaction.connection
        context = transaction.context
        _require_personal_key_owner(connection, context, virtual_key_id, personal_only)
        before = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
        # Soft deletion preserves billing history while releasing the owner's key slot.
        already_deleted = connection.execute(
            "SELECT deleted_at FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
            (context.user.tenant_id, virtual_key_id),
        ).fetchone()["deleted_at"] is not None
        if not already_deleted:
            now = _to_db_time(_utcnow())
            try:
                connection.execute(
                    """
                    UPDATE virtual_keys
                    SET status='disabled', disabled_at=?, disabled_by=?, disabled_reason='manual',
                        encrypted_key=NULL, deleted_at=?,
                        updated_at=?
                    WHERE tenant_id=? AND virtual_key_id=?
                    """,
                    (
                        now,
                        context.user.user_id,
                        now,
                        now,
                        context.user.tenant_id,
                        virtual_key_id,
                    ),
                )
                key = _load_virtual_key(connection, context.user.tenant_id, virtual_key_id)
                audit_record = record_audit_log(
                    connection,
                    context,
                    action="virtual_key.updated",
                    object_type="virtual_key",
                    object_id=key.virtual_key_id,
                    object_name=f"{key.name} / {key.key_prefix}",
                    before_summary=_virtual_key_audit_summary(before),
                    after_summary=_virtual_key_audit_summary(key),
                    request_meta=_audit_request_meta(audit_request_meta),
                    request_id=request_id,
                    trace_id=trace_id,
                )
                transaction.commit(commit_audited_transaction)
            except Exception:
                connection.rollback()
                raise
            dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
        else:
            # 重复禁用是幂等请求，不产生没有实际变化的审计行。
            key = before
        if before.status != "disabled":
            _log_disabled(context, key, request_id, trace_id)
        return VirtualKeyResponse(virtual_key=key, request_id=request_id, trace_id=trace_id)


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    """为内部直调或旧测试补齐空来源元数据，同时保留强制审计。"""

    return value or AuditRequestMeta(client_ip=None, user_agent=None)


def _virtual_key_audit_summary(key: VirtualKeyInfo) -> dict[str, object]:
    """构造 Virtual Key 审计白名单摘要，排除明文、完整哈希和自由文本描述。"""

    return {
        "name": key.name,
        "key_prefix": key.key_prefix,
        "environment_id": key.environment_id,
        "project_id": key.project_id,
        "application_id": key.application_id,
        "consumer_id": key.consumer_id,
        "credential_revision": key.credential_revision,
        "description_hash": _audit_text_hash(key.description),
        "status": key.status,
        "rpm_limit": key.rate_limit_rule.rpm_limit if key.rate_limit_rule else None,
        "tpm_limit": key.rate_limit_rule.tpm_limit if key.rate_limit_rule else None,
        "concurrency_limit": key.rate_limit_rule.concurrency_limit if key.rate_limit_rule else None,
        "rate_limit_status": key.rate_limit_rule.status if key.rate_limit_rule else None,
    }


def load_virtual_key_audit_snapshot(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str,
) -> tuple[str, dict[str, object]]:
    """按租户读取可供其他治理模块使用的 Virtual Key 安全审计快照。

    参数为调用方当前事务连接、可信 tenant_id 和待审计 Key ID；返回值只包含可展示的
    ``object_name`` 与审计字段白名单，不返回明文 Key、key_hash 或自由文本描述。跨模块批量
    修改 Key 归属时必须在写入前后分别调用，并由调用方把业务更新和审计记录放在同一事务。
    """

    key = _load_virtual_key(connection, tenant_id, virtual_key_id)
    return f"{key.name} / {key.key_prefix}", _virtual_key_audit_summary(key)


def _audit_text_hash(value: str | None) -> str | None:
    """用 SHA-256 追踪 description 是否变化，不保存可能含业务信息的自由文本。"""

    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 Virtual Key 管理 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _require_personal_key_owner(connection: sqlite3.Connection, context: AuthContext, virtual_key_id: str, personal_only: bool) -> None:
    if not personal_only:
        return
    row = connection.execute(
        "SELECT created_by FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        (context.user.tenant_id, virtual_key_id),
    ).fetchone()
    if row is None or row["created_by"] != context.user.user_id:
        raise ApiError("virtual_key.not_found")


def _load_virtual_key(connection: sqlite3.Connection, tenant_id: str, virtual_key_id: str) -> VirtualKeyInfo:
    """按当前租户加载 Virtual Key 管理 的 load virtual key 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT
          vk.virtual_key_id, vk.tenant_id, vk.name, vk.key_prefix, vk.environment,
          e.environment_id AS environment_id, e.name AS environment_name, e.status AS environment_status, vk.description,
          vk.project_id, p.name AS project_name, vk.application_id, a.name AS application_name,
          vk.created_by AS owner_user_id, creator.display_name AS owner_display_name,
          vk.status, vk.disabled_reason, vk.last_used_at, vk.created_at, vk.updated_at, vk.disabled_at,
          kc.consumer_id, consumer.status AS consumer_status, vk.credential_revision,
          rlr.rate_limit_rule_id, rlr.rpm_limit, rlr.tpm_limit, rlr.concurrency_limit,
          rlr.status AS rate_limit_status, rlr.updated_at AS rate_limit_updated_at,
          CASE WHEN vk.encrypted_key IS NOT NULL THEN 1 ELSE 0 END AS has_recoverable_secret
        FROM virtual_keys vk
        LEFT JOIN virtual_key_consumers kc
          ON kc.tenant_id=vk.tenant_id AND kc.virtual_key_id=vk.virtual_key_id
        LEFT JOIN workspace_consumers consumer
          ON consumer.tenant_id=kc.tenant_id AND consumer.consumer_id=kc.consumer_id
        LEFT JOIN virtual_key_rate_limit_rules rlr
          ON rlr.tenant_id = vk.tenant_id AND rlr.virtual_key_id = vk.virtual_key_id
        LEFT JOIN projects p ON p.tenant_id = vk.tenant_id AND p.project_id = vk.project_id
        LEFT JOIN applications a ON a.tenant_id = vk.tenant_id AND a.application_id = vk.application_id
        LEFT JOIN users creator ON creator.tenant_id = vk.tenant_id AND creator.user_id = vk.created_by
        JOIN environments e
          ON e.tenant_id=vk.tenant_id
         AND (e.environment_id=vk.environment_id OR (vk.environment_id IS NULL AND e.code=vk.environment))
        WHERE vk.tenant_id=? AND vk.virtual_key_id=?
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.not_found")
    return _virtual_key_info(row)


def _virtual_key_info(row: sqlite3.Row) -> VirtualKeyInfo:
    """在 Virtual Key 管理 内部复用 virtual key info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return VirtualKeyInfo(
        virtual_key_id=row["virtual_key_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        environment=row["environment"],
        environment_id=row["environment_id"],
        environment_name=row["environment_name"],
        environment_status=row["environment_status"],
        description=row["description"],
        owner_display_name=row["owner_display_name"],
        owner_user_id=row["owner_user_id"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        application_id=row["application_id"],
        application_name=row["application_name"],
        consumer_id=row["consumer_id"],
        consumer_status=row["consumer_status"],
        credential_revision=row["credential_revision"],
        status=row["status"],
        disabled_reason=row["disabled_reason"] or "manual",
        last_used_at=_parse_optional_db_time(row["last_used_at"]),
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        disabled_at=_parse_optional_db_time(row["disabled_at"]),
        has_recoverable_secret=bool(row["has_recoverable_secret"]),
        rate_limit_rule=_rate_limit_rule_info(row),
    )


def _rate_limit_rule_info(row: sqlite3.Row) -> RateLimitRuleInfo | None:
    """从 Virtual Key 查询行中提取限流规则响应结构。

    LEFT JOIN 没有命中规则时返回 None；这表示该 Key 当前不限制 RPM、TPM 和并发。
    """

    if row["rate_limit_rule_id"] is None:
        return None
    return RateLimitRuleInfo(
        rate_limit_rule_id=row["rate_limit_rule_id"],
        rpm_limit=row["rpm_limit"],
        tpm_limit=row["tpm_limit"],
        concurrency_limit=row["concurrency_limit"],
        status=row["rate_limit_status"],
        updated_at=_parse_db_time(row["rate_limit_updated_at"]),
    )


def _provided_fields(payload: UpdateVirtualKeyLimitsRequest, allowed: set[str]) -> set[str]:
    """读取调用方实际提交的限制字段。

    Pydantic v1/v2 的字段集合属性名称不同，这里做兼容，确保 null 能被识别为“清除限制”，
    而不是和字段缺失混淆。
    """

    if hasattr(payload, "model_fields_set"):
        raw_fields = payload.model_fields_set
    else:
        raw_fields = getattr(payload, "__fields_set__", set())
    return {field for field in raw_fields if field in allowed}


def _normalize_limit_value(value: object | None) -> int | None:
    """校验并归一化单个限流数值。

    None 表示清除限制；布尔值虽然是 int 的子类，但不能作为限流数值，避免 true/false 被误写成 1/0。
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        # bool 在 Python 中属于 int 子类，必须显式排除，避免 true 被当作阈值 1。
        raise ApiError("virtual_key.limit_invalid")
    if value <= 0 or value > MAX_LIMIT_VALUE:
        # 限流阈值只接受合理正整数，过大数值会让配置失去治理意义并增加计数器压力。
        raise ApiError("virtual_key.limit_invalid")
    return value


def _normalize_limit_status(value: object | None) -> str:
    """校验限流规则启停状态，保持错误码落在 virtual_key.limit_invalid。"""

    status = str(value or "").strip().lower()
    if status not in VALID_STATUSES:
        # 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
        raise ApiError("virtual_key.limit_invalid")
    return status



def _normalize_optional_text(value: object | None) -> str | None:
    """归一化可选归属 ID，空字符串按未绑定处理为 None。"""

    text = str(value or "").strip()
    return text or None


def _resolve_project_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    raw_project_id: object | None,
    raw_application_id: object | None,
) -> tuple[str | None, str | None]:
    """解析 Virtual Key 的项目和应用绑定，并返回最终可信的 project_id 与 application_id。"""

    project_id = _normalize_optional_text(raw_project_id)
    application_id = _normalize_optional_text(raw_application_id)
    if application_id:
        row = connection.execute(
            """
            SELECT a.project_id, a.status AS application_status, p.status AS project_status
            FROM applications a
            JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
            WHERE a.tenant_id=? AND a.application_id=?
            """,
            (tenant_id, application_id),
        ).fetchone()
        if row is None:
            # 应用 ID 必须在当前租户内存在，缺失时不能暴露其他租户是否拥有该应用。
            raise ApiError("application.not_found")
        if row["application_status"] != "active" or row["project_status"] != "active":
            # 只有 active 的项目和应用允许绑定新 Key，避免停用归属继续产生用量。
            raise ApiError("virtual_key.project_binding_invalid")
        if project_id and project_id != row["project_id"]:
            # Application 的父项目是最终事实来源，显式传入冲突项目时必须拒绝。
            raise ApiError("application.project_mismatch")
        return row["project_id"], application_id
    if project_id:
        row = connection.execute(
            "SELECT status FROM projects WHERE tenant_id=? AND project_id=?",
            (tenant_id, project_id),
        ).fetchone()
        if row is None:
            # 单独绑定项目时返回项目域错误，便于控制台把问题定位到 Project 选择器。
            raise ApiError("project.not_found")
        if row["status"] != "active":
            # 非 active 项目不能绑定新 Key，防止归档或停用项目继续产生调用成本。
            raise ApiError("virtual_key.project_binding_invalid")
        return project_id, None
    return None, None


def _resolve_personal_application_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
) -> tuple[str | None, str | None, str | None]:
    """Find the member's latest active application assignment for a new key."""

    row = connection.execute(
        """
        SELECT a.project_id, a.application_id, p.status AS project_status,
               a.status AS application_status
        FROM application_memberships am
        JOIN workspace_memberships wm
          ON wm.tenant_id=am.tenant_id
         AND wm.workspace_membership_id=am.workspace_membership_id
        JOIN applications a
          ON a.tenant_id=am.tenant_id AND a.application_id=am.application_id
        JOIN projects p
          ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id
        WHERE am.tenant_id=? AND am.status='active'
          AND wm.status='active' AND wm.legacy_user_id=?
        ORDER BY am.updated_at DESC, am.application_membership_id DESC
        LIMIT 1
        """,
        (tenant_id, user_id),
    ).fetchone()
    if row is None:
        return None, None, None
    status = "active" if row["project_status"] == "active" and row["application_status"] == "active" else "inactive"
    return row["project_id"], row["application_id"], status


def _ensure_virtual_key_project_active(
    connection: sqlite3.Connection,
    tenant_id: str,
    project_id: str | None,
    application_id: str | None,
) -> None:
    """Only an administrator-assigned active application can make a key usable."""

    if not project_id or not application_id:
        raise ApiError("virtual_key.project_binding_invalid")
    row = connection.execute(
        "SELECT p.status AS project_status, a.status AS application_status "
        "FROM projects p JOIN applications a ON a.tenant_id=p.tenant_id "
        "AND a.project_id=p.project_id WHERE p.tenant_id=? AND p.project_id=? AND a.application_id=?",
        (tenant_id, project_id, application_id),
    ).fetchone()
    if row is None or row["project_status"] != "active" or row["application_status"] != "active":
        raise ApiError("virtual_key.project_binding_invalid")


def _close_virtual_key_binding_history(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    effective_to: str,
) -> None:
    """Close the current binding interval before assigning the next project."""

    connection.execute(
        """
        UPDATE virtual_key_binding_history
        SET effective_to=?
        WHERE tenant_id=? AND virtual_key_id=? AND effective_to IS NULL
        """,
        (effective_to, tenant_id, virtual_key_id),
    )


def _open_virtual_key_binding_history(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    project_id: str,
    application_id: str | None,
    effective_from: str,
    reason: str,
) -> None:
    """Record a current key-to-project interval without storing any secret."""

    binding_id = f"vkbind_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO virtual_key_binding_history(
          virtual_key_binding_id, tenant_id, virtual_key_id, project_id, application_id,
          effective_from, effective_to, change_reason, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            binding_id,
            tenant_id,
            virtual_key_id,
            project_id,
            application_id,
            effective_from,
            reason,
            effective_from,
        ),
    )

def _normalize_name(value: str | None) -> str:
    """规范化 Virtual Key 管理 的 normalize name 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    name = (value or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.name_invalid")
    return name


def _normalize_description(value: str | None) -> str | None:
    """规范化 Virtual Key 管理 的 normalize description 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.create_invalid")
    return description or None


def _normalize_environment(value: str | None) -> str:
    """规范化 Virtual Key 管理 的 normalize environment 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    environment = (value or "dev").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.environment_invalid")
    return environment


def _normalize_optional_environment(value: str | None) -> str | None:
    """规范化 Virtual Key 管理 的 normalize optional environment 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_environment(value)


def _normalize_status(value: str | None) -> str:
    """规范化 Virtual Key 管理 的 normalize status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    status = (value or "").strip().lower()
    if status not in VALID_STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("virtual_key.status_invalid")
    return status


def _normalize_optional_status(value: str | None) -> str | None:
    """规范化 Virtual Key 管理 的 normalize optional status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_status(value)


def _generate_virtual_key(environment: str) -> str:
    # token_urlsafe 使用系统安全随机源；前缀便于人类识别环境，但鉴权只依赖哈希。
    """在 Virtual Key 管理 内部复用 generate virtual key 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return f"mhv_{environment}_{token_urlsafe(32)}"


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 Virtual Key 管理 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 Virtual Key 管理 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 Virtual Key 管理 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 Virtual Key 管理 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)


def _log_created(context: AuthContext, key: VirtualKeyInfo, request_id: str, trace_id: str) -> None:
    """在 Virtual Key 管理 内部复用 log created 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "virtual_key.created",
        extra={
            "event": "virtual_key.created",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": key.virtual_key_id,
            "payload": {
                "name": key.name,
                "key_prefix": key.key_prefix,
                "environment": key.environment,
            },
        },
    )


def _log_disabled(context: AuthContext, key: VirtualKeyInfo, request_id: str, trace_id: str) -> None:
    """在 Virtual Key 管理 内部复用 log disabled 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    logger.info(
        "virtual_key.disabled",
        extra={
            "event": "virtual_key.disabled",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": key.virtual_key_id,
            "payload": {
                "name": key.name,
                "key_prefix": key.key_prefix,
                "environment": key.environment,
            },
        },
    )


def _log_limits_updated(context: AuthContext, key: VirtualKeyInfo, request_id: str, trace_id: str) -> None:
    """记录 Virtual Key 限制配置变更事件。

    日志只包含规则数字和 Key 前缀等安全元数据，不包含明文 Virtual Key、key_hash 或调用内容。
    """

    rule = key.rate_limit_rule
    logger.info(
        "virtual_key.limits_updated",
        extra={
            "event": "virtual_key.limits_updated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": key.virtual_key_id,
            "payload": {
                "key_prefix": key.key_prefix,
                "rpm_limit": rule.rpm_limit if rule else None,
                "tpm_limit": rule.tpm_limit if rule else None,
                "concurrency_limit": rule.concurrency_limit if rule else None,
                "status": rule.status if rule else None,
            },
        },
    )
