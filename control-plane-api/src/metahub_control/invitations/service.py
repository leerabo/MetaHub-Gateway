"""实现 PRD-060 邀请创建、一次性接受和提交后事件投递。

业务写入、AuditLog 与 Invitation source event 必须在同一 SQLite 事务中成功或回滚；
Webhook 网络 I/O 只能在 commit 后执行。原始 Token、邮箱和密码不得进入日志或事件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password, hash_token
from metahub_shared.rbac import ROLE_CODES
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.audit import (
    AuditActor,
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    dispatch_audit_recorded_fact,
    new_audit_log_id,
    record_audit_log,
    record_audit_log_for_actor,
)
from metahub_control.auth import AuthContext
from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_control.workspace_access.service import (
    bridge_legacy_invitation_in_transaction,
    consume_legacy_invitation_in_transaction,
    workspace_access_schema_available_in_transaction,
)

from .models import (
    AcceptedInvitationUser,
    AcceptInvitationRequest,
    CreateInvitationRequest,
    InvitationAcceptResponse,
    InvitationCreateResponse,
    InvitationInfo,
)

logger = logging.getLogger("metahub.control.invitations")

# token_urlsafe(32) 固定产生 43 个 base64url 字符；格式门禁与未知 Token 使用同一公开错误。
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
MIN_PASSWORD_LENGTH = 8
MAX_EXPIRATION_HOURS = 168
# 匿名接受没有可信 Session 邮箱；固定使用 RFC 2606 保留域标记，避免把受邀邮箱复制到
# AuditLog。数据库 owner trigger 仍会用 actor_user_id 反查新用户并校验 Invitation 归属。
INVITATION_ACCEPTED_AUDIT_EMAIL = "invitation-acceptor@redacted.invalid"
_CREATE_DOMAIN_ERRORS = frozenset(
    {
        "invitation.create_invalid",
        "invitation.role_invalid",
        "invitation.user_exists",
        "invitation.pending_exists",
    }
)
_ACCEPT_DOMAIN_ERRORS = frozenset(
    {
        "invitation.not_available",
        "invitation.accept_invalid",
    }
)


def create_invitation(
    config: AppConfig,
    context: AuthContext,
    payload: CreateInvitationRequest,
    *,
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> InvitationCreateResponse:
    """为当前认证租户创建一次性邀请。

    参数：tenant/actor 来自 AuthContext；payload 只包含邮箱、固定角色和小时数。
    返回：Invitation 安全摘要和仅本次返回的原始 Token。
    约束：Invitation、AuditLog 和 ``user.invited`` source event 同事务；任一强事实失败
    都不得返回 Token 或留下无法追踪的 pending 邀请。
    """

    tenant_id = _trusted_tenant_id(context)
    email = _normalize_email(payload.email, "invitation.create_invalid")
    role_code = _normalize_role_code(payload.role, "invitation.role_invalid")
    expiration_hours = _normalize_expiration_hours(payload.expires_in_hours)
    invitation_id = f"inv_{uuid4().hex}"
    event_id = f"ivevt_{uuid4().hex}"
    audit_log_id = new_audit_log_id()
    raw_token = secrets.token_urlsafe(32)
    token_digest = hash_token(raw_token)
    email_fingerprint = _email_fingerprint(invitation_id, email)
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    webhook_event: WebhookEvent | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 写锁可能排队；只有成功取得锁后的时钟才能决定旧邀请是否过期，并作为新邀请起点。
        occurred_at = _utcnow()
        occurred_at_text = _to_db_time(occurred_at)
        expires_at = occurred_at + timedelta(hours=expiration_hours)
        expires_at_text = _to_db_time(expires_at)
        # 同邮箱旧 pending 只按服务端 UTC 到期时间推进，不相信客户端时钟。
        connection.execute(
            """
            UPDATE invitations
            SET status='expired', updated_at=?
            WHERE tenant_id=? AND email=? AND status='pending'
              AND unixepoch(expires_at)<=unixepoch(?)
            """,
            (occurred_at_text, tenant_id, email, occurred_at_text),
        )
        # 已有用户属于当前租户唯一性事实，不能再创建会与账号竞争的邀请。
        if _user_email_exists(connection, tenant_id, email):
            raise ApiError("invitation.user_exists")
        # 同租户同邮箱仍有 pending 邀请时稳定返回冲突，禁止生成第二个有效 Token。
        if _pending_invitation_exists(connection, tenant_id, email):
            raise ApiError("invitation.pending_exists")
        _require_active_role(connection, tenant_id, role_code, "invitation.role_invalid")

        connection.execute(
            """
            INSERT INTO invitations(
              invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
              invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'pending',?,NULL,?,NULL,?,?)
            """,
            (
                invitation_id,
                tenant_id,
                email,
                email_fingerprint,
                role_code,
                token_digest,
                context.user.user_id,
                expires_at_text,
                occurred_at_text,
                occurred_at_text,
            ),
        )
        bridge_legacy_invitation_in_transaction(
            connection,
            config,
            legacy_invitation_id=invitation_id,
            tenant_id=tenant_id,
            inviter_user_id=context.user.user_id,
            target_email=email,
            legacy_role=role_code,
            token_hash=token_digest,
            expires_at=expires_at_text,
            request_id=request_id,
            trace_id=trace_id,
            now=occurred_at,
        )
        after_summary = _audit_summary(
            email_fingerprint=email_fingerprint,
            role_code=role_code,
            status="pending",
            expires_at=expires_at_text,
            accepted_user_id=None,
        )
        audit_record = record_audit_log(
            connection,
            context,
            action="user.invited",
            object_type="invitation",
            object_id=invitation_id,
            object_name=None,
            before_summary={},
            after_summary=after_summary,
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            audit_log_id=audit_log_id,
            created_at=occurred_at,
        )
        if audit_record is None:
            # 创建事实必然从空摘要变为 pending；无审计表示领域接入合同已经损坏。
            raise ApiError("invitation.unavailable")
        event_payload = {
            "schema_version": 1,
            "invitation_id": invitation_id,
            "email_fingerprint": email_fingerprint,
            "role_code": role_code,
            "expires_at": expires_at_text,
            "invited_at": occurred_at_text,
        }
        # 提交前完成严格 payload/envelope 校验；这里只构造对象，不执行网络 I/O。
        webhook_event = build_webhook_event(
            tenant_id=tenant_id,
            event_type="user.invited",
            event_id=event_id,
            actor_id=context.user.user_id,
            object_id=invitation_id,
            trace_id=trace_id,
            source_request_id=request_id,
            timestamp=occurred_at,
            payload=event_payload,
        )
        _insert_source_event(
            connection,
            event_id=event_id,
            tenant_id=tenant_id,
            invitation_id=invitation_id,
            event_type="user.invited",
            actor_user_id=context.user.user_id,
            actor_role=context.user.role,
            audit_log_id=audit_log_id,
            request_id=request_id,
            trace_id=trace_id,
            payload=event_payload,
            occurred_at=occurred_at_text,
        )
        commit_audited_transaction(connection)
    # 事务内领域错误先统一回滚，再决定是否保留公开业务语义。
    except ApiError as exc:
        connection.rollback()
        # 只有创建合同声明的可诊断错误可原样返回，内部强事实故障必须折叠。
        if exc.code in _CREATE_DOMAIN_ERRORS:
            raise
        # 审计、目录或其他内部错误统一折叠，不能泄漏表名、SQL 或 Token 摘要。
        raise ApiError("invitation.unavailable") from exc
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError, KeyError) as exc:
        connection.rollback()
        # SQL、文件或类型异常可能含内部结构，统一转换为可重试 503。
        raise ApiError("invitation.unavailable") from exc
    finally:
        connection.close()

    if audit_record is None or webhook_event is None:
        # 理论不可达分支显式失败关闭，避免未来重构跳过强审计初始化。
        raise ApiError("invitation.unavailable")
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    _dispatch_invitation_event(
        config,
        tenant_id,
        event_id,
        webhook_event,
        actor_user_id=context.user.user_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    logger.info(
        "invitation.created",
        extra={
            "event": "invitation.created",
            "tenant_id": tenant_id,
            "actor_id": context.user.user_id,
            "object_id": invitation_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "role_code": role_code,
                "email_fingerprint": email_fingerprint,
                "expires_at": expires_at_text,
            },
        },
    )
    return InvitationCreateResponse(
        invitation=InvitationInfo(
            invitation_id=invitation_id,
            tenant_id=tenant_id,
            email=email,
            role=role_code,
            status="pending",
            expires_at=expires_at,
            created_at=occurred_at,
        ),
        accept_token=raw_token,
        request_id=request_id,
        trace_id=trace_id,
    )


def accept_invitation(
    config: AppConfig,
    token: str,
    payload: AcceptInvitationRequest,
    *,
    request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> InvitationAcceptResponse:
    """使用一次性 Token 创建目标租户用户并消费 Invitation。

    参数：Token 只用于计算查询哈希；payload 只含显示名和密码。
    返回：不含 tenant、Token、密码哈希或 Session 的新用户摘要。
    约束：租户、邮箱、角色完全来自锁内 Invitation；并发请求最多一个提交成功。
    """

    token_digest = _token_digest(token)
    display_name = _normalize_display_name(payload.display_name)
    # 密码最小长度在哈希前复核，过短输入不进入数据库写事务。
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise ApiError("invitation.accept_invalid")
    event_id = f"ivevt_{uuid4().hex}"
    audit_log_id = new_audit_log_id()
    user_id = str(uuid4())
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    webhook_event: WebhookEvent | None = None
    accepted_user: AcceptedInvitationUser | None = None
    tenant_id = ""
    invitation_id = ""
    email_fingerprint = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Token 是否过期必须以成功取得写锁后的时间判断，避免锁等待跨过期限仍按旧时钟接收。
        occurred_at = _utcnow()
        occurred_at_text = _to_db_time(occurred_at)
        invitation = connection.execute(
            """
            SELECT invitation.*, tenant.status AS tenant_status,
                   role.status AS role_status,
                   inviter.status AS inviter_status,
                   CASE WHEN EXISTS (
                     SELECT 1
                     FROM roles inviter_role
                     JOIN role_permissions relation
                       ON relation.tenant_id=inviter_role.tenant_id
                      AND relation.role_id=inviter_role.role_id
                     JOIN permissions permission
                       ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                     WHERE inviter_role.tenant_id=invitation.tenant_id
                       AND inviter_role.code=inviter.role
                       AND inviter_role.status='active'
                       AND permission.code='user.manage'
                       AND permission.status='active'
                   ) THEN 1 ELSE 0 END AS inviter_can_manage
            FROM invitations invitation
            JOIN tenants tenant ON tenant.tenant_id=invitation.tenant_id
            JOIN users inviter
              ON inviter.tenant_id=invitation.tenant_id
             AND inviter.user_id=invitation.invited_by
            LEFT JOIN roles role
              ON role.tenant_id=invitation.tenant_id AND role.code=invitation.role_code
            WHERE invitation.token_hash=?
            """,
            (token_digest,),
        ).fetchone()
        # 未命中与非 pending 必须同形，不能证明 Token 是否曾经有效。
        if invitation is None or invitation["status"] != "pending":
            raise ApiError("invitation.not_available")
        tenant_id = str(invitation["tenant_id"])
        invitation_id = str(invitation["invitation_id"])
        email_fingerprint = str(invitation["email_fingerprint"])
        expires_at = _parse_db_time(str(invitation["expires_at"]))
        if expires_at <= occurred_at:
            # 时间已过即使 status 尚未清扫也不可使用；状态推进后提交，便于后续管理员重新邀请。
            cursor = connection.execute(
                """
                UPDATE invitations SET status='expired',updated_at=?
                WHERE tenant_id=? AND invitation_id=? AND status='pending'
                """,
                (occurred_at_text, tenant_id, invitation_id),
            )
            if cursor.rowcount != 1:
                # 并发终态推进失败同样保持公开 410，不暴露真实状态竞争。
                raise ApiError("invitation.not_available")
            connection.commit()
            # 到期状态已持久化，但公开端仍只看到统一不可用语义。
            raise ApiError("invitation.not_available")
        if (
            invitation["tenant_status"] != "active"
            or invitation["role_status"] != "active"
            or invitation["inviter_status"] != "active"
            or invitation["inviter_can_manage"] != 1
        ):
            # 不允许角色/租户停用或邀请人失权后继续加入；公开端统一 410，不能用差异码
            # 证明 Token 已命中真实 Invitation，也不能静默回退默认角色。
            raise ApiError("invitation.not_available")
        role_code = _normalize_role_code(
            invitation["role_code"], "invitation.not_available"
        )
        email = _normalize_email(invitation["email"], "invitation.not_available")
        _require_active_role(
            connection,
            tenant_id,
            role_code,
            "invitation.not_available",
        )
        # 邮箱竞争创建同样不能证明 Token 有效；管理员可在受保护用户页查看真实账号。
        if _user_email_exists(connection, tenant_id, email):
            raise ApiError("invitation.not_available")

        # 明文密码只在本事务内参与 PBKDF2；不能写入日志、事件、响应或普通数据库列。
        password_digest = hash_password(payload.password)
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                user_id,
                tenant_id,
                email,
                display_name,
                password_digest,
                role_code,
                occurred_at_text,
                occurred_at_text,
            ),
        )
        cursor = connection.execute(
            """
            UPDATE invitations
            SET status='accepted',accepted_user_id=?,accepted_at=?,updated_at=?
            WHERE tenant_id=? AND invitation_id=? AND token_hash=? AND status='pending'
              AND unixepoch(expires_at)>unixepoch(?)
            """,
            (
                user_id,
                occurred_at_text,
                occurred_at_text,
                tenant_id,
                invitation_id,
                token_digest,
                occurred_at_text,
            ),
        )
        if cursor.rowcount != 1:
            # 锁内 CAS 未命中只能按公开同形不可用处理，不能暴露真实终态。
            raise ApiError("invitation.not_available")
        if workspace_access_schema_available_in_transaction(connection):
            account = connection.execute(
                """
                SELECT account_id FROM account_identity_links
                WHERE legacy_tenant_id=? AND legacy_user_id=? AND status='active'
                """,
                (tenant_id, user_id),
            ).fetchone()
            if account is None or not consume_legacy_invitation_in_transaction(
                connection,
                config,
                legacy_invitation_id=invitation_id,
                account_id=account["account_id"],
                email=email,
                existing_user_id=user_id,
                request_id=request_id,
                trace_id=trace_id,
                now=occurred_at,
            ):
                raise ApiError("invitation.not_available")

        before_summary = _audit_summary(
            email_fingerprint=email_fingerprint,
            role_code=role_code,
            status="pending",
            expires_at=str(invitation["expires_at"]),
            accepted_user_id=None,
        )
        after_summary = _audit_summary(
            email_fingerprint=email_fingerprint,
            role_code=role_code,
            status="accepted",
            expires_at=str(invitation["expires_at"]),
            accepted_user_id=user_id,
        )
        audit_actor = AuditActor(
            tenant_id=tenant_id,
            user_id=user_id,
            # 接受者邮箱只属于 Invitation/User 业务表；审计通过固定标记和不可逆指纹追踪，
            # 不能为了复用普通 AuthContext 审计合同再复制一份个人信息。
            email=INVITATION_ACCEPTED_AUDIT_EMAIL,
            role=role_code,
        )
        audit_record = record_audit_log_for_actor(
            connection,
            audit_actor,
            action="invitation.accepted",
            object_type="invitation",
            object_id=invitation_id,
            object_name=None,
            before_summary=before_summary,
            after_summary=after_summary,
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            audit_log_id=audit_log_id,
            created_at=occurred_at,
        )
        if audit_record is None:
            # 接受事实没有强审计时必须整体回滚，不能留下无追踪的新用户。
            raise ApiError("invitation.unavailable")
        event_payload = {
            "schema_version": 1,
            "invitation_id": invitation_id,
            "user_id": user_id,
            "email_fingerprint": email_fingerprint,
            "role_code": role_code,
            "accepted_at": occurred_at_text,
        }
        webhook_event = build_webhook_event(
            tenant_id=tenant_id,
            event_type="invitation.accepted",
            event_id=event_id,
            actor_id=user_id,
            object_id=invitation_id,
            trace_id=trace_id,
            source_request_id=request_id,
            timestamp=occurred_at,
            payload=event_payload,
        )
        _insert_source_event(
            connection,
            event_id=event_id,
            tenant_id=tenant_id,
            invitation_id=invitation_id,
            event_type="invitation.accepted",
            actor_user_id=user_id,
            actor_role=role_code,
            audit_log_id=audit_log_id,
            request_id=request_id,
            trace_id=trace_id,
            payload=event_payload,
            occurred_at=occurred_at_text,
        )
        accepted_user = AcceptedInvitationUser(
            user_id=user_id,
            email=email,
            display_name=display_name,
            role=role_code,
        )
        commit_audited_transaction(connection)
    # 接受事务中的所有领域失败先回滚 User、Invitation、Audit 与 source event。
    except ApiError as exc:
        connection.rollback()
        # 只保留公开同形 410 和正文 422，其余内部错误统一折叠为 503。
        if exc.code in _ACCEPT_DOMAIN_ERRORS:
            raise
        # 未声明领域码可能包含内部依赖信息，不能透传给公开接受页。
        raise ApiError("invitation.unavailable") from exc
    # SQLite、哈希或严格解码失败也必须原子回滚并隐藏基础设施细节。
    except (sqlite3.Error, DbError, OSError, TypeError, ValueError, KeyError) as exc:
        connection.rollback()
        # 固定错误不附加原始异常文本，避免邮箱、Token 或 SQL 进入响应。
        raise ApiError("invitation.unavailable") from exc
    finally:
        connection.close()

    # 理论不可达的初始化缺口仍失败关闭，防止未来重构绕过强事实构造。
    if audit_record is None or webhook_event is None or accepted_user is None or not tenant_id:
        raise ApiError("invitation.unavailable")
    dispatch_audit_recorded_fact(
        config,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    _dispatch_invitation_event(
        config,
        tenant_id,
        event_id,
        webhook_event,
        actor_user_id=user_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    logger.info(
        "invitation.accepted",
        extra={
            "event": "invitation.accepted",
            "tenant_id": tenant_id,
            "actor_id": user_id,
            "object_id": invitation_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "role_code": accepted_user.role,
                "email_fingerprint": email_fingerprint,
            },
        },
    )
    return InvitationAcceptResponse(
        user=accepted_user,
        request_id=request_id,
        trace_id=trace_id,
    )


def _insert_source_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    tenant_id: str,
    invitation_id: str,
    event_type: str,
    actor_user_id: str,
    actor_role: str,
    audit_log_id: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    occurred_at: str,
) -> None:
    """在调用方事务内写入不可变 Invitation source event。"""

    connection.execute(
        """
        INSERT INTO invitation_events(
          invitation_event_id,tenant_id,invitation_id,event_type,actor_user_id,actor_role,
          audit_log_id,request_id,trace_id,payload_json,delivery_status,last_error_code,
          created_at,updated_at,dispatched_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,'pending',NULL,?,?,NULL)
        """,
        (
            event_id,
            tenant_id,
            invitation_id,
            event_type,
            actor_user_id,
            actor_role,
            audit_log_id,
            request_id,
            trace_id,
            _json_text(payload),
            occurred_at,
            occurred_at,
        ),
    )


def _dispatch_invitation_event(
    config: AppConfig,
    tenant_id: str,
    event_id: str,
    event: WebhookEvent,
    *,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递 Invitation 事件，并推进 source event 投递终态。"""

    delivery_status = "dispatched"
    error_code: str | None = None
    try:
        deliveries = dispatch_webhook_event(
            config,
            tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
        if any(delivery.status != "success" for delivery in deliveries):
            delivery_status = "failed"
            error_code = "webhook.delivery_unavailable"
    except Exception:
        # 外部异常可能含 Endpoint URL、响应或签名；日志只写固定字段和错误码。
        delivery_status = "failed"
        error_code = "webhook.delivery_unavailable"
        logger.error(
            "invitation.webhook_dispatch_failed",
            extra={
                "event": "invitation.webhook_dispatch_failed",
                "tenant_id": tenant_id,
                "actor_id": actor_user_id,
                "object_id": event.object_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": error_code,
            },
        )
    _mark_event_delivery(
        config,
        tenant_id,
        event_id,
        status=delivery_status,
        error_code=error_code,
        request_id=request_id,
        trace_id=trace_id,
    )


def _mark_event_delivery(
    config: AppConfig,
    tenant_id: str,
    event_id: str,
    *,
    status: str,
    error_code: str | None,
    request_id: str,
    trace_id: str,
) -> None:
    """用独立短事务把 source event 从 pending 推进到 dispatched 或 failed。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(config)
        now = _to_db_time(_utcnow())
        cursor = connection.execute(
            """
            UPDATE invitation_events
            SET delivery_status=?,last_error_code=?,updated_at=?,dispatched_at=?
            WHERE tenant_id=? AND invitation_event_id=? AND delivery_status='pending'
            """,
            (status, error_code, now, now, tenant_id, event_id),
        )
        if cursor.rowcount != 1:
            # source event 只能从 pending 推进一次，重复或缺失写入按内部状态故障处理。
            raise sqlite3.IntegrityError("invitation_event_state")
        connection.commit()
    except Exception:
        if connection is not None:
            connection.rollback()
        # 业务与审计已经提交，状态回写失败只能记录固定事件，不能把成功响应改成 5xx。
        logger.error(
            "invitation.event_status_update_failed",
            extra={
                "event": "invitation.event_status_update_failed",
                "tenant_id": tenant_id,
                "object_id": event_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "invitation.unavailable",
            },
        )
    finally:
        if connection is not None:
            connection.close()


def _trusted_tenant_id(context: AuthContext) -> str:
    """复核认证用户、租户和角色均来自同一可信上下文。"""

    tenant_id = context.user.tenant_id
    if (
        type(tenant_id) is not str
        or not tenant_id
        or tenant_id != context.tenant.tenant_id
        or context.user.role not in ROLE_CODES
    ):
        # 上下文损坏属于内部授权完整性故障，不能回退默认租户。
        raise ApiError("invitation.unavailable")
    return tenant_id


def _normalize_email(value: object, error_code: str) -> str:
    """规范化有界邮箱，并拒绝控制字符、空 local/domain 和多重 @。"""

    # 非字符串不能宽松转换为邮箱，否则 bool/对象可能绕过 strict wire 的业务层复核。
    if type(value) is not str:
        raise ApiError(error_code)
    normalized = value.strip().lower()
    if (
        len(normalized) < 3
        or len(normalized) > 255
        or normalized.count("@") != 1
        or any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in normalized
        )
    ):
        # 长度、@ 数量、空白或控制字符任一异常都使用调用方指定的稳定错误码。
        raise ApiError(error_code)
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        # 空 local/domain 和边界点号会产生不可投递或歧义账号，不能进入租户唯一键。
        raise ApiError(error_code)
    return normalized


def _normalize_display_name(value: object) -> str:
    """规范化接受页显示名，拒绝控制字符和空白结果。"""

    # 显示名不接受宽松类型转换，防止对象 repr 或 bool 进入用户资料。
    if type(value) is not str:
        raise ApiError("invitation.accept_invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        # 空结果、超长值和控制字符统一在密码哈希与用户写入前拒绝。
        raise ApiError("invitation.accept_invalid")
    return normalized


def _normalize_role_code(value: object, error_code: str) -> str:
    """规范化固定角色 code；未知值不能进入租户角色查询。"""

    # 角色 code 必须是明确字符串，不能从数字、bool 或对象推导默认角色。
    if type(value) is not str:
        raise ApiError(error_code)
    normalized = value.strip().lower()
    # 只接受冻结六角色目录，未知 code 不进入 tenant-qualified 数据库查询。
    if normalized not in ROLE_CODES:
        raise ApiError(error_code)
    return normalized


def _normalize_expiration_hours(value: object) -> int:
    """要求真实整数且处于 1..168 小时，拒绝 bool 和宽松字符串转换。"""

    # bool 是 int 子类，因此必须用精确类型和产品上限同时校验。
    if type(value) is not int or value < 1 or value > MAX_EXPIRATION_HOURS:
        raise ApiError("invitation.create_invalid")
    return value


def _token_digest(value: object) -> str:
    """校验固定 base64url Token 并只返回不可逆哈希；所有失败保持同形 410。"""

    # 格式错误与未知 Token 完全同形，不能通过长度或字符集差异形成枚举侧信道。
    if type(value) is not str or TOKEN_PATTERN.fullmatch(value) is None:
        raise ApiError("invitation.not_available")
    # 仅返回不可逆摘要供索引查询，原始 Token 不得传入数据库或日志层。
    return hash_token(value)


def _email_fingerprint(invitation_id: str, email: str) -> str:
    """用随机 Invitation ID 加盐生成不可逆邮箱指纹，供审计和事件关联。"""

    return hashlib.sha256(f"{invitation_id}\x00{email}".encode()).hexdigest()


def _require_active_role(
    connection: sqlite3.Connection,
    tenant_id: str,
    role_code: str,
    error_code: str,
) -> None:
    """按 tenant-qualified 条件复核目标固定角色仍为 active。"""

    row = connection.execute(
        "SELECT 1 FROM roles WHERE tenant_id=? AND code=? AND status='active'",
        (tenant_id, role_code),
    ).fetchone()
    if row is None:
        # 角色缺失或停用时禁止默认回退 Developer，调用方决定公开或管理错误语义。
        raise ApiError(error_code)


def _user_email_exists(connection: sqlite3.Connection, tenant_id: str, email: str) -> bool:
    """只在当前租户检查用户邮箱，禁止跨租户枚举同名账号。"""

    return connection.execute(
        "SELECT 1 FROM users WHERE tenant_id=? AND email=?",
        (tenant_id, email),
    ).fetchone() is not None


def _pending_invitation_exists(
    connection: sqlite3.Connection,
    tenant_id: str,
    email: str,
) -> bool:
    """只在当前租户检查仍未消费的同邮箱邀请。"""

    return connection.execute(
        "SELECT 1 FROM invitations WHERE tenant_id=? AND email=? AND status='pending'",
        (tenant_id, email),
    ).fetchone() is not None


def _audit_summary(
    *,
    email_fingerprint: str,
    role_code: str,
    status: str,
    expires_at: str,
    accepted_user_id: str | None,
) -> dict[str, object]:
    """构造 Invitation AuditLog 白名单摘要，不复制邮箱、Token 或邮件文案。"""

    return {
        "email_fingerprint": email_fingerprint,
        "role_code": role_code,
        "status": status,
        "expires_at": expires_at,
        "accepted_user_id": accepted_user_id,
    }


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 Invitation 短连接，并把基础设施细节折叠为稳定领域错误。"""

    try:
        return connect_database(config.database_url)
    # 连接配置或文件系统异常不能暴露数据库 URL 和路径，统一折叠为领域不可用。
    except (DbError, OSError) as exc:
        raise ApiError("invitation.unavailable") from exc


def _json_text(value: object) -> str:
    """生成稳定紧凑 JSON，供 source event trigger 和测试逐字复演。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _utcnow() -> datetime:
    """返回秒级 UTC 时间，保持 Invitation、审计和事件完全一致。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把带时区时间规范化为 SQLite 使用的 canonical UTC second。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: str) -> datetime:
    """解析 Invitation 数据库时间；损坏值不能被当作仍有效邀请。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 损坏时间值不能被当作仍有效邀请，也不能把原文本带入公开错误。
    except ValueError as exc:
        raise ApiError("invitation.not_available") from exc
    if parsed.tzinfo is None:
        # 无时区时间不可与服务端 UTC 安全比较，必须失败关闭。
        raise ApiError("invitation.not_available")
    return parsed.astimezone(UTC)
