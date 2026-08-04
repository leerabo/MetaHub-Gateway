"""注册 PRD-063 九类 Provider spec 的显式契约夹具。

注册表有意逐项列出，不能从 spec 自动生成默认夹具；否则新增 Adapter 时会在没有研发提交
新测试的情况下被通用断言放行。所有正文均为合成数据，不得替换为客户 Prompt、真实响应或 Key。
"""

from __future__ import annotations

import json
from typing import Mapping

from .models import AdapterContractFixture, ContractUsageExpectation


# 固定合成租户只用于离线合并门禁，不能与开发库默认租户或真实客户租户混用。
CONTRACT_TENANT_ID = "ci-contract"
# 夹具正文中的 canary 用来证明错误链和报告会脱敏；它不是 Provider Key。
SENSITIVE_CANARY = "contract-provider-secret-canary"


def _success_payload(usage_key: str, usage: Mapping[str, object]) -> dict[str, object]:
    """构造不含客户数据的成功响应，并把 usage 放入指定公开方言。

    参数：usage 根字段名和合成数值。返回：仅供 mock server 使用的新字典。调用方不能把
    该字典写入报告或日志。
    """

    payload: dict[str, object] = {
        "id": "contract-response",
        "choices": [{"message": {"role": "assistant", "content": "contract-ok"}}],
    }
    # output.usage 是唯一两段根路径；显式构造嵌套对象，避免测试解析器误把点号当普通键。
    if usage_key == "output.usage":
        payload["output"] = {"usage": dict(usage)}
    else:
        payload[usage_key] = dict(usage)
    return payload


def _openai_stream(usage: Mapping[str, object]) -> bytes:
    """生成 OpenAI profile 的固定 SSE。

    参数：仅含合成数值的 usage。返回：包含 final usage 与 `[DONE]` 的有界字节串。
    约束：不能加入真实响应正文，也不能复用于 Generic profile。
    """

    events = (
        {"choices": [{"delta": {"content": "contract"}}]},
        {"choices": [], "usage": dict(usage)},
    )
    body = b"".join(
        b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
        for event in events
    )
    return body + b"data: [DONE]\n\n"


def _generic_stream() -> bytes:
    """生成 Generic profile 的固定 SSE，不伪造 OpenAI 私有终止或 final usage。"""

    event = {"choices": [{"delta": {"content": "generic"}}]}
    return b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _invalid_usage_payload(root: str) -> dict[str, object]:
    """构造非法数值和敏感键并存的 usage，用于验证归零、warning 和递归脱敏。"""

    usage = {
        "prompt_tokens": "invalid",
        "completion_tokens": -7,
        "total_tokens": 1,
        "cached_tokens": -3,
        "api_key": SENSITIVE_CANARY,
    }
    return _success_payload(root, usage)


# 三组 usage 方言和两组期望值只含合成计数，用来证明 Adapter 归一规则而非保存上游样本。
_OPENAI_USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "total_tokens": 18,
    "cached_tokens": 2,
    "cost": "0.0123",
    "currency": "CNY",
}
_INPUT_OUTPUT_USAGE = {
    "input_tokens": 11,
    "output_tokens": 7,
    "total_tokens": 18,
    "cache_tokens": 2,
    "cost": "0.0123",
    "currency": "CNY",
}
_GEMINI_USAGE = {
    "promptTokenCount": 11,
    "candidatesTokenCount": 7,
    "totalTokenCount": 18,
    "cachedContentTokenCount": 2,
    "cost": "0.0123",
    "currency": "CNY",
}
_STANDARD_EXPECTATION = ContractUsageExpectation(
    input_tokens=11,
    output_tokens=7,
    total_tokens=18,
    cache_tokens=2,
    estimated=False,
    provider_cost_amount="0.0123",
    provider_cost_currency="CNY",
)
_INVALID_EXPECTATION = ContractUsageExpectation(
    input_tokens=0,
    output_tokens=0,
    total_tokens=1,
    cache_tokens=0,
    estimated=False,
    warning_codes=("invalid_input_tokens", "invalid_output_tokens", "invalid_cache_tokens"),
)


