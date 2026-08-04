"""PRD-053 成本异常配置、错误码与 Webhook 的 Shared 聚焦合同测试。

测试只使用固定虚构 ID 和整数微元，不包含 Provider Key、Token、Prompt、Response、
真实租户名称或真实请求正文；拒绝敏感键的用例也只使用无业务含义的占位值。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from metahub_shared.runtime.config import ConfigError, DEFAULT_CONFIG, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    WebhookEvent,
    build_webhook_event,
)


# 固定 ID 仅用于证明持久事件与异常对象绑定，32 位小写十六进制段不承载真实业务信息。
EVENT_ID = "caevt_" + "a" * 32
ANOMALY_ID = "can_" + "b" * 32
OTHER_ANOMALY_ID = "can_" + "c" * 32
TRACE_ID = "trace_prd053_detector"
WINDOW_START = "2026-07-27T01:00:00Z"
WINDOW_END = "2026-07-27T01:05:00Z"
MAX_SAFE_INTEGER = 9_007_199_254_740_991

# payload 字段必须精确冻结；集合校验同时防止漏字段和未经审计的未来字段外发。
EXPECTED_PAYLOAD_FIELDS = frozenset(
    {
        "anomaly_id",
        "rule_type",
        "severity",
        "status",
        "environment_id",
        "scope_type",
        "scope_id",
        "window_start",
        "window_end",
        "current_cost_micros",
        "baseline_total_micros",
        "observed_ratio_millis",
        "baseline_bucket_count",
        "threshold_multiplier_millis",
        "minimum_cost_micros",
        "source_entry_count",
        "estimated_entry_count",
        "affected_virtual_key_count",
        "recommended_action",
        "detection_version",
    }
)

COST_CONFIG_DEFAULTS = {
    "METAHUB_COST_ANOMALY_ENABLED": "false",
    "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "30",
    "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "5000",
    "METAHUB_COST_ANOMALY_MIN_COST_MICROS": "10000",
    "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "9",
    "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "18",
}

ERROR_CONTRACTS = {
    "cost_anomaly.query_invalid": (422, False, "成本异常查询不合法", "Invalid cost anomaly query"),
    "cost_anomaly.not_found": (404, False, "成本异常资源不存在", "Cost anomaly not found"),
    "cost_anomaly.media_type_invalid": (
        415,
        False,
        "成本异常请求格式不合法",
        "Invalid action media type",
    ),
    "cost_anomaly.action_invalid": (
        422,
        False,
        "成本异常处置不合法",
        "Invalid cost anomaly action",
    ),
    "cost_anomaly.action_conflict": (
        409,
        False,
        "成本异常状态冲突",
        "Cost anomaly action conflict",
    ),
    "cost_anomaly.idempotency_conflict": (
        409,
        False,
        "成本异常幂等键冲突",
        "Cost anomaly idempotency conflict",
    ),
    "cost_anomaly.mitigation_not_applicable": (
        409,
        False,
        "临时限流不适用",
        "Temporary rate limit not applicable",
    ),
    "cost_anomaly.mitigation_active": (
        409,
        False,
        "临时限流已生效",
        "Temporary rate limit active",
    ),
    "cost_anomaly.mitigation_expired": (
        409,
        False,
        "异常处置窗口已过期",
        "Mitigation window expired",
    ),
    "cost_anomaly.mitigation_key_unavailable": (
        409,
        False,
        "异常 Key 暂不可处置",
        "Virtual Key unavailable for mitigation",
    ),
    "cost_anomaly.mitigation_no_effect": (
        409,
        False,
        "临时限流未实际收紧",
        "Temporary rate limit has no effect",
    ),
    "cost_anomaly.mitigation_not_active": (
        409,
        False,
        "临时限流未生效",
        "Temporary rate limit not active",
    ),
    "cost_anomaly.unavailable": (
        503,
        True,
        "成本异常服务暂不可用",
        "Cost anomalies unavailable",
    ),
    "cost_anomaly.detector_unavailable": (
        503,
        True,
        "成本异常检测暂不可用",
        "Cost anomaly detection unavailable",
    ),
}


def _set_cost_config(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str] | None = None,
) -> None:
    """显式设置六项成本异常配置，隔离仓库 ``.env`` 或宿主环境的同名覆盖。"""

    values = dict(COST_CONFIG_DEFAULTS)
    values.update(overrides or {})
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _payload(rule_type: str = "tenant_spend_spike") -> dict[str, object]:
    """按规则生成满足五分钟窗口、整数比率与精确字段集的安全 payload。"""

    contracts = {
        "tenant_spend_spike": ("critical", "tenant", "review_impacted_keys"),
        "virtual_key_spend_spike": (
            "critical",
            "virtual_key",
            "apply_temporary_rate_limit",
        ),
        "virtual_key_off_hours": (
            "warning",
            "virtual_key",
            "apply_temporary_rate_limit",
        ),
    }
    severity, scope_type, recommended_action = contracts[rule_type]
    is_off_hours = rule_type == "virtual_key_off_hours"
    return {
        "anomaly_id": ANOMALY_ID,
        "rule_type": rule_type,
        "severity": severity,
        "status": "open",
        "environment_id": "env_prod",
        "scope_type": scope_type,
        "scope_id": "tenant_default" if scope_type == "tenant" else "vk_prd053_test",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "current_cost_micros": "12000",
        "baseline_total_micros": "0" if is_off_hours else "288000",
        "observed_ratio_millis": None if is_off_hours else "12000",
        "baseline_bucket_count": 288,
        "threshold_multiplier_millis": 5000,
        "minimum_cost_micros": 10000,
        "source_entry_count": 3,
        "estimated_entry_count": 1,
        "affected_virtual_key_count": 3 if scope_type == "tenant" else 1,
        "recommended_action": recommended_action,
        "detection_version": "cost-anomaly-rules-v1",
    }


def _build(payload: dict[str, object], **overrides: object) -> WebhookEvent:
    """用稳定信封构造成本异常事件，并允许覆盖单个字段验证失败关闭路径。"""

    arguments = {
        "tenant_id": "tenant_prd053",
        "event_type": "cost.anomaly_detected",
        "payload": payload,
        "actor_id": None,
        "object_id": ANOMALY_ID,
        "trace_id": TRACE_ID,
        "event_id": EVENT_ID,
        "timestamp": datetime(2026, 7, 27, 1, tzinfo=UTC),
    }
    arguments.update(overrides)
    return build_webhook_event(**arguments)


def test_cost_anomaly_config_defaults_are_loaded_and_exposed_as_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认关闭检测器，同时加载有界门限并在功能摘要中暴露关闭状态。"""

    assert {key: DEFAULT_CONFIG[key] for key in COST_CONFIG_DEFAULTS} == COST_CONFIG_DEFAULTS
    _set_cost_config(monkeypatch)

    config = load_app_config("control-plane-api")

    assert config.cost_anomaly_enabled is False
    assert config.cost_anomaly_poll_interval_seconds == 30
    assert config.cost_anomaly_spike_multiplier_millis == 5000
    assert config.cost_anomaly_min_cost_micros == 10_000
    assert config.cost_anomaly_workday_start_hour == 9
    assert config.cost_anomaly_workday_end_hour == 18
    assert config.features["cost_anomaly_enabled"] is False
    assert COST_CONFIG_DEFAULTS.keys() <= {item.key for item in config.config_items}


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "METAHUB_COST_ANOMALY_ENABLED": "true",
                "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "5",
                "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "1001",
                "METAHUB_COST_ANOMALY_MIN_COST_MICROS": "1",
                "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "0",
                "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "1",
            },
            (True, 5, 1001, 1, 0, 1),
        ),
        (
            {
                "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "300",
                "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "100000",
                "METAHUB_COST_ANOMALY_MIN_COST_MICROS": "1000000000000",
                "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "22",
                "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "23",
            },
            (False, 300, 100_000, 1_000_000_000_000, 22, 23),
        ),
    ],
)
def test_cost_anomaly_config_accepts_inclusive_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    expected: tuple[bool, int, int, int, int, int],
) -> None:
    """轮询、倍数、最低金额与工作时段应接受设计规定的闭区间边界。"""

    _set_cost_config(monkeypatch, overrides)
    config = load_app_config("control-plane-api")

    assert (
        config.cost_anomaly_enabled,
        config.cost_anomaly_poll_interval_seconds,
        config.cost_anomaly_spike_multiplier_millis,
        config.cost_anomaly_min_cost_micros,
        config.cost_anomaly_workday_start_hour,
        config.cost_anomaly_workday_end_hour,
    ) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "4"},
        {"METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "301"},
        {"METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "1000"},
        {"METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "100001"},
        {"METAHUB_COST_ANOMALY_MIN_COST_MICROS": "0"},
        {"METAHUB_COST_ANOMALY_MIN_COST_MICROS": "1000000000001"},
        {"METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "-1"},
        {"METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "24"},
        {"METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "1.5"},
        {
            "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "9",
            "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "9",
        },
        {
            "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "19",
            "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "18",
        },
    ],
)
def test_cost_anomaly_config_rejects_out_of_range_or_ambiguous_values(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    """越界、非整点以及非递增工作时段必须在启动时统一失败关闭。"""

    _set_cost_config(monkeypatch, overrides)
    with pytest.raises(ConfigError) as raised:
        load_app_config("control-plane-api")
    assert raised.value.code == "infra.config_invalid"


def test_cost_anomaly_error_specs_and_bilingual_labels_are_complete() -> None:
    """14 个公开错误必须保持 HTTP、重试语义、owner 与中英文短标签完整一致。"""

    actual_codes = {code for code in ERROR_SPECS if code.startswith("cost_anomaly.")}
    assert actual_codes == ERROR_CONTRACTS.keys()

    for code, (status, retryable, zh_label, en_label) in ERROR_CONTRACTS.items():
        spec = ERROR_SPECS[code]
        assert spec.http_status == status
        assert spec.retryable is retryable
        assert spec.owner == "cost_anomaly"
        assert error_label(code, "zh-CN") == zh_label
        assert error_label(code, "en-US") == en_label


@pytest.mark.parametrize(
    ("rule_type", "expected_mapping"),
    [
        ("tenant_spend_spike", ("critical", "tenant", "review_impacted_keys")),
        (
            "virtual_key_spend_spike",
            ("critical", "virtual_key", "apply_temporary_rate_limit"),
        ),
        (
            "virtual_key_off_hours",
            ("warning", "virtual_key", "apply_temporary_rate_limit"),
        ),
    ],
)
def test_cost_anomaly_detected_accepts_three_frozen_rule_contracts(
    rule_type: str,
    expected_mapping: tuple[str, str, str],
) -> None:
    """三类合法事件应进入订阅目录，并保留规则映射、精确字段与稳定信封。"""

    payload = _payload(rule_type)
    event = _build(payload)

    assert "cost.anomaly_detected" in WEBHOOK_EVENT_TYPES
    assert "cost.anomaly_detected" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert set(event.payload) == EXPECTED_PAYLOAD_FIELDS
    assert len(event.payload) == 20
    assert (
        event.payload["severity"],
        event.payload["scope_type"],
        event.payload["recommended_action"],
    ) == expected_mapping
    assert event.event_id == EVENT_ID
    assert event.event_type == "cost.anomaly_detected"
    assert event.timestamp == WINDOW_START
    assert event.tenant_id == "tenant_prd053"
    assert event.actor_id is None
    assert event.object_id == ANOMALY_ID
    assert event.trace_id == TRACE_ID
    assert event.payload == payload


@pytest.mark.parametrize("field", sorted(EXPECTED_PAYLOAD_FIELDS))
def test_cost_anomaly_detected_rejects_each_missing_field(field: str) -> None:
    """精确合同中的任一字段缺失都必须拒绝，不能由接收端猜测默认值。"""

    payload = _payload()
    payload.pop(field)

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_cost_anomaly_detected_rejects_unreviewed_extra_field() -> None:
    """普通扩展字段即使不敏感也不能绕过 20 字段公开白名单。"""

    payload = _payload()
    payload["future_field"] = "not-reviewed"

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("anomaly_id", "can_" + "A" * 32),
        ("anomaly_id", "can_" + "a" * 31),
        ("rule_type", 1),
        ("rule_type", "provider_spend_spike"),
        ("severity", "warning"),
        ("status", "resolved"),
        ("environment_id", ""),
        ("environment_id", True),
        ("scope_type", "virtual_key"),
        ("scope_id", "scope with space"),
        ("current_cost_micros", 12000),
        ("baseline_total_micros", 288000),
        ("observed_ratio_millis", 12000),
        ("baseline_bucket_count", True),
        ("baseline_bucket_count", 287),
        ("threshold_multiplier_millis", True),
        ("minimum_cost_micros", True),
        ("source_entry_count", True),
        ("estimated_entry_count", 1.0),
        ("affected_virtual_key_count", -1),
        ("affected_virtual_key_count", MAX_SAFE_INTEGER + 1),
        ("recommended_action", "apply_temporary_rate_limit"),
        ("detection_version", "cost-anomaly-rules-v2"),
    ],
)
def test_cost_anomaly_detected_rejects_wrong_types_identity_or_rule_mapping(
    field: str,
    value: object,
) -> None:
    """bool、浮点、未知枚举、错误 ID 与被篡改的规则映射都必须失败关闭。"""

    payload = _payload()
    payload[field] = value

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"current_cost_micros": "-1"},
        {"current_cost_micros": "012000"},
        {"current_cost_micros": "+12000"},
        {"current_cost_micros": "12000.0"},
        {"current_cost_micros": "12e3"},
        {"current_cost_micros": "9" * 41},
        {"baseline_total_micros": "00"},
        {"baseline_total_micros": "0"},
        {"observed_ratio_millis": None},
        {"observed_ratio_millis": "11999"},
        {"observed_ratio_millis": "012000"},
        {"threshold_multiplier_millis": 1000},
        {"threshold_multiplier_millis": 100_001},
        {"minimum_cost_micros": 0},
        {"minimum_cost_micros": 1_000_000_000_001},
        {"current_cost_micros": "9999"},
        {
            "current_cost_micros": "5000",
            "observed_ratio_millis": "5000",
            "minimum_cost_micros": 1,
        },
    ],
)
def test_spike_event_rejects_noncanonical_money_or_unproven_ratio(
    updates: dict[str, object],
) -> None:
    """金额文本、最低金额、整数比率与严格越限公式必须同时成立。"""

    payload = _payload()
    payload.update(updates)

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


