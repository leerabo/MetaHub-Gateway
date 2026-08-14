"""Security primitives shared by the PRD-078 approval control paths.

The module owns canonical serialization and the independent approval HMAC
keyring.  It deliberately exposes only versioned digests; plaintext tickets,
idempotency keys, passwords, prompts, responses, and provider credentials must
remain in the caller's short-lived memory and must never be logged here.
提供审批专用规范化与密钥轮换契约，所有非法输入固定失败且不泄露敏感原文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

# 每类审批摘要使用独立 HMAC 域；容量上限与固定摘要长度阻止跨域复用和无界规范化。
CREATE_KEY_DOMAIN: Final[bytes] = b"metahub.approval.create-key.v1"
CREATE_INTENT_DOMAIN: Final[bytes] = b"metahub.approval.create-intent.v1"
DECISION_KEY_DOMAIN: Final[bytes] = b"metahub.approval.decision-key.v1"
DECISION_INTENT_DOMAIN: Final[bytes] = b"metahub.approval.decision-intent.v1"
STEP_UP_DOMAIN: Final[bytes] = b"metahub.approval.step-up.v1"
CURSOR_DOMAIN: Final[bytes] = b"metahub.approval.cursor.v1"
TARGET_FINGERPRINT_DOMAIN: Final[bytes] = b"metahub.approval.target-fingerprint.v1"

MAX_APPROVAL_CANONICAL_BYTES: Final[int] = 256 * 1024
MAX_APPROVAL_DOMAIN_BYTES: Final[int] = 128
_HEX_DIGEST_LENGTH: Final[int] = 64


class ApprovalContractError(ValueError):
    """Fixed, non-sensitive failure for invalid approval crypto inputs.
    ``ApprovalContractError`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
    """

    def __init__(self, code: str = "approval.contract_invalid") -> None:
        """``__init__`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。"""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class VersionedApprovalSecret:
    """One process-local approval root secret with a positive version.
    根密钥只驻留当前进程且不得进入 repr。
    """

    version: int
    secret: bytes | str = field(repr=False)

    def __post_init__(self) -> None:
        """规范化审批根密钥版本与强度，并确保明文 Secret 不进入对象表示。"""
        if type(self.version) is not int or self.version < 1:
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid")
        encoded = _secret_bytes(self.secret)
        object.__setattr__(self, "secret", encoded)


@dataclass(frozen=True, slots=True)
class ApprovalDigest:
    """A version-qualified internal SHA-256 HMAC digest.
    ``ApprovalDigest`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
    """

    version: int
    digest: str = field(repr=False)

    def __post_init__(self) -> None:
        """验证版本化摘要的固定十六进制形状，拒绝长度、字符集或版本污染。"""
        if (
            type(self.version) is not int
            or self.version < 1
            or type(self.digest) is not str
            or len(self.digest) != _HEX_DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in self.digest)
        ):
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            raise ApprovalContractError()


@dataclass(frozen=True, slots=True)
class ApprovalKeyring:
    """Current-first approval HMAC keyring with one retained old key.
    ``ApprovalKeyring`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
    """

    current: VersionedApprovalSecret
    previous: VersionedApprovalSecret | None = None

    def __post_init__(self) -> None:
        """验证 current-first 密钥轮换关系，阻止同密钥复用和版本倒挂。"""
        # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
        if type(self.current) is not VersionedApprovalSecret:
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid")
        if self.previous is None:
            return
        if (
            type(self.previous) is not VersionedApprovalSecret
            or self.previous.version >= self.current.version
            or hmac.compare_digest(self.previous.secret, self.current.secret)
        ):
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid")

    def key_for_version(self, version: int) -> VersionedApprovalSecret | None:
        """Return only an explicitly configured key for the exact version.
        ``key_for_version`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
        """

        if type(version) is not int:
            return None
        if version == self.current.version:
            return self.current
        if self.previous is not None and version == self.previous.version:
            return self.previous
        return None

    def digest(self, domain: str | bytes, payload: Mapping[str, object]) -> ApprovalDigest:
        """Create a new digest with the current key only.
        ``digest`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
        """

        return _approval_digest(self.current, domain, payload)

    def digest_candidates(
        self, domain: str | bytes, payload: Mapping[str, object]
    ) -> tuple[ApprovalDigest, ...]:
        """Return current then previous digests for bounded rotation lookup.
        ``digest_candidates`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。
        """

        current = _approval_digest(self.current, domain, payload)
        if self.previous is None:
            return (current,)
        return (current, _approval_digest(self.previous, domain, payload))


def canonical_approval_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize a bounded JSON object without implicit object conversion.
    输入只接受有界精确类型，不做隐式对象转换。
    """

    if not isinstance(payload, Mapping):
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    canonical = _canonical_value(payload, depth=0)
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (TypeError, ValueError, UnicodeEncodeError):
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid") from None
    if not encoded or len(encoded) > MAX_APPROVAL_CANONICAL_BYTES:
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    return encoded


def _approval_digest(
    key: VersionedApprovalSecret,
    domain: str | bytes,
    payload: Mapping[str, object],
) -> ApprovalDigest:
    """使用独立 domain 和版本化根密钥生成摘要。"""
    # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
    if type(key) is not VersionedApprovalSecret:
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    domain_bytes = _domain_bytes(domain)
    canonical = canonical_approval_bytes(payload)
    mac = hmac.new(key.secret, digestmod=hashlib.sha256)
    mac.update(domain_bytes)
    mac.update(len(canonical).to_bytes(4, byteorder="big"))
    mac.update(canonical)
    return ApprovalDigest(version=key.version, digest=mac.hexdigest())


def _secret_bytes(secret: object) -> bytes:
    """``_secret_bytes`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。"""
    # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
    if type(secret) is str:
        try:
            encoded = secret.encode("utf-8")
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except UnicodeEncodeError:
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid") from None
        text = secret
    # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
    elif type(secret) is bytes:
        encoded = secret
        try:
            text = encoded.decode("utf-8")
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except UnicodeDecodeError:
            # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
            raise ApprovalContractError("approval.hmac_key_invalid") from None
    else:
        # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
        raise ApprovalContractError("approval.hmac_key_invalid")
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in text
    ):
        # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
        raise ApprovalContractError("approval.hmac_key_invalid")
    return encoded


def _domain_bytes(domain: object) -> bytes:
    """``_domain_bytes`` 规范化审批加密输入并固定失败语义，任何密钥、票据和业务原文都不得外泄。"""
    if type(domain) is str:
        try:
            encoded = domain.encode("utf-8")
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        except UnicodeEncodeError:
            # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
            raise ApprovalContractError("approval.hmac_input_invalid") from None
    elif type(domain) is bytes:
        encoded = domain
    else:
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    if not encoded or len(encoded) > MAX_APPROVAL_DOMAIN_BYTES:
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    return encoded


def _canonical_value(value: object, *, depth: int) -> object:
    """递归规范化受支持的精确 JSON 值。"""
    if depth > 32:
        # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
        raise ApprovalContractError("approval.hmac_input_invalid")
    if value is None or type(value) in {str, bool, int}:
        if type(value) is str:
            try:
                if len(value.encode("utf-8")) > MAX_APPROVAL_CANONICAL_BYTES:
                    # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
                    raise ApprovalContractError("approval.hmac_input_invalid")
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            except UnicodeEncodeError:
                # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
                raise ApprovalContractError("approval.hmac_input_invalid") from None
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key or key in result:
                # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
                raise ApprovalContractError("approval.hmac_input_invalid")
            result[key] = _canonical_value(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_canonical_value(item, depth=depth + 1) for item in value]
    # 规范化摘要输入必须有界且类型精确；非法值统一拒绝，不能将原始内容带入异常信息。
    raise ApprovalContractError("approval.hmac_input_invalid")


__all__ = [
    "CREATE_INTENT_DOMAIN",
    "CREATE_KEY_DOMAIN",
    "CURSOR_DOMAIN",
    "DECISION_INTENT_DOMAIN",
    "DECISION_KEY_DOMAIN",
    "MAX_APPROVAL_CANONICAL_BYTES",
    "MAX_APPROVAL_DOMAIN_BYTES",
    "STEP_UP_DOMAIN",
    "TARGET_FINGERPRINT_DOMAIN",
    "ApprovalContractError",
    "ApprovalDigest",
    "ApprovalKeyring",
    "VersionedApprovalSecret",
    "canonical_approval_bytes",
]
