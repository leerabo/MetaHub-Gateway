"""会话风险身份的版本化、域分隔 HMAC 派生。

所有派生统一使用 ``domain_tag || uint32_be(len(part)) || part...``，避免字符串分隔符
碰撞和跨用途重放。模块只接受精确 ``str``/``bytes``，拒绝自定义对象隐式转换；密钥与
摘要均从 ``repr`` 隐藏，调用方也不得把返回摘要放进公开 API、Webhook 或普通日志。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from struct import pack
from typing import Final

from .models import SessionRiskContractError, SessionRiskScope, VersionedHmacDigest

# 每个用途使用不同常量域；即使 owner 组成完全相同，也不能跨 session、turn 或 action 复用摘要。
SESSION_SCOPE_HMAC_DOMAIN: Final[bytes] = b"metahub.session-risk.session.v1"
OWNER_SLOT_HMAC_DOMAIN: Final[bytes] = b"metahub.session-risk.owner-slot.v1"
TURN_KEY_HMAC_DOMAIN: Final[bytes] = b"metahub.session-risk.turn.v1"
TURN_INTENT_HMAC_DOMAIN: Final[bytes] = b"metahub.session-risk.turn-intent.v1"
ACTION_IDEMPOTENCY_HMAC_DOMAIN: Final[bytes] = (
    b"metahub.session-risk.action-idempotency.v1"
)
ACTION_INTENT_HMAC_DOMAIN: Final[bytes] = b"metahub.session-risk.action-intent.v1"

# 256 KiB 是当前 user turn 投影的冻结上限；其他 owner 字段远小于该值并复用同一硬门禁。
MAX_HMAC_PART_BYTES: Final[int] = 256 * 1024
MAX_HMAC_DOMAIN_BYTES: Final[int] = 128


@dataclass(frozen=True, slots=True)
class SessionRiskHmacKey:
    """保存一把带正整数版本的进程内 HMAC 密钥。

    参数：版本与 32..512 UTF-8 bytes 的独立根密钥。返回值：不可变 key 对象。
    关键约束：secret 不进入 repr；禁止空白/控制字符、弱短值和自定义字节对象。
    """

    version: int
    secret: bytes | str = field(repr=False)

    def __post_init__(self) -> None:
        """规范化密钥为 bytes，并在任何派生前执行强度和稳定编码校验。"""

        if type(self.version) is not int or self.version < 1:
            # bool 也是 int 子类，必须用精确类型拒绝 ``True`` 这类多义版本。
            raise SessionRiskContractError("session_risk.hmac_key_invalid")
        if type(self.secret) is str:
            try:
                encoded = self.secret.encode("utf-8")
            # 不可稳定编码的代理字符不能在多进程间派生一致摘要，且异常不得回显原值。
            except UnicodeEncodeError:
                raise SessionRiskContractError("session_risk.hmac_key_invalid") from None
            if any(character.isspace() or ord(character) < 32 for character in self.secret):
                # 空白和控制字符常来自配置复制错误，启动时失败比静默使用错误 key 更安全。
                raise SessionRiskContractError("session_risk.hmac_key_invalid")
        elif type(self.secret) is bytes:
            encoded = self.secret
            try:
                decoded = encoded.decode("utf-8")
            # 配置合同统一为 UTF-8；任意 bytes 不得形成另一套跨服务编码语义。
            except UnicodeDecodeError:
                raise SessionRiskContractError("session_risk.hmac_key_invalid") from None
            # bytes 解码后仍禁止空白和控制字符，确保跨服务环境变量解析得到同一物理密钥。
            if any(character.isspace() or ord(character) < 32 for character in decoded):
                # 统一固定错误避免在启动日志中回显密钥内容、编码或异常字符位置。
                raise SessionRiskContractError("session_risk.hmac_key_invalid")
        else:
            # 禁止 bytearray、memoryview 或对象 ``__bytes__`` 在派生期间执行可变副作用。
            raise SessionRiskContractError("session_risk.hmac_key_invalid")
        if not 32 <= len(encoded) <= 512:
            # 长度错误使用固定码，不公开实际长度或密钥前后缀。
            raise SessionRiskContractError("session_risk.hmac_key_invalid")
        object.__setattr__(self, "secret", encoded)


@dataclass(frozen=True, slots=True)
class SessionRiskHmacKeyring:
    """保存 current 与可选 previous 两把会话风险密钥。

    current 始终用于新 cycle；previous 仅用于查找或继续数据库仍引用的既有 cycle。
    两个版本必须严格递增且 secret 不同，防止轮换配置产生同版本多密钥歧义。
    """

    current: SessionRiskHmacKey
    previous: SessionRiskHmacKey | None = None

    def __post_init__(self) -> None:
        """校验 key 对象、版本顺序及物理密钥分离。"""

        # current 必须是冻结密钥对象，禁止字典或子类绕过构造期长度与编码校验。
        if type(self.current) is not SessionRiskHmacKey:
            # 统一合同错误不得回显密钥类型、内容或长度，避免启动日志泄露配置细节。
            raise SessionRiskContractError("session_risk.hmac_key_invalid")
        if self.previous is None:
            return
        if (
            type(self.previous) is not SessionRiskHmacKey
            or self.current.version <= self.previous.version
            or hmac.compare_digest(self.current.secret, self.previous.secret)
        ):
            # 版本倒置或复用 secret 会破坏轮换审计，必须在启动阶段失败关闭。
            raise SessionRiskContractError("session_risk.hmac_key_invalid")

    def derive_candidates(
        self, domain_tag: str | bytes, *parts: str | bytes
    ) -> tuple[VersionedHmacDigest, ...]:
        """按 current、previous 顺序派生候选，用于无明文的轮换期查找。

        参数：固定 domain 与严格字符串/字节段。返回：一或两个版本化摘要。
        关键约束：调用方只能用候选定位同 tenant owner，不能把候选写入响应或日志。
        """

        current = derive_versioned_hmac(self.current, domain_tag, *parts)
        if self.previous is None:
            return (current,)
        return (current, derive_versioned_hmac(self.previous, domain_tag, *parts))

    def key_for_version(self, version: int) -> SessionRiskHmacKey | None:
        """返回精确版本的已配置 key，缺失时返回空且不回退 current。"""

        if type(version) is not int:
            return None
        if version == self.current.version:
            return self.current
        if self.previous is not None and version == self.previous.version:
            return self.previous
        return None


def derive_versioned_hmac(
    key: SessionRiskHmacKey,
    domain_tag: str | bytes,
    *parts: str | bytes,
) -> VersionedHmacDigest:
    """按冻结长度前缀编码派生一个版本化 SHA-256 HMAC。

    参数：已验证 key、非空独立 domain 及至少一个有界 part。返回：``srh_`` 小写摘要。
    关键约束：domain 不加长度前缀，part 逐个使用四字节网络序长度；禁止隐式字符串化。
    """

    # 派生入口再次执行精确类型校验，阻断调用方绕过 dataclass 构造器传入可变密钥。
    if type(key) is not SessionRiskHmacKey:
        # 输入错误统一收敛为固定码，禁止把 owner、domain 或密钥内容带入异常。
        raise SessionRiskContractError("session_risk.hmac_input_invalid")
    domain = _encode_exact(domain_tag, is_domain=True)
    if not parts:
        # 没有 owner 段会让不同调用方共享常量摘要，属于不可恢复的身份设计错误。
        raise SessionRiskContractError("session_risk.hmac_input_invalid")
    mac = hmac.new(key.secret, digestmod=hashlib.sha256)
    mac.update(domain)
    for part in parts:
        encoded = _encode_exact(part, is_domain=False)
        # 固定四字节大端长度使 ("ab","c") 与 ("a","bc") 永远不可碰撞。
        mac.update(pack(">I", len(encoded)))
        mac.update(encoded)
    return VersionedHmacDigest(key_version=key.version, digest=f"srh_{mac.hexdigest()}")


def derive_owner_slot(
    key: SessionRiskHmacKey,
    *,
    tenant_id: str,
    virtual_key_id: str,
    scope_type: str,
    scope_key: str,
) -> VersionedHmacDigest:
    """派生 Redis owner slot，绑定 tenant、Virtual Key、scope 类型与持久 scope key。

    owner 任一字段为空都会拒绝；这样相同 API Header 在不同 tenant/Key 下无法共享热状态。
    返回摘要只用于内部 Redis key，不提供给客户端。
    """

    _require_nonempty_owner_parts(tenant_id, virtual_key_id, scope_type, scope_key)
    return derive_versioned_hmac(
        key,
        OWNER_SLOT_HMAC_DOMAIN,
        tenant_id,
        virtual_key_id,
        scope_type,
        scope_key,
    )


def derive_api_session_scope(
    key: SessionRiskHmacKey,
    *,
    tenant_id: str,
    virtual_key_id: str,
    session_id: str,
) -> SessionRiskScope:
    """把已校验公开 Session Header 派生为租户限定 API scope。

    参数：current/selected key、认证 owner 与规范 ``mhs_`` Header。返回：只含 HMAC 的
    ``SessionRiskScope``。关键约束：Header 不进入对象、数据库、Redis key 或异常文本。
    """

    _require_nonempty_owner_parts(tenant_id, virtual_key_id, session_id)
    if (
        type(session_id) is not str
        or len(session_id) != 36
        or not session_id.startswith("mhs_")
        or any(character not in "0123456789abcdef" for character in session_id[4:])
    ):
        # Header 规范由公开协议冻结，大小写或长度漂移不能静默归一成另一会话。
        raise SessionRiskContractError("session_risk.identifier_invalid")
    scope_digest = derive_versioned_hmac(
        key,
        SESSION_SCOPE_HMAC_DOMAIN,
        tenant_id,
        virtual_key_id,
        session_id,
    )
    owner_slot = derive_owner_slot(
        key,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type="api_session",
        scope_key=scope_digest.digest,
    )
    # 返回对象只含 tenant-qualified HMAC，不保留公开 Header 明文或可逆身份材料。
    return SessionRiskScope(
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type="api_session",
        scope_key=scope_digest.digest,
        owner_slot=owner_slot.digest,
        hmac_key_version=key.version,
    )


def _encode_exact(value: str | bytes, *, is_domain: bool) -> bytes:
    """把精确 str/bytes 转为有界 UTF-8 bytes，并拒绝空 domain 或超长段。"""

    if type(value) is str:
        try:
            encoded = value.encode("utf-8")
        # 编码异常不回显调用方内容，避免异常日志泄露 owner 或正文片段。
        except UnicodeEncodeError:
            raise SessionRiskContractError("session_risk.hmac_input_invalid") from None
    elif type(value) is bytes:
        encoded = value
    else:
        # 自定义 ``__str__``/``__bytes__`` 可能产生副作用或不同进程结果，必须明确拒绝。
        raise SessionRiskContractError("session_risk.hmac_input_invalid")
    maximum = MAX_HMAC_DOMAIN_BYTES if is_domain else MAX_HMAC_PART_BYTES
    # 超长 part 或空 domain 会破坏冻结编码合同，必须在 HMAC 计算前固定失败关闭。
    if len(encoded) > maximum or (is_domain and not encoded):
        raise SessionRiskContractError("session_risk.hmac_input_invalid")
    return encoded


def _require_nonempty_owner_parts(*parts: object) -> None:
    """拒绝 owner 关键字段为空、非精确字符串或包含控制字符。"""

    if any(
        type(part) is not str
        or not part
        or len(part.encode("utf-8")) > MAX_HMAC_PART_BYTES
        or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        # 关键 owner 为空会合并租户边界；统一固定错误且不指出具体字段。
        raise SessionRiskContractError("session_risk.hmac_input_invalid")


__all__ = [
    "ACTION_IDEMPOTENCY_HMAC_DOMAIN",
    "ACTION_INTENT_HMAC_DOMAIN",
    "MAX_HMAC_DOMAIN_BYTES",
    "MAX_HMAC_PART_BYTES",
    "OWNER_SLOT_HMAC_DOMAIN",
    "SESSION_SCOPE_HMAC_DOMAIN",
    "TURN_INTENT_HMAC_DOMAIN",
    "TURN_KEY_HMAC_DOMAIN",
    "SessionRiskHmacKey",
    "SessionRiskHmacKeyring",
    "derive_api_session_scope",
    "derive_owner_slot",
    "derive_versioned_hmac",
]
