"""PRD-066 企业 Chat 控制面编排服务。

该服务只持久化会话和执行元数据。用户 history 与 assistant 正文只在当前函数调用栈中
传递，绝不写入数据库、结构化日志、Webhook 或错误正文。每次发送先以 tenant、用户、
登录 Session 和 ``chat.use`` 权限建立后端边界，再通过单次 grant 进入 Gateway 全治理链。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from secrets import token_urlsafe
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.prompt_templates import (
    PromptTemplateContractError,
    PromptTemplateDefinitionUnavailable,
    PromptTemplateOwner,
    PromptTemplateSecretUnavailable,
    decrypt_definition,
    render_prompt,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.auth import AuthContext, require_transaction_permission
from metahub_control.webhooks.service import dispatch_webhook_event

from .models import (
    ChatAssistantMessage,
    ChatExecutionSummary,
    ChatHistoryMessage,
    ChatLimits,
    ChatMessageResponse,
    ChatModelAliasOption,
    ChatOptionsResponse,
    ChatPolicySummary,
    ChatProjectOption,
    ChatPromptTemplateOption,
    ChatSessionInfo,
    ChatSessionResponse,
    ChatTemplateMessageResponse,
    CreateChatSessionRequest,
    SendChatMessageRequest,
)

logger = logging.getLogger("metahub.enterprise_chat")

# PAGE-093 是有界演示入口，不提供无限历史或流式正文；常量同时用于 API 返回和后端复核。
MAX_HISTORY_MESSAGES = 20
MAX_MESSAGE_CHARS = 8_000
MAX_HISTORY_BYTES = 65_536
CHAT_SESSION_TTL_SECONDS = 28_800
CHAT_GRANT_TTL_SECONDS = 30
GATEWAY_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """后端解析出的项目、环境、模型别名和唯一 Virtual Key 绑定。

    该结构永远不进入浏览器响应；``virtual_key_id`` 只供 Session/grant 元数据和 Gateway
    可信上下文使用，明文 Key 不存在于 Control 进程。
    """

    project_id: str
    project_name: str
    environment_id: str
    environment_code: str
    environment_name: str
    model_alias_id: str
    model_alias: str
    model_alias_name: str
    virtual_key_id: str


@dataclass(frozen=True, slots=True)
class PromptTemplateInvocationBinding:
    """准备事务从 current published 版本恢复的模板身份和内存渲染结果。

    正文关闭 repr，且只用于 Control 预校验和成功响应交叉核对；数据库 Invocation 仅保存
    template/version/number owner，不保存变量、定义、渲染文本或 Response。
    """

    prompt_template_id: str
    prompt_template_version_id: str
    version_number: int
    rendered_user: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedChatMessage:
    """准备事务提交后可传给 Gateway 的非敏感执行身份。

    明文 grant 使用 ``repr=False``，且只在本次 Control -> Gateway 调用栈存活；数据库仅保存
   其 SHA-256。history 不放入该结构，避免元数据对象被误序列化时夹带正文。
    """

    tenant_id: str
    chat_session_id: str
    chat_message_id: str
    client_message_id: str
    gateway_request_id: str
    gateway_trace_id: str
    grant: str = field(repr=False)
    prompt_template_invocation_id: str | None = None
    expected_rendered_user: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChatTerminalFact:
    """ChatMessage 终态及 Webhook 所需的严格安全字段。"""

    tenant_id: str
    actor_user_id: str
    chat_session_id: str
    chat_message_id: str
    project_id: str
    environment_id: str
    model_alias_id: str
    gateway_request_id: str
    gateway_trace_id: str
    webhook_event_id: str
    status: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_tokens: int | None
    cost_cny_micros: int | None
    policy_decision: str | None
    policy_reason_code: str | None
    fallback_triggered: bool | None
    cache_status: str | None
    error_code: str | None
    completed_at: str


class ChatDispatchError(Exception):
    """携带已提交失败终态和待后台派发事件的内部控制流异常。

    对外仍抛其中的 ``ApiError``，本异常自身不包含 history、assistant、Token 或底层异常正文。
    """

    def __init__(self, error: ApiError, event: WebhookEvent) -> None:
        """保存统一错误与安全事件，供路由注册 BackgroundTask 后重新抛错。"""

        super().__init__(error.code)
        self.error = error
        self.event = event


def chat_limits() -> ChatLimits:
    """返回前后端共享的固定输入上限。"""

    return ChatLimits(
        max_history_messages=MAX_HISTORY_MESSAGES,
        max_message_chars=MAX_MESSAGE_CHARS,
        max_history_bytes=MAX_HISTORY_BYTES,
        session_ttl_seconds=CHAT_SESSION_TTL_SECONDS,
    )


def list_chat_options(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> ChatOptionsResponse:
    """返回当前租户可用于 PAGE-093 的最小项目和模型别名目录。

    Viewer 可凭 ``chat.use`` 读取本目录，但不会因此获得 Project、Model、Virtual Key 或
    Provider 的通用读权限。Key 只折叠为 ready/unavailable/ambiguous 三态。
    """

    connection = _connect(config)
    try:
        projects = connection.execute(
            """
            SELECT project_id,name
            FROM projects
            WHERE tenant_id=? AND status='active'
            ORDER BY name,project_id
            """,
            (context.user.tenant_id,),
        ).fetchall()
        environments = connection.execute(
            """
            SELECT environment_id,code,name
            FROM environments
            WHERE tenant_id=? AND status='active'
            ORDER BY code,environment_id
            """,
            (context.user.tenant_id,),
        ).fetchall()
        aliases = connection.execute(
            """
            SELECT ma.model_alias_id,ma.alias,ma.description,e.environment_id
            FROM model_aliases ma
            JOIN environments e
              ON e.tenant_id=ma.tenant_id
             AND (ma.environment_id=e.environment_id
                  OR (ma.environment_id IS NULL AND ma.environment=e.code))
            WHERE ma.tenant_id=? AND ma.status='active' AND e.status='active'
            ORDER BY ma.alias,ma.model_alias_id
            """,
            (context.user.tenant_id,),
        ).fetchall()
        alias_environment_ids = {row["environment_id"] for row in aliases}
        key_counts = {
            (row["project_id"], row["environment_id"]): int(row["key_count"])
            for row in connection.execute(
                """
                SELECT vk.project_id,e.environment_id,COUNT(*) AS key_count
                FROM virtual_keys vk
                JOIN environments e
                  ON e.tenant_id=vk.tenant_id
                 AND (vk.environment_id=e.environment_id
                      OR (vk.environment_id IS NULL AND vk.environment=e.code))
                WHERE vk.tenant_id=? AND vk.status='active' AND e.status='active'
                  AND vk.project_id IS NOT NULL
                GROUP BY vk.project_id,e.environment_id
                """,
                (context.user.tenant_id,),
            ).fetchall()
        }
        project_options: list[ChatProjectOption] = []
        for project in projects:
            for environment in environments:
                # 没有任何 active 别名的环境无法完成 Chat，目录无需制造不可操作组合。
                if environment["environment_id"] not in alias_environment_ids:
                    continue
                count = key_counts.get(
                    (project["project_id"], environment["environment_id"]), 0
                )
                availability = (
                    "ready" if count == 1 else "key_unavailable" if count == 0 else "ambiguous_key"
                )
                project_options.append(
                    ChatProjectOption(
                        project_id=project["project_id"],
                        name=project["name"],
                        environment_id=environment["environment_id"],
                        environment_name=environment["name"],
                        availability=availability,
                    )
                )
        alias_options = [
            ChatModelAliasOption(
                model_alias_id=row["model_alias_id"],
                alias=row["alias"],
                display_name=(row["description"] or row["alias"]).strip(),
                environment_id=row["environment_id"],
            )
            for row in aliases
        ]
        # use 是独立授权边界：缺权时不查询、不解密任何模板；read/write 也不能替代 use。
        prompt_template_options = (
            _list_chat_prompt_templates(connection, config, context)
            if "prompt_template.use" in context.permission_codes
            else []
        )
        revision_payload = {
            "projects": [item.model_dump(mode="json") for item in project_options],
            "model_aliases": [item.model_dump(mode="json") for item in alias_options],
            # revision 只含安全调用投影，不包含模板正文、change summary 或密文。
            "prompt_templates": [
                item.model_dump(mode="json") for item in prompt_template_options
            ],
        }
        revision = hashlib.sha256(
            json.dumps(
                revision_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("ascii")
        ).hexdigest()
        return ChatOptionsResponse(
            options_revision=revision,
            projects=project_options,
            model_aliases=alias_options,
            prompt_templates=prompt_template_options,
            limits=chat_limits(),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def create_chat_session(
    config: AppConfig,
    context: AuthContext,
    payload: CreateChatSessionRequest,
    *,
    request_id: str,
    trace_id: str,
) -> ChatSessionResponse:
    """创建仅属于当前 tenant、用户和登录 Session 的短时 ChatSession。

    options 只是页面提示；本函数重新验证所有资源和唯一 active Project Key，防止配置在
    目录读取后变化或客户端手写 ID 绕过前端禁用状态。
    """

    connection = _connect(config)
    try:
        # 资源 active 状态、唯一 Project Key 和最终 Session 写入必须处于同一快照。
        # 先取得 IMMEDIATE 锁，避免校验通过后资源被禁用或新增第二把 Key，产生失效绑定。
        connection.execute("BEGIN IMMEDIATE")
        # 路由依赖负责在正文解析前拒绝无权请求；这里仍要在写事务内重读当前登录
        # Session owner 和 chat.use，关闭依赖通过后立即撤权却仍创建 Session 的竞态。
        require_transaction_permission(
            connection,
            context,
            "chat.use",
            unavailable_error_code="chat.gateway_unavailable",
        )
        binding = _resolve_binding(
            connection,
            context.user.tenant_id,
            project_id=payload.project_id,
            environment_id=payload.environment_id,
            model_alias_id=payload.model_alias_id,
        )
        now = _utcnow()
        expires_at = now + timedelta(seconds=CHAT_SESSION_TTL_SECONDS)
        chat_session_id = f"chs_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO chat_sessions(
              chat_session_id,tenant_id,owner_user_id,owner_auth_session_id,
              project_id,environment_id,model_alias_id,model_alias,virtual_key_id,
              status,message_sequence,state_revision,expires_at,cleared_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'active',0,1,?,NULL,?,?)
            """,
            (
                chat_session_id,
                context.user.tenant_id,
                context.user.user_id,
                context.session.session_id,
                binding.project_id,
                binding.environment_id,
                binding.model_alias_id,
                binding.model_alias,
                binding.virtual_key_id,
                _to_db_time(expires_at),
                _to_db_time(now),
                _to_db_time(now),
            ),
        )
        connection.commit()
        # 事务提交后才返回 Session，防止页面拿到数据库中并不存在的孤立会话标识。
        return ChatSessionResponse(
            session=ChatSessionInfo(
                chat_session_id=chat_session_id,
                project_id=binding.project_id,
                project_name=binding.project_name,
                environment_id=binding.environment_id,
                environment_name=binding.environment_name,
                model_alias_id=binding.model_alias_id,
                model_alias=binding.model_alias,
                state_revision=1,
                expires_at=expires_at,
            ),
            limits=chat_limits(),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 业务错误必须先回滚显式事务，避免资源校验失败仍长期占用 SQLite 写锁。
    except ApiError:
        # 资源不存在、跨租户或 Key 歧义同样发生在显式事务内，必须立即释放写锁。
        connection.rollback()
        raise
    # 数据库实现错误统一折叠，不能让 SQL、trigger 或文件路径进入公开错误链。
    except sqlite3.Error:
        connection.rollback()
        # SQL、路径和 trigger 文本都不能进入公开错误；资源业务错误已在前面用 ApiError 表达。
        raise ApiError("chat.gateway_unavailable") from None
    finally:
        connection.close()


async def send_chat_message(
    config: AppConfig,
    context: AuthContext,
    chat_session_id: str,
    payload: SendChatMessageRequest,
    *,
    request_id: str,
    trace_id: str,
) -> tuple[ChatTemplateMessageResponse | ChatMessageResponse, WebhookEvent]:
    """准备一次消息、调用 Gateway，并提交成功或失败元数据。

    参数中的 history 只在本调用栈传给内部 Gateway；任何失败分支都只持久化固定错误码。
    返回成功响应和安全 Webhook，路由把事件放入 BackgroundTasks。Gateway 失败在元数据终结后
    抛 ``ChatDispatchError``，从而既保持 HTTP 错误语义，又不丢事件派发机会。
    """

    template_mode = payload.prompt_template_version_id is not None
    normalized_history = _normalize_history(payload.history, template_mode=template_mode)
    template_variables = _normalize_template_variables(payload.template_variables)
    client_message_id = _canonical_client_message_id(payload.client_message_id)
    prepared = _prepare_message(
        config,
        context,
        chat_session_id,
        client_message_id=client_message_id,
        history=normalized_history,
        prompt_template_version_id=payload.prompt_template_version_id,
        template_variables=template_variables,
        request_id=request_id,
        trace_id=trace_id,
    )
    try:
        if template_variables is None:
            # 普通 Chat 保持 PRD-066 的内部调用形状，避免模板扩展迫使既有受控替身或调用方
            # 接收一个没有业务意义的空参数；只有模板 wire 才显式携带变量 map。
            gateway_payload = await _call_gateway(
                config,
                grant=prepared.grant,
                history=normalized_history,
                request_id=prepared.gateway_request_id,
                trace_id=prepared.gateway_trace_id,
            )
        else:
            gateway_payload = await _call_gateway(
                config,
                grant=prepared.grant,
                history=normalized_history,
                template_variables=template_variables,
                request_id=prepared.gateway_request_id,
                trace_id=prepared.gateway_trace_id,
            )
        assistant_content = _assistant_content(gateway_payload)
        rendered_user = _rendered_user_content(gateway_payload, prepared)
        response, terminal = _finalize_success(
            config,
            context,
            prepared,
            assistant_content=assistant_content,
            rendered_user=rendered_user,
            control_request_id=request_id,
            control_trace_id=trace_id,
        )
        return response, _chat_event(terminal)
    # 客户端取消不代表 Gateway 未调用，必须保留同一链路事实并禁止自动重试。
    except asyncio.CancelledError:
        # 客户端断开不能触发自动重试；尽力释放 in-flight 槽位，同时保留同一 request ID 的
        # Gateway RequestLog/Usage 作为可能已计费的权威事实，然后原样传播取消语义。
        _finalize_failure(config, context, prepared, error_code="chat.gateway_unavailable")
        raise
    # 目录化业务错误先提交失败终态，再由路由派发同一 event ID 的安全事件。
    except ApiError as exc:
        terminal = _finalize_failure(config, context, prepared, error_code=exc.code)
        # 内部异常对象不进入响应，只传播冻结 ApiError 和不含正文的事件事实。
        raise ChatDispatchError(exc, _chat_event(terminal)) from None
    # 网络和解析异常可能携带 URL 或正文，必须在终结 Message 前映射固定错误码。
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # 底层网络/解析异常可能含 URL 或响应正文，只映射固定错误，绝不能直接记录或回显。
        error = ApiError("chat.gateway_unavailable")
        if isinstance(exc, (ValueError, TypeError)):
            error = ApiError("chat.response_invalid")
        terminal = _finalize_failure(config, context, prepared, error_code=error.code)
        # 原异常链被切断，避免通用 traceback 收集器记录 Gateway 或 Provider 自由正文。
        raise ChatDispatchError(error, _chat_event(terminal)) from None


def clear_chat_session(
    config: AppConfig,
    context: AuthContext,
    chat_session_id: str,
) -> None:
    """清空并终止当前 owner 的 ChatSession。

    in-flight 消息可能已经被 Gateway 消费并产生费用，因此固定返回 busy；只有没有在途消息时
    才撤销 issued grant 并提交 cleared 终态。重复清空对同一 owner 保持幂等。
    """

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _owned_session(connection, context, chat_session_id)
        # 不存在和跨 tenant/user/login Session 共用 404，避免构造会话存在性探针。
        if row is None:
            raise ApiError("chat.session_not_found")
        # cleared 是幂等终态；重复清空不得再次增加 revision 或伪造第二次生命周期事件。
        if row["status"] == "cleared":
            connection.commit()
            return
        in_flight = connection.execute(
            """
            SELECT 1 FROM chat_messages
            WHERE tenant_id=? AND chat_session_id=? AND status='in_flight'
            LIMIT 1
            """,
            (context.user.tenant_id, chat_session_id),
        ).fetchone()
        # 在途调用可能已经计费，清空不能把浏览器 abort 误当作服务端取消证明。
        if in_flight is not None:
            raise ApiError("chat.session_busy")
        now = _to_db_time(_utcnow())
        connection.execute(
            """
            UPDATE chat_execution_grants
            SET status='revoked',revoked_at=?
            WHERE tenant_id=? AND chat_session_id=? AND status='issued'
            """,
            (now, context.user.tenant_id, chat_session_id),
        )
        if row["status"] == "expired":
            # expired 已是不可回退终态；DELETE 的业务目标只是确保 grant 失效并让页面清除
            # 内存正文，因此撤销 issued grant 后保持 expired，不能伪造 expired -> cleared。
            connection.commit()
            return
        connection.execute(
            """
            UPDATE chat_sessions
            SET status='cleared',cleared_at=?,state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND chat_session_id=? AND status='active'
            """,
            (now, now, context.user.tenant_id, chat_session_id),
        )
        connection.commit()
    # 状态或 owner 业务错误发生于 IMMEDIATE 事务内，必须先释放写锁再传播固定码。
    except ApiError:
        connection.rollback()
        raise
    # 清空事务的 SQL 错误不能暴露 grant、数据库路径或 trigger 细节。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("chat.gateway_unavailable") from None
    finally:
        connection.close()


def dispatch_chat_event_safely(
    config: AppConfig,
    event: WebhookEvent,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """在响应提交后的 BackgroundTask 中机会性派发 Chat 终态事件。

    事件与模型调用都已提交，派发失败不能改变页面结果。日志只含 tenant、稳定 event ID 和
    固定错误码，不记录 Endpoint、payload、异常正文或任何 Chat 内容。
    """

    try:
        dispatch_webhook_event(
            config,
            event.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        logger.warning(
            "chat.webhook_dispatch_failed",
            extra={
                "event": "chat.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "object_id": event.event_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _resolve_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    project_id: str,
    environment_id: str,
    model_alias_id: str,
) -> ChatBinding:
    """按可信 tenant 解析 active 项目、环境、别名和唯一 active Project Key。"""

    project_environment = connection.execute(
        """
        SELECT p.project_id,p.name AS project_name,e.environment_id,e.code,
               e.name AS environment_name
        FROM projects p
        JOIN environments e ON e.tenant_id=p.tenant_id
        WHERE p.tenant_id=? AND p.project_id=? AND p.status='active'
          AND e.environment_id=? AND e.status='active'
        """,
        (tenant_id, project_id, environment_id),
    ).fetchone()
    if project_environment is None:
        # 跨租户 ID 与禁用/不存在资源共享不可用错误，避免形成资源存在性探测器。
        raise ApiError("chat.project_unavailable")
    alias = connection.execute(
        """
        SELECT model_alias_id,alias,description
        FROM model_aliases
        WHERE tenant_id=? AND model_alias_id=? AND status='active'
          AND (environment_id=? OR (environment_id IS NULL AND environment=?))
        """,
        (
            tenant_id,
            model_alias_id,
            environment_id,
            project_environment["code"],
        ),
    ).fetchone()
    # 跨租户、禁用和环境错配统一为模型不可用，不透露别名是否在其他 tenant 存在。
    if alias is None:
        raise ApiError("chat.model_unavailable")
    keys = connection.execute(
        """
        SELECT virtual_key_id
        FROM virtual_keys
        WHERE tenant_id=? AND project_id=? AND status='active'
          AND (environment_id=? OR (environment_id IS NULL AND environment=?))
        ORDER BY virtual_key_id
        LIMIT 2
        """,
        (tenant_id, project_id, environment_id, project_environment["code"]),
    ).fetchall()
    # 零把与多把都失败关闭；绝不能按排序静默选第一把，也不能把数量或 ID 暴露给页面。
    if len(keys) != 1:
        raise ApiError("chat.virtual_key_unavailable")
    return ChatBinding(
        project_id=project_environment["project_id"],
        project_name=project_environment["project_name"],
        environment_id=project_environment["environment_id"],
        environment_code=project_environment["code"],
        environment_name=project_environment["environment_name"],
        model_alias_id=alias["model_alias_id"],
        model_alias=alias["alias"],
        model_alias_name=(alias["description"] or alias["alias"]).strip(),
        virtual_key_id=keys[0]["virtual_key_id"],
    )


def _list_chat_prompt_templates(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
) -> list[ChatPromptTemplateOption]:
    """解密当前 active 版本并生成仅供 ``prompt_template.use`` 的最小调用投影。

    参数：当前 tenant 查询连接、运行配置与已认证权限快照。返回：稳定排序的模板选项。
    正文和 change summary 只在本函数栈内短暂存在，不进入 DTO、revision、日志或数据库。
    """

    rows = connection.execute(
        """
        SELECT template.prompt_template_id,template.name,template.current_version_id,
               version.prompt_template_version_id,version.version_number,
               version.definition_ciphertext,version.project_scope
        FROM prompt_templates template
        JOIN prompt_template_versions version
          ON version.tenant_id=template.tenant_id
         AND version.prompt_template_id=template.prompt_template_id
         AND version.prompt_template_version_id=template.current_version_id
         AND version.state='published'
        WHERE template.tenant_id=? AND template.status='active'
          AND template.current_version_state='published'
        ORDER BY template.name COLLATE NOCASE,template.prompt_template_id
        """,
        (context.user.tenant_id,),
    ).fetchall()
    options: list[ChatPromptTemplateOption] = []
    for row in rows:
        definition = _decrypt_chat_template_definition(config, context.user.tenant_id, row)
        project_ids = [
            str(item["project_id"])
            for item in connection.execute(
                """
                SELECT project_id FROM prompt_template_version_projects
                WHERE tenant_id=? AND prompt_template_id=?
                  AND prompt_template_version_id=?
                ORDER BY project_id
                """,
                (
                    context.user.tenant_id,
                    row["prompt_template_id"],
                    row["prompt_template_version_id"],
                ),
            ).fetchall()
        ]
        model_ids = [
            str(item["model_alias_id"])
            for item in connection.execute(
                """
                SELECT model_alias_id FROM prompt_template_version_models
                WHERE tenant_id=? AND prompt_template_id=?
                  AND prompt_template_version_id=?
                ORDER BY display_order
                """,
                (
                    context.user.tenant_id,
                    row["prompt_template_id"],
                    row["prompt_template_version_id"],
                ),
            ).fetchall()
        ]
        options.append(
            ChatPromptTemplateOption(
                prompt_template_id=str(row["prompt_template_id"]),
                prompt_template_version_id=str(row["prompt_template_version_id"]),
                version_number=int(row["version_number"]),
                name=str(row["name"]),
                variables=[
                    {
                        "name": variable.name,
                        "label": variable.label,
                        "required": variable.required,
                        "max_chars": variable.max_chars,
                    }
                    for variable in definition.variables
                ],
                project_scope=str(row["project_scope"]),
                applicable_project_ids=project_ids,
                recommended_model_alias_ids=model_ids,
            )
        )
    return options


def _resolve_prompt_template_invocation(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    *,
    prompt_template_version_id: str,
    project_id: str,
    variables: dict[str, str],
) -> PromptTemplateInvocationBinding:
    """在消息准备写锁内恢复 exact version、复核 current/Project 并预渲染正文。

    客户端只提交 version ID；template ID 必须由 tenant-qualified 数据库反查。不同 tenant 与
    不存在版本同形 404，禁用、stale 和不适用保持可修正固定码。推荐模型不参与准入或切换。
    """

    version_id = _canonical_prompt_template_version_id(prompt_template_version_id)
    row = connection.execute(
        """
        SELECT template.prompt_template_id,template.status,template.current_version_id,
               template.current_version_state,version.prompt_template_version_id,
               version.version_number,version.state,version.definition_ciphertext,
               version.project_scope
        FROM prompt_template_versions version
        JOIN prompt_templates template
          ON template.tenant_id=version.tenant_id
         AND template.prompt_template_id=version.prompt_template_id
        WHERE version.tenant_id=? AND version.prompt_template_version_id=?
        LIMIT 1
        """,
        (context.user.tenant_id, version_id),
    ).fetchone()
    # version 查询始终带当前 tenant；未命中与跨租户同形 404，禁止资源枚举。
    if row is None:
        raise ApiError("prompt_template.not_found")
    # 禁用模板拒绝新 Invocation，但不删除历史定义或调用事实。
    if row["status"] != "active":
        raise ApiError("prompt_template.disabled")
    if (
        row["state"] != "published"
        or row["current_version_state"] != "published"
        or row["current_version_id"] != row["prompt_template_version_id"]
    ):
        # 调用只接受 active current published head，迟到客户端不能继续使用旧版本。
        raise ApiError("prompt_template.version_conflict")
    if row["project_scope"] == "selected":
        applicable = connection.execute(
            """
            SELECT 1 FROM prompt_template_version_projects
            WHERE tenant_id=? AND prompt_template_id=?
              AND prompt_template_version_id=? AND project_id=?
            LIMIT 1
            """,
            (
                context.user.tenant_id,
                row["prompt_template_id"],
                row["prompt_template_version_id"],
                project_id,
            ),
        ).fetchone()
        if applicable is None:
            # selected scope 未绑定当前 Project 时明确可修正失败，不借推荐模型改变适用性。
            raise ApiError("prompt_template.not_applicable")
    definition = _decrypt_chat_template_definition(config, context.user.tenant_id, row)
    try:
        rendered = render_prompt(definition, variables)
    # renderer 固定错误不携带变量名称和值，可安全映射到模板公开域供用户修正。
    except PromptTemplateContractError as exc:
        raise ApiError(exc.code) from None
    # 防御性拒绝全空白渲染，不能把无有效 user 内容的调用交给治理链。
    if not rendered.strip():
        raise ApiError("prompt_template.variables_invalid")
    return PromptTemplateInvocationBinding(
        prompt_template_id=str(row["prompt_template_id"]),
        prompt_template_version_id=str(row["prompt_template_version_id"]),
        version_number=int(row["version_number"]),
        rendered_user=rendered,
    )


def _decrypt_chat_template_definition(
    config: AppConfig,
    tenant_id: str,
    row: sqlite3.Row,
):
    """使用独立密钥和可信四维 owner 解密定义，并折叠密码学故障细节。

    返回共享不可变 definition，仅供 options 投影或一次预渲染。密钥缺失使用 503，认证
    密文/schema 损坏使用固定 500；异常链不会持有密文、正文或变量。
    """

    owner = PromptTemplateOwner(
        tenant_id=tenant_id,
        prompt_template_id=str(row["prompt_template_id"]),
        prompt_template_version_id=str(row["prompt_template_version_id"]),
        version_number=int(row["version_number"]),
    )
    try:
        return decrypt_definition(
            row["definition_ciphertext"],
            secret=config.prompt_template_encryption_secret,
            owner=owner,
        )
    # 独立根密钥缺失属于部署暂不可用，不能回退 Auth/Provider 密钥或伪装成资源不存在。
    except PromptTemplateSecretUnavailable:
        raise ApiError("prompt_template.unavailable") from None
    # AAD/tag/schema 损坏统一为定义不可用，异常链不得携带密文或密码学细节。
    except PromptTemplateDefinitionUnavailable:
        raise ApiError("prompt_template.definition_unavailable") from None


def _canonical_prompt_template_version_id(value: object) -> str:
    """校验 ``ptv_`` 加 32 位小写 hex；非法与未知版本保持同形 404。"""

    normalized = value if type(value) is str else ""
    suffix = normalized[4:] if normalized.startswith("ptv_") else ""
    # 非规范 ID 与未知/跨租户版本保持同形 404，避免形成格式和存在性探针。
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ApiError("prompt_template.not_found")
    return normalized


def _validate_template_final_history(
    history: list[dict[str, str]],
    rendered_user: str,
) -> None:
    """预演“完整历史对 + rendered user”的最终 20 条/64 KiB 约束。"""

    # 模板追加最终 user 后仍必须满足 PRD-066 消息条数上限，不能只检查客户端历史片段。
    if len(history) + 1 > MAX_HISTORY_MESSAGES:
        raise ApiError("prompt_template.variables_invalid")
    try:
        total_bytes = sum(len(item["content"].encode("utf-8")) for item in history)
        total_bytes += len(rendered_user.encode("utf-8"))
    # history 或渲染正文若不能稳定编码，切断异常链以免客户内容进入 traceback。
    except (KeyError, TypeError, UnicodeEncodeError):
        raise ApiError("prompt_template.variables_invalid") from None
    # 变量展开后的最终字节总量也必须受限，防止合法小输入组合放大内部 wire。
    if total_bytes > MAX_HISTORY_BYTES:
        raise ApiError("prompt_template.variables_invalid")


def _normalize_history(
    history: list[ChatHistoryMessage],
    *,
    template_mode: bool,
) -> list[dict[str, str]]:
    """按普通/模板模式验证纯文本 history 的顺序、字符数和 UTF-8 总量。

    返回的新字典只交给内部 Gateway。函数不生成正文摘要或 hash，避免所谓脱敏指纹成为长期
    可关联敏感数据。模板模式允许空历史或完整消息对，最终 user 会在两端从定义渲染。
    """

    # 普通模式仍要求本轮 user；模板模式需要给渲染 user 留出一条最终消息容量。
    if (
        len(history) > MAX_HISTORY_MESSAGES
        or (not template_mode and not history)
        or (template_mode and len(history) >= MAX_HISTORY_MESSAGES)
    ):
        # 两种模式分别冻结最小/最大条数，拒绝自动补消息或截断敏感历史。
        raise ApiError("chat.request_invalid")
    normalized: list[dict[str, str]] = []
    total_bytes = 0
    for index, item in enumerate(history):
        expected_role = "user" if index % 2 == 0 else "assistant"
        content = item.content.strip()
        # 角色错序、空白和单条超限统一拒绝，不能把正文值或具体位置写入错误。
        if item.role != expected_role or not content or len(content) > MAX_MESSAGE_CHARS:
            raise ApiError("chat.request_invalid")
        try:
            total_bytes += len(content.encode("utf-8"))
        # 非法代理字符无法稳定进入 Gateway/DLP/Provider，且不能保留带正文的异常链。
        except UnicodeEncodeError:
            raise ApiError("chat.request_invalid") from None
        # 总量按 UTF-8 字节计算，防止多字节文本绕过字符数边界扩大内部请求。
        if total_bytes > MAX_HISTORY_BYTES:
            raise ApiError("chat.request_invalid")
        normalized.append({"role": item.role, "content": content})
    # 普通模式以本轮 user 结束；模板模式只能提交完整 user/assistant 对，最终 user 后端追加。
    if (not template_mode and normalized[-1]["role"] != "user") or (
        template_mode and len(normalized) % 2 != 0
    ):
        # 普通模式须以 user 结束，模板模式只接收完整消息对，禁止静默修补角色顺序。
        raise ApiError("chat.request_invalid")
    return normalized


def _normalize_template_variables(
    variables: dict[str, str] | None,
) -> dict[str, str] | None:
    """复制并有界校验模板变量 wire，具体 exact-set/必填约束留给解密定义。

    参数：严格 DTO 中的可选变量 map。返回：只在本次调用栈存活的新字典或 None。
    键和值不得进入异常、日志或持久化；总 UTF-8 上限可避免持有写锁后才处理无界输入。
    """

    if variables is None:
        return None
    # 变量只接受小型普通 dict，拒绝嵌套/宽容 Mapping 在持锁阶段放大资源开销。
    if type(variables) is not dict or len(variables) > 32:
        raise ApiError("prompt_template.variables_invalid")
    normalized: dict[str, str] = {}
    total_bytes = 0
    for name, value in variables.items():
        if (
            type(name) is not str
            or not 1 <= len(name) <= 64
            or not name.isascii()
            or type(value) is not str
            or len(value) > MAX_MESSAGE_CHARS
        ):
            # 名称/值基础形状失败只返回固定错误，不指出具体变量或客户输入。
            raise ApiError("prompt_template.variables_invalid")
        try:
            total_bytes += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        # 非法 Unicode 不能进入 renderer/HMAC/Gateway wire，异常链也不得携带输入值。
        except UnicodeEncodeError:
            raise ApiError("prompt_template.variables_invalid") from None
        # 多变量总量使用与 Chat history 相同硬边界，不能靠拆分值绕过上限。
        if total_bytes > MAX_HISTORY_BYTES:
            raise ApiError("prompt_template.variables_invalid")
        normalized[name] = value
    return normalized


def _canonical_client_message_id(value: str) -> str:
    """要求幂等 ID 为无歧义的规范 UUID 文本。"""

    try:
        parsed = UUID(value)
    # UUID 解析异常不保留客户端原值，避免幂等标识进入通用 traceback。
    except (TypeError, ValueError, AttributeError):
        raise ApiError("chat.request_invalid") from None
    # 只接受 canonical 小写连字符文本，避免同一 UUID 存在多种幂等键表示。
    if str(parsed) != value:
        raise ApiError("chat.request_invalid")
    return value


def _prepare_message(
    config: AppConfig,
    context: AuthContext,
    chat_session_id: str,
    *,
    client_message_id: str,
    history: list[dict[str, str]] | None = None,
    prompt_template_version_id: str | None = None,
    template_variables: dict[str, str] | None = None,
    request_id: str,
    trace_id: str,
) -> PreparedChatMessage:
    """在单个 IMMEDIATE 事务中占用会话，并创建 Message、grant 与可选 Invocation。

    模板模式在写锁内再次复核 ``prompt_template.use``、current head、Project 和密文定义，
    先完成一次内存渲染与最终 history 上限校验，再把不含正文的 Invocation 同事务落库。
    """

    # 路径标识先执行有界检查，非法值与跨 owner 会话共用不可枚举的 404。
    if not chat_session_id or len(chat_session_id) > 120:
        raise ApiError("chat.session_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 消息准备会创建可触发真实成本的单次 grant，必须先在同一事务快照中复核
        # tenant/user/login Session owner 和实时 chat.use，再读取 ChatSession 或生成元数据。
        live_context = require_transaction_permission(
            connection,
            context,
            "chat.use",
            unavailable_error_code="chat.gateway_unavailable",
        )
        session = _owned_session(connection, context, chat_session_id)
        # tenant、用户或登录 Session 任一不匹配都返回空，不能泄露真实 owner。
        if session is None:
            raise ApiError("chat.session_not_found")
        # cleared 是不可恢复终态，禁止生成新 Message 或 execution grant。
        if session["status"] == "cleared":
            raise ApiError("chat.session_cleared")
        now = _utcnow()
        # 显式或自然过期都原子冻结为 expired，再返回固定冲突码阻止重放。
        if session["status"] == "expired" or _parse_db_time(session["expires_at"]) <= now:
            connection.execute(
                """
                UPDATE chat_sessions
                SET status='expired',state_revision=state_revision+1,updated_at=?
                WHERE tenant_id=? AND chat_session_id=? AND status='active'
                """,
                (_to_db_time(now), context.user.tenant_id, chat_session_id),
            )
            connection.commit()
            # 过期终态已持久提交，返回固定 409 后客户端只能新建会话，不能复用旧 grant。
            raise ApiError("chat.session_expired")
        duplicate = connection.execute(
            """
            SELECT 1 FROM chat_messages
            WHERE tenant_id=? AND chat_session_id=? AND client_message_id=?
            LIMIT 1
            """,
            (context.user.tenant_id, chat_session_id, client_message_id),
        ).fetchone()
        # client_message_id 一经占用永不复用，防止页面重试造成二次 Provider 计费。
        if duplicate is not None:
            raise ApiError("chat.message_duplicate")
        in_flight = connection.execute(
            """
            SELECT 1 FROM chat_messages
            WHERE tenant_id=? AND chat_session_id=? AND status='in_flight'
            LIMIT 1
            """,
            (context.user.tenant_id, chat_session_id),
        ).fetchone()
        # 每个 Session 只允许一个在途 Message，数据库 partial unique index 同步兜底。
        if in_flight is not None:
            raise ApiError("chat.session_busy")
        binding = _resolve_binding(
            connection,
            context.user.tenant_id,
            project_id=session["project_id"],
            environment_id=session["environment_id"],
            model_alias_id=session["model_alias_id"],
        )
        if (
            binding.virtual_key_id != session["virtual_key_id"]
            or binding.model_alias != session["model_alias"]
        ):
            # 新增第二把 Key、换绑或别名漂移都使旧 Session 失效，不能把它静默迁移到新资源。
            raise ApiError("chat.virtual_key_unavailable")
        template_binding: PromptTemplateInvocationBinding | None = None
        if prompt_template_version_id is not None:
            if template_variables is None:
                # 模板版本和变量必须精确成对，不能猜测为空对象或降级为普通 Chat。
                raise ApiError("chat.request_invalid")
            # read/write 不能替代 use；二次权限复核与模板 current/project 校验共享同一写锁。
            live_context = require_transaction_permission(
                connection,
                live_context,
                "prompt_template.use",
                unavailable_error_code="chat.gateway_unavailable",
            )
            template_binding = _resolve_prompt_template_invocation(
                connection,
                config,
                live_context,
                prompt_template_version_id=prompt_template_version_id,
                project_id=binding.project_id,
                variables=template_variables,
            )
            _validate_template_final_history(history or [], template_binding.rendered_user)
        elif template_variables is not None:
            # 单独提交变量没有可信 definition owner，必须在创建 Invocation 前失败关闭。
            raise ApiError("chat.request_invalid")
        chat_message_id = f"chm_{uuid4().hex}"
        grant_id = f"chg_{uuid4().hex}"
        invocation_id = f"ptinv_{uuid4().hex}" if template_binding is not None else None
        grant = token_urlsafe(48)
        grant_hash = hashlib.sha256(grant.encode("ascii")).hexdigest()
        sequence = int(session["message_sequence"]) + 1
        now_text = _to_db_time(now)
        # 先用 Session revision 做 CAS 并占用 message_sequence，再创建 Message 和 grant。
        # 0058 的数据库门禁要求 Message 顺序必须与父 Session 当前顺序一致；三步位于同一
        # IMMEDIATE 事务中，后续任一步失败都会整体回滚，不会留下只推进序号的半成品。
        connection.execute(
            """
            UPDATE chat_sessions
            SET message_sequence=?,state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND chat_session_id=? AND state_revision=?
            """,
            (
                sequence,
                now_text,
                context.user.tenant_id,
                chat_session_id,
                session["state_revision"],
            ),
        )
        # CAS 必须精确推进一行，未命中表示并发请求已先修改 Session revision。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("chat.session_busy")
        connection.execute(
            """
            INSERT INTO chat_messages(
              chat_message_id,tenant_id,chat_session_id,owner_user_id,client_message_id,
              message_sequence,gateway_request_id,gateway_trace_id,status,
              input_tokens,output_tokens,total_tokens,cache_tokens,cost_cny_micros,
              policy_decision,policy_reason_code,fallback_triggered,cache_status,error_code,
              webhook_event_id,started_at,completed_at
            ) VALUES(
              ?,?,?,?,?,?,?,?,'in_flight',
              NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?, ?,NULL
            )
            """,
            (
                chat_message_id,
                context.user.tenant_id,
                chat_session_id,
                context.user.user_id,
                client_message_id,
                sequence,
                request_id,
                trace_id,
                f"evt_{chat_message_id}",
                now_text,
            ),
        )
        # Message INSERT 也必须精确一行，trigger/竞争失败不能留下仅推进 Session 的半成品。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("chat.session_busy")
        connection.execute(
            """
            INSERT INTO chat_execution_grants(
              chat_execution_grant_id,tenant_id,grant_hash,chat_session_id,chat_message_id,
              owner_user_id,owner_auth_session_id,project_id,environment_id,model_alias_id,
              virtual_key_id,gateway_request_id,gateway_trace_id,status,expires_at,
              issued_at,consumed_at,revoked_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?,NULL,NULL)
            """,
            (
                grant_id,
                context.user.tenant_id,
                grant_hash,
                chat_session_id,
                chat_message_id,
                context.user.user_id,
                context.session.session_id,
                binding.project_id,
                binding.environment_id,
                binding.model_alias_id,
                binding.virtual_key_id,
                request_id,
                trace_id,
                _to_db_time(now + timedelta(seconds=CHAT_GRANT_TTL_SECONDS)),
                now_text,
            ),
        )
        if template_binding is not None and invocation_id is not None:
            # Invocation 只冻结版本和调用 owner；变量、history、渲染正文及响应均不得落库。
            connection.execute(
                """
                INSERT INTO chat_prompt_template_invocations(
                  prompt_template_invocation_id,tenant_id,prompt_template_id,
                  prompt_template_version_id,version_number,chat_session_id,chat_message_id,
                  actor_user_id,actor_auth_session_id,gateway_request_id,gateway_trace_id,
                  status,failure_code,created_at,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'in_flight',NULL,?,NULL)
                """,
                (
                    invocation_id,
                    context.user.tenant_id,
                    template_binding.prompt_template_id,
                    template_binding.prompt_template_version_id,
                    template_binding.version_number,
                    chat_session_id,
                    chat_message_id,
                    context.user.user_id,
                    context.session.session_id,
                    request_id,
                    trace_id,
                    now_text,
                ),
            )
        connection.commit()
        return PreparedChatMessage(
            tenant_id=context.user.tenant_id,
            chat_session_id=chat_session_id,
            chat_message_id=chat_message_id,
            client_message_id=client_message_id,
            gateway_request_id=request_id,
            gateway_trace_id=trace_id,
            grant=grant,
            prompt_template_invocation_id=invocation_id,
            expected_rendered_user=(
                template_binding.rendered_user if template_binding is not None else None
            ),
        )
    # 业务错误回滚尚未提交的序号、Message 和 grant，避免遗留半成品状态。
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    # 唯一键或 trigger 竞争统一视为 Session busy，不向客户端暴露数据库结构。
    except sqlite3.IntegrityError:
        connection.rollback()
        # 唯一幂等键和 partial in-flight 竞争统一映射，不把 trigger 名或 SQL 暴露给调用方。
        raise ApiError("chat.session_busy") from None
    # 其他 SQL 故障不能带出数据库路径或语句，统一映射为暂时不可用。
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("chat.gateway_unavailable") from None
    finally:
        connection.close()


async def _call_gateway(
    config: AppConfig,
    *,
    grant: str,
    history: list[dict[str, str]],
    request_id: str,
    trace_id: str,
    template_variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """使用独立内部 Token 和单次 grant 调用 Gateway 普通或模板 Chat 入口。

    不启用重定向或系统代理，避免内部 Token/grant 被转发到非预期目标。错误响应只读取统一
    ``code``，不会把 Provider 或 Gateway 自由正文带入 Control 错误。
    """

    internal_token = getattr(config, "internal_chat_token", None)
    # 内部服务身份缺失时失败关闭，绝不能降级为公开 Virtual Key 或直连 Provider。
    if not internal_token:
        raise ApiError("chat.gateway_unavailable")
    base_url = str(config.gateway_api_base_url or "").rstrip("/")
    parsed = urlsplit(base_url)
    # 运行时再次验证 scheme/host，防止绕过启动配置校验构造任意目标。
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApiError("chat.gateway_unavailable")
    url = f"{base_url}/internal/chat/completions"
    headers = {
        "X-MetaHub-Internal-Chat-Token": internal_token,
        "X-Request-ID": request_id,
        "X-Trace-ID": trace_id,
        "Content-Type": "application/json",
    }
    wire: dict[str, object] = {"grant": grant, "history": history}
    if template_variables is not None:
        # version/template owner 只存在于数据库 Invocation；内部 wire 仅携带当前栈变量值。
        wire["variables"] = template_variables
    async with httpx.AsyncClient(
        timeout=GATEWAY_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        response = await client.post(url, headers=headers, json=wire)
    try:
        body = response.json()
    # JSONDecodeError 保存完整响应文档；切断异常链可避免 assistant 正文进入 traceback。
    except (ValueError, TypeError):
        raise ApiError("chat.response_invalid") from None
    if response.status_code >= 400:
        code = body.get("code") if isinstance(body, dict) else None
        # 浏览器只能看到本 PRD 冻结的 Chat 错误域。内部 Token、普通 Gateway、Provider
        # 或未知错误即使已注册也不能穿透 Control 边界，避免暴露服务身份和内部拓扑。
        public_code = (
            code
            if isinstance(code, str) and code.startswith("chat.") and code in ERROR_SPECS
            else "chat.gateway_unavailable"
        )
        # 只传播冻结 Chat 错误域，内部鉴权、Provider 和未知错误全部折叠为不可用。
        raise ApiError(public_code)
    # 成功状态也必须是 JSON object，禁止列表或标量绕过后续响应白名单。
    if not isinstance(body, dict):
        raise ApiError("chat.response_invalid")
    return body


def _assistant_content(payload: dict[str, Any]) -> str:
    """从 OpenAI-compatible 成功响应提取唯一支持的 assistant 纯文本。"""

    choices = payload.get("choices")
    # choices 必须至少有一个 object，未知成功形状不能被猜测为可展示回复。
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ApiError("chat.response_invalid")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    # 工具、多模态、空白或超大正文都不属于 PAGE-093，且不能被持久化后再转换。
    if (
        not isinstance(content, str)
        or not content
        or len(content.encode("utf-8")) > MAX_HISTORY_BYTES
    ):
        # 工具调用、多模态 part 或异常大响应不属于本期演示入口，不能猜测转换后持久化。
        raise ApiError("chat.response_invalid")
    return content


def _rendered_user_content(
    payload: dict[str, Any],
    prepared: PreparedChatMessage,
) -> str | None:
    """校验 Gateway 模板成功正文与 Control 预渲染完全一致，并保持普通响应 exact。

    参数：Gateway 成功对象和当前调用的 Prepared owner。返回：模板 rendered user 或 None。
    比较只在内存中对 UTF-8 SHA-256 使用常量时间，不持久化摘要，也不把差异正文放入错误。
    """

    expected = prepared.expected_rendered_user
    if expected is None:
        if "rendered_user" in payload:
            # 普通 Chat 响应不得被 Gateway 扩展模板字段，保持 PRD-066 wire 兼容。
            raise ApiError("chat.response_invalid")
        return None
    rendered = payload.get("rendered_user")
    # 模板成功必须返回非空 rendered user，缺失时不能用 Control 预渲染值伪造 Gateway 结果。
    if type(rendered) is not str or not rendered.strip():
        raise ApiError("chat.response_invalid")
    try:
        expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
        actual_digest = hashlib.sha256(rendered.encode("utf-8")).digest()
    # 任一正文无法稳定 UTF-8 编码时统一失败，且切断可能携带正文的异常链。
    except UnicodeEncodeError:
        raise ApiError("chat.response_invalid") from None
    # 两端 definition/AAD/variables 解释必须完全一致，不把任一正文或摘要写入诊断。
    if not hmac.compare_digest(expected_digest, actual_digest):
        raise ApiError("chat.response_invalid")
    return rendered


def _cache_status(*, cache_tokens: int, final_selection_exists: bool) -> str:
    """仅按正式 Usage 和最终成功路由的 Cache selection 推导页面三态。

    真实 cache token 大于零始终表示命中；零 Token 只有在最终成功 attempt 存在 selection 时
    才是 miss，否则表示未配置。预估命中和失败 attempt 都不能改变该结论。
    """

    # 正式账本出现缓存 Token 是最强事实，即使 selection 记录缺失也不能降级为未配置。
    if cache_tokens > 0:
        return "hit"
    if final_selection_exists:
        return "miss"
    return "not_configured"


def _finalize_success(
    config: AppConfig,
    context: AuthContext,
    prepared: PreparedChatMessage,
    *,
    assistant_content: str,
    rendered_user: str | None,
    control_request_id: str,
    control_trace_id: str,
) -> tuple[ChatTemplateMessageResponse | ChatMessageResponse, ChatTerminalFact]:
    """从 Gateway 持久事实汇总治理摘要并原子提交 Message 成功终态。"""

    # Invocation 与 rendered user 必须同时存在或同时缺失，防止模板事实和公开响应分叉。
    if (prepared.prompt_template_invocation_id is None) != (rendered_user is None):
        raise ApiError("chat.response_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _terminal_message_row(connection, context, prepared)
        request_log = connection.execute(
            """
            SELECT status,policy_decision,policy_reason_code,fallback_triggered,
                   trace_id,final_routing_attempt_id
            FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (prepared.tenant_id, prepared.gateway_request_id),
        ).fetchone()
        if (
            request_log is None
            or request_log["status"] != "success"
            or request_log["trace_id"] != prepared.gateway_trace_id
        ):
            # RequestLog 缺失、非成功或 trace 错配都不能伪造为可计费的 Chat 成功。
            raise ApiError("chat.response_invalid")
        usage_rows = connection.execute(
            """
            SELECT input_tokens,output_tokens,total_tokens,cache_tokens,cost_cny
            FROM usage_ledger
            WHERE tenant_id=? AND request_id=?
            """,
            (prepared.tenant_id, prepared.gateway_request_id),
        ).fetchall()
        # 成功摘要只能来自正式 UsageLedger；RequestLog 的 Token/estimated_cost 是展示快照，
        # 不能在账本缺失时伪造为已完成计费事实，否则页面和 Webhook 会掩盖账务缺口。
        if not usage_rows:
            raise ApiError("chat.response_invalid")
        input_tokens = sum(int(item["input_tokens"] or 0) for item in usage_rows)
        output_tokens = sum(int(item["output_tokens"] or 0) for item in usage_rows)
        total_tokens = sum(int(item["total_tokens"] or 0) for item in usage_rows)
        cache_tokens = sum(int(item["cache_tokens"] or 0) for item in usage_rows)
        cost_cny = sum((Decimal(str(item["cost_cny"] or "0")) for item in usage_rows), Decimal("0"))
        cost_micros = int(
            (cost_cny * Decimal("1000000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        final_selection = connection.execute(
            """
            SELECT 1 FROM cache_affinity_selections
            WHERE tenant_id=? AND request_id=? AND routing_attempt_id=?
              AND outcome_status='succeeded'
            LIMIT 1
            """,
            (
                prepared.tenant_id,
                prepared.gateway_request_id,
                request_log["final_routing_attempt_id"],
            ),
        ).fetchone()
        # estimated_hit 只是路由前预测，不能当作真实缓存命中。真实 Token 优先；未命中时
        # 只查看最终成功 attempt，避免主尝试失败、Fallback 成功时误采信旧 selection。
        cache_status = _cache_status(
            cache_tokens=cache_tokens,
            final_selection_exists=final_selection is not None,
        )
        completed_at = _to_db_time(_utcnow())
        policy_decision = request_log["policy_decision"] or "allow"
        connection.execute(
            """
            UPDATE chat_messages
            SET status='succeeded',input_tokens=?,output_tokens=?,total_tokens=?,cache_tokens=?,
                cost_cny_micros=?,policy_decision=?,policy_reason_code=?,fallback_triggered=?,
                cache_status=?,error_code=NULL,completed_at=?
            WHERE tenant_id=? AND chat_session_id=? AND chat_message_id=? AND status='in_flight'
            """,
            (
                input_tokens,
                output_tokens,
                total_tokens,
                cache_tokens,
                cost_micros,
                policy_decision,
                request_log["policy_reason_code"],
                int(request_log["fallback_triggered"] or 0),
                cache_status,
                completed_at,
                prepared.tenant_id,
                prepared.chat_session_id,
                prepared.chat_message_id,
            ),
        )
        # 终结 CAS 必须精确命中在途 Message，迟到或重复响应不能覆盖既有终态。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("chat.session_busy")
        _finalize_prompt_template_invocation(
            connection,
            prepared,
            status="succeeded",
            error_code=None,
            completed_at=completed_at,
        )
        connection.execute(
            """
            UPDATE chat_sessions
            SET state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND chat_session_id=?
            """,
            (completed_at, prepared.tenant_id, prepared.chat_session_id),
        )
        revision_row = connection.execute(
            """
            SELECT state_revision FROM chat_sessions
            WHERE tenant_id=? AND chat_session_id=?
            """,
            (prepared.tenant_id, prepared.chat_session_id),
        ).fetchone()
        connection.commit()
        terminal = _terminal_fact(
            row,
            status="succeeded",
            completed_at=completed_at,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_tokens=cache_tokens,
            cost_cny_micros=cost_micros,
            policy_decision=policy_decision,
            policy_reason_code=request_log["policy_reason_code"],
            fallback_triggered=bool(request_log["fallback_triggered"]),
            cache_status=cache_status,
            error_code=None,
        )
        response_fields: dict[str, object] = {
            "chat_session_id": prepared.chat_session_id,
            "chat_message_id": prepared.chat_message_id,
            "client_message_id": prepared.client_message_id,
            "assistant": ChatAssistantMessage(content=assistant_content),
            "execution": ChatExecutionSummary(
                request_id=prepared.gateway_request_id,
                trace_id=prepared.gateway_trace_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_tokens=cache_tokens,
                cost_cny=format(Decimal(cost_micros) / Decimal("1000000"), ".6f"),
                policy=ChatPolicySummary(
                    decision=policy_decision,
                    reason_code=request_log["policy_reason_code"],
                ),
                fallback_triggered=bool(request_log["fallback_triggered"]),
                cache_status=cache_status,
            ),
            "session_revision": int(revision_row["state_revision"]),
            "request_id": control_request_id,
            "trace_id": control_trace_id,
        }
        response: ChatTemplateMessageResponse | ChatMessageResponse
        if rendered_user is not None:
            response = ChatTemplateMessageResponse(
                **response_fields,
                rendered_user=rendered_user,
            )
        else:
            response = ChatMessageResponse(**response_fields)
        return response, terminal
    # 业务一致性错误回滚 Chat 终态写入，但保留 Gateway 已提交的 RequestLog/Usage。
    except ApiError:
        connection.rollback()
        raise
    # SQL 和金额转换异常统一脱敏，绝不能自动重放已经可能计费的 Provider 请求。
    except (sqlite3.Error, ArithmeticError):
        connection.rollback()
        raise ApiError("chat.gateway_unavailable") from None
    finally:
        connection.close()


def _finalize_failure(
    config: AppConfig,
    context: AuthContext,
    prepared: PreparedChatMessage,
    *,
    error_code: str,
) -> ChatTerminalFact:
    """提交固定错误码失败终态并释放 Session 的 partial in-flight 槽位。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _terminal_message_row(connection, context, prepared)
        completed_at = _to_db_time(_utcnow())
        connection.execute(
            """
            UPDATE chat_messages
            SET status='failed',error_code=?,completed_at=?
            WHERE tenant_id=? AND chat_session_id=? AND chat_message_id=? AND status='in_flight'
            """,
            (
                error_code,
                completed_at,
                prepared.tenant_id,
                prepared.chat_session_id,
                prepared.chat_message_id,
            ),
        )
        # Message 与模板 Invocation 必须在同一事务进入失败终态，
        # 避免模板审计事实长期停留在 in_flight。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("chat.session_busy")
        _finalize_prompt_template_invocation(
            connection,
            prepared,
            status="failed",
            error_code=error_code,
            completed_at=completed_at,
        )
        connection.execute(
            """
            UPDATE chat_execution_grants
            SET status='revoked',revoked_at=?
            WHERE tenant_id=? AND chat_message_id=? AND status='issued'
            """,
            (completed_at, prepared.tenant_id, prepared.chat_message_id),
        )
        connection.execute(
            """
            UPDATE chat_sessions
            SET state_revision=state_revision+1,updated_at=?
            WHERE tenant_id=? AND chat_session_id=?
            """,
            (completed_at, prepared.tenant_id, prepared.chat_session_id),
        )
        connection.commit()
        return _terminal_fact(
            row,
            status="failed",
            completed_at=completed_at,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cache_tokens=None,
            cost_cny_micros=None,
            policy_decision=None,
            policy_reason_code=None,
            fallback_triggered=None,
            cache_status=None,
            error_code=error_code,
        )
    # 失败终结写入异常不能触发 Provider 重试，只返回固定可追踪不可用错误。
    except sqlite3.Error:
        connection.rollback()
        # 终结失败不能重放 Provider；对外仍返回统一可追踪错误，RequestLog/Usage 保留原事实。
        raise ApiError("chat.gateway_unavailable") from None
    finally:
        connection.close()


def _finalize_prompt_template_invocation(
    connection: sqlite3.Connection,
    prepared: PreparedChatMessage,
    *,
    status: Literal["succeeded", "failed"],
    error_code: str | None,
    completed_at: str,
) -> None:
    """与 ChatMessage 同事务把可选 Invocation 从 in_flight 推进到唯一终态。

    参数：当前写事务、Prepared owner、目标终态、固定错误码和完成时间。返回值：无。
    普通 Message 若意外存在 Invocation 也失败关闭，防止模板调用事实与公开响应分叉。
    """

    invocation_id = prepared.prompt_template_invocation_id
    if invocation_id is None:
        unexpected = connection.execute(
            """
            SELECT 1 FROM chat_prompt_template_invocations
            WHERE tenant_id=? AND chat_session_id=? AND chat_message_id=?
            LIMIT 1
            """,
            (
                prepared.tenant_id,
                prepared.chat_session_id,
                prepared.chat_message_id,
            ),
        ).fetchone()
        if unexpected is not None:
            # 普通 Message 意外关联 Invocation 表示 owner 图损坏，不能静默忽略模板事实。
            raise ApiError("chat.response_invalid")
        return
    # 成功只能配空错误码、失败必须配固定错误码，禁止写入相互矛盾的终态。
    if (status == "succeeded") != (error_code is None):
        raise ApiError("chat.response_invalid")
    connection.execute(
        """
        UPDATE chat_prompt_template_invocations
        SET status=?,failure_code=?,completed_at=?
        WHERE tenant_id=? AND prompt_template_invocation_id=?
          AND chat_session_id=? AND chat_message_id=?
          AND gateway_request_id=? AND gateway_trace_id=? AND status='in_flight'
        """,
        (
            status,
            error_code,
            completed_at,
            prepared.tenant_id,
            invocation_id,
            prepared.chat_session_id,
            prepared.chat_message_id,
            prepared.gateway_request_id,
            prepared.gateway_trace_id,
        ),
    )
    # Invocation CAS 必须精确命中同 owner 的 in_flight 行，迟到响应不能覆盖既有终态。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("chat.session_busy")


def _terminal_message_row(
    connection: sqlite3.Connection,
    context: AuthContext,
    prepared: PreparedChatMessage,
) -> sqlite3.Row:
    """按 tenant、owner、Session、Message 和 request ID 加载唯一在途元数据并阻断迟到响应。"""

    row = connection.execute(
        """
        SELECT m.*,s.project_id,s.environment_id,s.model_alias_id
        FROM chat_messages m
        JOIN chat_sessions s
          ON s.tenant_id=m.tenant_id AND s.chat_session_id=m.chat_session_id
        WHERE m.tenant_id=? AND m.chat_session_id=? AND m.chat_message_id=?
          AND m.owner_user_id=? AND m.gateway_request_id=? AND m.status='in_flight'
          AND s.owner_auth_session_id=?
        """,
        (
            prepared.tenant_id,
            prepared.chat_session_id,
            prepared.chat_message_id,
            context.user.user_id,
            prepared.gateway_request_id,
            context.session.session_id,
        ),
    ).fetchone()
    # tenant、用户、登录 Session、request 或状态任一漂移都视为并发终态冲突。
    if row is None:
        raise ApiError("chat.session_busy")
    return row


def _terminal_fact(row: sqlite3.Row, **values: Any) -> ChatTerminalFact:
    """把 tenant-qualified Message 行与终结值组合成安全事件事实。"""

    return ChatTerminalFact(
        tenant_id=row["tenant_id"],
        actor_user_id=row["owner_user_id"],
        chat_session_id=row["chat_session_id"],
        chat_message_id=row["chat_message_id"],
        project_id=row["project_id"],
        environment_id=row["environment_id"],
        model_alias_id=row["model_alias_id"],
        gateway_request_id=row["gateway_request_id"],
        gateway_trace_id=row["gateway_trace_id"],
        webhook_event_id=row["webhook_event_id"],
        **values,
    )


def _chat_event(fact: ChatTerminalFact) -> WebhookEvent:
    """从已提交终态构造 ``chat.message_sent`` 精确安全信封。"""

    payload = {
        "schema_version": 1,
        "chat_session_id": fact.chat_session_id,
        "chat_message_id": fact.chat_message_id,
        "project_id": fact.project_id,
        "environment_id": fact.environment_id,
        "model_alias_id": fact.model_alias_id,
        "gateway_request_id": fact.gateway_request_id,
        "status": fact.status,
        "input_tokens": fact.input_tokens,
        "output_tokens": fact.output_tokens,
        "total_tokens": fact.total_tokens,
        "cache_tokens": fact.cache_tokens,
        "cost_cny_micros": fact.cost_cny_micros,
        "policy_decision": fact.policy_decision,
        "policy_reason_code": fact.policy_reason_code,
        "fallback_triggered": fact.fallback_triggered,
        "cache_status": fact.cache_status,
        "error_code": fact.error_code,
        "completed_at": fact.completed_at,
    }
    return build_webhook_event(
        tenant_id=fact.tenant_id,
        event_type="chat.message_sent",
        payload=payload,
        actor_id=fact.actor_user_id,
        object_id=fact.chat_message_id,
        trace_id=fact.gateway_trace_id,
        source_request_id=fact.gateway_request_id,
        event_id=fact.webhook_event_id,
        timestamp=_parse_db_time(fact.completed_at),
    )


def _owned_session(
    connection: sqlite3.Connection,
    context: AuthContext,
    chat_session_id: str,
) -> sqlite3.Row | None:
    """按 tenant、用户和当前登录 Session 加载会话，跨 owner 统一返回空。"""

    return connection.execute(
        """
        SELECT * FROM chat_sessions
        WHERE tenant_id=? AND chat_session_id=?
          AND owner_user_id=? AND owner_auth_session_id=?
        """,
        (
            context.user.tenant_id,
            chat_session_id,
            context.user.user_id,
            context.session.session_id,
        ),
    ).fetchone()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 Chat 数据库连接并把配置/连接错误折叠为统一业务错误。"""

    try:
        return connect_database(config.database_url)
    # DbError 可能携带数据库路径和运维建议，Chat 公共 API 只能返回固定不可用错误。
    except DbError:
        raise ApiError("chat.gateway_unavailable") from None


def _utcnow() -> datetime:
    """返回去微秒的当前 UTC，保证迁移 trigger 与测试使用同一时间格式。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把时区时间序列化为数据库 canonical UTC 秒。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: str) -> datetime:
    """解析数据库 canonical UTC 时间；非法存量状态统一视为服务不可用。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法存量时间不能把原始数据库值带入 traceback，统一视为服务暂时不可用。
    except (TypeError, ValueError, AttributeError):
        raise ApiError("chat.gateway_unavailable") from None
    # 无时区值无法证明是 UTC，禁止参与 Session/grant 的安全过期判断。
    if parsed.tzinfo is None:
        raise ApiError("chat.gateway_unavailable")
    return parsed.astimezone(UTC)
