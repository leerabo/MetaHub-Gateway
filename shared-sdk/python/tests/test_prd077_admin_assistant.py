"""验证 PRD-077 管理助手 Shared 解析、回答图、RBAC、错误、事件和配置合同。"""

from __future__ import annotations

import copy
import os
from datetime import UTC, datetime, timedelta

import pytest
from metahub_shared.admin_assistant import (
    ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS,
    ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS,
    ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS,
    AdminAssistantContractError,
    AnswerClaim,
    AnswerRecommendation,
    AnswerSource,
    build_assistant_answer,
    parse_assistant_query,
    required_permissions_for_query,
    validate_assistant_answer_wire,
)
from metahub_shared.rbac import (
    ADMIN_ROLE,
    CATALOG_REVISION,
    DEVELOPER_ROLE,
    FINANCE_ADMIN_ROLE,
    OPS_ADMIN_ROLE,
    PERMISSION_BY_CODE,
    PERMISSION_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    SECURITY_ADMIN_ROLE,
    VIEWER_ROLE,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import DEFAULT_CONFIG, ConfigError, load_app_config
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_assistant_queried_event,
    build_webhook_event,
)

# 固定时间与对象 ID 只用于可重复合同样本，不携带真实租户、问题或回答内容。
NOW = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
ASQ_ID = "asq_" + "1" * 32
ASA_ID = "asa_" + "2" * 32
ASEVT_ID = "asevt_" + "3" * 32


def _assert_contract_error(code: str, value: object) -> None:
    """断言异常只暴露预期固定错误码，不附带失败输入或内部对象表示。"""

    assert isinstance(value, AdminAssistantContractError)
    assert value.code == code
    assert str(value) == code


def test_deadline_contract_keeps_source_decision_and_governance_ordered() -> None:
    """三阶段预算必须固定为递增绝对窗口，避免治理写重新挤占来源决策预算。"""

    assert ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS == 1.2
    assert ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS == 1.5
    assert ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS == 5.0


@pytest.mark.parametrize(
    ("question", "query_type", "window_hours", "target_request_id"),
    (
        ("过去 7 天调用情况如何？", "usage_statistics", 168, None),
        (
            "Explain the error for req_demo in the last 24 hours",
            "error_explanation",
            24,
            "req_demo",
        ),
        ("请给出最近30天的成本建议", "cost_advice", 720, None),
        ("解释 req_policy_demo 对应的策略", "policy_explanation", 168, "req_policy_demo"),
        ("过去７天的调用量", "usage_statistics", 168, None),
    ),
)
def test_parser_supports_bounded_chinese_english_and_nfkc(
    question: str,
    query_type: str,
    window_hours: int,
    target_request_id: str | None,
) -> None:
    """验证中英文及全角字符都只能归入有限意图、窗口和可选请求范围。"""

    parsed = parse_assistant_query(question)
    assert parsed.query_type == query_type
    assert parsed.window_hours == window_hours
    assert parsed.target_request_id == target_request_id
    # 解析结果不能保存规范化前后的问题正文，避免 DTO 被日志 repr 意外记录。
    assert "question" not in {field for field in parsed.__slots__}
    assert question not in repr(parsed)


def test_parser_applies_read_only_gate_before_intent_resolution() -> None:
    """验证中英文写动作先于意图识别被拒绝，不能降级成看似只读查询。"""

    with pytest.raises(AdminAssistantContractError) as captured:
        parse_assistant_query("请修改策略并解释成本")
    _assert_contract_error("admin_assistant.read_only_violation", captured.value)

    with pytest.raises(AdminAssistantContractError) as captured_en:
        parse_assistant_query("Update the budget and explain usage")
    _assert_contract_error("admin_assistant.read_only_violation", captured_en.value)


