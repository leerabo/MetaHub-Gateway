"""PRD-066 共享合同专项：RBAC、内部身份配置和 Chat Webhook 安全边界。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.webhooks import build_webhook_event

# 固定时间、Chat 对象和链路标识用于验证事件信封逐字绑定，不包含任何正文或凭据。
COMPLETED_AT = datetime(2026, 8, 2, 1, 2, 3, tzinfo=UTC)
CHAT_SESSION_ID = "chs_" + "1" * 32
CHAT_MESSAGE_ID = "chm_" + "2" * 32
GATEWAY_REQUEST_ID = "req_prd066_shared_contract"


def _success_payload() -> dict[str, object]:
    """返回不含正文和凭据的规范成功事件 payload，供变体测试复用。"""

    return {
        "schema_version": 1,
        "chat_session_id": CHAT_SESSION_ID,
        "chat_message_id": CHAT_MESSAGE_ID,
        "project_id": "prj_shared_contract",
        "environment_id": "env_shared_contract",
        "model_alias_id": "mal_shared_contract",
        "gateway_request_id": GATEWAY_REQUEST_ID,
        "status": "succeeded",
        "input_tokens": 42,
        "output_tokens": 18,
        "total_tokens": 60,
        "cache_tokens": 0,
        "cost_cny_micros": 1234,
        "policy_decision": "allow",
        "policy_reason_code": "policy.default_allow",
        "fallback_triggered": False,
        "cache_status": "not_configured",
        "error_code": None,
        "completed_at": "2026-08-02T01:02:03Z",
    }


def _build_event(payload: dict[str, object]):
    """使用与持久 Message 逐字绑定的信封构造 Chat 事件。"""

    return build_webhook_event(
        tenant_id="tenant_prd066",
        event_type="chat.message_sent",
        payload=payload,
        actor_id="user_prd066",
        object_id=CHAT_MESSAGE_ID,
        trace_id="trc_prd066_shared_contract",
        source_request_id=GATEWAY_REQUEST_ID,
        event_id=f"evt_{CHAT_MESSAGE_ID}",
        timestamp=COMPLETED_AT,
    )


def test_chat_permission_catalog_revision_and_default_role_boundaries() -> None:
    """验证权限目录边界：仅 Admin、Developer 和 Viewer 默认获得独立 chat.use。"""

    # 本回归冻结 revision 10 已发布的 64 项投影；后续领域权限按规范目录位置插入，
    # 因此必须按稳定 code 排除，不能假设新权限只追加在元组末尾。
    assert CATALOG_REVISION >= 10
    legacy_permission_codes = frozenset(PERMISSION_CODES) - {
        "experiment.read",
        "experiment.write",
        "response_cache.read",
        "response_cache.write",
        "response_cache.invalidate",
        "admin_assistant.query",
        "approval.read",
        "approval.decide",
    }
    assert len(legacy_permission_codes) == 64
    assert "chat.use" in ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert "chat.use" in ROLE_GRANTABLE_PERMISSIONS["developer"]
    assert "chat.use" in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert "chat.use" not in ROLE_GRANTABLE_PERMISSIONS["finance_admin"]
    assert "chat.use" not in ROLE_GRANTABLE_PERMISSIONS["security_admin"]
    assert "chat.use" not in ROLE_GRANTABLE_PERMISSIONS["ops_admin"]
    assert "prompt_template.use" in ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert "prompt_template.use" in ROLE_GRANTABLE_PERMISSIONS["developer"]
    assert "prompt_template.use" in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert "prompt_template.read" not in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    assert "prompt_template.write" not in ROLE_GRANTABLE_PERMISSIONS["viewer"]
    # 只投影 revision 10 的权限计算历史 hash；后续权限不能污染旧合同。
    assert permission_matrix_sha256(
        ROLE_GRANTABLE_PERMISSIONS["admin"] & legacy_permission_codes
    ) == (
        "ca6468544f0c3b9d3b52180786bc812e29e854a51e160d792a6536ba0c491262"
    )
    assert permission_matrix_sha256(
        ROLE_GRANTABLE_PERMISSIONS["developer"] & legacy_permission_codes
    ) == (
        "89e9455fe2abf258658f2571128c392cdb79d95b525980b7ac653135fd6a7b30"
    )
    assert permission_matrix_sha256(
        ROLE_GRANTABLE_PERMISSIONS["viewer"] & legacy_permission_codes
    ) == (
        "4dc0ae3ce438a1ea54159d946e0ec051eaab00b08939eeb1fd4616d9ea2dab24"
    )


def test_internal_chat_token_is_local_only_and_never_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置合同：开发有回环默认值，生产缺失保持 None，摘要和 repr 都不含明文。"""

    monkeypatch.delenv("METAHUB_INTERNAL_CHAT_TOKEN", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    dev_config = load_app_config("control-plane-api")
    assert dev_config.internal_chat_token is not None
    assert dev_config.internal_chat_token not in repr(dev_config)
    assert "internal_chat_token" not in dev_config.model_dump()
    summary = {item.key: item for item in dev_config.config_items}
    assert summary["METAHUB_INTERNAL_CHAT_TOKEN"].status == "missing"
    assert summary["METAHUB_INTERNAL_CHAT_TOKEN"].value_preview == ""

    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "https://gateway.internal.example")
    prod_config = load_app_config("control-plane-api")
    assert prod_config.internal_chat_token is None


