"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/security.py
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from secrets import token_bytes


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """使用标准库 PBKDF2 保存管理员密码。

    当前运行时没有 bcrypt/argon2 依赖，所以 PRD-003 使用 PBKDF2-HMAC-SHA256。
    明文密码只在进程内参与哈希，不能写入数据库、日志或 seed_runs。
    """

    actual_salt = salt or token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt, 200_000)
    return "pbkdf2_sha256$200000${}${}".format(
        base64.urlsafe_b64encode(actual_salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """处理 数据库基础设施 的哈希或校验逻辑，保证调用方只保存不可逆摘要。

    安全：明文只在函数入参的短生命周期内使用，不能写入日志、数据库明文字段或响应。
    """

    algorithm, rounds, salt_b64, digest_b64 = stored_hash.split("$", 3)
    if algorithm != "pbkdf2_sha256":
        return False
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(actual, expected)


def hash_secret(secret: str) -> str:
    """处理 数据库基础设施 的哈希或校验逻辑，保证调用方只保存不可逆摘要。

    安全：明文只在函数入参的短生命周期内使用，不能写入日志、数据库明文字段或响应。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_token(token: str) -> str:
    """对访问 Token 生成不可逆哈希，避免明文 Bearer Token 落库。"""

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return hash_secret(token)


def secret_prefix(secret: str, size: int = 12) -> str:
    """只保留密钥前缀用于排障展示，不能用于认证。"""

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return secret[:size]


def secret_fingerprint(secret: str) -> str:
    """生成短指纹，便于定位 Provider Key，但不能反推明文。"""

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]


def placeholder_encrypted_secret(secret: str) -> str:
    """生成 M0 占位密文。

    PRD-003 先建立“绝不明文落库”的边界。真正 KMS/信封加密由后续安全 PRD 增强。
    这里不保存 secret 本身，只保存指纹和占位文本，避免 seed 数据泄露真实 Provider Key。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return f"local-placeholder:{secret_fingerprint(secret)}"


def encrypt_secret(secret: str, *, encryption_secret: str | None = None) -> str:
    """加密通用业务密钥。

    Webhook signing secret、Provider Key 等可解密业务密钥都通过同一 AES-GCM 边界保存；
    调用方只能保存密文、前缀和指纹，不能把明文写入日志、数据库普通字段或 API 列表响应。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return _encrypt_aesgcm_secret(secret, encryption_secret=encryption_secret)


def decrypt_secret(encrypted: str, *, encryption_secret: str | None = None) -> str:
    """解密通用业务密钥。

    该函数只在需要签名或调用上游时短暂恢复明文，失败时抛出 ValueError，
    调用方再映射为统一业务错误码。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return _decrypt_aesgcm_secret(encrypted, encryption_secret=encryption_secret)


def encrypt_provider_key(secret: str, *, encryption_secret: str | None = None) -> str:
    """加密 Provider Key。

    M0 使用本地 AES-GCM 加密串保存上游 Key，先满足“可解密调用但不明文落库”的边界。
    生产环境必须配置 METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET；后续安全 PRD 可替换为 KMS。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return encrypt_secret(secret, encryption_secret=encryption_secret)


def decrypt_provider_key(encrypted: str, *, encryption_secret: str | None = None) -> str:
    """解密 Provider Key。

    旧 seed 使用的 local-placeholder 不是可解密密文，调用方应捕获 ValueError 并跳过。
    """

    # 密钥和令牌处理只能使用哈希或密文，任何路径都不得记录明文。
    return decrypt_secret(encrypted, encryption_secret=encryption_secret)


def _encrypt_aesgcm_secret(secret: str, *, encryption_secret: str | None = None) -> str:
    """在数据库安全层内部执行 AES-GCM 加密，供不同类型的可解密密钥复用。"""

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = token_bytes(12)
    aesgcm = AESGCM(_provider_key_encryption_key(encryption_secret))
    ciphertext = aesgcm.encrypt(nonce, secret.encode("utf-8"), None)
    return f"aesgcm-v1:{_b64(nonce)}:{_b64(ciphertext)}"


def _decrypt_aesgcm_secret(encrypted: str, *, encryption_secret: str | None = None) -> str:
    """在数据库安全层内部执行 AES-GCM 解密，并统一拒绝不支持的密文格式。"""

    import binascii

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = encrypted.split(":", 2)
    if len(parts) != 3 or parts[0] != "aesgcm-v1":
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ValueError("Unsupported provider key ciphertext.")
    try:
        nonce = _unb64(parts[1])
        ciphertext = _unb64(parts[2])
        aesgcm = AESGCM(_provider_key_encryption_key(encryption_secret))
        return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")
    # 密文认证、Base64、编码或密钥参数错误都属于同一不可信密文输入边界，调用方只应
    # 接收到稳定 ValueError，不能让具体密码学异常进入 API 或结构化日志。
    except (InvalidTag, binascii.Error, UnicodeDecodeError, ValueError):
        # 密文损坏、认证标签不匹配和明文编码异常均属于同一个不可信输入边界。统一抛出
        # ValueError，避免密码学异常穿透 Gateway/Control API，同时不得回显密文片段。
        raise ValueError("Provider key ciphertext could not be decrypted.") from None


def configured_secret(name: str, default: str) -> str:
    """实现 数据库基础设施 的 configured secret 逻辑，保持职责、输入边界和错误码口径清晰。

    边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
    """

    return os.getenv(name, default)


def _provider_key_encryption_key(encryption_secret: str | None = None) -> bytes:
    """在 数据库基础设施 内部复用 provider key encryption key 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    secret = encryption_secret or os.getenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET")
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    if not secret:
        if os.getenv("METAHUB_ENV", "dev") == "prod":
            raise ValueError("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET is required in prod.")
        secret = os.getenv("METAHUB_AUTH_TOKEN_SECRET")
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    if not secret:
        raise ValueError("Provider key encryption secret is missing.")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _b64(value: bytes) -> str:
    """在 数据库基础设施 内部复用 b64 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return base64.urlsafe_b64encode(value).decode("ascii")


def _unb64(value: str) -> bytes:
    """在 数据库基础设施 内部复用 unb64 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return base64.urlsafe_b64decode(value.encode("ascii"))