def test_off_hours_event_accepts_40_digit_canonical_amounts_and_null_ratio() -> None:
    """金额允许 40 位 canonical 十进制，而非工作时段规则必须显式使用空比率。"""

    payload = _payload("virtual_key_off_hours")
    payload["current_cost_micros"] = "9" * 40
    payload["baseline_total_micros"] = "8" * 40

    event = _build(payload)

    assert event.payload["current_cost_micros"] == "9" * 40
    assert event.payload["baseline_total_micros"] == "8" * 40
    assert event.payload["observed_ratio_millis"] is None


def test_off_hours_event_rejects_synthetic_ratio() -> None:
    """非工作时段检测不依赖历史倍数，非空比率会制造虚假解释，必须拒绝。"""

    payload = _payload("virtual_key_off_hours")
    payload["observed_ratio_millis"] = "0"

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"window_start": "2026-07-27T01:01:00Z"},
        {"window_start": "2026-07-27T01:00:01Z"},
        {"window_start": "2026-07-27T01:00:00+00:00"},
        {"window_start": "2026-07-27T01:00:00z"},
        {"window_start": "2026-02-30T01:00:00Z"},
        {"window_end": "2026-07-27T01:00:00Z"},
        {"window_end": "2026-07-27T01:10:00Z"},
        {"window_end": "2026-07-27T00:55:00Z"},
    ],
)
def test_cost_anomaly_detected_rejects_noncanonical_or_non_five_minute_window(
    updates: dict[str, object],
) -> None:
    """窗口必须是 canonical UTC 且严格覆盖一个左闭右开的五分钟桶。"""

    payload = _payload()
    payload.update(updates)

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": None},
        {"event_id": "evt_" + "a" * 32},
        {"event_id": "caevt_" + "A" * 32},
        {"event_id": "caevt_" + "a" * 31},
        {"actor_id": "user_admin"},
        {"object_id": OTHER_ANOMALY_ID},
        {"trace_id": None},
        {"trace_id": ""},
        {"trace_id": "trace with space"},
        {"trace_id": "x" * 129},
    ],
)
def test_cost_anomaly_detected_rejects_unbound_or_unbounded_envelope(
    overrides: dict[str, object],
) -> None:
    """信封必须绑定 caevt_/can_、空 actor、同一异常对象和有界检测 trace。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build(_payload(), **overrides)


@pytest.mark.parametrize(
    "sensitive_key",
    ["Prompt", "response", "access-token", "signing_secret", "provider_api_key"],
)
def test_cost_anomaly_detected_recursively_rejects_sensitive_keys(
    sensitive_key: str,
) -> None:
    """任意嵌套层级的正文、Token、Secret 或 API Key 键都必须整体拒绝。"""

    payload = _payload()
    payload["metadata"] = [{"nested": {sensitive_key: "redacted-test-value"}}]

    with pytest.raises(ValueError, match="webhook.payload_sensitive"):
        _build(payload)
