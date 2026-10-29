"""PRD-076 Response Cache 的 per-entry HKDF 与 owner-bound AES-256-GCM 合同。

密钥、nonce、ciphertext 和 Response 正文只存在于调用栈或专用数据库列；异常只返回固定码。
AAD 仅绑定 entry 创建时永久不可变的 source 元数据，未来 hit 的 reference/saved/billed 金额
明确不在本结构中，避免价格变化后无法重建认证数据。
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from secrets import token_bytes
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .contracts import (
    CACHE_KEY_SCHEMA_VERSION,
    MAX_CNY_MICROS,
    ResponseCacheContractError,
    typed_canonical_json_bytes,
)

# 密文格式、明文预算、GCM nonce/tag 尺寸与碰撞重试次数共同冻结数据库 wire 合同。
RESPONSE_CACHE_CIPHERTEXT_FORMAT = "response-cache-aesgcm-v1"
MAX_RESPONSE_CACHE_PLAINTEXT_BYTES = 1024 * 1024
NONCE_BYTES = 12
AES_GCM_TAG_BYTES = 16
MAX_NONCE_ATTEMPTS = 8

SourceCostStatus = Literal["known", "unavailable"]

# HKDF salt/info 和摘要正则分离 entry key 派生域，并限制数据库 AAD 摘要表示。
_HKDF_SALT = b"metahub/response-cache/hkdf/salt/v1"
_HKDF_INFO_PREFIX = b"metahub/response-cache/entry-key/v1\x00"
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ResponseCacheEntryOwner:
    """重建 entry AAD 所需的完整、不可变数据库 owner。

    source request/Usage/FinOps 身份只允许存在于数据库与此受控内存 DTO，全部从 repr 隐藏。
    """

    tenant_id: str
    entry_id: str
    cache_policy_id: str
    cache_policy_version: int
    cache_policy_generation: int
    hmac_key_version: int
    encryption_key_version: int
    cache_identity_digest: str = field(repr=False)
    cache_key_digest: str = field(repr=False)
    key_generation: int
    model_alias_id: str
    model_id: str
    provider_id: str
    route_revision_digest: str = field(repr=False)
    credential_revision_digest: str = field(repr=False)
    source_request_id: str = field(repr=False)
    source_trace_id: str = field(repr=False)
    source_usage_ledger_id: str = field(repr=False)
    source_finops_ledger_entry_id: str = field(repr=False)
    input_tokens: int
    output_tokens: int
    total_tokens: int
    source_generation_cost_status: SourceCostStatus
    source_generation_cost_cny_micros: int | None
    expires_at: str
    plaintext_byte_count: int
    key_schema_version: int = CACHE_KEY_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class EncryptedResponseCachePayload:
    """数据库可持久化的 nonce/ciphertext；两者都不进入 repr。"""

    encryption_key_version: int
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


def encrypt_response_payload(
    plaintext: object,
    *,
    secret: object,
    owner: ResponseCacheEntryOwner,
    nonce_factory: Callable[[int], bytes] = token_bytes,
    nonce_available: Callable[[bytes], bool] | None = None,
    max_nonce_attempts: int = MAX_NONCE_ATTEMPTS,
) -> EncryptedResponseCachePayload:
    """认证 JSON Response 并使用独立 per-entry key 加密。

    ``nonce_available`` 由 repository 连接数据库唯一约束前置检查；即使回调遗漏，数据库仍必须以
    ``(encryption_key_version, nonce)`` UNIQUE 作为最终防线。碰撞只重试新 nonce，
    绝不复用 key/nonce。
    """

    payload = _validated_plaintext(plaintext, owner=owner)
    nonce = _new_unique_nonce(
        nonce_factory=nonce_factory,
        nonce_available=nonce_available,
        max_attempts=max_nonce_attempts,
    )
    try:
        ciphertext = AESGCM(_entry_key(secret, owner)).encrypt(nonce, payload, entry_aad(owner))
    # 纯合同异常已经是固定安全码，不能被通用分支改写成带底层细节的异常。
    except ResponseCacheContractError:
        raise
    # 密码库异常只表达配置不可用，绝不拼接 Secret、nonce、AAD 或 Response 正文。
    except Exception:
        raise ResponseCacheContractError("response_cache.configuration_unavailable") from None
    return EncryptedResponseCachePayload(owner.encryption_key_version, nonce, ciphertext)


def decrypt_response_payload(
    encrypted: object,
    *,
    secret: object,
    owner: ResponseCacheEntryOwner,
) -> bytes:
    """验证 key version、AAD、tag、长度、UTF-8 与 JSON object 后返回栈内明文 bytes。"""

    # 只接受冻结密文 DTO，mapping/tuple 可能绕过 nonce、版本与 repr 脱敏约束。
    if not isinstance(encrypted, EncryptedResponseCachePayload):
        raise ResponseCacheContractError("response_cache.integrity_failed")
    # 密钥版本、nonce 长度和最小 GCM tag 是解密前必须成立的数据库完整性事实。
    if (
        encrypted.encryption_key_version != owner.encryption_key_version
        or type(encrypted.nonce) is not bytes
        or len(encrypted.nonce) != NONCE_BYTES
        or type(encrypted.ciphertext) is not bytes
        or len(encrypted.ciphertext) < AES_GCM_TAG_BYTES
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    try:
        plaintext = AESGCM(_entry_key(secret, owner)).decrypt(
            encrypted.nonce,
            encrypted.ciphertext,
            entry_aad(owner),
        )
    # tag、AAD、nonce 或密文任一漂移均统一视为完整性失败，不能区分给调用方。
    except (InvalidTag, ValueError, TypeError):
        raise ResponseCacheContractError("response_cache.integrity_failed") from None
    # owner/Secret 校验的固定合同异常保持原码，便于安全淘汰逻辑一致处理。
    except ResponseCacheContractError:
        raise
    # 未预期密码库错误同样不能泄露实现或输入，只允许固定完整性码。
    except Exception:
        raise ResponseCacheContractError("response_cache.integrity_failed") from None
    return _validated_plaintext(plaintext, owner=owner)


def entry_aad(owner: object) -> bytes:
    """把 entry 与 source 永久不可变元数据编码为精确 typed canonical 认证附加数据。"""

    checked = _validated_owner(owner)
    payload = {
        "ciphertext_format": RESPONSE_CACHE_CIPHERTEXT_FORMAT,
        "key_schema_version": checked.key_schema_version,
        "hmac_key_version": checked.hmac_key_version,
        "encryption_key_version": checked.encryption_key_version,
        "tenant_id": checked.tenant_id,
        "entry_id": checked.entry_id,
        "cache_policy_id": checked.cache_policy_id,
        "cache_policy_version": checked.cache_policy_version,
        "cache_policy_generation": checked.cache_policy_generation,
        "cache_identity_digest": checked.cache_identity_digest,
        "cache_key_digest": checked.cache_key_digest,
        "key_generation": checked.key_generation,
        "model_alias_id": checked.model_alias_id,
        "model_id": checked.model_id,
        "provider_id": checked.provider_id,
        "route_revision_digest": checked.route_revision_digest,
        "credential_revision_digest": checked.credential_revision_digest,
        "source_request_id": checked.source_request_id,
        "source_trace_id": checked.source_trace_id,
        "source_usage_ledger_id": checked.source_usage_ledger_id,
        "source_finops_ledger_entry_id": checked.source_finops_ledger_entry_id,
        "input_tokens": checked.input_tokens,
        "output_tokens": checked.output_tokens,
        "total_tokens": checked.total_tokens,
        "source_generation_cost_status": checked.source_generation_cost_status,
        "source_generation_cost_cny_micros": checked.source_generation_cost_cny_micros,
        "expires_at": checked.expires_at,
        "plaintext_byte_count": checked.plaintext_byte_count,
    }
    return typed_canonical_json_bytes(payload)


def validate_response_cache_secrets(
    *,
    hmac_secret: object,
    encryption_secret: object,
    control_idempotency_secret: object,
    failure_spool_secret: object,
    forbidden_secrets: tuple[object, ...] = (),
) -> None:
    """证明四个 PRD-076 根密钥彼此独立且不复用其他业务域。

    比较使用 constant-time；错误不会指出与哪个既有 Secret 相同，避免配置探测侧信道。
    """

    candidates = tuple(
        _secret_bytes(value)
        for value in (
            hmac_secret,
            encryption_secret,
            control_idempotency_secret,
            failure_spool_secret,
        )
    )
    for index, candidate in enumerate(candidates):
        # 四个 active 根域必须物理独立，不能只依赖 HKDF label 掩盖部署复用错误。
        if any(hmac.compare_digest(candidate, other) for other in candidates[index + 1 :]):
            raise ResponseCacheContractError("response_cache.configuration_unavailable")
        for forbidden in forbidden_secrets:
            try:
                forbidden_bytes = _secret_bytes(forbidden)
            # 其他域的非法或缺失值由其配置门禁负责，本函数不能借异常探测其内容。
            except ResponseCacheContractError:
                continue
            # 与 Auth/Provider/其他能力根密钥相同会扩大泄露半径，必须拒绝启动。
            if hmac.compare_digest(candidate, forbidden_bytes):
                raise ResponseCacheContractError("response_cache.configuration_unavailable")


def _entry_key(secret: object, owner: ResponseCacheEntryOwner) -> bytes:
    """按 tenant、entry 与加密版本从独立根密钥派生每条记录唯一的 256-bit AES key。"""

    checked = _validated_owner(owner)
    info = _HKDF_INFO_PREFIX + typed_canonical_json_bytes(
        {
            "tenant_id": checked.tenant_id,
            "entry_id": checked.entry_id,
            "encryption_key_version": checked.encryption_key_version,
        }
    )
    try:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=info,
        ).derive(_secret_bytes(secret))
    # Secret/owner 合同错误保留固定配置码，调用方据此旁路或失败关闭。
    except ResponseCacheContractError:
        raise
    # HKDF 实现异常不能暴露派生材料，统一收敛为配置不可用。
    except Exception:
        raise ResponseCacheContractError("response_cache.configuration_unavailable") from None


def _new_unique_nonce(
    *,
    nonce_factory: Callable[[int], bytes],
    nonce_available: Callable[[bytes], bool] | None,
    max_attempts: int,
) -> bytes:
    """有界重试 96-bit 随机 nonce；回调异常不能降级为“可用”。"""

    # 测试/数据库回调也属于安全边界，非 callable 不能退化为默认随机源或“可用”。
    if not callable(nonce_factory) or (
        nonce_available is not None and not callable(nonce_available)
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    # 重试必须严格有界，既避免一次碰撞直接失败，也防止异常回调形成无限循环。
    if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    for _ in range(max_attempts):
        try:
            nonce = nonce_factory(NONCE_BYTES)
        # 随机源故障不能改用固定 nonce，否则可能在同一 AES key 下灾难性复用。
        except Exception:
            raise ResponseCacheContractError("response_cache.configuration_unavailable") from None
        # GCM v1 只接受 96-bit bytes nonce，其他类型或长度必须在加密前拒绝。
        if type(nonce) is not bytes or len(nonce) != NONCE_BYTES:
            raise ResponseCacheContractError("response_cache.configuration_unavailable")
        if nonce_available is None:
            return nonce
        try:
            available = nonce_available(nonce)
        # 数据库唯一性探针故障不能被当作 nonce 可用，最终约束前仍应失败关闭。
        except Exception:
            raise ResponseCacheContractError("response_cache.configuration_unavailable") from None
        # 回调只接受 exact bool，避免 None/整数的真值转换掩盖存储故障。
        if type(available) is not bool:
            raise ResponseCacheContractError("response_cache.configuration_unavailable")
        if available:
            return nonce
    # 所有候选均冲突时返回固定配置码，禁止复用最后一次 nonce 继续加密。
    raise ResponseCacheContractError("response_cache.configuration_unavailable")


def _validated_plaintext(value: object, *, owner: ResponseCacheEntryOwner) -> bytes:
    """验证明文字节数、owner 镜像、严格 UTF-8 与唯一 JSON object 解释后才允许加解密。"""

    checked = _validated_owner(owner)
    # 明文只接受有界 bytes 且必须与数据库 byte count 完全相等，禁止截断后缓存。
    if (
        type(value) is not bytes
        or len(value) != checked.plaintext_byte_count
        or not 1 <= len(value) <= MAX_RESPONSE_CACHE_PLAINTEXT_BYTES
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    try:
        text = value.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    # 非 UTF-8、重复 key、非有限数或 JSON 语法错误都表示不可安全返回的缓存正文。
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ResponseCacheContractError("response_cache.integrity_failed") from None
    # 顶层数组/标量不符合 OpenAI-compatible Response object 合同，不能进入命中路径。
    if type(parsed) is not dict:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    return value


def _validated_owner(value: object) -> ResponseCacheEntryOwner:
    """验证所有 AAD owner 的类型、关系和有界表示。"""

    # AAD 只能从冻结 DTO 重建，宽松 mapping 会遗漏未来新增的不可变 owner 字段。
    if not isinstance(value, ResponseCacheEntryOwner):
        raise ResponseCacheContractError("response_cache.integrity_failed")
    for identifier in (
        value.tenant_id,
        value.entry_id,
        value.cache_policy_id,
        value.model_alias_id,
        value.model_id,
        value.provider_id,
        value.source_request_id,
        value.source_trace_id,
        value.source_usage_ledger_id,
        value.source_finops_ledger_entry_id,
    ):
        _owner_id(identifier)
    # entry/policy 前缀证明对象类型，防止把其他租户对象 ID 错绑到密文 owner。
    if not value.entry_id.startswith("rce_") or not value.cache_policy_id.startswith("rcp_"):
        raise ResponseCacheContractError("response_cache.integrity_failed")
    for digest in (
        value.cache_identity_digest,
        value.cache_key_digest,
        value.route_revision_digest,
        value.credential_revision_digest,
    ):
        # AAD 摘要只能使用数据库冻结的 64 位小写 hex，不能接受截断或大小写归一。
        if type(digest) is not str or _LOWER_HEX_64.fullmatch(digest) is None:
            raise ResponseCacheContractError("response_cache.integrity_failed")
    for number in (
        value.cache_policy_version,
        value.cache_policy_generation,
        value.hmac_key_version,
        value.encryption_key_version,
        value.key_generation,
    ):
        _positive_int(number)
    # 不同 key schema 的 AAD 不兼容，解密时不能猜测旧格式或隐式升级。
    if value.key_schema_version != CACHE_KEY_SCHEMA_VERSION:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    input_tokens = _nonnegative_int(value.input_tokens)
    output_tokens = _nonnegative_int(value.output_tokens)
    total_tokens = _nonnegative_int(value.total_tokens)
    # total 必须由 input+output 精确重建；漂移意味着 source Usage owner 已被错绑。
    if (
        input_tokens + output_tokens != total_tokens
        or input_tokens + output_tokens > MAX_CNY_MICROS
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    # known 必须带整数微元，unavailable 必须为 null，不能把缺价记录伪造为零。
    if value.source_generation_cost_status == "known":
        _nonnegative_int(value.source_generation_cost_cny_micros)
    elif (
        value.source_generation_cost_status != "unavailable"
        or value.source_generation_cost_cny_micros is not None
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    # 明文字节数既参与 AAD 又限制内存，超界值不能延迟到解密分配之后处理。
    if (
        type(value.plaintext_byte_count) is not int
        or not 1
        <= value.plaintext_byte_count
        <= MAX_RESPONSE_CACHE_PLAINTEXT_BYTES
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    _canonical_utc_second(value.expires_at)
    return value


def _owner_id(value: object) -> str:
    """AAD 身份只接受有界可打印 ASCII，避免跨语言编码漂移。"""

    # owner ID 只接受有界可打印 ASCII，避免空白折叠和跨语言 Unicode 归一差异。
    if (
        type(value) is not str
        or not 1 <= len(value) <= 160
        or value != value.strip()
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    return value


def _positive_int(value: object) -> int:
    """验证 SQLite signed-64 正整数并拒绝 bool，供版本与 generation owner 复用。"""

    # exact int 防止 true 被序列化为版本一并进入 AAD 或 key 派生信息。
    if type(value) is not int or not 1 <= value <= MAX_CNY_MICROS:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    return value


def _nonnegative_int(value: object) -> int:
    """验证 SQLite signed-64 非负整数并拒绝 bool，供 Token 与成本事实复用。"""

    # Token/金额允许零但不允许 bool、负数或超出 SQLite 可持久范围的值。
    if type(value) is not int or not 0 <= value <= MAX_CNY_MICROS:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    return value


def _canonical_utc_second(value: object) -> str:
    """要求数据库时间使用精确 ``YYYY-MM-DDTHH:MM:SSZ`` UTC 秒。"""

    # 数据库时间固定 UTC 秒精度，偏移或小数秒会让同一 owner 形成不同 AAD bytes。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    # parser 细节不能外泄，非法时间统一归入密文 owner 完整性失败。
    except ValueError:
        raise ResponseCacheContractError("response_cache.integrity_failed") from None
    # 即使 parser 接受，也必须证明零偏移与零微秒，避免宽松 ISO 表示进入 AAD。
    if (
        parsed.utcoffset() is None
        or parsed.utcoffset().total_seconds() != 0
        or parsed.microsecond != 0
    ):
        # 共享缓存合同必须保持租户身份、加密边界和统一错误稳定，不能携带敏感正文。
        raise ResponseCacheContractError("response_cache.integrity_failed")
    # round-trip 必须逐字一致，拒绝非 canonical 年月日或时区写法。
    if parsed.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        raise ResponseCacheContractError("response_cache.integrity_failed")
    return value


def _secret_bytes(value: object) -> bytes:
    """严格读取独立加密根密钥，不 trim 或隐式转换。"""

    # 不 trim Secret；不同进程若各自修复空白会派生不同 entry key，导致永久不可解密。
    if type(value) is not str or value != value.strip():
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    try:
        encoded = value.encode("utf-8", errors="strict")
    # 非法 Unicode 不能跨服务稳定派生，同样只返回固定配置错误。
    except UnicodeEncodeError:
        raise ResponseCacheContractError("response_cache.configuration_unavailable") from None
    # 纯密码合同保留 32..4096 边界；AppConfig 进一步收紧到部署允许的 512 bytes。
    if not 32 <= len(encoded) <= 4_096:
        raise ResponseCacheContractError("response_cache.configuration_unavailable")
    return encoded


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """缓存正文 JSON 也拒绝重复 key，避免 decrypt 后产生多种解释。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 重复 key 在不同 JSON parser 中可能选择首项或末项，缓存正文必须只有一种解释。
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """拒绝 Python JSON parser 默认接受的 NaN/Infinity 扩展，保持严格 JSON 合同。"""

    # 非有限常量没有跨语言统一 JSON 表示，不能在解密后被静默转换为浮点值。
    raise ValueError("non-finite JSON constant")