@pytest.mark.parametrize(
    "question",
    (
        "调用\x00用量",
        "调用\x07用量",
        "调用\u202e用量",
        "调用\ufdd0用量",
        "调用\ud800用量",
        "x" * 2001,
    ),
)
def test_parser_rejects_unsafe_unicode_without_reflecting_input(question: str) -> None:
    """验证危险 Unicode、控制符和超长输入被拒绝且不会出现在异常表示中。"""

    with pytest.raises(AdminAssistantContractError) as captured:
        parse_assistant_query(question)
    _assert_contract_error("admin_assistant.request_invalid", captured.value)
    assert question not in repr(captured.value)


@pytest.mark.parametrize(
    "question",
    (
        "今天调用用量如何",
        "show usage this week",
        "过去 7 天和 30 天的成本",
        "解释错误 req_one 和 req_two",
        "解释错误和策略",
        "req_usage 的调用量",
    ),
)
def test_parser_rejects_calendar_multi_scope_and_cross_domain_id(question: str) -> None:
    """验证日历歧义、多范围及跨域请求 ID 都不会被解析器自行猜测。"""

    with pytest.raises(AdminAssistantContractError) as captured:
        parse_assistant_query(question)
    _assert_contract_error("admin_assistant.query_ambiguous", captured.value)


def test_parser_rejects_unknown_intent_with_fixed_code() -> None:
    """验证未知问题返回固定不支持错误，不回退到任一默认业务查询。"""

    with pytest.raises(AdminAssistantContractError) as captured:
        parse_assistant_query("你好，请介绍一下你自己")
    _assert_contract_error("admin_assistant.query_unsupported", captured.value)


def test_parser_rejects_malformed_request_marker_instead_of_broadening_scope() -> None:
    """验证畸形 req_ 标记不会静默退化成更宽的时间窗口聚合。"""

    with pytest.raises(AdminAssistantContractError) as captured:
        parse_assistant_query("解释错误 req_")
    _assert_contract_error("admin_assistant.request_invalid", captured.value)


def test_required_permission_matrix_keeps_base_and_source_domains_separate() -> None:
    """验证基础查询权限不能替代各意图读取真实来源所需的业务域权限。"""

    assert required_permissions_for_query("usage_statistics") == (
        "admin_assistant.query",
        "billing.usage.read",
        "dashboard.read",
    )
    assert required_permissions_for_query("error_explanation", request_specific=True) == (
        "admin_assistant.query",
        "request_log.read",
    )
    assert required_permissions_for_query("policy_explanation", request_specific=True) == (
        "admin_assistant.query",
        "policy.read",
        "request_explanation.read",
    )
    assert required_permissions_for_query("cost_advice") == (
        "admin_assistant.query",
        "billing.anomaly.read",
        "billing.budget.read",
        "billing.finops.read",
        "billing.usage.read",
        "dashboard.read",
        "model_price.read",
    )


def _usage_answer():
    """构造同时引用概览和用量来源的最小合法类型化回答图。"""

    overview = AnswerSource(
        source_type="overview",
        record_count=120,
        window_started_at=NOW - timedelta(days=7),
        window_ended_at=NOW,
        snapshot_at=NOW,
    )
    usage = AnswerSource(
        source_type="usage",
        record_count=120,
        window_started_at=NOW - timedelta(days=7),
        window_ended_at=NOW,
        snapshot_at=NOW,
    )
    claims = (
        AnswerClaim(
            claim_id="request_count",
            claim_type="integer",
            label="调用次数",
            value=120,
            unit="requests",
            source_ids=(overview.source_id,),
        ),
        AnswerClaim(
            claim_id="usage_cost_cny",
            claim_type="decimal",
            label="用量成本",
            value="12.340000",
            unit="CNY",
            source_ids=(usage.source_id,),
        ),
    )
    return build_assistant_answer(
        assistant_query_id=ASQ_ID,
        assistant_audit_id=ASA_ID,
        query_type="usage_statistics",
        claims=claims,
        recommendations=(),
        sources=(overview, usage),
        request_id="req_prd077_contract",
        trace_id="trc_prd077_contract",
    )


