"""PRD-103 Webhook keyring、receipt AES-GCM owner 绑定与配置脱敏测试。"""

from __future__ import annotations

import pytest
from metahub_shared.payment_commerce import (
    PaymentCryptoError,
    decrypt_receipt_body,
    encrypt_receipt_body,
    sign_webhook,
    validate_payment_secret,
    verify_webhook_signature,
)
from metahub_shared.runtime.config import load_app_config

CURRENT = "test-prd103-current-webhook-secret-independent-2026"
PREVIOUS = "test-prd103-previous-webhook-secret-independent-2026"
RECEIPT = "test-prd103-receipt-encryption-secret-independent-2026"


def test_webhook_current_previous_and_tamper_boundaries() -> None:
    """当前和上一 key 按声明版本验签，正文或版本篡改必须失败。"""

    body = b'{"event_id":"evt-prd103-crypto"}'
    timestamp = "1787443200"
    current_signature = sign_webhook(CURRENT, timestamp=timestamp, body=body)
    assert verify_webhook_signature(
        timestamp=timestamp,
        body=body,
        signature=current_signature,
        declared_key_version=2,
        current_key_version=2,
        current_secret=CURRENT,
        previous_key_version=1,
        previous_secret=PREVIOUS,
    ).key_version == 2
    previous_signature = sign_webhook(PREVIOUS, timestamp=timestamp, body=body)
    assert verify_webhook_signature(
        timestamp=timestamp,
        body=body,
        signature=previous_signature,
        declared_key_version=1,
        current_key_version=2,
        current_secret=CURRENT,
        previous_key_version=1,
        previous_secret=PREVIOUS,
    ).key_version == 1
    with pytest.raises(PaymentCryptoError):
        verify_webhook_signature(
            timestamp=timestamp,
            body=body + b" ",
            signature=current_signature,
            declared_key_version=2,
            current_key_version=2,
            current_secret=CURRENT,
        )
    with pytest.raises(PaymentCryptoError):
        verify_webhook_signature(
            timestamp=timestamp,
            body=body,
            signature=previous_signature,
            declared_key_version=2,
            current_key_version=2,
            current_secret=CURRENT,
            previous_key_version=1,
            previous_secret=PREVIOUS,
        )


def test_receipt_ciphertext_binds_provider_receipt_and_version() -> None:
    """AES-GCM envelope 只可由相同 provider、receipt 和 key version 解密。"""

    body = b'{"client_token":"pci-canary-only-in-encrypted-object"}'
    encrypted = encrypt_receipt_body(
        RECEIPT,
        provider="mockpay",
        receipt_id="pwr_" + "1" * 32,
        key_version=3,
        body=body,
    )
    assert body not in encrypted
    assert decrypt_receipt_body(
        RECEIPT,
        provider="mockpay",
        receipt_id="pwr_" + "1" * 32,
        key_version=3,
        envelope=encrypted,
    ) == body
    for provider, receipt_id, version in (
        ("cloudpay", "pwr_" + "1" * 32, 3),
        ("mockpay", "pwr_" + "2" * 32, 3),
        ("mockpay", "pwr_" + "1" * 32, 4),
    ):
        with pytest.raises(PaymentCryptoError):
            decrypt_receipt_body(
                RECEIPT,
                provider=provider,
                receipt_id=receipt_id,
                key_version=version,
                envelope=encrypted,
            )


def test_payment_secrets_reject_reuse_and_config_masks(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产密钥拒绝复用，运行配置 dump、repr 和摘要均不得暴露值。"""

    with pytest.raises(PaymentCryptoError):
        validate_payment_secret(
            CURRENT,
            production=True,
            forbidden_secrets=("other-independent-secret-value-2026", CURRENT),
        )
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_PAYMENT_WEBHOOK_KEY_VERSION": "2",
        "METAHUB_PAYMENT_WEBHOOK_SECRET": CURRENT,
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION": "1",
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET": PREVIOUS,
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_KEY_VERSION": "3",
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET": RECEIPT,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    config = load_app_config("control-plane-api")
    serialized = str(config.model_dump()) + repr(config)
    assert all(secret not in serialized for secret in (CURRENT, PREVIOUS, RECEIPT))
    items = {item.key: item for item in config.config_items}
    assert items["METAHUB_PAYMENT_WEBHOOK_SECRET"].status == "masked"
    assert items["METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET"].status == "masked"
    assert items["METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET"].status == "masked"
