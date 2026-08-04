"""PRD-066 企业 Chat 的 Gateway 内部可信入口。

该模块只接受独立内部服务 Token 和一次性 grant。grant 明文、Prompt 与 Response 只存在于
当前调用栈；数据库只保存 grant 的 SHA-256 和执行元数据。消费时会实时复核 tenant、用户、
登录 Session、RBAC、项目、环境、模型别名及唯一 Project Key，再把固定请求交给公开入口
复用的完整治理编排，绝不直接调用 Provider Adapter。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
from metahub_shared.runtime.errors import ApiError

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.session_risk import (
    reject_public_session_headers_on_internal_route,
    resolve_enterprise_chat_session_risk_candidates,
)

from .openai_compatible import _handle_gateway_request

router = APIRouter(prefix="/internal/chat", tags=["internal-enterprise-chat"])

# 内部协议常量固定身份 Header、审计入口与正文上限，不能由调用方或 Provider 响应覆盖。
_INTERNAL_TOKEN_HEADER = "X-MetaHub-Internal-Chat-Token"
_PROVIDER_ENDPOINT = "chat/completions"
_AUDIT_ENDPOINT = "/internal/chat/completions"
_MAX_HISTORY_MESSAGES = 20
_MAX_MESSAGE_CHARS = 8_000
_MAX_HISTORY_BYTES = 65_536
# 该上限覆盖合法 history 的最坏 JSON 转义体积，同时阻断内部调用者发送无界原始正文。
_MAX_WIRE_BODY_BYTES = 512 * 1024


class _DuplicateJsonKey(ValueError):
    """表示内部 Chat JSON 任意对象层出现重复键，异常不得保存具体键名。"""


@dataclass(frozen=True, slots=True)
class InternalChatExecution:
    """grant 消费后恢复的可信 Gateway 身份、模型别名和最终消息。

    ``messages`` 与可选 ``rendered_user`` 关闭 repr，只能在本次 HTTP 调用栈内使用；结构
    不包含 grant、Provider Key 或 Endpoint，也不得进入日志、缓存或持久化 DTO。
    """

    auth_context: GatewayAuthContext
    chat_session_id: str
    chat_message_id: str
    model_alias: str
    messages: tuple[dict[str, str], ...] = field(repr=False)
    rendered_user: str | None = field(default=None, repr=False)


@router.post("/completions")
async def internal_chat_completions(request: Request) -> JSONResponse:
    """消费单次 grant，并通过共用治理函数执行非流式 Chat Completions。

    参数：内部 Token Header、与 grant 冻结值一致的链路 Header，以及 exact-set grant/history
    JSON。返回：共用治理路径生成的 OpenAI-compatible 响应。任何身份字段均不从正文读取。
    """

    config = _config(request)
    _authenticate_internal_service(request, config)
    # 内部身份只能来自 grant；在读取 JSON 前拒绝公开 Session/Turn Header，防止伪造第二 owner。
    reject_public_session_headers_on_internal_route(request)
    # tenant 与模型绑定只能来自 grant，query 中任何身份或资源提示都必须拒绝。
    if request.query_params.multi_items():
        raise ApiError("chat.request_invalid")
    # 内部服务 Token 是唯一入口身份，Cookie 或 Bearer 并存会制造冲突 owner。
    if (
        request.headers.get("Authorization") is not None
        or request.headers.get("Cookie") is not None
    ):
        # 内部服务身份与浏览器/Virtual Key 身份不能并存，否则调用方可以制造 owner 歧义。
        raise ApiError("chat.execution_invalid")
    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    # 只接收明确 JSON，避免表单或文本编码绕过 grant/history 的 exact-set 校验。
    if content_type != "application/json":
        raise ApiError("chat.request_invalid")
    grant, history, variables = await _validated_body(request)
    execution = _consume_execution_grant(
        config,
        grant=grant,
        history=history,
        variables=variables,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response = await _handle_gateway_request(
        request,
        endpoint=_PROVIDER_ENDPOINT,
        request_kind="chat",
        audit_endpoint=_AUDIT_ENDPOINT,
        trusted_auth_context=execution.auth_context,
        trusted_payload={
            "model": execution.model_alias,
            "messages": list(execution.messages),
            "stream": False,
        },
        trusted_session_risk_candidates=resolve_enterprise_chat_session_risk_candidates(
            config,
            execution.auth_context,
            chat_session_id=execution.chat_session_id,
            chat_message_id=execution.chat_message_id,
        ),
    )
    # 内部入口固定非流式；若共用治理器违反约束，必须失败关闭而不能返回半个流响应。
    if isinstance(response, StreamingResponse):  # pragma: no cover - 固定 stream=False。
        raise ApiError("chat.response_invalid")
    return _attach_rendered_user(response, execution.rendered_user)


def _authenticate_internal_service(request: Request, config: AppConfig) -> None:
    """要求 wire 上恰好一个内部 Token，并使用常量时间比较。

    缺失与错误使用不同固定错误码，但不会返回配置值、Header 值或比较细节。重复 Header 和
    代理逗号合并值一律按错误 Token 处理，不能由框架替调用方选择其中一个。
    """

    expected = config.internal_chat_token
    # 生产环境未配置服务身份属于部署不可用，而不是调用方凭据错误；固定 503 也与配置加载
    # 合同一致，避免运维人员被误导为反复轮换一个实际上没有加载的 Token。
    if expected is None:
        raise ApiError("chat.gateway_unavailable")
    values = tuple(request.headers.getlist(_INTERNAL_TOKEN_HEADER))
    # 缺失身份与错误身份使用不同固定码，但两者都不能继续读取 grant 或正文。
    if not values or values == ("",):
        raise ApiError("auth.internal_token_required")
    # 重复 Header 失败关闭，不能由框架替调用方猜测其中一个有效值。
    if len(values) != 1:
        raise ApiError("auth.internal_token_invalid")
    supplied = values[0]
    try:
        valid = supplied.isascii() and hmac.compare_digest(supplied, expected)
    # Header 类型或编码异常不能进入普通字符串比较，也不得把原值写入错误链。
    except (TypeError, UnicodeError):
        valid = False
    # 常量时间比较失败只返回固定错误，不泄露长度、前缀或配置状态。
    if not valid:
        raise ApiError("auth.internal_token_invalid")


async def _validated_body(
    request: Request,
) -> tuple[str, list[dict[str, str]], dict[str, str] | None]:
    """解析普通或模板 exact-set wire，返回当前调用栈中的规范值。

    普通模式只接受 ``grant/history``，history 必须以 user 结束；模板模式额外且仅额外
    接受 ``variables``，history 必须为空或为完整 user/assistant 对。变量定义、必填和
    exact-set 仍须在可信版本解密后复核，wire 校验不能替代数据库 owner 与模板合同。
    """

    try:
        raw_body = await _read_bounded_body(request)
        body = json.loads(
            raw_body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
    # JSONDecodeError 会保存原始文档；切断异常链可防止错误追踪系统把 Prompt 带入 traceback。
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
    ):
        # 解析器异常可能持有原始文档或冲突键，必须切断异常链并只返回统一公开错误。
        raise ApiError("chat.request_invalid") from None
    if not isinstance(body, dict):
        raise ApiError("chat.request_invalid")
    fields = set(body)
    template_mode = fields == {"grant", "history", "variables"}
    # 两个 wire exact-set 互斥；tenant、模板 ID、正文、Key、Provider 等身份均不能注入。
    if fields != {"grant", "history"} and not template_mode:
        raise ApiError("chat.request_invalid")
    grant = body.get("grant")
    history = body.get("history")
    if (
        type(grant) is not str
        or not 43 <= len(grant) <= 128
        or not grant.isascii()
        or any(not (character.isalnum() or character in "-_") for character in grant)
    ):
        # grant 形状和值均统一为 execution_invalid，避免形成有效长度或字符集探针。
        raise ApiError("chat.execution_invalid")
    # 模板模式允许空历史，普通模式仍至少需要本轮 user；两者共用硬上限。
    if (
        not isinstance(history, list)
        or len(history) > _MAX_HISTORY_MESSAGES
        or (not template_mode and not history)
    ):
        # 历史形状/条数失败统一为请求无效，错误中不泄露消息数量或正文。
        raise ApiError("chat.request_invalid")
    normalized: list[dict[str, str]] = []
    total_bytes = 0
    for index, item in enumerate(history):
        expected_role = "user" if index % 2 == 0 else "assistant"
        # 每条消息必须是 role/content exact-set，禁止工具、附件或额外身份字段。
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ApiError("chat.request_invalid")
        role = item.get("role")
        content = item.get("content")
        if (
            role != expected_role
            or type(content) is not str
            or not content.strip()
            or len(content) > _MAX_MESSAGE_CHARS
        ):
            # 角色错序、空白或单条超限统一拒绝，不能回显具体正文和校验位置。
            raise ApiError("chat.request_invalid")
        try:
            total_bytes += len(content.encode("utf-8"))
        # 非法代理字符无法稳定进入 DLP/Provider wire，必须在治理前失败关闭。
        except UnicodeEncodeError:
            raise ApiError("chat.request_invalid") from None
        if total_bytes > _MAX_HISTORY_BYTES:
            # 总字节上限按 UTF-8 计算，防止多字节文本绕过字符数限制扩大内存占用。
            raise ApiError("chat.request_invalid")
        normalized.append({"role": expected_role, "content": content})
    # 普通模式必须以本轮 user 结束；模板模式只能携带完整历史对，最终 user 由 Gateway 渲染追加。
    if (not template_mode and normalized[-1]["role"] != "user") or (
        template_mode and len(normalized) % 2 != 0
    ):
        # 普通请求须以 user 结束，模板请求须仅含完整历史对；不允许自动修补角色顺序。
        raise ApiError("chat.request_invalid")
    variables = _validated_template_variables(body.get("variables")) if template_mode else None
    return grant, normalized, variables


def _validated_template_variables(value: object) -> dict[str, str]:
    """校验模板变量 wire 的基础形状和资源上限，不解释变量业务定义。

    参数：JSON 解码后的 variables。返回：只含严格字符串键值的新字典。名称、必填和单值
    ``max_chars`` 必须在解密定义后由共享 renderer 复核；这里仅阻断嵌套值、原型保留形状和
    过量字节，避免在持有数据库写锁时才分配无界输入。
    """

    if type(value) is not dict or len(value) > 32:
        # 变量必须是小型 exact object，禁止数组/嵌套容器在持锁阶段放大资源消耗。
        raise ApiError("chat.request_invalid")
    normalized: dict[str, str] = {}
    total_bytes = 0
    for name, item in value.items():
        if (
            type(name) is not str
            or not 1 <= len(name) <= 64
            or not name.isascii()
            or type(item) is not str
            or len(item) > _MAX_MESSAGE_CHARS
        ):
            # 键和值可能含客户正文，公开错误不能指出具体名称、位置或内容。
            raise ApiError("chat.request_invalid")
        try:
            total_bytes += len(name.encode("utf-8")) + len(item.encode("utf-8"))
        # 非法 UTF-8 变量不能进入 HMAC、模板渲染或 Provider wire，且不回显具体值。
        except UnicodeEncodeError:
            raise ApiError("chat.request_invalid") from None
        # 变量集合与历史共用 64 KiB 安全上限，避免用多个合法小值绕过总量门禁。
        if total_bytes > _MAX_HISTORY_BYTES:
            raise ApiError("chat.request_invalid")
        normalized[name] = item
    return normalized


async def _read_bounded_body(request: Request) -> bytes:
    """按声明与实际流量有界读取内部 JSON，不信任可省略或伪造的 Content-Length。

    返回值只在当前请求栈用于结构化 JSON 解析。超限、空正文、截断和 receive 异常均转换为
    固定 ``chat.request_invalid``，不能把内部网络细节或已接收 Prompt 链入 traceback。
    """

    declared = _content_length(request)
    if declared is not None and (
        declared < 1 or declared > _MAX_WIRE_BODY_BYTES
    ):
        # 声明已越界时在分配正文缓冲区前失败，不能等待完整 body 到达后再做 history 校验。
        raise ApiError("chat.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            # 实际流独立累计，chunked 或虚假 Content-Length 也不能绕过硬上限。
            if len(body) + len(chunk) > _MAX_WIRE_BODY_BYTES:
                raise ApiError("chat.request_invalid")
            body.extend(chunk)
    # 已目录化的请求错误必须保留固定码，不能被后续网络异常转换覆盖。
    except ApiError:
        raise
    # ASGI 断连可能携带底层连接信息，只返回固定错误且切断异常链。
    except Exception:
        raise ApiError("chat.request_invalid") from None
    if not body or (declared is not None and declared != len(body)):
        # 空、截断或额外拼接正文没有唯一 grant/history 语义，必须失败关闭。
        raise ApiError("chat.request_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    """读取 wire 上唯一规范长度；重复、符号、空白和超长数字声明全部拒绝。"""

    values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not value
        or len(value) > 12
        or any(character < "0" or character > "9" for character in value)
    ):
        # 不同代理可能以不同方式合并畸形长度，Gateway 不能替内部调用者猜测。
        raise ApiError("chat.request_invalid")
    return int(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object，并拒绝任意嵌套层的重复字段以消除解析器歧义。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 键名可能由攻击者构造，不得放入异常文本或通用错误收集器。
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _consume_execution_grant(
    config: AppConfig,
    *,
    grant: str,
    history: list[dict[str, str]],
    variables: dict[str, str] | None,
    request_id: str,
    trace_id: str,
) -> InternalChatExecution:
    """在单个 IMMEDIATE 事务中实时复核 owner 图并原子消费 grant。

    参数：服务配置、只在当前栈使用的 grant/history/变量及规范化链路 ID。返回：可信
    Gateway 身份、模型别名和最终治理消息。过期、重放、跨 owner、撤权或资源失效全部
    折叠为固定 Chat 错误，解密与渲染必须发生在 grant 消费 CAS 之前的同一写事务中。
    """

    connection = _connect(config)
    now = _db_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM chat_execution_grants
            WHERE grant_hash=? AND status='issued'
              AND gateway_request_id=? AND gateway_trace_id=?
              AND julianday(expires_at)>julianday(?)
            """,
            (hashlib.sha256(grant.encode("ascii")).hexdigest(), request_id, trace_id, now),
        ).fetchone()
        # 过期、重放、链路错配和未知 grant 共享一个错误，避免形成执行状态枚举器。
        if row is None:
            raise ApiError("chat.execution_invalid")
        tenant_id = str(row["tenant_id"])
        session = connection.execute(
            """
            SELECT * FROM chat_sessions
            WHERE tenant_id=? AND chat_session_id=? AND owner_user_id=?
              AND owner_auth_session_id=? AND project_id=? AND environment_id=?
              AND model_alias_id=? AND virtual_key_id=? AND status='active'
              AND julianday(expires_at)>julianday(?)
            """,
            (
                tenant_id,
                row["chat_session_id"],
                row["owner_user_id"],
                row["owner_auth_session_id"],
                row["project_id"],
                row["environment_id"],
                row["model_alias_id"],
                row["virtual_key_id"],
                now,
            ),
        ).fetchone()
        message = connection.execute(
            """
            SELECT 1 FROM chat_messages
            WHERE tenant_id=? AND chat_session_id=? AND chat_message_id=?
              AND owner_user_id=? AND gateway_request_id=? AND gateway_trace_id=?
              AND status='in_flight'
            """,
            (
                tenant_id,
                row["chat_session_id"],
                row["chat_message_id"],
                row["owner_user_id"],
                request_id,
                trace_id,
            ),
        ).fetchone()
        invocation = connection.execute(
            """
            SELECT * FROM chat_prompt_template_invocations
            WHERE tenant_id=? AND chat_session_id=? AND chat_message_id=?
            LIMIT 1
            """,
            (tenant_id, row["chat_session_id"], row["chat_message_id"]),
        ).fetchone()
        actor = connection.execute(
            """
            SELECT user.role FROM users user
            JOIN tenants tenant ON tenant.tenant_id=user.tenant_id
            JOIN roles role
              ON role.tenant_id=user.tenant_id AND role.code=user.role AND role.status='active'
            JOIN role_permissions relation
              ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE user.tenant_id=? AND user.user_id=? AND user.status='active'
              AND tenant.status='active' AND permission.code='chat.use'
              AND permission.status='active'
            LIMIT 1
            """,
            (tenant_id, row["owner_user_id"]),
        ).fetchone()
        auth_session = connection.execute(
            """
            SELECT 1 FROM sessions
            WHERE tenant_id=? AND session_id=? AND user_id=? AND status='active'
              AND julianday(expires_at)>julianday(?)
            """,
            (tenant_id, row["owner_auth_session_id"], row["owner_user_id"], now),
        ).fetchone()
        project = connection.execute(
            "SELECT 1 FROM projects WHERE tenant_id=? AND project_id=? AND status='active'",
            (tenant_id, row["project_id"]),
        ).fetchone()
        environment = connection.execute(
            """
            SELECT code,name,status FROM environments
            WHERE tenant_id=? AND environment_id=? AND status='active'
            """,
            (tenant_id, row["environment_id"]),
        ).fetchone()
        alias = connection.execute(
            """
            SELECT alias FROM model_aliases
            WHERE tenant_id=? AND model_alias_id=? AND alias=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["model_alias_id"],
                session["model_alias"] if session is not None else "",
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()
        virtual_key = connection.execute(
            """
            SELECT key_prefix,application_id FROM virtual_keys
            WHERE tenant_id=? AND virtual_key_id=? AND project_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["virtual_key_id"],
                row["project_id"],
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()
        key_count = connection.execute(
            """
            SELECT COUNT(*) FROM virtual_keys
            WHERE tenant_id=? AND project_id=? AND status='active'
              AND (environment_id=? OR (environment_id IS NULL AND environment=?))
            """,
            (
                tenant_id,
                row["project_id"],
                row["environment_id"],
                environment["code"] if environment is not None else "",
            ),
        ).fetchone()[0]
        if any(
            value is None
            for value in (
                session,
                message,
                actor,
                auth_session,
                project,
                environment,
                alias,
                virtual_key,
            )
        ) or int(key_count) != 1:
            # 任一 owner 或资源实时失效都必须在消费 CAS 前退出，不得调用完整治理链。
            raise ApiError("chat.execution_invalid")
        # variables 与 Invocation 必须精确同现或同缺；不能把模板 Message 当普通 Chat 静默发送，
        # 也不能仅凭内部 wire 中出现 variables 就选择一个数据库并未授权的模板。
        if (variables is None) != (invocation is None):
            raise ApiError("chat.execution_invalid")
        trusted_messages = list(history)
        rendered_user: str | None = None
        if invocation is not None:
            if any(
                str(invocation[column]) != str(expected)
                for column, expected in (
                    ("actor_user_id", row["owner_user_id"]),
                    ("actor_auth_session_id", row["owner_auth_session_id"]),
                    ("gateway_request_id", request_id),
                    ("gateway_trace_id", trace_id),
                    ("status", "in_flight"),
                )
            ):
                # 0059 trigger 已约束这些 owner；Gateway 仍显式复演，防止旧库或旁路写入被信任。
                raise ApiError("chat.execution_invalid")
            rendered_user = _render_invocation_user(
                connection,
                config,
                invocation=invocation,
                tenant_id=tenant_id,
                actor_user_id=str(row["owner_user_id"]),
                project_id=str(row["project_id"]),
                variables=variables if variables is not None else {},
            )
            trusted_messages.append({"role": "user", "content": rendered_user})
        _validate_final_messages(trusted_messages)
        connection.execute(
            """
            UPDATE chat_execution_grants
            SET status='consumed',consumed_at=?
            WHERE tenant_id=? AND chat_execution_grant_id=? AND status='issued'
              AND grant_hash=? AND julianday(expires_at)>julianday(?)
            """,
            (
                now,
                tenant_id,
                row["chat_execution_grant_id"],
                row["grant_hash"],
                now,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # CAS 未精确修改一行表示并发消费或状态漂移，不能复用同一 grant。
            raise ApiError("chat.execution_invalid")
        connection.commit()
        return InternalChatExecution(
            auth_context=GatewayAuthContext(
                tenant_id=tenant_id,
                virtual_key_id=str(row["virtual_key_id"]),
                key_prefix=str(virtual_key["key_prefix"]),
                environment=str(environment["code"]),
                environment_id=str(row["environment_id"]),
                environment_name=str(environment["name"]),
                environment_status=str(environment["status"]),
                project_id=str(row["project_id"]),
                application_id=(
                    str(virtual_key["application_id"])
                    if virtual_key["application_id"] is not None
                    else None
                ),
            ),
            chat_session_id=str(row["chat_session_id"]),
            chat_message_id=str(row["chat_message_id"]),
            model_alias=str(alias["alias"]),
            messages=tuple(trusted_messages),
            rendered_user=rendered_user,
        )
    # 统一业务错误保持原固定码，同时回滚本事务内尚未提交的 grant 状态。
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    # 约束竞争说明 owner/CAS 状态已变化，对外折叠为不可枚举的 execution_invalid。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # trigger 或索引名称属于数据库实现细节，不得进入内部 HTTP 错误正文。
        raise ApiError("chat.execution_invalid") from exc
    # 其他数据库故障不代表 grant 无效，统一使用可追踪的暂时不可用错误。
    except sqlite3.Error as exc:
        connection.rollback()
        # SQL、数据库路径和底层异常消息只能留在受控诊断，不向调用方公开。
        raise ApiError("chat.gateway_unavailable") from exc
    finally:
        connection.close()


def _render_invocation_user(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    invocation: sqlite3.Row,
    tenant_id: str,
    actor_user_id: str,
    project_id: str,
    variables: dict[str, str],
) -> str:
    """在 grant 写事务内复核模板全图、解密并渲染最终 user 正文。

    参数：当前写连接、配置、不可变 Invocation owner、可信租户/用户/项目和内存变量。返回：
    仅供本次治理调用的正文。关键约束：推荐模型不参与准入；任何跨租户、stale head、禁用、
    撤权或 Project 不适用都在 grant CAS 前失败，密文与变量值不会进入异常或日志。
    """

    permission = connection.execute(
        """
        SELECT 1 FROM users user
        JOIN tenants tenant ON tenant.tenant_id=user.tenant_id
        JOIN roles role
          ON role.tenant_id=user.tenant_id AND role.code=user.role AND role.status='active'
        JOIN role_permissions relation
          ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE user.tenant_id=? AND user.user_id=? AND user.status='active'
          AND tenant.status='active' AND permission.code='prompt_template.use'
          AND permission.status='active'
        LIMIT 1
        """,
        (tenant_id, actor_user_id),
    ).fetchone()
    version = connection.execute(
        """
        SELECT version.definition_ciphertext,version.version_number,
               version.prompt_template_id,version.prompt_template_version_id
        FROM prompt_template_versions version
        JOIN prompt_templates template
          ON template.tenant_id=version.tenant_id
         AND template.prompt_template_id=version.prompt_template_id
        WHERE version.tenant_id=?
          AND version.prompt_template_id=?
          AND version.prompt_template_version_id=?
          AND version.version_number=?
          AND version.state='published'
          AND template.status='active'
          AND template.current_version_id=version.prompt_template_version_id
          AND template.current_version_state=version.state
          AND (
            version.project_scope='all'
            OR EXISTS (
              SELECT 1 FROM prompt_template_version_projects binding
              WHERE binding.tenant_id=version.tenant_id
                AND binding.prompt_template_id=version.prompt_template_id
                AND binding.prompt_template_version_id=version.prompt_template_version_id
                AND binding.project_id=?
            )
          )
        LIMIT 1
        """,
        (
            tenant_id,
            invocation["prompt_template_id"],
            invocation["prompt_template_version_id"],
            invocation["version_number"],
            project_id,
        ),
    ).fetchone()
    # 撤权、禁用、stale/current 漂移和 Project 失配共享不可枚举的执行失效语义。
    if permission is None or version is None:
        raise ApiError("chat.execution_invalid")
    owner = PromptTemplateOwner(
        tenant_id=tenant_id,
        prompt_template_id=str(version["prompt_template_id"]),
        prompt_template_version_id=str(version["prompt_template_version_id"]),
        version_number=int(version["version_number"]),
    )
    try:
        definition = decrypt_definition(
            version["definition_ciphertext"],
            secret=config.prompt_template_encryption_secret,
            owner=owner,
        )
        rendered = render_prompt(definition, variables)
    # 部署密钥缺失是暂时不可用，不能伪装成用户模板失效或泄露配置细节。
    except PromptTemplateSecretUnavailable:
        raise ApiError("chat.gateway_unavailable") from None
    except PromptTemplateDefinitionUnavailable:
        # 已认证密文、AAD 或 schema 损坏属于不可信成功数据，固定折叠为响应无效。
        raise ApiError("chat.response_invalid") from None
    except PromptTemplateContractError:
        # 变量 exact-set/必填/长度在 Control 后发生漂移时不得穿透 prompt_template.* 域。
        raise ApiError("chat.execution_invalid") from None
    if not rendered.strip():
        # 可选变量可能把仅含占位符的模板渲染为空；空 user message 不进入 DLP 或 Provider。
        raise ApiError("chat.execution_invalid")
    return rendered


def _validate_final_messages(messages: list[dict[str, str]]) -> None:
    """在模板追加后重新复核最终治理 history 的交替、条数和 UTF-8 总量。

    参数：普通 history 或“完整历史对 + 渲染 user”。返回值：无。该复核发生在 grant CAS
    之前，确保模板模式不能借空 history 或大变量绕过 PRD-066 的 20 条、64 KiB 治理上限。
    """

    if not 1 <= len(messages) <= _MAX_HISTORY_MESSAGES:
        # 模板追加后的最终历史也必须满足既有硬上限，不能只校验客户端提交片段。
        raise ApiError("chat.execution_invalid")
    total_bytes = 0
    for index, item in enumerate(messages):
        expected_role = "user" if index % 2 == 0 else "assistant"
        if (
            type(item) is not dict
            or set(item) != {"role", "content"}
            or item.get("role") != expected_role
            or type(item.get("content")) is not str
            or not str(item["content"]).strip()
        ):
            # 最终消息只允许严格交替 user/assistant，拒绝任何模板渲染后的形状漂移。
            raise ApiError("chat.execution_invalid")
        try:
            total_bytes += len(str(item["content"]).encode("utf-8"))
        # 渲染结果若不能稳定编码，必须在 grant 消费前失败，且不把正文挂到异常链。
        except UnicodeEncodeError:
            raise ApiError("chat.execution_invalid") from None
        # 二次按 UTF-8 总量复核，防止变量展开把合法输入放大到治理链上限之外。
        if total_bytes > _MAX_HISTORY_BYTES:
            raise ApiError("chat.execution_invalid")
    # 治理链最终输入必须以本轮 user 结束，模板不能注入 assistant/system 身份。
    if messages[-1]["role"] != "user":
        raise ApiError("chat.execution_invalid")


def _attach_rendered_user(
    response: JSONResponse,
    rendered_user: str | None,
) -> JSONResponse:
    """仅在模板成功响应附加内存渲染正文，并强制 no-store。

    参数：完整治理器的非流式响应和可选正文。返回：普通响应原对象，或保留状态/Header/
    background 的新 JSONResponse。Provider 错误、非 JSON 成功和字段冲突一律失败关闭；正文
    不进入错误链、持久层或日志。
    """

    response.headers["Cache-Control"] = "no-store"
    if rendered_user is None or not 200 <= response.status_code < 300:
        return response
    try:
        payload = json.loads(bytes(response.body).decode("utf-8", errors="strict"))
    # JSON 异常可能持有 assistant 正文，切断异常链后只暴露固定 Chat 错误。
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("chat.response_invalid") from None
    # 上游成功体必须是对象且不能抢占 Control 专用字段，冲突时拒绝而不是覆盖正文。
    if not isinstance(payload, dict) or "rendered_user" in payload:
        raise ApiError("chat.response_invalid")
    payload["rendered_user"] = rendered_user
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    headers["Cache-Control"] = "no-store"
    return JSONResponse(
        payload,
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开 Gateway 数据库连接，并把路径/驱动错误折叠为统一 Chat 可用性错误。"""

    try:
        return connect_database(config.database_url)
    # 连接配置和文件路径可能敏感，公开错误只能说明 Gateway 暂时不可用。
    except DbError as exc:
        raise ApiError("chat.gateway_unavailable") from exc


def _config(request: Request) -> AppConfig:
    """读取应用启动时冻结的服务配置。"""

    return request.app.state.config


def _request_id(request: Request) -> str:
    """读取共享中间件生成并完成敏感碰撞扫描的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享中间件生成并完成敏感碰撞扫描的 trace_id。"""

    return getattr(request.state, "trace_id", "")


def _db_now() -> str:
    """返回与 0058 trigger 相同的 UTC 秒文本，保证 grant 过期比较没有时区歧义。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
