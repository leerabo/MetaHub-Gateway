"""PRD-056 Shared 错误注册、Webhook 与运行配置集成合同测试。"""

from __future__ import annotations

import pytest
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 错误码集合同时冻结 HTTP 状态与可重试属性，防止控制面、网关和诊断文案出现合同漂移。
ERROR_MAP_CODES = {
    "error_map.not_found": (404, False),
    "error_map.input_invalid": (422, False),
    "error_map.update_empty": (422, False),
    "error_map.output_code_invalid": (422, False),
    "error_map.pattern_invalid": (422, False),
    "error_map.pattern_sensitive": (422, False),
    "error_map.conflict": (409, False),
    "error_map.version_conflict": (409, False),
    "error_map.request_conflict": (409, False),
    "error_map.limit_exceeded": (409, False),
    "error_map.unavailable": (503, True),
    "error_map.snapshot_unavailable": (503, True),
    "error_map.event_unavailable": (503, True),
    "error_map.tenant_unavailable": (404, False),
    "gateway.upstream_resource_not_found": (502, False),
}


def _event_payload(**overrides: object) -> dict[str, object]:
    """构造不含 pattern 与上游正文的精确错误映射事件摘要。"""

    payload: dict[str, object] = {
        "schema_version": 1,
        "operation": "update",
        "provider_error_map_id": "pem_0123456789abcdef0123456789abcdef",
        "rule_version": 2,
        "head_revision": 3,
        "provider_scope": "all",
        "status": "active",
        "priority": 500,
        "output_error_code": "gateway.upstream_quota_exceeded",
        "pattern_hash": "a" * 64,
        "request_id": "request-prd056-webhook",
    }
    payload.update(overrides)
    return payload


