"""共享密钥加解密边界测试。

测试只使用进程内固定假密钥，不输出密文或明文；重点验证损坏输入始终收敛为调用方
约定的 ``ValueError``，避免密码学实现异常穿透 Gateway 和 Control API。
"""

from __future__ import annotations

import base64

import pytest
from metahub_shared.db.security import decrypt_provider_key, encrypt_provider_key, verify_password

# 测试加密主密钥只存在当前进程，用于验证合同，禁止复用到任何本地或生产数据库。
ENCRYPTION_SECRET = "test-only-provider-encryption-secret"


@pytest.mark.parametrize("parallelism", (2, 4))
def test_existing_argon2_password_profiles(parallelism: int) -> None:
    from argon2 import PasswordHasher

    stored = PasswordHasher(parallelism=parallelism).hash("test-only-password")
    assert verify_password("test-only-password", stored)
    assert not verify_password("wrong-password", stored)


def test_unbounded_argon2_parameters_are_rejected_before_verification(monkeypatch) -> None:
    from argon2 import PasswordHasher

    stored = PasswordHasher(parallelism=4).hash("test-only-password")

    def unexpected_verify(*_args):
        pytest.fail("Unbounded parameters must never reach the password KDF")

    monkeypatch.setattr(PasswordHasher, "verify", unexpected_verify)
    for parameter in ("m=131072,t=3,p=4", "m=65536,t=6,p=4", "m=65536,t=3,p=8"):
        assert not verify_password("test-only-password", stored.replace("m=65536,t=3,p=4", parameter))


def test_provider_key_ciphertext_round_trip() -> None:
    """有效 AES-GCM 密文应只在显式解密边界恢复原始 Provider Key。"""

    encrypted = encrypt_provider_key("test-provider-key", encryption_secret=ENCRYPTION_SECRET)

    assert (
        decrypt_provider_key(encrypted, encryption_secret=ENCRYPTION_SECRET) == "test-provider-key"
    )


def test_provider_key_invalid_tag_is_normalized_to_value_error() -> None:
    """认证标签被篡改时必须固定抛 ValueError，不泄漏 InvalidTag 或密文内容。"""

    encrypted = encrypt_provider_key("test-provider-key", encryption_secret=ENCRYPTION_SECRET)
    version, nonce, ciphertext = encrypted.split(":", 2)
    ciphertext_bytes = bytearray(base64.urlsafe_b64decode(ciphertext.encode("ascii")))
    ciphertext_bytes[-1] ^= 1
    tampered = ":".join(
        (version, nonce, base64.urlsafe_b64encode(bytes(ciphertext_bytes)).decode("ascii"))
    )

    with pytest.raises(ValueError, match="could not be decrypted") as caught:
        decrypt_provider_key(tampered, encryption_secret=ENCRYPTION_SECRET)

    assert tampered not in str(caught.value)


@pytest.mark.parametrize(
    "encrypted",
    (
        "local-placeholder:test",
        "aesgcm-v1:invalid:invalid",
        "aesgcm-v1::",
    ),
)
def test_provider_key_malformed_ciphertext_is_normalized(encrypted: str) -> None:
    """旧占位符与结构损坏密文均应落入同一安全调用方合同。"""

    with pytest.raises(ValueError):
        decrypt_provider_key(encrypted, encryption_secret=ENCRYPTION_SECRET)
