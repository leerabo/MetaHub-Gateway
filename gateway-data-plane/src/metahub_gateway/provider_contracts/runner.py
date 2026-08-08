"""执行 Provider Adapter exact-set 合并门禁并生成安全运行报告。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import httpx
from metahub_gateway.api import openai_compatible as gateway_api_module
from metahub_gateway.api.openai_compatible import _SseUsageCollector
from metahub_gateway.providers import base as provider_base_module
from metahub_gateway.providers import factory as provider_factory_module
from metahub_gateway.providers import generic_http as provider_generic_module
from metahub_gateway.providers import keypool as provider_keypool_module
from metahub_gateway.providers import openai_compatible as provider_adapter_module
from metahub_gateway.providers.base import NormalizedUsage
from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.providers.keypool import SelectedProviderKey
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime import provider_error_map as provider_error_map_module
from metahub_shared.runtime import provider_adapter_spec as provider_spec_module
from metahub_shared.runtime import usage as usage_module
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    CATALOG_VERSION,
    ProviderAdapterSpec,
    provider_adapter_spec,
)

from .fixtures import CONTRACT_FIXTURES, SENSITIVE_CANARY
from .matrix import build_contract_matrix, validate_result_coverage
from .mock_server import MockRequestObservation, MockResponsePlan, run_mock_upstream
from .models import (
    AdapterContractFixture,
    ContractCaseDefinition,
    ContractCaseResult,
    ContractRunReport,
    ContractUsageExpectation,
)


# 报告 schema 与 runner 版本独立演进；任一变化都必须同步 Control 严格 validator。
REPORT_SCHEMA_VERSION = "adapter-contract-report-v1"
RUNNER_VERSION = "1.0.0"
# case 失败码是 API、数据库和 Webhook 共同白名单，禁止拼入 Provider 原值。
_FAILURE_CODES = {
    "request": "adapter.contract_request_failed",
    "non_stream": "adapter.contract_non_stream_failed",
    "stream": "adapter.contract_stream_failed",
    "error": "adapter.contract_error_mapping_failed",
    "usage": "adapter.contract_usage_failed",
}
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_TRACE_ID = re.compile(r"trc_[A-Za-z0-9][A-Za-z0-9._:-]{0,155}\Z")
_TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
# 合同 runner 只在三类受控错误 case 中过滤完全匹配的合成 warning；logger 名称固定为
# 生产 Adapter 的结构化日志通道，未知日志、错误码漂移或真实请求都不得被吞掉。
_PROVIDER_ADAPTER_LOGGER = logging.getLogger("metahub.provider_adapter")


class _ExpectedAdapterErrorLogFilter(logging.Filter):
    """仅过滤一个预期合成上游错误对应的精确结构化日志。

    参数包含本 case 新生成的 request/trace、固定 HTTP 状态和平台错误码。任一字段缺失或
    漂移都会返回 ``True`` 保留日志，因此本过滤器不会掩盖新的 Adapter 失败。
    """

    def __init__(
        self,
        *,
        request_id: str,
        trace_id: str,
        http_status: int,
        error_code: str,
    ) -> None:
        """保存只由 runner 生成的安全匹配条件，不接收响应正文或凭据。"""

        super().__init__()
        self._request_id = request_id
        self._trace_id = trace_id
        self._http_status = http_status
        self._error_code = error_code

    def filter(self, record: logging.LogRecord) -> bool:
        """匹配预期 warning 时返回假，其余记录全部保留。"""

        payload = getattr(record, "payload", None)
        expected = (
            record.name == "metahub.provider_adapter"
            and record.getMessage() == "provider_adapter.error"
            and getattr(record, "request_id", None) == self._request_id
            and getattr(record, "trace_id", None) == self._trace_id
            and getattr(record, "tenant_id", None) == "ci-contract"
            and isinstance(payload, Mapping)
            and payload.get("http_status") == self._http_status
            and payload.get("error_code") == self._error_code
        )
        return not expected


@contextmanager
def _suppress_expected_adapter_error_log(
    *,
    request_id: str,
    trace_id: str,
    http_status: int,
    error_code: str,
):
    """在单个预期错误请求期间安装精确过滤器，并在任何异常后可靠移除。"""

    log_filter = _ExpectedAdapterErrorLogFilter(
        request_id=request_id,
        trace_id=trace_id,
        http_status=http_status,
        error_code=error_code,
    )
    _PROVIDER_ADAPTER_LOGGER.addFilter(log_filter)
    try:
        yield
    finally:
        _PROVIDER_ADAPTER_LOGGER.removeFilter(log_filter)


def _utc_text(value: datetime | None = None) -> str:
    """把当前或指定时间规范为无微秒 UTC 文本，保持报告与数据库排序一致。"""

    current = value or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_hash(value: Mapping[str, object]) -> str:
    """对仅含安全断言结论的映射计算 canonical SHA-256。"""

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _route(spec: ProviderAdapterSpec, base_url: str) -> GatewayRoute:
    """构造只含合成租户和 Provider 身份的生产 GatewayRoute。"""

    auth_type = "bearer" if "bearer" in spec.allowed_auth_types else spec.allowed_auth_types[0]
    return GatewayRoute(
        tenant_id="ci-contract",
        model_id=f"contract-model-{spec.provider_type}",
        model_alias="contract-model",
        upstream_model_name="contract-upstream-model",
        capabilities={"chat": True, "stream": True},
        provider_id=f"contract-provider-{spec.provider_type}",
        provider_code=f"contract-{spec.provider_type}",
        provider_type=spec.provider_type,
        provider_auth_type=auth_type,
        provider_name="Contract Provider",
        provider_base_url=base_url,
    )


def _selected_key() -> SelectedProviderKey:
    """返回只在进程内构造 Authorization 的合成 Key，repr 永不暴露其值。"""

    return SelectedProviderKey(None, SENSITIVE_CANARY, "contract", None)


def _request_payload(*, stream: bool) -> dict[str, object]:
    """生成不含客户数据的合成 Chat 请求；runner 永不把该字典写入报告。"""

    return {
        "model": "contract-model",
        "messages": [{"role": "user", "content": "contract-input"}],
        "stream": stream,
    }


def _build_request(
    spec: ProviderAdapterSpec,
    base_url: str,
    *,
    stream: bool,
    request_id: str,
    trace_id: str,
    auth_type: str | None = None,
):
    """使用生产工厂构造请求并补齐真实 Gateway API normally 写入的链路 ID。

    返回 Adapter、请求和原始 payload。数据库 URL 与 credential ID 保持空值，错误合同不会产生
    KeyPool 持久副作用。
    """

    route = _route(spec, base_url)
    # request case 必须显式覆盖 none/bearer；这里只替换冻结路由字段，不根据 Key 是否存在猜测鉴权。
    if auth_type is not None:
        route = GatewayRoute(**{**asdict(route), "provider_auth_type": auth_type})
    adapter = adapter_for_route(route)
    payload = _request_payload(stream=stream)
    request = adapter.build_request(
        route,
        "chat/completions",
        payload,
        selected_key=_selected_key(),
    )
    request.request_id = request_id
    request.trace_id = trace_id
    return adapter, request, payload


def _assert_usage(usage: NormalizedUsage, expected: ContractUsageExpectation) -> None:
    """核对所有标准 usage 数值、估算来源和稳定 warning 集合。"""

    actual = (
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.cache_tokens,
        usage.estimated,
        usage.estimate_reason,
        str(usage.provider_cost_amount)
        if usage.provider_cost_amount is not None
        else None,
        usage.currency,
        usage.normalization_warnings,
    )
    wanted = (
        expected.input_tokens,
        expected.output_tokens,
        expected.total_tokens,
        expected.cache_tokens,
        expected.estimated,
        expected.estimate_reason,
        expected.provider_cost_amount,
        expected.provider_cost_currency,
        expected.warning_codes,
    )
    # usage 任一数值、估算来源、成本币种或 warning 漂移都会影响计费追踪，必须整体失败。
    if actual != wanted:
        raise AssertionError("usage contract mismatch")


def _assert_observation(
    observation: MockRequestObservation,
    spec: ProviderAdapterSpec,
    *,
    stream: bool,
) -> None:
    """核对 loopback 请求的安全投影，不读取 Authorization 值或原始 JSON。"""

    expected_auth = "bearer" in spec.allowed_auth_types
    expected_include_usage = stream and spec.stream_profile.allow_include_usage
    if (
        observation.request_count != 1
        or observation.method != "POST"
        or observation.path != "/v1/chat/completions"
        or not observation.parse_valid
        or observation.model != "contract-upstream-model"
        or observation.stream is not stream
        or observation.include_usage is not expected_include_usage
        or observation.has_authorization is not expected_auth
        or (expected_auth and observation.authorization_scheme != "Bearer")
    ):
        # 观察投影不含凭据正文，因此失败只能输出固定断言，不能拼接原 Header 或 payload。
        raise AssertionError("request observation mismatch")


async def _case_request(
    definition: ContractCaseDefinition,
    spec: ProviderAdapterSpec,
    fixture: AdapterContractFixture,
    request_id: str,
    trace_id: str,
) -> Mapping[str, object]:
    """验证指定 auth 的请求映射、目录身份、Header 约束和 payload 不变性。"""

    adapter, request, payload = _build_request(
        spec,
        "http://127.0.0.1:1/v1",
        stream=True,
        request_id=request_id,
        trace_id=trace_id,
        auth_type=definition.auth_type,
    )
    expected_auth = definition.auth_type == "bearer"
    if (
        request.tenant_id != fixture.tenant_id
        or request.provider_type != spec.provider_type
        or request.adapter_name != spec.adapter_name
        or request.adapter_version != spec.adapter_version
        or request.adapter_catalog_hash != CATALOG_HASH
        or request.payload.get("model") != "contract-upstream-model"
        or ("Authorization" in request.headers) is not expected_auth
        or payload["model"] != "contract-model"
        or ("stream_options" in payload)
        or request.request_id != request_id
        or request.trace_id != trace_id
        or adapter.name != spec.adapter_name
    ):
        # 任一租户、模型、版本或鉴权漂移都表示生产构造器不再满足冻结 spec。
        raise AssertionError("request contract mismatch")
    include_usage = isinstance(request.payload.get("stream_options"), Mapping) and (
        request.payload["stream_options"].get("include_usage") is True
    )
    # OpenAI 与 Generic 对 include_usage 的语义相反，禁止用统一默认值掩盖协议差异。
    if include_usage is not spec.stream_profile.allow_include_usage:
        raise AssertionError("stream option contract mismatch")
    return {
        "auth": expected_auth,
        "include_usage": include_usage,
        "tenant": True,
        "version": True,
    }


async def _case_non_stream(
    spec: ProviderAdapterSpec,
    fixture: AdapterContractFixture,
    request_id: str,
    trace_id: str,
) -> Mapping[str, object]:
    """通过 loopback 运行生产 `send_non_stream` 并验证标准响应与 usage。"""

    body = json.dumps(fixture.success_payload, separators=(",", ":")).encode("utf-8")
    plan = MockResponsePlan(200, "application/json", body)
    with run_mock_upstream(plan) as (base_url, observation):
        adapter, request, _ = _build_request(
            spec,
            base_url,
            stream=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        response = await adapter.send_non_stream(request)
    _assert_observation(observation, spec, stream=False)
    _assert_usage(response.usage, fixture.standard_usage)
    # 成功正文即使可解析，非 200 状态也不能作为非流式合同通过。
    if response.status_code != 200:
        raise AssertionError("non-stream status mismatch")
    return {"status": response.status_code, "usage": True, "request": True}


async def _case_stream(
    spec: ProviderAdapterSpec,
    fixture: AdapterContractFixture,
    request_id: str,
    trace_id: str,
) -> Mapping[str, object]:
    """通过 loopback 运行生产 SSE 握手、读取、计量 collector 与幂等关闭。"""

    plan = MockResponsePlan(200, "text/event-stream", fixture.stream_body)
    with run_mock_upstream(plan) as (base_url, observation):
        adapter, request, _ = _build_request(
            spec,
            base_url,
            stream=True,
            request_id=request_id,
            trace_id=trace_id,
        )
        stream = await adapter.send_stream(request)
        collector = _SseUsageCollector(adapter)
        chunks: list[bytes] = []
        async for chunk in stream.iter_bytes():
            chunks.append(chunk)
            collector.feed(chunk)
        await stream.aclose()
        usage = collector.finish(request.payload)
    _assert_observation(observation, spec, stream=True)
    received = b"".join(chunks)
    # 必须逐字节消费完整固定 SSE，防止 collector 在中途结束却误报成功。
    if received != fixture.stream_body:
        raise AssertionError("stream wire mismatch")
    has_done = b"data: [DONE]" in received
    # `[DONE]` 只属于声明终止标记的 OpenAI profile，Generic 不得被猜测式补齐。
    if has_done is not bool(spec.stream_profile.termination_marker):
        raise AssertionError("stream termination mismatch")
    # OpenAI final usage 必须与非流式方言一致；Generic 缺失时只能产生显式估算。
    if spec.stream_profile.final_usage_expected:
        _assert_usage(usage, fixture.standard_usage)
    elif not usage.estimated:
        # Generic profile 没有 final usage 时必须显式估算，不能伪装成上游权威用量。
        raise AssertionError("generic stream usage must be estimated")
    return {
        "wire": True,
        "termination": has_done,
        "estimated": usage.estimated,
        "closed": True,
    }


async def _case_error(
    definition: ContractCaseDefinition,
    spec: ProviderAdapterSpec,
    request_id: str,
    trace_id: str,
) -> Mapping[str, object]:
    """验证 401/429/5xx 完整发送链和 timeout 固定归一，且不回显 canary。"""

    expected = {
        "auth_401": (401, "invalid_api_key", "gateway.upstream_auth_failed"),
        "rate_limit_429": (429, "rate_limit", "gateway.upstream_rate_limited"),
        "server_5xx": (503, "server_error", "gateway.upstream_error"),
    }
    if definition.variant == "timeout":
        adapter, request, _ = _build_request(
            spec,
            "http://127.0.0.1:1/v1",
            stream=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        normalized = adapter.normalize_error(
            exc=httpx.ReadTimeout(SENSITIVE_CANARY),
            request=request,
        )
        # timeout 必须归一为可重试固定码，异常 canary 不能改变分类或进入报告。
        if normalized.code != "gateway.upstream_timeout" or not normalized.retryable:
            raise AssertionError("timeout error mismatch")
        return {"code": normalized.code, "retryable": normalized.retryable}
    status, error_type, error_code = expected[definition.variant]
    body = json.dumps(
        {"error": {"type": error_type, "message": SENSITIVE_CANARY}},
        separators=(",", ":"),
    ).encode("utf-8")
    plan = MockResponsePlan(status, "application/json", body)
    with run_mock_upstream(plan) as (base_url, observation):
        adapter, request, _ = _build_request(
            spec,
            base_url,
            stream=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 预期错误仍完整运行生产发送和归一链；只压制字段完全匹配的合成 warning。若映射成
        # 其他错误码，过滤器会保留日志，下面的断言也会把合同标为失败。
        with _suppress_expected_adapter_error_log(
            request_id=request_id,
            trace_id=trace_id,
            http_status=status,
            error_code=error_code,
        ):
            try:
                await adapter.send_non_stream(request)
            # 401/429/5xx 在生产发送链中应转换成统一 ApiError，而不是保留 httpx 正文。
            except ApiError as error:
                actual_code = error.code
                actual_message = error.message
            else:
                # 错误 HTTP 状态若未抛出会让失败请求被计为成功，必须立即阻断。
                raise AssertionError("error response unexpectedly passed")
    _assert_observation(observation, spec, stream=False)
    # 错误码必须精确匹配冻结映射，安全消息中也不得包含 mock canary。
    if actual_code != error_code or SENSITIVE_CANARY in actual_message:
        raise AssertionError("provider error contract mismatch")
    return {"http_status": status, "code": actual_code, "redacted": True}


async def _case_usage(
    definition: ContractCaseDefinition,
    spec: ProviderAdapterSpec,
    fixture: AdapterContractFixture,
    request_id: str,
    trace_id: str,
) -> Mapping[str, object]:
    """直接运行生产 `parse_usage`，验证标准、缺失和非法方言。"""

    adapter, _, _ = _build_request(
        spec,
        "http://127.0.0.1:1/v1",
        stream=False,
        request_id=request_id,
        trace_id=trace_id,
    )
    if definition.variant == "standard":
        usage = adapter.parse_usage(fixture.standard_usage_payload)
        _assert_usage(usage, fixture.standard_usage)
    elif definition.variant == "missing":
        usage = adapter.parse_usage({})
        # 缺失上游 usage 不能伪装为权威零值，必须保留统一估算原因供成本追踪。
        if not usage.estimated or usage.estimate_reason != "missing_upstream_usage":
            raise AssertionError("missing usage contract mismatch")
    else:
        usage = adapter.parse_usage(fixture.invalid_usage_payload)
        _assert_usage(usage, fixture.invalid_usage)
        # raw usage 的秘密类键必须在归一层删除，摘要前再次检查防止递归字段遗漏。
        if SENSITIVE_CANARY in json.dumps(usage.provider_raw_usage, sort_keys=True):
            raise AssertionError("usage sanitizer exposed canary")
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "total": usage.total_tokens,
        "cache": usage.cache_tokens,
        "estimated": usage.estimated,
        "warnings": list(usage.normalization_warnings),
    }


async def _execute_case(
    definition: ContractCaseDefinition,
    fixtures: Mapping[str, AdapterContractFixture],
    request_id: str,
    trace_id: str,
    run_id: str,
) -> ContractCaseResult:
    """执行单个 case，并把任何失败折叠为 kind 对应固定码和空敏感证据。"""

    started = time.perf_counter()
    failure_code: str | None = None
    evidence: Mapping[str, object] = {"passed": False, "kind": definition.case_kind}
    try:
        spec = provider_adapter_spec(definition.provider_type)
        fixture = fixtures[definition.contract_case_id]
        callbacks: dict[str, Callable[[], Awaitable[Mapping[str, object]]]] = {
            "request": lambda: _case_request(
                definition, spec, fixture, request_id, trace_id
            ),
            "non_stream": lambda: _case_non_stream(spec, fixture, request_id, trace_id),
            "stream": lambda: _case_stream(spec, fixture, request_id, trace_id),
            "error": lambda: _case_error(definition, spec, request_id, trace_id),
            "usage": lambda: _case_usage(
                definition, spec, fixture, request_id, trace_id
            ),
        }
        evidence = await callbacks[definition.case_kind]()
        status = "passed"
    # 原始异常可能含 URL、Header 或响应正文；先在异常边界折叠，再构造固定失败证据。
    except (ApiError, AssertionError, KeyError, RuntimeError, ValueError, httpx.HTTPError):
        status = "failed"
        failure_code = _FAILURE_CODES[definition.case_kind]
    duration_ms = min(max(int((time.perf_counter() - started) * 1000), 0), 300_000)
    safe_evidence = {
        "case_key": definition.case_key,
        "status": status,
        "failure_code": failure_code,
        "assertions": evidence if status == "passed" else {"passed": False},
    }
    return ContractCaseResult(
        case_key=definition.case_key,
        adapter_contract_case_id=(
            "acc_"
            + hashlib.sha256(
                f"{run_id}:{definition.case_key}".encode("utf-8")
            ).hexdigest()[:32]
        ),
        contract_case_id=definition.contract_case_id,
        tenant_id=definition.tenant_id,
        spec_id=definition.spec_id,
        provider_type=definition.provider_type,
        adapter_name=definition.adapter_name,
        adapter_version=definition.adapter_version,
        case_kind=definition.case_kind,
        variant=definition.variant,
        auth_type=definition.auth_type,
        status=status,
        failure_code=failure_code,
        duration_ms=duration_ms,
        evidence_hash=_safe_hash(safe_evidence),
    )


async def run_merge_gate(
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    source_revision_hash: str,
    fixtures: Mapping[str, AdapterContractFixture] = CONTRACT_FIXTURES,
    definitions: Sequence[ContractCaseDefinition] | None = None,
) -> ContractRunReport:
    """执行完整离线合并门禁，并返回可严格持久化的安全报告。

    tenant 是报告最终归属的部署租户；固定 ``ci-contract`` 只属于合成夹具，不能覆盖调用方显式
    tenant。真实租户探测使用独立 suite，不能把 subset 结果冒充 coverage complete。失败 case
    继续执行，以便一次报告显示全部兼容性回归。
    """

    # 报告 tenant 可由受控 CI 指定，但必须先通过短标识白名单，不能夹带路径或日志控制字符。
    if not _TENANT_ID.fullmatch(tenant_id):
        raise ValueError("adapter.contract_tenant_invalid")
    if (
        not _REQUEST_ID.fullmatch(request_id)
        or not _TRACE_ID.fullmatch(trace_id)
        or not _LOWER_HEX_64.fullmatch(source_revision_hash)
    ):
        # 链路或源码摘要错误不能回显原值，避免控制字符和绝对路径进入 CI 输出。
        raise ValueError("adapter.contract_report_invalid")
    started = _utc_text()
    expected_matrix = build_contract_matrix(fixtures, tenant_id=tenant_id)
    matrix = tuple(definitions if definitions is not None else expected_matrix)
    validate_result_coverage(expected_matrix, (item.case_key for item in matrix))
    run_id = f"acr_{uuid4().hex}"
    results: list[ContractCaseResult] = []
    for definition in matrix:
        # 每个派生 case 都必须由报告 tenant 所有，防止注入跨租户定义后写入同一 run。
        if definition.tenant_id != tenant_id:
            raise ValueError("adapter.contract_tenant_invalid")
        results.append(
            await _execute_case(definition, fixtures, request_id, trace_id, run_id)
        )
    validate_result_coverage(expected_matrix, (item.case_key for item in results))
    results.sort(key=lambda item: item.case_key)
    passed = sum(result.status == "passed" for result in results)
    failed = len(results) - passed
    failure_codes = tuple(
        sorted(
            {
                result.failure_code
                for result in results
                if result.failure_code is not None
            }
        )
    )
    return ContractRunReport(
        schema_version=REPORT_SCHEMA_VERSION,
        tenant_id=tenant_id,
        run_id=run_id,
        request_id=request_id,
        trace_id=trace_id,
        catalog_version=CATALOG_VERSION,
        catalog_hash=CATALOG_HASH,
        runner_version=RUNNER_VERSION,
        suite_kind="merge_gate",
        mode="mock",
        source_revision_hash=source_revision_hash,
        status="passed" if failed == 0 else "failed",
        coverage_complete=True,
        total_cases=len(results),
        passed_cases=passed,
        failed_cases=failed,
        failure_codes=failure_codes,
        started_at=started,
        completed_at=_utc_text(),
        cases=tuple(results),
    )


def run_merge_gate_sync(**kwargs: object) -> ContractRunReport:
    """为 PowerShell/CLI 提供同步入口，禁止在已有 event loop 内隐式嵌套运行。"""

    return asyncio.run(run_merge_gate(**kwargs))


def contract_source_revision_hash() -> str:
    """组合框架、工厂、两类 Adapter 及共享归一实现摘要，不依赖 `.git` 可见性。

    返回值只包含 64 位摘要；文件路径和源码正文不会进入报告或 stdout。读取失败必须阻止
    merge gate，因为一个无法确定执行制品身份的报告不能作为合并证据。
    """

    digest = hashlib.sha256()
    digest.update(CATALOG_HASH.encode("ascii"))
    package_root = Path(__file__).resolve().parent
    # 使用固定逻辑名和显式文件清单，既覆盖 CLI/report/mock 等关键框架代码，也不会通过目录
    # 枚举把缓存、测试证据或机器路径纳入制品身份。
    framework_sources = tuple(
        (f"provider_contracts/{name}", package_root / name)
        for name in (
            "__init__.py",
            "__main__.py",
            "cli.py",
            "fixtures.py",
            "matrix.py",
            "mock_server.py",
            "models.py",
            "network.py",
            "probe.py",
            "report.py",
            "runner.py",
            "upload.py",
        )
    )
    module_sources = (
        ("gateway/api/openai_compatible.py", gateway_api_module.__file__),
        ("gateway/providers/base.py", provider_base_module.__file__),
        ("gateway/providers/factory.py", provider_factory_module.__file__),
        ("gateway/providers/generic_http.py", provider_generic_module.__file__),
        ("gateway/providers/keypool.py", provider_keypool_module.__file__),
        ("gateway/providers/openai_compatible.py", provider_adapter_module.__file__),
        ("shared/provider_adapter_spec.py", provider_spec_module.__file__),
        ("shared/provider_error_map.py", provider_error_map_module.__file__),
        ("shared/usage.py", usage_module.__file__),
    )
    for logical_name, source in (*framework_sources, *module_sources):
        # 模块没有可定位源码时无法证明执行制品身份，必须在生成报告前失败关闭。
        if source is None:
            raise RuntimeError("adapter.contract_source_unavailable")
        source_path = Path(source)
        # 路径存在但不是普通源码文件同样不能继续，避免特殊节点产生不稳定摘要。
        if not source_path.is_file():
            raise RuntimeError("adapter.contract_source_unavailable")
        # 每个文件先加固定逻辑名和自身摘要，避免简单字节拼接的边界歧义，同时不把开发机
        # 绝对路径编码进结果。
        digest.update(logical_name.encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(source_path.read_bytes()).digest())
    return digest.hexdigest()