def test_answer_builder_derives_exact_summary_links_redactions_and_round_trips() -> None:
    """验证回答只派生固定摘要、内部链接、脱敏声明并能通过精确 wire 往返。"""

    answer = _usage_answer()
    wire = answer.to_wire()
    assert set(wire) == {
        "assistant_query_id",
        "assistant_audit_id",
        "query_type",
        "answer",
        "sources",
        "redactions",
        "request_id",
        "trace_id",
    }
    assert wire["answer"]["summary"] == "基于 2 个可验证来源生成。"
    assert wire["redactions"] == ["content_omitted", "credentials_omitted"]
    assert wire["sources"][0]["source_id"] == "src_overview"
    assert wire["sources"][0]["href"] == "/#overview"
    assert validate_assistant_answer_wire(wire) == answer
    # claim value 可能包含租户聚合事实，因此 dataclass repr 必须隐藏该值。
    assert "12.340000" not in repr(answer.claims[1])


def test_claim_wire_uses_cross_language_integer_text_label_and_nullable_unit_bounds() -> None:
    """验证 Shared 与 JavaScript 对 claim 的精确可表示范围保持一致。"""

    source_ids = ("src_overview",)
    maximum = AnswerClaim(
        claim_id="maximum_safe_count",
        claim_type="integer",
        label="L" * 80,
        value=9_007_199_254_740_991,
        unit=None,
        source_ids=source_ids,
    )
    assert maximum.to_wire()["unit"] is None
    with pytest.raises(AdminAssistantContractError):
        AnswerClaim(
            claim_id="unsafe_count",
            claim_type="integer",
            label="调用次数",
            value=9_007_199_254_740_992,
            unit=None,
            source_ids=source_ids,
        )
    with pytest.raises(AdminAssistantContractError):
        AnswerClaim(
            claim_id="long_label",
            claim_type="integer",
            label="L" * 81,
            value=1,
            unit=None,
            source_ids=source_ids,
        )
    AnswerClaim(
        claim_id="maximum_text",
        claim_type="text",
        label="文本",
        value="T" * 512,
        unit=None,
        source_ids=source_ids,
    )
    with pytest.raises(AdminAssistantContractError):
        AnswerClaim(
            claim_id="long_text",
            claim_type="text",
            label="文本",
            value="T" * 513,
            unit=None,
            source_ids=source_ids,
        )


def test_answer_source_normalizes_internal_snapshot_microseconds_to_utc_second() -> None:
    """验证内部微秒快照在公开合同中规范化为唯一 UTC 秒表示。"""

    source = AnswerSource(
        source_type="overview",
        record_count=1,
        window_started_at=NOW - timedelta(days=7),
        window_ended_at=NOW,
        snapshot_at=NOW.replace(microsecond=987654),
    )
    assert source.snapshot_at == NOW
    assert source.to_wire()["snapshot_at"] == "2026-08-11T00:00:00Z"


def test_error_answer_adds_raw_provider_redaction_only_as_fixed_code() -> None:
    """验证错误解释仅用固定枚举声明省略原始 Provider 错误，不携带其正文。"""

    source = AnswerSource(
        source_type="request_logs",
        record_count=1,
        window_started_at=NOW - timedelta(hours=24),
        window_ended_at=NOW,
        snapshot_at=NOW,
    )
    answer = build_assistant_answer(
        assistant_query_id=ASQ_ID,
        assistant_audit_id=ASA_ID,
        query_type="error_explanation",
        claims=(
            AnswerClaim(
                claim_id="error_count",
                claim_type="integer",
                label="错误次数",
                value=1,
                unit="requests",
                source_ids=(source.source_id,),
            ),
        ),
        recommendations=(),
        sources=(source,),
        request_id="req_prd077_error",
        trace_id="trc_prd077_error",
    )
    assert answer.to_wire()["redactions"] == [
        "content_omitted",
        "credentials_omitted",
        "raw_provider_error_omitted",
    ]


