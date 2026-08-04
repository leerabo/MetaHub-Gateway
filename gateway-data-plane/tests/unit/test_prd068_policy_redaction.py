"""PRD-068 Gateway Policy mask 输入脱敏专项测试。

模块职责：证明脱敏主流程、copy-on-write、未知结构失败关闭和资源边界。
安全边界：测试断言只在内存固定样本中使用敏感格式，不写数据库、日志或外部网络。
"""

from __future__ import annotations

import pytest
from metahub_gateway.policies.redaction import (
    MAX_CHAT_CONTENT_PARTS,
    MAX_CHAT_MESSAGES,
    MAX_EMBEDDING_INPUTS,
    MAX_REDACTION_TEXT_SEGMENTS,
    MAX_REDACTION_TOTAL_CHARACTERS,
    PolicyMaskUnsupportedError,
    policy_mask_runtime_self_test,
    redact_policy_payload,
)
from metahub_gateway.policies.runtime import (
    PolicyExecutionResult,
    apply_policy_execution_result,
    apply_policy_mask_payload,
)
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.errors import ApiError


def test_chat_redaction_is_copy_on_write_and_user_text_only() -> None:
    """主流程：只替换用户文本中的手机号、身份证和邮箱，原 payload 与系统文本保持不变。"""

    source = {
        "model": "customer-service",
        "stream": True,
        "messages": [
            {"role": "system", "content": "contact admin@example.com"},
            {
                "role": "user",
                "content": [
                    "手机 13800138000",
                    {"type": "text", "text": "身份证 11010519491231002X"},
                    {"type": "image_url", "image_url": {"url": "https://example.invalid/a"}},
                ],
            },
        ],
    }

    result = redact_policy_payload("chat", source)

    assert result is not source
    assert result["stream"] is True
    assert source["stream"] is True
    assert source["messages"][1]["content"][0] == "手机 13800138000"
    assert result["messages"][0] is source["messages"][0]
    assert result["messages"][0]["content"] == "contact admin@example.com"
    assert result["messages"][1] is not source["messages"][1]
    assert result["messages"][1]["content"] is not source["messages"][1]["content"]
    assert result["messages"][1]["content"][0] == "手机 [MOBILE_REDACTED]"
    assert result["messages"][1]["content"][1] is not source["messages"][1]["content"][1]
    assert result["messages"][1]["content"][1]["text"] == "身份证 [CN_ID_REDACTED]"
    assert result["messages"][1]["content"][2] is source["messages"][1]["content"][2]


def test_embeddings_redaction_supports_string_and_string_list() -> None:
    """主流程：字符串和纯字符串数组均完整脱敏，输入对象不被修改。"""

    single = {"model": "embed", "input": "mail user@example.com"}
    multiple = {"model": "embed", "input": ["13800138000", "safe"]}

    redacted_single = redact_policy_payload("embeddings", single)
    redacted_multiple = redact_policy_payload("embeddings", multiple)

    assert redacted_single is not single
    assert redacted_multiple is not multiple
    assert redacted_single["input"] == "mail [EMAIL_REDACTED]"
    assert redacted_multiple["input"] == [
        "[MOBILE_REDACTED]",
        "safe",
    ]
    assert single["input"] == "mail user@example.com"
    assert multiple["input"][0] == "13800138000"


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": [{"role": "user", "content": [{"type": "text", "text": 1}]}]},
        {"messages": [{"role": "user", "content": [["nested"]]}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "future_text", "value": "13800138000"}],
                }
            ]
        },
    ],
)
def test_chat_redaction_rejects_unknown_or_partial_text_shapes(payload: dict) -> None:
    """异常流程：未知文本和嵌套结构必须整体拒绝，不能返回已经处理的前缀。"""

    with pytest.raises(PolicyMaskUnsupportedError):
        redact_policy_payload("chat", payload)


@pytest.mark.parametrize("value", [[1, 2], ["safe", 1], [["nested"]]])
def test_embeddings_redaction_rejects_token_and_mixed_input(value: object) -> None:
    """异常流程：数字 token、混合数组和嵌套数组均不属于本期可安全脱敏合同。"""

    with pytest.raises(PolicyMaskUnsupportedError):
        redact_policy_payload("embeddings", {"input": value})


