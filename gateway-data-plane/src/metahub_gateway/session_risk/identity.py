"""PRD-070 可信会话身份解析与版本化分域 HMAC 派生。

公开 API 的 Session/Turn Header 只在功能启用时解析，必须成对、唯一且符合固定随机格式；
企业 Chat 身份只从已消费 grant 恢复。原始 Header 值仅在当前函数调用栈存在，派生摘要后
不会进入日志、异常、数据库、Redis、Webhook 或 Provider payload。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from fastapi import Request
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.session_risk import (
    MAX_TURN_INTENT_UTF8_BYTES,
    TURN_INTENT_HMAC_DOMAIN,
    TURN_KEY_HMAC_DOMAIN,
    SessionRiskContractError,
    SessionRiskHmacKey,
    SessionRiskHmacKeyring,
    SessionRiskScope,
    derive_api_session_scope,
    derive_owner_slot,
    derive_versioned_hmac,
)

from metahub_gateway.auth.virtual_key import GatewayAuthContext

# Header 名称和随机 ID 语法是公开兼容合同；固定格式可阻止代理合并值或业务文本进入摘要域。
SESSION_ID_HEADER: Final[str] = "X-MetaHub-Session-ID"
TURN_ID_HEADER: Final[str] = "X-MetaHub-Turn-ID"

_SESSION_ID_PATTERN = re.compile(r"^mhs_[0-9a-f]{32}$")
_TURN_ID_PATTERN = re.compile(r"^mht_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class SessionRiskRequestIdentity:
    """保存可信 session scope 与本轮 turn 摘要，不保存原始 Header。

    ``hmac_secret`` 不进入 repr/序列化，只用于 Policy mask 后派生 turn-intent 摘要。它来自
    AppConfig 专用 keyring，不得复用 Auth、Provider 或 Prompt Template 密钥。
    """

    scope: SessionRiskScope
    turn_key_digest: str
    hmac_key: SessionRiskHmacKey = field(repr=False)
    chat_message_id: str | None = None

    def derive_turn_intent_digest(self, current_turn_text: str) -> str:
        """对完整 current-turn 文本做分域 HMAC，避免同 Turn ID 异意图重放。

        参数：Policy mask 后最后一条 user 文本投影。返回：只在 Store/数据库内部使用的摘要。
        关键约束：UTF-8 投影不得超过 256 KiB；错误固定为 413 且不回显长度或正文。
        """

        encoded = current_turn_text.encode("utf-8")
        # 超限正文不得进入 HMAC 或错误详情，先在内存边界以固定 413 结束处理。
        if len(encoded) > MAX_TURN_INTENT_UTF8_BYTES:
            raise ApiError("gateway.session_risk_turn_too_large")
        try:
            return derive_versioned_hmac(
                self.hmac_key,
                TURN_INTENT_HMAC_DOMAIN,
                self.scope.tenant_id,
                self.scope.virtual_key_id,
                self.scope.scope_type,
                self.scope.scope_key,
                self.turn_key_digest,
                current_turn_text,
            ).digest
        # Shared 固定合同异常不回显正文或摘要，Gateway 只映射为稳定公开错误。
        except SessionRiskContractError:
            raise ApiError("gateway.session_risk_hmac_version_unavailable") from None


def session_risk_enabled(config: AppConfig) -> bool:
    """读取部署级开关；旧配置对象缺字段时安全兼容为 disabled。"""

    # 旧部署没有此字段时保持 PRD-069 行为，不能因升级代码自动启用跨请求关联。
    return bool(getattr(config, "session_risk_enabled", False))


def resolve_public_session_risk_candidates(
    request: Request,
    config: AppConfig,
    auth_context: GatewayAuthContext,
) -> tuple[SessionRiskRequestIdentity, ...]:
    """解析公开 Chat Header，并按 current/previous keyring 返回候选身份。

    参数：真实 ASGI 请求、启动时校验配置和已认证 Virtual Key。返回：无 Header 时为空，
    有 Header 时 current 在前、previous 在后。禁用状态完全不解析新 Header，保持旧客户端合同。
    """

    # 禁用时连 Header 都不解析，避免兼容客户端因携带同名扩展字段突然收到新错误。
    if not session_risk_enabled(config):
        return ()
    sessions = _header_values(request, SESSION_ID_HEADER)
    turns = _header_values(request, TURN_ID_HEADER)
    # 两个字段同时缺失表示调用方没有声明会话，继续执行单请求 DLP/Intent 安全路径。
    if not sessions and not turns:
        return ()
    if (
        len(sessions) != 1
        or len(turns) != 1
        or _SESSION_ID_PATTERN.fullmatch(sessions[0]) is None
        or _TURN_ID_PATTERN.fullmatch(turns[0]) is None
    ):
        # 重复行、代理逗号合并、大小写漂移、空值和只出现一侧都使用同一固定错误。
        raise ApiError("gateway.session_risk_identifier_invalid")

    session_id = sessions[0]
    turn_id = turns[0]
    keyring = _session_risk_keyring(config)
    keys = (keyring.current,) + ((keyring.previous,) if keyring.previous is not None else ())
    candidates = tuple(
        _public_candidate(
            auth_context,
            session_id=session_id,
            turn_id=turn_id,
            key=key,
        )
        for key in keys
    )
    # 原始值不赋给任何长生命周期对象；函数返回前只剩不可逆摘要。
    session_id = ""
    turn_id = ""
    return candidates


def resolve_enterprise_chat_session_risk_candidates(
    config: AppConfig,
    auth_context: GatewayAuthContext,
    *,
    chat_session_id: str,
    chat_message_id: str,
) -> tuple[SessionRiskRequestIdentity, ...]:
    """从 grant 已复核的 ChatSession/Message 构造 current/previous 候选。

    客户端 Header、body、历史消息和 request_id 均不能覆盖这两个服务端 ID。禁用时返回空，
    由预检层单独检查“活动 overlay 禁止关闭功能”的部署门禁。
    """

    # 企业 Chat 也服从同一部署开关，但身份始终来自服务端 grant 而非客户端 Header。
    if not session_risk_enabled(config):
        return ()
    # grant 中任一 owner ID 缺失都表示内部调用合同损坏，不能退化为公开 Header 身份。
    if not chat_session_id or not chat_message_id:
        raise ApiError("chat.execution_invalid")
    keyring = _session_risk_keyring(config)
    keys = (keyring.current,) + ((keyring.previous,) if keyring.previous is not None else ())
    return tuple(
        _enterprise_candidate(
            auth_context,
            chat_session_id=chat_session_id,
            chat_message_id=chat_message_id,
            key=key,
        )
        for key in keys
    )


def reject_public_session_headers_on_internal_route(request: Request) -> None:
    """拒绝内部 Chat 携带公开 Session/Turn Header，避免制造第二身份来源。"""

    # 内部路由已有唯一 grant 身份，再接受公开 Header 会制造可选择的第二 owner 来源。
    if _header_values(request, SESSION_ID_HEADER) or _header_values(request, TURN_ID_HEADER):
        raise ApiError("chat.execution_invalid")


def _public_candidate(
    auth_context: GatewayAuthContext,
    *,
    session_id: str,
    turn_id: str,
    key: SessionRiskHmacKey,
) -> SessionRiskRequestIdentity:
    """把一对公开 Header 派生为租户/Key 限定的 API scope 和 turn 摘要。"""

    try:
        scope = derive_api_session_scope(
            key,
            tenant_id=auth_context.tenant_id,
            virtual_key_id=auth_context.virtual_key_id,
            session_id=session_id,
        )
        turn_digest = derive_versioned_hmac(
            key,
            TURN_KEY_HMAC_DOMAIN,
            auth_context.tenant_id,
            auth_context.virtual_key_id,
            scope.scope_key,
            turn_id,
        ).digest
    # 派生失败只能暴露固定配置错误，不能把 Header、tenant 或摘要写进异常链。
    except SessionRiskContractError:
        raise ApiError("gateway.session_risk_hmac_version_unavailable") from None
    # 返回对象只携带不可逆摘要；原始公开 Header 到此不再向下游传播。
    return SessionRiskRequestIdentity(
        scope=scope,
        turn_key_digest=turn_digest,
        hmac_key=key,
    )


def _enterprise_candidate(
    auth_context: GatewayAuthContext,
    *,
    chat_session_id: str,
    chat_message_id: str,
    key: SessionRiskHmacKey,
) -> SessionRiskRequestIdentity:
    """用服务端授权标识派生 Redis 所有者槽位与当前轮摘要。

    参数只能来自已消费的内部 Chat grant；返回身份对象仍保留租户限定的 ChatSession 标识供数据库
    归属校验，原始消息正文和客户端伪造 Header 均不参与该路径。
    """

    try:
        owner_slot = derive_owner_slot(
            key,
            tenant_id=auth_context.tenant_id,
            virtual_key_id=auth_context.virtual_key_id,
            scope_type="enterprise_chat",
            scope_key=chat_session_id,
        )
        turn_digest = derive_versioned_hmac(
            key,
            TURN_KEY_HMAC_DOMAIN,
            auth_context.tenant_id,
            auth_context.virtual_key_id,
            chat_session_id,
            chat_message_id,
        ).digest
        scope = SessionRiskScope(
            tenant_id=auth_context.tenant_id,
            virtual_key_id=auth_context.virtual_key_id,
            scope_type="enterprise_chat",
            scope_key=chat_session_id,
            owner_slot=owner_slot.digest,
            hmac_key_version=key.version,
            chat_session_id=chat_session_id,
        )
    # grant 身份派生失败同样按配置不可用处理，禁止退回不带 tenant 的弱摘要。
    except SessionRiskContractError:
        raise ApiError("gateway.session_risk_hmac_version_unavailable") from None
    # ChatSession/Message ID 是服务端资源 ID，仍只在 tenant-qualified scope 内使用。
    return SessionRiskRequestIdentity(
        scope=scope,
        turn_key_digest=turn_digest,
        chat_message_id=chat_message_id,
        hmac_key=key,
    )


def _session_risk_keyring(config: AppConfig) -> SessionRiskHmacKeyring:
    """读取 current/previous 专用 keyring，并拒绝缺失、倒序或复用同一版本。"""

    current_version = getattr(config, "session_risk_hmac_version", None)
    current_secret = getattr(config, "session_risk_hmac_secret", None)
    previous_version = getattr(config, "session_risk_previous_hmac_version", None)
    previous_secret = getattr(config, "session_risk_previous_hmac_secret", None)
    # previous 版本与密钥必须成对出现，否则旧冻结 scope 可能出现不可证明的半轮换状态。
    if (previous_version is None) != (previous_secret is None):
        raise ApiError("gateway.session_risk_hmac_version_unavailable")
    try:
        current = SessionRiskHmacKey(current_version, current_secret)
        previous = (
            SessionRiskHmacKey(previous_version, previous_secret)
            if previous_version is not None and previous_secret is not None
            else None
        )
        # Shared keyring 继续校验版本顺序和密钥差异，Gateway 不自行放宽轮换规则。
        return SessionRiskHmacKeyring(current=current, previous=previous)
    # 配置对象中的密钥值不得进入日志或 cause，公开层只保留固定错误码。
    except SessionRiskContractError:
        raise ApiError("gateway.session_risk_hmac_version_unavailable") from None


def _header_values(request: Request, name: str) -> tuple[str, ...]:
    """从 ASGI 原始 Header 保留重复行，并将非 ASCII/合并值变成固定非法哨兵。

    Starlette 的普通 ``get`` 会选择或合并重复值，不能用于安全身份。这里直接读取 wire
    headers，大小写不敏感匹配名称，但不修改 value 的大小写或空白。
    """

    expected = name.lower().encode("ascii")
    values: list[str] = []
    for raw_name, raw_value in request.scope.get("headers", ()):
        if bytes(raw_name).lower() != expected:
            continue
        try:
            value = bytes(raw_value).decode("ascii")
        # 非 ASCII wire value 不做替换解码，统一转为空哨兵并由严格格式校验拒绝。
        except UnicodeDecodeError:
            values.append("")
            continue
        # 逗号表示代理可能合并了重复行；固定 ID 本身不允许逗号，因此必须失败关闭。
        values.append("" if "," in value else value)
    return tuple(values)


__all__ = [
    "SESSION_ID_HEADER",
    "TURN_ID_HEADER",
    "SessionRiskRequestIdentity",
    "reject_public_session_headers_on_internal_route",
    "resolve_enterprise_chat_session_risk_candidates",
    "resolve_public_session_risk_candidates",
    "session_risk_enabled",
]