def _fixture(
    *,
    case_id: str,
    spec_id: str,
    provider_type: str,
    adapter_name: str,
    usage_root: str,
    usage: Mapping[str, object],
    openai_stream: bool,
) -> AdapterContractFixture:
    """创建一个显式注册项，统一固定安全占位数据但不推导 Provider 身份。

    Provider 身份、case ID 和 usage 方言均由下方逐项源码声明；该 helper 只消除无业务差异的
    合成 JSON/SSE 重复。
    """

    success = _success_payload(usage_root, usage)
    return AdapterContractFixture(
        contract_case_id=case_id,
        tenant_id=CONTRACT_TENANT_ID,
        spec_id=spec_id,
        provider_type=provider_type,
        adapter_name=adapter_name,
        adapter_version="1.0.0",
        success_payload=success,
        stream_body=_openai_stream(usage) if openai_stream else _generic_stream(),
        standard_usage_payload=success,
        standard_usage=_STANDARD_EXPECTATION,
        invalid_usage_payload=_invalid_usage_payload(usage_root),
        invalid_usage=_INVALID_EXPECTATION,
    )


# 九个注册项逐一对应冻结 spec；任何新增 spec 都必须显式增加一项并接受代码审查。
CONTRACT_FIXTURES: dict[str, AdapterContractFixture] = {
    "prd063_custom_minimum_contract": _fixture(
        case_id="prd063_custom_minimum_contract",
        spec_id="adapter-spec-custom-v1",
        provider_type="custom",
        adapter_name="generic_http",
        usage_root="usage_metadata",
        usage=_INPUT_OUTPUT_USAGE,
        openai_stream=False,
    ),
    "prd063_dashscope_minimum_contract": _fixture(
        case_id="prd063_dashscope_minimum_contract",
        spec_id="adapter-spec-dashscope-v1",
        provider_type="dashscope",
        adapter_name="openai_compatible",
        usage_root="usage_metadata",
        usage=_INPUT_OUTPUT_USAGE,
        openai_stream=True,
    ),
    "prd063_deepseek_minimum_contract": _fixture(
        case_id="prd063_deepseek_minimum_contract",
        spec_id="adapter-spec-deepseek-v1",
        provider_type="deepseek",
        adapter_name="openai_compatible",
        usage_root="usage",
        usage=_OPENAI_USAGE,
        openai_stream=True,
    ),
    "prd063_hunyuan_minimum_contract": _fixture(
        case_id="prd063_hunyuan_minimum_contract",
        spec_id="adapter-spec-hunyuan-v1",
        provider_type="hunyuan",
        adapter_name="openai_compatible",
        usage_root="usageMetadata",
        usage=_GEMINI_USAGE,
        openai_stream=True,
    ),
    "prd063_migration_compatible_minimum_contract": _fixture(
        case_id="prd063_migration_compatible_minimum_contract",
        spec_id="adapter-spec-migration_compatible-v1",
        provider_type="migration_compatible",
        adapter_name="generic_http",
        usage_root="usageMetadata",
        usage=_GEMINI_USAGE,
        openai_stream=False,
    ),
    "prd063_openai_compatible_minimum_contract": _fixture(
        case_id="prd063_openai_compatible_minimum_contract",
        spec_id="adapter-spec-openai_compatible-v1",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        usage_root="output.usage",
        usage=_OPENAI_USAGE,
        openai_stream=True,
    ),
    "prd063_volcengine_minimum_contract": _fixture(
        case_id="prd063_volcengine_minimum_contract",
        spec_id="adapter-spec-volcengine-v1",
        provider_type="volcengine",
        adapter_name="openai_compatible",
        usage_root="usage",
        usage=_INPUT_OUTPUT_USAGE,
        openai_stream=True,
    ),
    "prd063_wenxin_minimum_contract": _fixture(
        case_id="prd063_wenxin_minimum_contract",
        spec_id="adapter-spec-wenxin-v1",
        provider_type="wenxin",
        adapter_name="openai_compatible",
        usage_root="usage",
        usage=_OPENAI_USAGE,
        openai_stream=True,
    ),
    "prd063_zhipu_minimum_contract": _fixture(
        case_id="prd063_zhipu_minimum_contract",
        spec_id="adapter-spec-zhipu-v1",
        provider_type="zhipu",
        adapter_name="openai_compatible",
        usage_root="usage",
        usage=_OPENAI_USAGE,
        openai_stream=True,
    ),
}
