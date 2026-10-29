"""支付回调使用独立 HMAC 与 AES-GCM 域，禁止复用身份或客户资料密钥。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DEFAULT_PAYMENT_WEBHOOK_SECRET = (
    "metahub-local-payment-webhook-secret-change-before-production-2026"
)
DEFAULT_PAYMENT_RECEIPT_ENCRYPTION_SECRET = (
    "metahub-local-payment-receipt-encryption-secret-change-before-production-2026"
)
_MAX_SECRET_BYTES = 512
_MIN_SECRET_BYTES = 32
_NONCE_BYTES = 12
_FORMAT_PREFIX = "mhpay1."


class PaymentCryptoError(ValueError):
    """把密钥、签名和密文错误折叠为固定类型，调用方不得记录输入值。"""


@dataclass(frozen=True, slots=True)
class VerifiedWebhookKey:
    """只返回匹配的部署 key version，不返回或保留签名密钥。"""

    key_version: int


def validate_payment_secret(
    value: str,
    *,
    production: bool,
    forbidden_secrets: tuple[str | None, ...] = (),
) -> str:
    """校验支付密钥强度和域隔离，比较结果不泄露冲突对象。"""

    if type(value) is not str:
        raise PaymentCryptoError("payment secret invalid")
    encoded = value.encode("utf-8")
    if not _MIN_SECRET_BYTES <= len(encoded) <= _MAX_SECRET_BYTES:
        raise PaymentCryptoError("payment secret invalid")
    if production and value in {
        DEFAULT_PAYMENT_WEBHOOK_SECRET,
        DEFAULT_PAYMENT_RECEIPT_ENCRYPTION_SECRET,
    }:
        raise PaymentCryptoError("payment secret invalid")
    # 比较采用常量时间；任何复用都只返回统一错误，避免形成密钥域探针。
    for forbidden in forbidden_secrets:
        if forbidden and hmac.compare_digest(value, forbidden):
            raise PaymentCryptoError("payment secret invalid")
    return value


def sign_webhook(secret: str, *, timestamp: str, body: bytes) -> str:
    """按不可歧义长度前缀构造签名消息，避免时间戳和正文拼接碰撞。"""

    validate_payment_secret(secret, production=False)
    if type(timestamp) is not str or not timestamp.isdigit() or type(body) is not bytes:
        raise PaymentCryptoError("payment signature input invalid")
    message = _signature_message(timestamp, body)
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    *,
    timestamp: str,
    body: bytes,
    signature: str,
    declared_key_version: int,
    current_key_version: int,
    current_secret: str,
    previous_key_version: int | None = None,
    previous_secret: str | None = None,
) -> VerifiedWebhookKey:
    """只在声明版本对应的当前或上一密钥上验签，拒绝跨版本试探。"""

    if type(signature) is not str or len(signature) != 64:
        raise PaymentCryptoError("payment signature invalid")
    try:
        bytes.fromhex(signature)
    except ValueError:
        raise PaymentCryptoError("payment signature invalid") from None
    candidates: tuple[tuple[int, str], ...] = ((current_key_version, current_secret),)
    if previous_key_version is not None and previous_secret is not None:
        candidates += ((previous_key_version, previous_secret),)
    for version, secret in candidates:
        if declared_key_version != version:
            continue
        expected = sign_webhook(secret, timestamp=timestamp, body=body)
        if hmac.compare_digest(expected, signature.lower()):
            return VerifiedWebhookKey(key_version=version)
    raise PaymentCryptoError("payment signature invalid")


def encrypt_receipt_body(
    secret: str,
    *,
    provider: str,
    receipt_id: str,
    key_version: int,
    body: bytes,
) -> bytes:
    """以 provider、receipt 和版本作为 AAD，把原始回调绑定到唯一对象。"""

    if not body:
        raise PaymentCryptoError("payment receipt body invalid")
    aad = _aad(provider, receipt_id, key_version)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(_derive_key(secret, key_version)).encrypt(nonce, body, aad)
    envelope = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return (_FORMAT_PREFIX + envelope).encode("ascii")


def decrypt_receipt_body(
    secret: str,
    *,
    provider: str,
    receipt_id: str,
    key_version: int,
    envelope: bytes,
) -> bytes:
    """解密时重新绑定对象 owner，复制或改名后的密文必须失败关闭。"""

    try:
        text = envelope.decode("ascii")
        if not text.startswith(_FORMAT_PREFIX):
            raise ValueError("format")
        encoded = text[len(_FORMAT_PREFIX) :]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) <= _NONCE_BYTES + 16:
            raise ValueError("length")
        return AESGCM(_derive_key(secret, key_version)).decrypt(
            raw[:_NONCE_BYTES],
            raw[_NONCE_BYTES:],
            _aad(provider, receipt_id, key_version),
        )
    except (UnicodeDecodeError, ValueError, InvalidTag):
        raise PaymentCryptoError("payment receipt decrypt failed") from None


def _derive_key(secret: str, key_version: int) -> bytes:
    """HKDF info 固定到支付 receipt 域和版本，防止跨用途解密。"""

    validate_payment_secret(secret, production=False)
    if type(key_version) is not int or key_version < 1:
        raise PaymentCryptoError("payment key version invalid")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"metahub-payment-receipt-v1",
        info=f"receipt:{key_version}".encode("ascii"),
    ).derive(secret.encode("utf-8"))


def _signature_message(timestamp: str, body: bytes) -> bytes:
    """签名编码包含长度，确保不同字段组合不能产生相同字节序列。"""

    stamp = timestamp.encode("ascii")
    return len(stamp).to_bytes(2, "big") + stamp + len(body).to_bytes(8, "big") + body


def _aad(provider: str, receipt_id: str, key_version: int) -> bytes:
    """AAD 只含非敏感 owner 元数据，可用于拒绝对象替换。"""

    if type(provider) is not str or type(receipt_id) is not str:
        raise PaymentCryptoError("payment receipt owner invalid")
    return f"metahub-payment-receipt-v1\0{provider}\0{receipt_id}\0{key_version}".encode(
        "ascii"
    )