@pytest.mark.parametrize(
    "mutator",
    (
        lambda wire: wire.update({"unknown": True}),
        lambda wire: wire["answer"].update({"summary": "free text"}),
        lambda wire: wire["sources"][0].update({"href": "https://example.invalid"}),
        lambda wire: wire["sources"][0].update({"source_id": "src_usage"}),
        lambda wire: wire["answer"]["claims"][0].update({"value": True}),
        lambda wire: wire["answer"]["claims"][0].update({"source_ids": ["src_policies"]}),
        lambda wire: wire.update({"redactions": ["credentials_omitted", "content_omitted"]}),
    ),
)
def test_answer_wire_rejects_unknown_fields_free_urls_and_broken_graph(mutator) -> None:
    """验证未知字段、自由 URL、类型漂移和断裂来源图都会使整份 wire 失效。"""

    wire = copy.deepcopy(_usage_answer().to_wire())
    mutator(wire)
    with pytest.raises(AdminAssistantContractError) as captured:
        validate_assistant_answer_wire(wire)
    _assert_contract_error("admin_assistant.answer_invalid", captured.value)


def test_answer_graph_rejects_unreferenced_source_and_free_recommendation_message() -> None:
    """验证装饰性未引用来源和调用方自由建议文案都不能进入公开回答。"""

    source = AnswerSource(
        source_type="overview",
        record_count=1,
        window_started_at=NOW - timedelta(days=7),
        window_ended_at=NOW,
        snapshot_at=NOW,
    )
    unused = AnswerSource(
        source_type="usage",
        record_count=0,
        window_started_at=NOW - timedelta(days=7),
        window_ended_at=NOW,
        snapshot_at=NOW,
    )
    claim = AnswerClaim(
        claim_id="request_count",
        claim_type="integer",
        label="调用次数",
        value=1,
        unit="requests",
        source_ids=(source.source_id,),
    )
    with pytest.raises(AdminAssistantContractError):
        build_assistant_answer(
            assistant_query_id=ASQ_ID,
            assistant_audit_id=ASA_ID,
            query_type="usage_statistics",
            claims=(claim,),
            recommendations=(),
            sources=(source, unused),
            request_id="req_prd077_graph",
            trace_id="trc_prd077_graph",
        )
    recommendation = AnswerRecommendation(
        code="insufficient_evidence", source_ids=(source.source_id,)
    )
    wire = _usage_answer().to_wire()
    wire["answer"]["recommendations"] = [recommendation.to_wire()]
    wire["answer"]["recommendations"][0]["message"] = "arbitrary advice"
    with pytest.raises(AdminAssistantContractError):
        validate_assistant_answer_wire(wire)


def test_rbac_revision_13_grants_only_three_default_roles() -> None:
    """冻结 PRD-077 子集及其三角色授权，同时允许后续目录追加权限。"""

    assert CATALOG_REVISION >= 13
    assert len(PERMISSION_CODES) >= 70
    assert sum(len(values) for values in ROLE_GRANTABLE_PERMISSIONS.values()) >= 188
    definition = PERMISSION_BY_CODE["admin_assistant.query"]
    assert (definition.category, definition.risk_level) == ("admin_assistant", "read")
    granted = {
        role
        for role, permissions in ROLE_GRANTABLE_PERMISSIONS.items()
        if "admin_assistant.query" in permissions
    }
    assert granted == {ADMIN_ROLE, FINANCE_ADMIN_ROLE, SECURITY_ADMIN_ROLE}
    assert "admin_assistant.query" not in ROLE_GRANTABLE_PERMISSIONS[DEVELOPER_ROLE]
    assert "admin_assistant.query" not in ROLE_GRANTABLE_PERMISSIONS[VIEWER_ROLE]
    assert "admin_assistant.query" not in ROLE_GRANTABLE_PERMISSIONS[OPS_ADMIN_ROLE]
    admin_prd077 = {
        code for code in ROLE_GRANTABLE_PERMISSIONS[ADMIN_ROLE] if not code.startswith("approval.")
    }
    finance_prd077 = {
        code
        for code in ROLE_GRANTABLE_PERMISSIONS[FINANCE_ADMIN_ROLE]
        if not code.startswith("approval.")
    }
    security_prd077 = {
        code
        for code in ROLE_GRANTABLE_PERMISSIONS[SECURITY_ADMIN_ROLE]
        if not code.startswith("approval.")
    }
    assert permission_matrix_sha256(admin_prd077) == (
        "0012b8538dcabe652d054716a350bd7abf5003c0bcca3a8021e830928afd295d"
    )
    assert permission_matrix_sha256(finance_prd077) == (
        "63691295710ed938c153dedd989d83412694b1b0b26b7fa83658d90a08a99ce8"
    )
    assert permission_matrix_sha256(security_prd077) == (
        "38148a7c165b4c2122a37209f88e3beee2c913d56a151b579d765f4f9c4a4656"
    )


