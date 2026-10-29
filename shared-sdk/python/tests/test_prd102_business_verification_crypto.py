"""验证 PRD-102 企业认证独立加密、盲索引、掩码与扫描 attestation 合同。"""

from __future__ import annotations

import pytest
from metahub_shared.business_verification import (
    BusinessFieldOwner,
    BusinessVerificationCryptoError,
    blind_index,
    decrypt_business_field,
    encrypt_business_field,
    evidence_scan_attestation,
    mask_sensitive_identifier,
    validate_business_verification_secret,
    verify_evidence_scan_attestation,
)
from metahub_shared.runtime.config import config_loaded_log_payload, load_app_config

# 测试根密钥仅存在当前 pytest 进程，且不写日志、快照或失败消息。
SECRET = "test-prd102-business-verification-secret-independent-2026"
WORKSPACE_ID = "wsp_" + "1" * 32


def test_business_fields_bind_workspace_object_field_and_key_version() -> None:
    """验证随机 nonce 与四维 AAD，跨 Workspace、对象、字段和版本均无法解密。"""

    owner = BusinessFieldOwner(WORKSPACE_ID, "lep_" + "2" * 32, "legal_name", 1)
    first = encrypt_business_field("Example Legal Entity", secret=SECRET, owner=owner)
    second = encrypt_business_field("Example Legal Entity", secret=SECRET, owner=owner)
    assert first != second
    assert decrypt_business_field(first, secret=SECRET, owner=owner) == "Example Legal Entity"
    for changed in (
        BusinessFieldOwner("wsp_" + "3" * 32, owner.object_id, owner.field_name, 1),
        BusinessFieldOwner(WORKSPACE_ID, "lep_" + "4" * 32, owner.field_name, 1),
        BusinessFieldOwner(WORKSPACE_ID, owner.object_id, "tax_number", 1),
        BusinessFieldOwner(WORKSPACE_ID, owner.object_id, owner.field_name, 2),
    ):
        with pytest.raises(BusinessVerificationCryptoError):
            decrypt_business_field(first, secret=SECRET, owner=changed)
    assert "Example Legal Entity" not in first


def test_blind_index_mask_and_secret_independence() -> None:
    """验证 blind index 规范化且分 Workspace，生产默认值与跨域复用均被拒绝。"""

    first = blind_index(
        "CN-91 0000 1234",
        secret=SECRET,
        workspace_id=WORKSPACE_ID,
        field_name="registration_number",
    )
    normalized = blind_index(
        "cn9100001234",
        secret=SECRET,
        workspace_id=WORKSPACE_ID,
        field_name="registration_number",
    )
    other = blind_index(
        "cn9100001234",
        secret=SECRET,
        workspace_id="wsp_" + "5" * 32,
        field_name="registration_number",
    )
    assert first == normalized
    assert first != other
    assert mask_sensitive_identifier("CN-91 0000 1234") == "****1234"
    assert validate_business_verification_secret(
        SECRET, production=True, forbidden_secrets=("another-independent-secret-value-2026",)
    ) == SECRET
    with pytest.raises(BusinessVerificationCryptoError):
        validate_business_verification_secret(
            SECRET, production=True, forbidden_secrets=(SECRET,)
        )


def test_evidence_attestation_rejects_browser_scan_tampering() -> None:
    """验证证据 owner、hash、MIME、大小、KMS 和扫描状态任一变化都会使 attestation 失效。"""

    values = {
        "workspace_id": WORKSPACE_ID,
        "evidence_type": "business_license",
        "object_reference": "obj_upload-prd102-0001",
        "content_sha256": "a" * 64,
        "mime_type": "application/pdf",
        "size_bytes": 4096,
        "kms_key_version": 3,
        "malware_status": "passed",
        "dlp_status": "passed",
    }
    token = evidence_scan_attestation(secret=SECRET, **values)
    assert verify_evidence_scan_attestation(token, secret=SECRET, **values) is True
    assert verify_evidence_scan_attestation(
        token, secret=SECRET, **{**values, "dlp_status": "pending"}
    ) is False
    assert verify_evidence_scan_attestation(
        token,
        secret=SECRET,
        **{**values, "workspace_id": "wsp_" + "6" * 32},
    ) is False


def test_business_verification_config_masks_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证企业认证密钥只存在排除字段，配置摘要只报告 masked 与公开版本/TTL。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET", SECRET)
    monkeypatch.setenv("METAHUB_BUSINESS_VERIFICATION_KEY_VERSION", "7")
    monkeypatch.setenv("METAHUB_BUSINESS_VERIFICATION_DOWNLOAD_TTL_SECONDS", "90")
    config = load_app_config("control-plane-api")
    assert config.business_verification_key_version == 7
    assert config.business_verification_download_ttl_seconds == 90
    assert "business_verification_encryption_secret" not in config.model_dump()
    assert SECRET not in repr(config)
    summary = config_loaded_log_payload(config)
    secret_item = next(
        item
        for item in summary["items"]
        if item["key"] == "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET"
    )
    assert secret_item["status"] == "masked"
    assert SECRET not in repr(summary)