def test_explicit_internal_chat_token_is_strict_and_control_url_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常合同：显式弱 Token 或非回环 HTTP Gateway URL 在启动阶段失败关闭。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_INTERNAL_CHAT_TOKEN", "too-short")
    with pytest.raises(ConfigError, match="METAHUB_INTERNAL_CHAT_TOKEN"):
        load_app_config("control-plane-api")

    monkeypatch.setenv(
        "METAHUB_INTERNAL_CHAT_TOKEN",
        "prd066-independent-internal-chat-token-0123456789",
    )
    monkeypatch.setenv("METAHUB_GATEWAY_API_BASE_URL", "http://gateway.internal.example")
    with pytest.raises(ConfigError, match="loopback"):
        load_app_config("control-plane-api")


def test_chat_message_sent_accepts_exact_success_and_failed_contracts() -> None:
    """主流程：成功与失败事件均使用同一持久 Message ID 和 Gateway 链路。"""

    success = _build_event(_success_payload())
    assert success.event_id == f"evt_{CHAT_MESSAGE_ID}"
    assert success.object_id == CHAT_MESSAGE_ID
    assert success.payload["status"] == "succeeded"

    failed_payload = _success_payload()
    failed_payload.update(
        {
            "status": "failed",
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cache_tokens": None,
            "cost_cny_micros": None,
            "policy_decision": None,
            "policy_reason_code": None,
            "fallback_triggered": None,
            "cache_status": None,
            "error_code": "gateway.policy_denied",
        }
    )
    failed = _build_event(failed_payload)
    assert failed.payload["error_code"] == "gateway.policy_denied"


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("content", "sensitive prompt", "webhook.payload_sensitive"),
        ("provider_key", "secret", "webhook.payload_sensitive"),
        ("total_tokens", 61, "webhook.payload_invalid"),
        ("error_code", "Provider said: bad key", "webhook.payload_invalid"),
    ],
)
def test_chat_message_sent_rejects_sensitive_or_inconsistent_payload(
    field: str,
    value: object,
    expected_error: str,
) -> None:
    """安全异常：敏感字段、自由错误正文和不可重算 Token 均整体拒绝。"""

    payload = _success_payload()
    payload[field] = value
    with pytest.raises(ValueError, match=expected_error):
        _build_event(payload)


def test_chat_message_sent_rejects_envelope_identity_drift() -> None:
    """租户/链路合同：随机事件 ID 或错误 source request 不能绑定同一 Message。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd066",
            event_type="chat.message_sent",
            payload=_success_payload(),
            actor_id="user_prd066",
            object_id=CHAT_MESSAGE_ID,
            trace_id="trc_prd066_shared_contract",
            source_request_id="req_drift",
            event_id="evt_" + "3" * 32,
            timestamp=COMPLETED_AT,
        )