def test_error_catalog_freezes_admin_assistant_status_owner_and_retryability() -> None:
    """冻结管理助手错误状态、owner 和重试语义，并排除敏感术语进入公开文案。"""

    expected = {
        "admin_assistant.request_invalid": (422, False),
        "admin_assistant.read_only_violation": (403, False),
        "admin_assistant.query_unsupported": (422, False),
        "admin_assistant.query_ambiguous": (422, False),
        "admin_assistant.source_not_found": (404, False),
        "admin_assistant.rate_limited": (429, True),
        "admin_assistant.source_unavailable": (503, True),
        "admin_assistant.answer_invalid": (503, False),
        "admin_assistant.audit_unavailable": (503, True),
        "admin_assistant.event_unavailable": (503, True),
    }
    for code, (status, retryable) in expected.items():
        spec = ERROR_SPECS[code]
        assert (spec.http_status, spec.retryable, spec.owner) == (
            status,
            retryable,
            "admin_assistant",
        )
        combined = f"{spec.message} {spec.suggestion}".lower()
        assert all(term not in combined for term in ("prompt", "response", "token", "provider key"))


def _build_event(**overrides):
    """构造不含问题与回答正文的 assistant.queried 固定事件样本。"""

    values = {
        "tenant_id": "tenant_prd077",
        "actor_id": "user_prd077",
        "assistant_query_id": ASQ_ID,
        "event_id": ASEVT_ID,
        "trace_id": "trc_prd077_event",
        "source_request_id": "req_prd077_event",
        "query_type": "usage_statistics",
        "outcome": "answered",
        "claim_count": 2,
        "source_count": 2,
        "redaction_count": 2,
        "duration_ms": 1499,
        "timestamp": NOW,
    }
    values.update(overrides)
    return build_assistant_queried_event(**values)


def test_assistant_webhook_uses_stable_owner_exact_payload_and_duration_bucket() -> None:
    """验证事件 owner、精确 payload 和有限耗时桶均由安全聚合字段构成。"""

    assert "assistant.queried" in WEBHOOK_EVENT_TYPES
    assert "assistant.queried" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = _build_event()
    assert event.event_id == ASEVT_ID
    assert event.event_type == "assistant.queried"
    assert event.object_id == ASQ_ID
    assert event.actor_id == "user_prd077"
    assert event.trace_id == "trc_prd077_event"
    assert event.payload == {
        "schema_version": "assistant-queried-v1",
        "query_type": "usage_statistics",
        "outcome": "answered",
        "claim_count": 2,
        "source_count": 2,
        "redaction_count": 2,
        "duration_bucket": "under_1500ms",
    }
    assert _build_event(duration_ms=1500).payload["duration_bucket"] == "at_least_1500ms"
    failed = _build_event(
        query_type="unknown",
        outcome="failed",
        claim_count=0,
        source_count=0,
        redaction_count=0,
    )
    assert failed.payload["query_type"] == "unknown"
    assert set(event.to_payload()) == {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {"event_id": "evt_" + "3" * 32},
        {"assistant_query_id": "asa_" + "1" * 32},
        {"actor_id": ""},
        {"source_request_id": ""},
        {"claim_count": True},
        {"redaction_count": 3},
        {"outcome": "failed", "claim_count": 1, "source_count": 0, "redaction_count": 0},
        {"query_type": "unknown"},
    ),
)
def test_assistant_webhook_rejects_drifted_owner_counts_and_bool(overrides) -> None:
    """验证事件身份、计数和布尔类型漂移统一被 webhook 合同拒绝。"""

    with pytest.raises(ValueError, match="^webhook.payload_invalid$"):
        _build_event(**overrides)


