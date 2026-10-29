"""企业法定主体、证据引用和开票字段的 owner-bound AES-GCM/HMAC 合同。

根密钥与 Identity、Workspace Session、文件和 Provider Key 完全隔离。AAD 绑定 Workspace、
对象、字段与 key version，跨主体或跨字段交换密文必然认证失败；异常永不携带输入值。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from secrets import token_bytes
from typing import Final

# 公开开发密钥只允许 dev/test 使用；生产必须由独立 KMS purpose 对应的部署秘密替换。
DEFAULT_BUSINESS_VERIFICATION_ENCRYPTION_SECRET: Final[str] = (
    "dev-only-change-me-metahub-business-verification-secret-2026"
)
BUSINESS_CIPHERTEXT_PREFIX: Final[str] = "mhbv-aesgcm-v1:"
_KEY_DOMAIN: Final[bytes] = b"metahub/business-verification/key/v1/"
_BLIND_DOMAIN: Final[bytes] = b"metahub/business-verification/blind/v1/"
_OWNER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{3,128}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class BusinessVerificationCryptoError(ValueError):
    """表示固定密码学合同失败，不保存或回显导致失败的敏感值。"""


@dataclass(frozen=True, slots=True)
class BusinessFieldOwner:
    """描述密文字段的服务端权威 owner，所有值都必须从会话和数据库行恢复。"""

    workspace_id: str
    object_id: str
    field_name: str
    key_version: int


def encrypt_business_field(
    value: object,
    *,
    secret: object,
    owner: BusinessFieldOwner,
) -> str:
    """加密一个非空 UTF-8 字段并返回带格式版本的 ASCII 密文。"""

    # 明文必须是有界非空字符串，错误响应不得携带原始字段内容。
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    aad = _owner_aad(owner)
    nonce = token_bytes(12)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        encrypted = AESGCM(_derive_key(secret, owner.field_name)).encrypt(
            nonce,
            value.encode("utf-8", errors="strict"),
            aad,
        )
    # 密码学库错误与无效输入都收敛为固定错误，禁止携带 key、nonce、密文或明文。
    except BusinessVerificationCryptoError:
        raise
    except Exception:
        # 底层密码学异常统一映射为固定不可用错误，禁止暴露 nonce、密钥或实现细节。
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable") from None
    return BUSINESS_CIPHERTEXT_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode(
        "ascii"
    ).rstrip("=")


def decrypt_business_field(
    value: object,
    *,
    secret: object,
    owner: BusinessFieldOwner,
) -> str:
    """验证格式与 AAD 后解密字段，认证失败不区分密钥、owner 或数据损坏。"""

    # 只接受当前版本前缀，避免旧格式或任意文本进入解密器产生格式探测差异。
    if type(value) is not str or not value.startswith(BUSINESS_CIPHERTEXT_PREFIX):
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    encoded = value[len(BUSINESS_CIPHERTEXT_PREFIX) :]
    # Base64URL 载荷设置严格上限，防止异常密文造成无界解码和内存消耗。
    if not encoded or len(encoded) > 8192:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    try:
        raw = base64.b64decode(
            encoded + "=" * ((4 - len(encoded) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
        # AES-GCM 至少包含十二字节 nonce 和十六字节认证标签，短载荷直接失败关闭。
        if len(raw) < 28:
            raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = AESGCM(_derive_key(secret, owner.field_name)).decrypt(
            raw[:12], raw[12:], _owner_aad(owner)
        )
        return plaintext.decode("utf-8", errors="strict")
    # 所有认证与解码失败均同形，避免形成字段、密钥版本或 owner 探测侧信道。
    except BusinessVerificationCryptoError:
        raise
    except Exception:
        # 解码、认证和 UTF-8 失败保持同一错误，避免形成 owner 或密钥版本侧信道。
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid") from None


def blind_index(
    value: object,
    *,
    secret: object,
    workspace_id: object,
    field_name: object,
) -> str:
    """生成 Workspace/字段分域的 keyed blind index，数据库不保存可枚举裸 hash。"""

    normalized = _normalize_identifier(value)
    owner_workspace = _owner_id(workspace_id)
    checked_field = _field_name(field_name)
    key = _derive_key(secret, f"blind:{checked_field}")
    digest = hmac.new(
        key,
        f"{owner_workspace}\0{checked_field}\0{normalized}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def mask_sensitive_identifier(value: object) -> str:
    """返回有界末四位掩码；过短值完全隐藏，且不保留原始分隔符。"""

    normalized = _normalize_identifier(value)
    if len(normalized) <= 4:
        return "****"
    return f"****{normalized[-4:]}"


def evidence_scan_attestation(
    *,
    secret: object,
    workspace_id: object,
    evidence_type: object,
    object_reference: object,
    content_sha256: object,
    mime_type: object,
    size_bytes: object,
    kms_key_version: object,
    malware_status: object,
    dlp_status: object,
) -> str:
    """供受信上传/扫描服务签发证据描述，防止浏览器伪造扫描状态或对象 owner。"""

    payload = _evidence_attestation_payload(
        workspace_id=workspace_id,
        evidence_type=evidence_type,
        object_reference=object_reference,
        content_sha256=content_sha256,
        mime_type=mime_type,
        size_bytes=size_bytes,
        kms_key_version=kms_key_version,
        malware_status=malware_status,
        dlp_status=dlp_status,
    )
    key = _derive_key(secret, "evidence_scan_attestation")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_evidence_scan_attestation(
    attestation: object,
    **values: object,
) -> bool:
    """以常量时间验证受信扫描描述；格式错误和签名错误统一返回 False。"""

    if type(attestation) is not str or re.fullmatch(r"[0-9a-f]{64}", attestation) is None:
        return False
    secret = values.pop("secret", None)
    try:
        expected = evidence_scan_attestation(secret=secret, **values)
    # 输入字段或密钥合同失败与签名不匹配统一返回 False，调用方不能据此探测内部配置。
    except BusinessVerificationCryptoError:
        return False
    return hmac.compare_digest(attestation, expected)


def validate_business_verification_secret(
    secret: object,
    *,
    production: bool,
    forbidden_secrets: tuple[object, ...] = (),
) -> str:
    """验证独立根密钥强度、生产默认值和跨安全域复用约束。"""

    checked = _secret(secret)
    # 生产环境拒绝开发默认值和部署占位符，保证企业资料不落入公开密钥域。
    if production and (
        hmac.compare_digest(checked, DEFAULT_BUSINESS_VERIFICATION_ENCRYPTION_SECRET)
        or checked.startswith("replace-with-")
    ):
        # 生产弱密钥一律映射为固定配置错误，不回显具体命中规则或密钥内容。
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable")
    for candidate in forbidden_secrets:
        # 企业认证根密钥不得复用 Identity、Session 或其他业务安全域的秘密。
        if type(candidate) is str and hmac.compare_digest(checked, candidate):
            raise BusinessVerificationCryptoError("business_verification.crypto_unavailable")
    return checked


def _evidence_attestation_payload(**values: object) -> bytes:
    """规范化证据 attestation 字段，所有枚举和大小必须与 HTTP/数据库合同一致。"""

    workspace_id = _owner_id(values.get("workspace_id"), prefix="wsp_")
    evidence_type = values.get("evidence_type")
    object_reference = values.get("object_reference")
    content_sha256 = values.get("content_sha256")
    mime_type = values.get("mime_type")
    size_bytes = values.get("size_bytes")
    kms_key_version = values.get("kms_key_version")
    malware_status = values.get("malware_status")
    dlp_status = values.get("dlp_status")
    # 证据类型和 MIME 只允许后端合同枚举，浏览器不能发明新的扫描语义。
    if evidence_type not in {
        "business_license",
        "registration_certificate",
        "authorization_letter",
    } or mime_type not in {"application/pdf", "image/png", "image/jpeg"}:
        # 枚举越界不返回具体字段值，避免证据元数据进入异常文本。
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    # 对象 owner、摘要、大小、KMS 版本和扫描状态共同签名，任一越界都拒绝签发。
    if (
        type(object_reference) is not str
        or re.fullmatch(r"obj_[A-Za-z0-9._:-]{8,252}", object_reference) is None
        or type(content_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        or type(size_bytes) is not int
        or not 1 <= size_bytes <= 10_485_760
        or type(kms_key_version) is not int
        or not 1 <= kms_key_version <= 1_000_000
        or malware_status not in {"passed", "blocked", "pending", "failed"}
        or dlp_status not in {"passed", "blocked", "pending", "failed"}
    ):
        # 签名字段任一越界均使用同形错误，调用方不能据此探测 owner 或扫描状态。
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    return json.dumps(
        {
            "schema": "metahub-business-evidence-attestation-v1",
            "workspace_id": workspace_id,
            "evidence_type": evidence_type,
            "object_reference": object_reference,
            "content_sha256": content_sha256,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "kms_key_version": kms_key_version,
            "malware_status": malware_status,
            "dlp_status": dlp_status,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _owner_aad(owner: object) -> bytes:
    """把可信 owner 编码为稳定 ASCII JSON，禁止字典或浏览器对象自行声明 AAD。"""

    # AAD 只能由强类型服务端 owner 构造，禁止调用方传入任意字典绕过字段绑定。
    if not isinstance(owner, BusinessFieldOwner):
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    payload = {
        "schema": "metahub-business-verification-aesgcm-v1",
        "workspace_id": _owner_id(owner.workspace_id, prefix="wsp_"),
        "object_id": _owner_id(owner.object_id),
        "field_name": _field_name(owner.field_name),
        "key_version": _key_version(owner.key_version),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _derive_key(secret: object, purpose: str) -> bytes:
    """按字段或 blind-index purpose 通过 HKDF 派生独立 256-bit 子密钥。"""

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"metahub-business-verification-hkdf-v1",
            info=(
                _BLIND_DOMAIN if purpose.startswith("blind:") else _KEY_DOMAIN
            ) + purpose.encode("ascii", errors="strict"),
        ).derive(_secret(secret).encode("utf-8"))
    # 合同错误原样保留固定错误码，其他库异常统一收敛且不暴露密钥材料。
    except BusinessVerificationCryptoError:
        raise
    except Exception:
        # HKDF 加载或派生失败不能回显 purpose、根密钥或底层异常正文。
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable") from None


def _normalize_identifier(value: object) -> str:
    """规范登记号、税号或银行账号，只保留 Unicode 字母和数字并统一大写。"""

    # 只规范字符串标识，拒绝数字强转导致前导零和主体事实发生变化。
    if type(value) is not str:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).strip().upper()
        if character.isalnum()
    )
    # 规范化后重新执行边界检查，避免分隔符被移除后得到空或异常长标识。
    if not 3 <= len(normalized) <= 128:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    return normalized


def _secret(value: object) -> str:
    """校验根密钥为无空白、无控制符且 32-512 bytes 的 UTF-8 字符串。"""

    # 根密钥类型错误统一视为配置不可用，不将配置原值带入异常。
    if type(value) is not str:
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable")
    try:
        encoded = value.encode("utf-8", errors="strict")
    # 编码失败在进入派生函数前收敛，禁止原始密钥进入异常上下文。
    except UnicodeEncodeError:
        # 非法 Unicode 密钥不能进入派生函数，异常保持固定且不回显输入。
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable") from None
    # 长度、空白和控制字符约束同时阻止弱密钥与部署文件截断问题。
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        # 根密钥强度或字符约束失败只报告配置不可用，不暴露长度和命中字符。
        raise BusinessVerificationCryptoError("business_verification.crypto_unavailable")
    return value


def _owner_id(value: object, *, prefix: str | None = None) -> str:
    """校验服务端 owner 标识，拒绝空白、路径符号和非规范范围。"""

    # owner 标识只接受有界安全字符集，并可额外固定 Workspace 命名空间前缀。
    if (
        type(value) is not str
        or _OWNER_ID_RE.fullmatch(value) is None
        or (prefix is not None and not value.startswith(prefix))
    ):
        # owner 格式失败不回显标识，避免从密码学错误反向枚举业务对象。
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    return value


def _field_name(value: object) -> str:
    """校验固定小写字段域，阻止调用方用任意文本创建新密钥 purpose。"""

    # 字段名决定 HKDF purpose，必须来自固定小写命名域而不是任意调用方文本。
    if type(value) is not str or _FIELD_RE.fullmatch(value) is None:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    return value


def _key_version(value: object) -> int:
    """限制 key version 为正整数，轮换不能使用浮点或布尔歧义。"""

    # 版本号参与 AAD，布尔、浮点、零值和无界整数都会导致跨版本歧义。
    if type(value) is not int or value < 1 or value > 1_000_000:
        raise BusinessVerificationCryptoError("business_verification.crypto_invalid")
    return value
