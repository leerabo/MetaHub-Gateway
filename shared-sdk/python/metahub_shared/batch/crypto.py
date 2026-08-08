"""PRD-074 批处理共享契约：统一字段、加密和状态语义，不持有租户授权或服务端业务决策。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

Owner-bound AES-GCM and domain-separated HMAC for PRD-074 Batch data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .contracts import MAX_BATCH_RESULT_BYTES

# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
BATCH_KEY_VERSION = 1
_FORMAT = "metahub-batch-aesgcm-v1"
_MAX_PAYLOAD_BYTES = 2_097_152
_PAYLOAD_KEY_DOMAIN = b"metahub/prd074/batch/payload/aesgcm/v1"
_RESULT_KEY_DOMAIN = b"metahub/prd074/batch/result/aesgcm/v1"
_PAYLOAD_HMAC_DOMAIN = b"metahub/prd074/batch/payload/hmac/v1"
_RESULT_HMAC_DOMAIN = b"metahub/prd074/batch/result/hmac/v1"
_OWNER_ID = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")


class BatchCryptoError(ValueError):
    """处理批输入加密步骤。

    定义：BatchCryptoError。
    明文只驻留内存，严禁进入日志或持久化证据。

    Fixed cryptographic availability/integrity failure.
    """


@dataclass(frozen=True, slots=True)
class BatchEnvelopeOwner:
    """处理批输入加密步骤。

    定义：BatchEnvelopeOwner。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    tenant_id: str
    batch_job_id: str
    batch_task_id: str
    batch_attempt_id: str
    request_id: str
    template_version: str
    key_version: int = BATCH_KEY_VERSION


def encrypt_batch_payload(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> str:
    """处理批输入加密步骤。

    定义：encrypt_batch_payload。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _encrypt(value, secret=secret, owner=owner, kind="payload")


def decrypt_batch_payload(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> bytes:
    """处理批输入加密步骤。

    定义：decrypt_batch_payload。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _decrypt(value, secret=secret, owner=owner, kind="payload")


def encrypt_batch_result(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> str:
    """处理批输入加密步骤。

    定义：encrypt_batch_result。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _encrypt(value, secret=secret, owner=owner, kind="result")


def decrypt_batch_result(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> str:
    """处理批输入加密步骤。

    定义：decrypt_batch_result。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    plaintext = _decrypt(value, secret=secret, owner=owner, kind="result")
    try:
        return plaintext.decode("utf-8", errors="strict")
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except UnicodeDecodeError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable") from None


def batch_payload_hmac(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> str:
    """处理批输入加密步骤。

    定义：batch_payload_hmac。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _fingerprint(value, secret=secret, owner=owner, kind="payload")


def batch_result_hmac(value: object, *, secret: object, owner: BatchEnvelopeOwner) -> str:
    """处理批输入加密步骤。

    定义：batch_result_hmac。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    return _fingerprint(value, secret=secret, owner=owner, kind="result")


def _encrypt(value: object, *, secret: object, owner: BatchEnvelopeOwner, kind: str) -> str:
    """处理批输入加密步骤。

    定义：_encrypt。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    plaintext = _plaintext(value, kind=kind)
    nonce = token_bytes(12)
    ciphertext = AESGCM(_key(secret, kind=kind, hmac_key=False)).encrypt(
        nonce, plaintext, _aad(owner, kind=kind)
    )
    return ":".join((_FORMAT, str(BATCH_KEY_VERSION), kind, _b64(nonce), _b64(ciphertext)))


def _decrypt(value: object, *, secret: object, owner: BatchEnvelopeOwner, kind: str) -> bytes:
    """处理批输入加密步骤。

    定义：_decrypt。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    if type(value) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable")
    parts = value.split(":")
    if len(parts) != 5 or parts[:3] != [_FORMAT, str(BATCH_KEY_VERSION), kind]:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable")
    try:
        nonce = _unb64(parts[3])
        ciphertext = _unb64(parts[4])
        if len(nonce) != 12 or len(ciphertext) < 16:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ValueError
        plaintext = AESGCM(_key(secret, kind=kind, hmac_key=False)).decrypt(
            nonce, ciphertext, _aad(owner, kind=kind)
        )
        return _plaintext(plaintext, kind=kind)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except (InvalidTag, binascii.Error, UnicodeEncodeError, ValueError, TypeError):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable") from None


def _fingerprint(value: object, *, secret: object, owner: BatchEnvelopeOwner, kind: str) -> str:
    """处理批输入加密步骤。

    定义：_fingerprint。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    plaintext = _plaintext(value, kind=kind)
    return hmac.new(
        _key(secret, kind=kind, hmac_key=True),
        _aad(owner, kind=kind) + b"\x00" + plaintext,
        hashlib.sha256,
    ).hexdigest()


def _plaintext(value: object, *, kind: str) -> bytes:
    """处理批输入加密步骤。

    定义：_plaintext。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    if type(value) is str:
        try:
            plaintext = value.encode("utf-8", errors="strict")
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        except UnicodeEncodeError:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise BatchCryptoError("batch.result_unavailable") from None
    elif type(value) is bytes:
        plaintext = value
    else:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable")
    maximum = _MAX_PAYLOAD_BYTES if kind == "payload" else MAX_BATCH_RESULT_BYTES
    if not plaintext or len(plaintext) > maximum:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable")
    return plaintext


def _aad(owner: BatchEnvelopeOwner, *, kind: str) -> bytes:
    """处理批输入加密步骤。

    定义：_aad。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    if (
        not isinstance(owner, BatchEnvelopeOwner)
        or owner.key_version != BATCH_KEY_VERSION
        or any(
            type(value) is not str or _OWNER_ID.fullmatch(value) is None
            for value in (
                owner.tenant_id,
                owner.batch_job_id,
                owner.batch_task_id,
                owner.batch_attempt_id,
                owner.request_id,
                owner.template_version,
            )
        )
    ):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.result_unavailable")
    return json.dumps(
        [
            "metahub.batch/v1",
            kind,
            owner.tenant_id,
            owner.batch_job_id,
            owner.batch_task_id,
            owner.batch_attempt_id,
            owner.request_id,
            owner.template_version,
            owner.key_version,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _key(secret: object, *, kind: str, hmac_key: bool) -> bytes:
    """处理批输入加密步骤。

    定义：_key。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    # 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
    if type(secret) is not str:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.worker_unavailable")
    try:
        raw = secret.encode("utf-8", errors="strict")
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except UnicodeEncodeError:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.worker_unavailable") from None
    if len(raw) < 32 or len(raw) > 512:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise BatchCryptoError("batch.worker_unavailable")
    if kind == "payload":
        domain = _PAYLOAD_HMAC_DOMAIN if hmac_key else _PAYLOAD_KEY_DOMAIN
    else:
        domain = _RESULT_HMAC_DOMAIN if hmac_key else _RESULT_KEY_DOMAIN
    return hmac.new(raw, domain, hashlib.sha256).digest()


def _b64(value: bytes) -> str:
    """处理批输入加密步骤。

    定义：_b64。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    """处理批输入加密步骤。

    定义：_unb64。
    明文只驻留内存，严禁进入日志或持久化证据。
    """
    if type(value) is not str or not value:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise binascii.Error
    decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    if _b64(decoded) != value:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise binascii.Error
    return decoded