def test_redaction_limits_are_all_or_nothing_and_self_test_passes() -> None:
    """资源与部署流程：结构和字符任一超限均失败关闭，固定 capability 自检必须通过。"""

    cases = (
        (
            "chat",
            {"messages": [{"role": "user", "content": "safe"}] * (MAX_CHAT_MESSAGES + 1)},
        ),
        (
            "chat",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": ["safe"] * (MAX_CHAT_CONTENT_PARTS + 1),
                    }
                ]
            },
        ),
        ("embeddings", {"input": ["safe"] * (MAX_EMBEDDING_INPUTS + 1)}),
        (
            "chat",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "x" * (MAX_REDACTION_TOTAL_CHARACTERS + 1),
                    }
                ]
            },
        ),
    )
    for request_kind, payload in cases:
        with pytest.raises(PolicyMaskUnsupportedError):
            redact_policy_payload(request_kind, payload)
    # 通用段数上限仍必须足以覆盖设计冻结的最坏 Chat 结构，不能悄悄收窄合法请求。
    assert MAX_REDACTION_TEXT_SEGMENTS == MAX_CHAT_MESSAGES * MAX_CHAT_CONTENT_PARTS
    assert policy_mask_runtime_self_test() is True


def _gateway_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> AppConfig:
    """加载不访问网络的测试配置，并显式设置部署级 mask 门禁。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_POLICY_MASK_RUNTIME_ENABLED", "true" if enabled else "false")
    return load_app_config("gateway-data-plane")


def test_runtime_gate_and_log_context_fail_closed_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署与日志流程：门禁关闭返回统一 503，开启后只记录安全 ID/布尔值并返回副本。"""

    source = {"messages": [{"role": "user", "content": "13800138000"}]}
    result = PolicyExecutionResult(
        "allow",
        "policy.no_match_default_allow",
        mask_required=True,
        mask_policy_id="pol_mask",
    )
    base = RequestLogContext(
        tenant_id="tenant_mask",
        request_id="req_mask",
        trace_id="trc_mask",
    )
    logged = apply_policy_execution_result(base, result)
    assert logged.policy_mask_applied is False
    assert logged.policy_mask_policy_id == "pol_mask"

    with pytest.raises(ApiError) as disabled:
        apply_policy_mask_payload(
            _gateway_config(monkeypatch, enabled=False),
            logged,
            result,
            source,
            request_kind="chat",
        )
    assert disabled.value.code == "gateway.policy_mask_unavailable"

    completed, redacted = apply_policy_mask_payload(
        _gateway_config(monkeypatch, enabled=True),
        logged,
        result,
        source,
        request_kind="chat",
    )
    assert completed.policy_mask_applied is True
    assert completed.policy_mask_policy_id == "pol_mask"
    assert redacted["messages"][0]["content"] == "[MOBILE_REDACTED]"
    assert source["messages"][0]["content"] == "13800138000"


def test_runtime_maps_unsupported_shape_without_exposing_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：数字 Embeddings token 映射固定 422，错误文本不包含输入数组。"""

    result = PolicyExecutionResult(
        "allow",
        "policy.no_match_default_allow",
        mask_required=True,
        mask_policy_id="pol_mask",
    )
    context = RequestLogContext(
        tenant_id="tenant_mask",
        request_id="req_mask_invalid",
        trace_id="trc_mask_invalid",
    )
    with pytest.raises(ApiError) as caught:
        apply_policy_mask_payload(
            _gateway_config(monkeypatch, enabled=True),
            context,
            result,
            {"input": [123, 456]},
            request_kind="embeddings",
        )
    assert caught.value.code == "gateway.policy_mask_unsupported"
    assert "123" not in str(caught.value)


def test_runtime_no_mask_is_identity_and_missing_policy_id_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """边界流程：未命中 mask 不复制正文；命中事实缺稳定 Policy ID 时禁止发送原文。"""

    source = {"messages": [{"role": "user", "content": "13800138000"}]}
    context = RequestLogContext(
        tenant_id="tenant_mask",
        request_id="req_mask_boundary",
        trace_id="trc_mask_boundary",
    )
    untouched_context, untouched_payload = apply_policy_mask_payload(
        _gateway_config(monkeypatch, enabled=True),
        context,
        PolicyExecutionResult("allow", "policy.no_match_default_allow"),
        source,
        request_kind="chat",
    )
    assert untouched_context is context
    assert untouched_payload is source

    with pytest.raises(ApiError) as unavailable:
        apply_policy_mask_payload(
            _gateway_config(monkeypatch, enabled=True),
            context,
            PolicyExecutionResult(
                "allow",
                "policy.no_match_default_allow",
                mask_required=True,
            ),
            source,
            request_kind="chat",
        )
    assert unavailable.value.code == "gateway.policy_mask_unavailable"
    assert source["messages"][0]["content"] == "13800138000"