def test_assistant_webhook_rejects_unknown_and_sensitive_payload_fields() -> None:
    """验证未知字段、容器漂移以及问题正文等敏感字段不能进入事件信封。"""

    payload = _build_event().payload
    payload["unknown"] = 1
    with pytest.raises(ValueError, match="^webhook.payload_invalid$"):
        build_webhook_event(
            tenant_id="tenant_prd077",
            event_type="assistant.queried",
            payload=payload,
            actor_id="user_prd077",
            object_id=ASQ_ID,
            trace_id="trc_prd077_event",
            source_request_id="req_prd077_event",
            event_id=ASEVT_ID,
            timestamp=NOW,
        )
    payload = _build_event().payload
    payload["query_type"] = ["usage_statistics"]
    with pytest.raises(ValueError, match="^webhook.payload_invalid$"):
        build_webhook_event(
            tenant_id="tenant_prd077",
            event_type="assistant.queried",
            payload=payload,
            actor_id="user_prd077",
            object_id=ASQ_ID,
            trace_id="trc_prd077_event",
            source_request_id="req_prd077_event",
            event_id=ASEVT_ID,
            timestamp=NOW,
        )
    payload = _build_event().payload
    payload["question"] = "must-not-leave-memory"
    with pytest.raises(ValueError, match="^webhook.payload_sensitive$"):
        build_webhook_event(
            tenant_id="tenant_prd077",
            event_type="assistant.queried",
            payload=payload,
            actor_id="user_prd077",
            object_id=ASQ_ID,
            trace_id="trc_prd077_event",
            source_request_id="req_prd077_event",
            event_id=ASEVT_ID,
            timestamp=NOW,
        )


def _clear_metahub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理当前测试进程中的 MetaHub 配置变量，保证默认值断言不受主机污染。"""

    for key in tuple(os.environ):
        if key.startswith("METAHUB_"):
            monkeypatch.delenv(key, raising=False)


def test_admin_assistant_event_config_defaults_features_and_bounded_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """验证事件开关和轮询间隔的默认值、特性镜像与有界覆盖保持一致。"""

    _clear_metahub_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = load_app_config("control")
    assert DEFAULT_CONFIG["METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED"] == "true"
    assert DEFAULT_CONFIG["METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS"] == "2"
    assert config.admin_assistant_event_enabled is True
    assert config.admin_assistant_event_poll_interval_seconds == 2
    assert config.features["admin_assistant_event_enabled"] is True
    summary = {item.key: item for item in config.config_items}
    assert summary["METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED"].value_preview == "true"

    monkeypatch.setenv("METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED", "false")
    monkeypatch.setenv("METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS", "60")
    overridden = load_app_config("control")
    assert overridden.admin_assistant_event_enabled is False
    assert overridden.admin_assistant_event_poll_interval_seconds == 60
    assert overridden.features["admin_assistant_event_enabled"] is False


@pytest.mark.parametrize("value", ("0", "61", "true", "1.5"))
def test_admin_assistant_event_poll_interval_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path, value: str
) -> None:
    """验证零值、越界值、布尔文本和小数轮询间隔均以固定配置错误关闭。"""

    _clear_metahub_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS", value)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control")
    assert captured.value.code == "infra.config_invalid"
    assert value not in captured.value.message