def _build_event(payload: dict[str, object] | None = None, **overrides: object):
    """以完整 Source Outbox 身份构造 error_map.updated Webhook，并保持信封与载荷关联字段一致。"""

    arguments: dict[str, object] = {
        "tenant_id": "tenant-a",
        "event_type": "error_map.updated",
        "payload": payload or _event_payload(),
        "actor_id": "user-a",
        "object_id": "pem_0123456789abcdef0123456789abcdef",
        "trace_id": "trace-prd056-webhook",
        "source_request_id": "request-prd056-webhook",
        "event_id": "pemevt_0123456789abcdef0123456789abcdef",
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def _prepare_config_test(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """隔离配置文件和环境变量，保证测试不读取工作机外部状态。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.delenv("METAHUB_INTERNAL_CONFIG_TOKEN", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_SERVICE_MODE", "internal_only")
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "false")
    for key in (
        "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED",
        "METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS",
        "METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS",
        "METAHUB_PROVIDER_ERROR_MAP_REFRESH_QUEUE_TIMEOUT_SECONDS",
        "METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS",
        "METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS",
        "METAHUB_PROVIDER_ERROR_MAP_MAX_PAYLOAD_BYTES",
        "METAHUB_PROVIDER_ERROR_MAP_REFRESH_CONCURRENCY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_error_map_error_registry_labels_and_diagnosis_are_complete() -> None:
    """统一错误目录必须同时提供状态、重试语义、中英文标签和静态诊断。"""

    for code, (http_status, retryable) in ERROR_MAP_CODES.items():
        spec = ERROR_SPECS[code]
        assert spec.http_status == http_status
        assert spec.retryable is retryable
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
        diagnosis = diagnose_error(code, http_status=http_status)
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.safe_summary
        assert code not in diagnosis.reason


def test_error_map_updated_webhook_accepts_only_exact_persistent_contract() -> None:
    """主流程：事件必须绑定持久 pemevt、租户、actor、map 对象和 trace。"""

    assert "error_map.updated" in WEBHOOK_EVENT_TYPES
    assert "error_map.updated" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES

    event = _build_event()

    assert event.event_id.startswith("pemevt_")
    assert event.object_id == event.payload["provider_error_map_id"]
    assert set(event.payload) == set(_event_payload())
    assert "pattern" not in event.payload


@pytest.mark.parametrize(
    "payload",
    [
        _event_payload(pattern="do-not-persist"),
        _event_payload(output_error_code="gateway.unknown"),
        _event_payload(output_error_code="gateway.upstream_auth_failed"),
        _event_payload(operation={"unsafe": "shape"}),
        _event_payload(status=["active"]),
        _event_payload(operation="enable", status="disabled"),
        _event_payload(priority=True),
        _event_payload(pattern_hash="A" * 64),
        _event_payload(provider_scope="provider with spaces"),
        _event_payload(request_id="bad\nrequest"),
    ],
)
def test_error_map_updated_webhook_rejects_invalid_or_sensitive_shapes(
    payload: dict[str, object],
) -> None:
    """异常流程：额外 pattern、未知枚举、类型混淆和控制字符都必须失败关闭。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build_event(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_0123456789abcdef0123456789abcdef"},
        {"tenant_id": ""},
        {"actor_id": None},
        {"object_id": "pem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"trace_id": "bad trace"},
        {"source_request_id": "request-other"},
    ],
)
def test_error_map_updated_webhook_rejects_unbound_envelope(
    overrides: dict[str, object],
) -> None:
    """身份流程：任何 source 信封漂移都不能退化为随机事件或跨资源投递。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build_event(**overrides)


def test_error_map_updated_webhook_checks_4096_bytes_before_shape() -> None:
    """资源边界：超大 payload 在通用递归复制前即被固定错误拒绝。"""

    payload = _event_payload(unexpected="x" * 5000)

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build_event(payload)


def test_provider_error_map_config_defaults_are_bounded_and_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """兼容流程：旧部署默认使用 builtin，且所有缓存资源上限与设计值一致。"""

    _prepare_config_test(monkeypatch, tmp_path)
    config = load_app_config("gateway-data-plane")

    assert config.provider_error_map_sync_enabled is False
    assert config.provider_error_map_poll_interval_seconds == 2
    assert config.provider_error_map_http_timeout_seconds == 2.0
    assert config.provider_error_map_refresh_queue_timeout_seconds == 2.0
    assert config.provider_error_map_fresh_seconds == 300.0
    assert config.provider_error_map_lkg_seconds == 3600.0
    assert config.provider_error_map_max_payload_bytes == 262_144
    assert config.provider_error_map_refresh_concurrency == 4
    assert config.features["provider_error_map_sync_enabled"] is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS", "0"),
        ("METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS", "301"),
        ("METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS", "0.09"),
        ("METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS", "nan"),
        ("METAHUB_PROVIDER_ERROR_MAP_REFRESH_QUEUE_TIMEOUT_SECONDS", "10.1"),
        ("METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS", "86401"),
        ("METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS", "604801"),
        ("METAHUB_PROVIDER_ERROR_MAP_MAX_PAYLOAD_BYTES", "4095"),
        ("METAHUB_PROVIDER_ERROR_MAP_REFRESH_CONCURRENCY", "33"),
    ],
)
def test_provider_error_map_config_rejects_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    key: str,
    value: str,
) -> None:
    """异常流程：无效 deadline、窗口、正文或并发配置必须在服务启动前拒绝。"""

    _prepare_config_test(monkeypatch, tmp_path)
    monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError) as error:
        load_app_config("gateway-data-plane")

    assert error.value.code == "infra.config_invalid"
    assert key in error.value.message


def test_provider_error_map_config_enforces_lkg_ordering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """状态流程：LKG 必须覆盖 fresh 窗口，避免同一快照出现反向生命周期。"""

    _prepare_config_test(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS", "61.5")
    monkeypatch.setenv("METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS", "61.4")

    with pytest.raises(ConfigError) as error:
        load_app_config("gateway-data-plane")

    assert error.value.code == "infra.config_invalid"
    assert "METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS" in error.value.message


def test_provider_error_map_sync_requires_token_and_safe_control_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """服务身份：同步 Token 必须是可写入请求头的高熵 ASCII，非 TLS HTTP 仅允许字面量回环地址。"""

    _prepare_config_test(monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED", "true")
    with pytest.raises(ConfigError) as missing:
        load_app_config("gateway-data-plane")
    assert missing.value.code == "infra.config_missing"
    # 非 ASCII 值即使 UTF-8 字节数足够，也无法写入 httpx Header，必须在启动时拒绝。
    non_ascii_token = "\u00e9" * 32
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", non_ascii_token)
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://127.0.0.1:8001")
    with pytest.raises(ConfigError) as non_ascii:
        load_app_config("gateway-data-plane")
    assert non_ascii.value.code == "infra.config_invalid"
    assert "ASCII" in non_ascii.value.message


    token = "provider-error-map-sync-token-0123456789abcdef"
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", token)
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://control.internal.example")
    with pytest.raises(ConfigError) as unsafe_url:
        load_app_config("gateway-data-plane")
    assert unsafe_url.value.code == "infra.config_invalid"
    assert token not in unsafe_url.value.message

    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://127.0.0.1:8001")
    config = load_app_config("gateway-data-plane")
    assert config.provider_error_map_sync_enabled is True
    assert token not in str(config.model_dump())



