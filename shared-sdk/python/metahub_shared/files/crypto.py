"""文件名、原文件和抽取正文的分域 AES-GCM/HMAC 合同。

文件根密钥与 Auth、Provider Key、Prompt 模板完全独立。密文 AAD 绑定 tenant、文件、抽取
对象和 blob kind，跨租户或跨字段交换必然认证失败；异常从不携带密钥、正文或密文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from secrets import token_bytes
from typing import Final, Literal

from .contracts import FileContractError

FileBlobKind = Literal["filename", "original", "extract"]

# 密文前缀、key version 和分域标签共同构成持久合同，轮换必须新增版本而不能静默改义。
FILE_CIPHERTEXT_FORMAT: Final[str] = "metahub-file-aesgcm-v1"
FILE_KEY_VERSION: Final[str] = "v1"
FILE_CIPHERTEXT_PREFIX: Final[bytes] = b"mhfile-aesgcm-v1\x00"
DEFAULT_FILE_ENCRYPTION_SECRET: Final[str] = (
    "dev-only-change-me-metahub-file-encryption-secret-2026"
)
_KEY_DOMAIN: Final[bytes] = b"metahub/file/key/v1/"
_FINGERPRINT_DOMAIN: Final[bytes] = b"metahub/file/fingerprint/v1/"
_HEX_FINGERPRINT = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class FileBlobOwner:
    """生成文件密文 AAD 所需的可信 owner。

    所有字段必须来自认证上下文和同 tenant 数据库行，不能从浏览器 JSON 恢复。抽取 ID
    仅用于 extract blob；content_fingerprint 是 keyed 摘要，不是公共 SHA-256。
    """

    tenant_id: str
    file_id: str
    blob_kind: FileBlobKind
    content_fingerprint: str
    file_extract_id: str | None = None
    extractor_version: str | None = None


def file_content_fingerprint(
    content: object,
    *,
    secret: object,
    tenant_id: object,
    file_id: object,
    blob_kind: FileBlobKind,
) -> str:
    """为文件内容生成 tenant/file/kind 分域 keyed fingerprint。

    参数：正文 bytes、独立根密钥和可信 owner。返回：带算法前缀的 HMAC。关键约束：
    不保存裸 hash，公共文档不能跨 tenant 直接比较；输入正文不进入异常或 repr。
    """

    if type(content) is not bytes:
        # 指纹只接受调用栈内原始 bytes，隐式转换会改变摘要语义并可能泄露对象表示。
        raise FileContractError("file.integrity_failed")
    owner = _fingerprint_owner_bytes(tenant_id, file_id, blob_kind)
    key = _derive_key(secret, _FINGERPRINT_DOMAIN + blob_kind.encode("ascii"))
    digest = hmac.new(key, owner + b"\x00" + content, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def encrypt_file_blob(content: object, *, secret: object, owner: FileBlobOwner) -> bytes:
    """使用 owner-bound AES-GCM 加密文件字段或正文。

    参数：明文 bytes、独立根密钥和可信 owner。返回：带版本前缀的二进制密文。
    关键约束：随机 nonce 每次不同；调用方只能持久化返回值，不能保存明文副本。
    """

    if type(content) is not bytes:
        # 加密入口禁止自行编码字符串，正文编码必须由上层格式合同唯一确定。
        raise FileContractError("file.integrity_failed")
    aad = _owner_aad(owner)
    key = _derive_key(secret, _KEY_DOMAIN + owner.blob_kind.encode("ascii"))
    nonce = token_bytes(12)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        encrypted = AESGCM(key).encrypt(nonce, content, aad)
    # 固定合同错误保持原码，其余密码学实现错误统一折叠为存储依赖不可用。
    except FileContractError:
        raise
    except Exception:
        # 密码学异常不能携带 key、nonce 或明文进入上层诊断。
        raise FileContractError("file.storage_unavailable") from None
    return FILE_CIPHERTEXT_PREFIX + nonce + encrypted


def decrypt_file_blob(value: object, *, secret: object, owner: FileBlobOwner) -> bytes:
    """认证并解密一个精确 owner 的文件密文。

    参数：持久密文、独立根密钥和数据库 owner。返回：调用栈内明文 bytes。格式、AAD、
    nonce、tag 或密钥任一不一致都映射为完整性错误，不区分密码学细节。
    """

    minimum_size = len(FILE_CIPHERTEXT_PREFIX) + 12 + 16
    if type(value) is not bytes or len(value) < minimum_size:
        # 过短或非 bytes 的值不可能包含完整 nonce/tag，按 owner 完整性失败处理。
        raise FileContractError("file.integrity_failed")
    if not value.startswith(FILE_CIPHERTEXT_PREFIX):
        # 未知密文版本不能尝试猜测解码，否则轮换期间会形成双重解释。
        raise FileContractError("file.integrity_failed")
    offset = len(FILE_CIPHERTEXT_PREFIX)
    nonce = value[offset : offset + 12]
    encrypted = value[offset + 12 :]
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(
            _derive_key(secret, _KEY_DOMAIN + owner.blob_kind.encode("ascii"))
        ).decrypt(nonce, encrypted, _owner_aad(owner))
    # owner 或密钥合同错误保持固定语义，不能被第三方异常重新分类。
    except FileContractError:
        raise
    except Exception:
        # InvalidTag 与格式损坏使用同一固定码，避免形成密钥探测侧信道。
        raise FileContractError("file.integrity_failed") from None


def validate_file_secret(
    secret: object,
    *,
    production: bool,
    forbidden_secrets: tuple[object, ...] = (),
) -> bytes:
    """验证文件独立根密钥的强度、生产默认值和跨域复用。

    参数：候选 secret、生产标志和其他业务密钥。返回：UTF-8 bytes。关键约束：比较使用
    constant-time；错误只返回文件存储不可用，不说明具体哪个密钥相同。
    """

    secret_bytes = _secret_bytes(secret)
    if production and hmac.compare_digest(
        secret_bytes, DEFAULT_FILE_ENCRYPTION_SECRET.encode("utf-8")
    ):
        # 仓库公开开发密钥绝不能用于生产企业文件加密。
        raise FileContractError("file.storage_unavailable")
    for forbidden in forbidden_secrets:
        try:
            forbidden_bytes = _secret_bytes(forbidden)
        # 未配置或不合法的其他域只表示无法比较，不影响候选文件密钥自身校验。
        except FileContractError:
            # 未配置的其他业务域不参与相等比较，文件 secret 自身仍已完成强度校验。
            continue
        if hmac.compare_digest(secret_bytes, forbidden_bytes):
            # 根密钥跨业务域复用会破坏隔离，即使 HKDF 标签不同也必须拒绝。
            raise FileContractError("file.storage_unavailable")
    # 返回仅供当前密码学调用使用的 bytes，不得写入配置摘要、日志或响应。
    return secret_bytes


def _owner_aad(owner: object) -> bytes:
    """把可信 owner 编码为稳定 ASCII JSON AAD，绑定租户、文件和字段语义。"""

    if not isinstance(owner, FileBlobOwner):
        # AAD 只能由服务端可信数据结构生成，禁止接收浏览器字典或任意对象。
        raise FileContractError("file.integrity_failed")
    _validate_owner_id(owner.tenant_id, maximum=128)
    _validate_owner_id(owner.file_id, prefix="file_", maximum=37)
    if owner.blob_kind not in {"filename", "original", "extract"}:
        # 未登记字段域没有密钥和持久化合同，不能借默认分支继续加解密。
        raise FileContractError("file.integrity_failed")
    if _HEX_FINGERPRINT.fullmatch(owner.content_fingerprint) is None:
        # AAD 必须绑定分域 HMAC，裸 hash 或任意文本不能充当内容 owner。
        raise FileContractError("file.integrity_failed")
    if owner.blob_kind == "extract":
        _validate_owner_id(owner.file_extract_id, prefix="fext_", maximum=37)
        if type(owner.extractor_version) is not str or not owner.extractor_version:
            # 抽取密文必须绑定解析器版本，防止不同解释器结果之间交换密文。
            raise FileContractError("file.integrity_failed")
    elif owner.file_extract_id is not None or owner.extractor_version is not None:
        # filename/original 不得偷偷携带 extract owner，防止同一明文产生多套 AAD 表示。
        raise FileContractError("file.integrity_failed")
    payload = {
        "schema": FILE_CIPHERTEXT_FORMAT,
        "key_version": FILE_KEY_VERSION,
        "tenant_id": owner.tenant_id,
        "file_id": owner.file_id,
        "file_extract_id": owner.file_extract_id,
        "blob_kind": owner.blob_kind,
        "content_fingerprint": owner.content_fingerprint,
        "extractor_version": owner.extractor_version,
    }
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _fingerprint_owner_bytes(
    tenant_id: object,
    file_id: object,
    blob_kind: object,
) -> bytes:
    """编码 fingerprint 的非敏感 owner，避免跨 tenant/kind 复用摘要。"""

    _validate_owner_id(tenant_id, maximum=128)
    _validate_owner_id(file_id, prefix="file_", maximum=37)
    if blob_kind not in {"filename", "original", "extract"}:
        # 摘要域必须与密文字段域一致，未知 kind 不允许共享默认 HMAC 域。
        raise FileContractError("file.integrity_failed")
    return json.dumps(
        [tenant_id, file_id, blob_kind],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _validate_owner_id(
    value: object,
    *,
    prefix: str | None = None,
    maximum: int,
) -> None:
    """校验 AAD owner ID 的类型、长度和可打印 ASCII 边界。"""

    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or not value.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or (prefix is not None and not value.startswith(prefix))
    ):
        # AAD owner 只允许服务端生成的有界可打印标识，避免编码歧义和跨对象交换。
        raise FileContractError("file.integrity_failed")


def _derive_key(secret: object, domain: bytes) -> bytes:
    """用 HKDF-SHA256 为指定业务域派生 256-bit key，禁止不同文件字段共用子密钥。"""

    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"metahub-file-hkdf-salt-v1",
            info=domain,
        ).derive(_secret_bytes(secret))
    # 根密钥合同错误保持原码，密码学库故障则统一视为文件存储不可用。
    except FileContractError:
        raise
    except Exception:
        # 不把第三方异常文本、算法对象或密钥材料带入上层诊断。
        raise FileContractError("file.storage_unavailable") from None


def _secret_bytes(secret: object) -> bytes:
    """把独立根密钥规范为有界 UTF-8 bytes，并拒绝空白和弱值。"""

    # 禁止对 bytes 或任意对象隐式转换，部署密钥必须来自明确字符串配置。
    if type(secret) is not str:
        raise FileContractError("file.storage_unavailable")
    encoded = secret.encode("utf-8")
    # 首尾空白会造成多副本配置漂移，过短或无界根密钥也不能进入 KDF。
    if secret != secret.strip() or len(encoded) < 32 or len(encoded) > 4_096:
        raise FileContractError("file.storage_unavailable")
    return encoded
