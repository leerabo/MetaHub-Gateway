"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    ProviderAdapterSpec,
    ProviderAdapterSpecError,
    validate_provider_auth,
)
from metahub_shared.runtime.provider_error_map import (
    MAX_ERROR_BODY_BYTES,
    ProviderErrorFacts,
    ProviderErrorMapLease,
    builtin_provider_error_map_lease,
    extract_provider_error_facts,
    normalize_provider_error,
)
from metahub_shared.runtime.usage import sanitize_provider_usage

from metahub_gateway.cache_affinity.models import CacheAffinitySelectionHandle
from metahub_gateway.routing.models import GatewayRoute

from .base import (
    NormalizedProviderError,
    NormalizedResponse,
    NormalizedUsage,
    ProviderRequest,
    ProviderStreamResponse,
    UpstreamApiError,
)
from .keypool import SelectedProviderKey, mark_provider_key_error, select_provider_key

logger = logging.getLogger("metahub.provider_adapter")

# 握手取消后的清理不能无限占用已取消请求；context 与 client 会并行获得同一个短等待窗口。
_STREAM_HANDSHAKE_CLEANUP_TIMEOUT_SECONDS = 1.0
# 错误正文总读取 deadline 与 Shared 64 KiB 上限固定，不能被 Provider 响应或租户规则修改。
_ERROR_BODY_READ_TIMEOUT_SECONDS = 1.0
_ERROR_BODY_CHUNK_BYTES = 8 * 1024

# 上游 type/code 由远端任意控制，结构化日志只能保存本平台错误码映射出的固定分类。
_SAFE_PROVIDER_ERROR_LOG_TYPES = {
    "gateway.upstream_auth_failed": "auth_failed",
    "gateway.upstream_rate_limited": "rate_limited",
    "gateway.upstream_quota_exceeded": "quota_exceeded",
    "gateway.upstream_model_not_found": "model_not_found",
    "gateway.upstream_resource_not_found": "resource_not_found",
    "gateway.upstream_timeout": "timeout",
    "gateway.upstream_request_invalid": "request_invalid",
    "gateway.upstream_unavailable": "unavailable",
    "gateway.upstream_error": "upstream_error",
}

# 只要任一已知 Token 字段存在，就认为上游明确提供了 Token 口径；字段值非法时由 warning 标记，
# 不能再用本地估算静默覆盖 Provider 原始异常，否则财务排障时无法区分缺失与错误数据。
_TOKEN_USAGE_KEYS = frozenset(
    {
        "prompt_tokens",
        "input_tokens",
        "promptTokenCount",
        "prompt_token_count",
        "completion_tokens",
        "output_tokens",
        "candidatesTokenCount",
        "candidates_token_count",
        "total_tokens",
        "totalTokenCount",
        "total_token_count",
    }
)


class UpstreamJsonResponse(dict):
    """定义 Provider 与 KeyPool 管理 的响应 DTO，统一携带 request_id/trace_id 便于前端和审计追踪。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    status_code: int


class OpenAICompatibleAdapter:
    """OpenAI-compatible Provider Adapter。

    该类是 M0 默认 Adapter：它把内部标准模型 ID 替换为 route 中解析出的
    provider_model_id/upstream_model_name，并对 usage/error 做最小归一化。
    """

    name = "openai_compatible"
    version = "1.0.0"

    def build_request(
        self,
        route: GatewayRoute,
        endpoint: str,
        payload: dict[str, Any],
        config: AppConfig | None = None,
        *,
        selected_key: SelectedProviderKey | None = None,
        cache_affinity_selection: CacheAffinitySelectionHandle | None = None,
        error_map_lease: ProviderErrorMapLease | None = None,
    ) -> ProviderRequest:
        """实现 Provider 与 KeyPool 管理 的 build request 逻辑，保持职责、输入边界和错误码口径清晰。

        边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
        """

        spec = _validated_adapter_spec(route, self.name, self.version)
        auth_type = route.provider_auth_type.strip().lower()
        upstream_payload = dict(payload)
        upstream_payload["model"] = route.upstream_model_name
        # 只有 spec 明确允许的协议才注入 OpenAI stream option；通用 JSON/SSE 端点可能拒绝它。
        if upstream_payload.get("stream") is True and spec.stream_profile.allow_include_usage:
            # OpenAI-compatible 路径需要最终 usage chunk，且必须复制调用方字典避免原地污染。
            stream_options = upstream_payload.get("stream_options")
            safe_stream_options = (
                dict(stream_options) if isinstance(stream_options, Mapping) else {}
            )
            safe_stream_options["include_usage"] = True
            upstream_payload["stream_options"] = safe_stream_options
        # 已提交亲和 Selection 后，凭据也是不可变目标的一部分。认证路由若丢失显式 Key 必须
        # 在建连前失败，绝不能重新调用旧 KeyPool 选择另一把 Key；无认证路由继续允许 None。
        if selected_key is None and auth_type == "bearer":
            if cache_affinity_selection is not None:
                raise ApiError("provider_key.unavailable")
            # 非亲和路径保留旧 Adapter 兼容入口，由当前租户 KeyPool 完成一次正常选择。
            selected_key = _selected_provider_key(route, config)
        elif auth_type == "none":
            # 无鉴权 spec 即使调用方误传 Key 也必须丢弃，避免向客户本地端点意外发送凭据。
            selected_key = None
        return ProviderRequest(
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            provider_type=route.provider_type,
            adapter_name=self.name,
            adapter_version=spec.adapter_version,
            adapter_catalog_hash=CATALOG_HASH,
            endpoint=endpoint,
            url=upstream_url(route, endpoint),
            headers=upstream_headers(spec, auth_type, selected_key),
            payload=upstream_payload,
            tenant_id=route.tenant_id,
            model_id=route.model_id,
            database_url=config.database_url if config else None,
            provider_credential_id=selected_key.provider_credential_id if selected_key else None,
            provider_key_source=selected_key.source if selected_key else None,
            cache_affinity_selection=cache_affinity_selection,
            stream_connect_timeout_seconds=config.stream_connect_timeout_seconds if config else 30,
            stream_write_timeout_seconds=config.stream_write_timeout_seconds if config else 30,
            stream_pool_timeout_seconds=config.stream_pool_timeout_seconds if config else 5,
            error_map_lease=error_map_lease or builtin_provider_error_map_lease(route.tenant_id),
        )

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """发送非流式 JSON 请求，并返回标准化响应。"""

        try:
            # 上游 Endpoint 是租户显式配置的模型服务地址，不能让系统代理把本机或内网地址改路。
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                headers = {**request.headers, "Accept-Encoding": "identity"}
                # 非 2xx 必须保持 wire stream，先分类再决定是否读取完整成功正文。
                async with client.stream(
                    "POST", request.url, json=request.payload, headers=headers
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        facts = await _read_provider_error_facts(response)
                        self._raise_adapter_error(
                            request,
                            self.normalize_error(
                                response=response, request=request, extracted_facts=facts
                            ),
                        )
                    # 成功响应沿用完整 JSON 读取；64 KiB 门禁只约束不可信错误正文。
                    await response.aread()
                    try:
                        data = response.json()
                    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
                    except ValueError as exc:
                        self._raise_adapter_error(
                            request,
                            self.normalize_error(response=response, exc=exc, request=request),
                        )
                    if not isinstance(data, dict):
                        self._raise_adapter_error(
                            request, self.normalize_error(response=response, request=request)
                        )
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        except (httpx.InvalidURL, httpx.TimeoutException, httpx.HTTPError) as exc:
            self._raise_adapter_error(request, self.normalize_error(exc=exc, request=request))
        usage = self.parse_usage(data)
        # 用量成本是审计账本字段，缺失或异常必须进入可追踪分支。
        if usage.estimated:
            # 上游缺少 usage 时只做长度估算，不把 prompt 或 response 文本写入账本与日志。
            usage = _estimate_missing_usage(request.payload, data, usage)
        _log_usage_normalized(request, usage)
        return NormalizedResponse(
            payload=data,
            usage=usage,
            status_code=response.status_code,
        )

    async def send_stream(self, request: ProviderRequest) -> ProviderStreamResponse:
        """打开 SSE 上游连接。

        响应开始前先检查 HTTP 状态，避免客户端已经收到 200 后才暴露上游 502/503。
        """

        # body read 不设置 httpx timeout，首块和块间停顿由编排器的 Inter-Token Watchdog 判定；
        # 建连、写请求和连接池等待仍必须有硬上限，防止握手前永久占用工作协程。
        timeout = httpx.Timeout(
            connect=float(request.stream_connect_timeout_seconds),
            read=None,
            write=float(request.stream_write_timeout_seconds),
            pool=float(request.stream_pool_timeout_seconds),
        )
        # 流式调用同样禁用环境代理，避免 SSE 连接被本机代理接管后返回非 Provider 响应。
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        stream: Any | None = None
        try:
            stream = client.stream(
                "POST",
                request.url,
                json=request.payload,
                # 禁止 httpx 自动解压不受控错误正文；成功 SSE 目前同样按 identity 传输。
                headers={**request.headers, "Accept-Encoding": "identity"},
            )
            response = await stream.__aenter__()
        # 客户端在响应头握手期间断开时，CancelledError 不属于普通 Provider 错误。先有界释放
        # 两类网络资源，再原样传播取消，让上层按客户端断开而不是 5xx/质量失败收口。
        except asyncio.CancelledError as exc:
            await _close_stream_handshake_resources(client, stream, exc)
            raise
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        except (httpx.InvalidURL, httpx.TimeoutException, httpx.HTTPError) as exc:
            await _close_stream_handshake_resources(client, stream, exc)
            self._raise_adapter_error(request, self.normalize_error(exc=exc, request=request))
        # Adapter/第三方库的未预期握手异常不应被错误归类；先尽力清理两类资源再原样抛出，
        # 上层统一异常边界负责生成安全错误码，且这里绝不记录可能含 Endpoint 的异常正文。
        except Exception as exc:
            await _close_stream_handshake_resources(client, stream, exc)
            raise
        if response.status_code < 200 or response.status_code >= 300:
            # response 尚未关闭时按同一 64 KiB wire helper 提取事实；读取失败只降级状态码分类。
            facts = await _read_provider_error_facts(response)
            try:
                await stream.__aexit__(None, None, None)
            # context 退出异常也不能跳过 client 关闭，否则连接池资源会泄漏到后续请求。
            finally:
                await client.aclose()
            self._raise_adapter_error(
                request,
                self.normalize_error(response=response, request=request, extracted_facts=facts),
            )
        return ProviderStreamResponse(client=client, response=response, stream_context=stream)

    def parse_usage(self, payload: Mapping[str, Any]) -> NormalizedUsage:
        """把 OpenAI、通用/Anthropic 与 Gemini usage dialect 归一为统一结构。

        仅提取 usage 子对象并立即脱敏；Provider 自报成本不会覆盖后续 PricePlan 内部成本。
        """

        raw_mapping = _provider_usage_mapping(payload)
        provider_raw_usage = sanitize_provider_usage(raw_mapping)
        warnings: list[str] = []
        input_tokens = _token_field(
            raw_mapping,
            ("prompt_tokens", "input_tokens", "promptTokenCount", "prompt_token_count"),
            "invalid_input_tokens",
            warnings,
        )
        output_tokens = _token_field(
            raw_mapping,
            (
                "completion_tokens",
                "output_tokens",
                "candidatesTokenCount",
                "candidates_token_count",
            ),
            "invalid_output_tokens",
            warnings,
        )
        total_tokens = _token_field(
            raw_mapping,
            ("total_tokens", "totalTokenCount", "total_token_count"),
            "invalid_total_tokens",
            warnings,
        )
        minimum_total = input_tokens + output_tokens
        # 总 Token 不能小于输入与输出之和；不一致时采用可解释的组件合计，避免成本被低估。
        if total_tokens < minimum_total:
            # 只有 Provider 明确返回了非零矛盾值才告警；完全缺失的 total 由组件合计正常补齐。
            if total_tokens > 0:
                warnings.append("total_tokens_less_than_components")
            total_tokens = minimum_total
        cache_tokens = _cache_tokens(raw_mapping, warnings)
        provider_cost, provider_currency, provider_cost_cny = _provider_cost(raw_mapping, warnings)
        has_token_usage = any(key in raw_mapping for key in _TOKEN_USAGE_KEYS)
        return NormalizedUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cache_tokens=cache_tokens,
            estimated=not has_token_usage,
            estimate_reason="missing_upstream_token_fields"
            if raw_mapping and not has_token_usage
            else ("missing_upstream_usage" if not raw_mapping else None),
            cost_cny=provider_cost_cny,
            currency=provider_currency,
            provider_cost_amount=provider_cost,
            normalization_warnings=tuple(dict.fromkeys(warnings)),
            raw_usage=provider_raw_usage,
        )

    def normalize_error(
        self,
        response: httpx.Response | None = None,
        exc: Exception | None = None,
        *,
        request: ProviderRequest | None = None,
        extracted_facts: ProviderErrorFacts | None = None,
    ) -> NormalizedProviderError:
        """用冻结 lease 按 guard -> tenant -> builtin 归一错误并返回安全 evidence。

        参数：可空响应/异常、可空标准请求和可复用的有界事实。返回统一 Provider 错误。
        关键约束：旧调用未传 request 时使用兼容 builtin lease；任何原始 body/type/message 都不会
        进入返回对象 repr、日志或数据库，租户规则也不能覆盖鉴权、超时和 5xx guard。
        """

        http_status = response.status_code if response is not None else None
        transport_kind = _transport_kind(exc, response)
        facts = extracted_facts or _facts_from_buffered_response(
            response, upstream_http_status=http_status, transport_kind=transport_kind
        )
        tenant_id = request.tenant_id if request is not None else "default"
        provider_id = request.provider_id if request is not None else "compatibility"
        lease = request.error_map_lease if request is not None else None
        # Adapter 只接受请求冻结的同租户租约，缺失或错租户时用可信 tenant 的 builtin 失败关闭。
        if lease is None or lease.tenant_id != tenant_id:
            lease = builtin_provider_error_map_lease(tenant_id)
        result = normalize_provider_error(facts, lease=lease, provider_id=provider_id)
        provider_error_type = (
            facts.provider_error_type_class
            or facts.provider_error_code_class
            or (exc.__class__.__name__ if exc is not None else "http_status")
        )
        return NormalizedProviderError(
            code=result.code,
            message=result.message,
            http_status=http_status,
            retryable=result.retryable,
            provider_error_type=provider_error_type,
            error_category=result.error_category,
            transport_kind=result.transport_kind,
            evidence=result.evidence,
        )

    def _raise_adapter_error(
        self, request: ProviderRequest, error: NormalizedProviderError
    ) -> None:
        """集中处理 Provider Adapter 错误映射。

        边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
        """

        mark_provider_key_error(
            request.database_url,
            tenant_id=request.tenant_id,
            provider_id=request.provider_id,
            provider_code=request.provider_code,
            model_id=request.model_id,
            provider_credential_id=request.provider_credential_id,
            request_id=request.request_id,
            trace_id=request.trace_id,
            error_code=error.code,
            message=error.message,
            http_status=error.http_status,
        )
        logger.warning(
            "provider_adapter.error",
            extra={
                "event": "provider_adapter.error",
                "request_id": request.request_id,
                "trace_id": request.trace_id,
                "tenant_id": request.tenant_id,
                "provider_id": request.provider_id,
                "model_id": request.model_id,
                "payload": {
                    "provider_code": request.provider_code,
                    "adapter": request.adapter_name,
                    "adapter_version": request.adapter_version,
                    "adapter_catalog_hash": request.adapter_catalog_hash,
                    "http_status": error.http_status,
                    "error_code": error.code,
                    "retryable": error.retryable,
                    # error.type/code 可能被恶意上游填入密钥或正文，只记录本平台固定分类。
                    "provider_error_type": _safe_provider_error_log_type(error),
                },
            },
        )
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise UpstreamApiError(error)


async def _close_stream_handshake_resources(
    client: httpx.AsyncClient,
    stream_context: Any | None,
    failure: BaseException,
) -> None:
    """在流式握手失败或取消后有界、尽力释放 context 与 client。

    参数：本次调用独占的 HTTP client、可能已创建但未完成进入的 stream context，以及原始
    异常。返回：无。两项清理并发启动，避免 context 卡住时阻止 client 关闭；
    清理超时或清理自身异常都不得覆盖调用方的原始失败或取消语义，也不得记录敏感正文。
    """

    tasks: set[asyncio.Task[Any]] = {
        asyncio.create_task(client.aclose(), name="provider-stream-handshake-client-close")
    }
    if stream_context is not None:
        tasks.add(
            asyncio.create_task(
                stream_context.__aexit__(
                    type(failure),
                    failure,
                    failure.__traceback__,
                ),
                name="provider-stream-handshake-context-close",
            )
        )
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=_STREAM_HANDSHAKE_CLEANUP_TIMEOUT_SECONDS,
        )
    # 请求在清理窗口内再次被取消时，也只能缩短清理等待，不能把资源任务遗留为未观察异常。
    except asyncio.CancelledError:
        done = {task for task in tasks if task.done()}
        pending = tasks - done
    for task in pending:
        task.cancel()
        task.add_done_callback(_consume_cleanup_task_result)
    for task in done:
        _consume_cleanup_task_result(task)


def _consume_cleanup_task_result(task: asyncio.Task[Any]) -> None:
    """观察握手清理任务的终态，防止后台异常告警覆盖原始请求异常。

    参数：context/client 清理任务。返回：无。清理异常只能被丢弃，调用方随后仍会重新抛出
    原始异常；此处禁止记录异常文本，因为第三方异常可能携带 Endpoint。
    """

    try:
        task.result()
    # 清理任务自身被 cancel 是超时收口的正常结果，不能替换客户端的原始取消。
    except asyncio.CancelledError:
        return
    # 第三方清理异常同样不得替换客户端取消或把潜在 Endpoint 文本带入应用日志。
    except Exception:
        return


async def _read_provider_error_facts(response: httpx.Response) -> ProviderErrorFacts:
    """在 1 秒内用 raw stream 读取最多 64 KiB 错误正文并提取安全事实。

    Content-Encoding、Content-Length、超限、读取异常或 timeout 任一异常都丢弃已累积正文；
    外部 CancelledError 不在本函数捕获，调用方关闭资源后必须原样传播。
    """

    encoding_values = [
        value.strip().lower()
        for raw in response.headers.get_list("content-encoding")
        for value in raw.split(",")
    ]
    if any(value and value != "identity" for value in encoding_values):
        return extract_provider_error_facts(
            upstream_http_status=response.status_code,
            transport_kind="none",
            raw_body=None,
            body_truncated=True,
            known_body_length=None,
        )
    content_lengths = response.headers.get_list("content-length")
    declared_length: int | None = None
    if content_lengths:
        # 重复或逗号合并的 Content-Length 在代理间语义不一致，不能选择其中一个继续解析。
        if (
            len(content_lengths) != 1
            or "," in content_lengths[0]
            or not content_lengths[0].isascii()
            or not content_lengths[0].isdigit()
        ):
            return extract_provider_error_facts(
                upstream_http_status=response.status_code,
                transport_kind="none",
                raw_body=None,
                body_truncated=True,
                known_body_length=None,
            )
        declared_length = int(content_lengths[0])
        # 声明超过上限时不读取也不保留任何部分正文，只生成截断长度事实。
        if declared_length > MAX_ERROR_BODY_BYTES:
            return extract_provider_error_facts(
                upstream_http_status=response.status_code,
                transport_kind="none",
                raw_body=None,
                body_truncated=True,
                known_body_length=declared_length,
            )
    body = bytearray()
    total = 0
    try:
        async with asyncio.timeout(_ERROR_BODY_READ_TIMEOUT_SECONDS):
            async for chunk in response.aiter_raw(chunk_size=_ERROR_BODY_CHUNK_BYTES):
                total += len(chunk)
                # 累计长度越界立即丢弃全部候选，不能让前 64 KiB 的部分 JSON 参与规则命中。
                if total > MAX_ERROR_BODY_BYTES:
                    return extract_provider_error_facts(
                        upstream_http_status=response.status_code,
                        transport_kind="none",
                        raw_body=None,
                        body_truncated=True,
                        known_body_length=total,
                    )
                body.extend(chunk)
    # 内部读取时限只降低正文诊断精度，固定状态码仍可归一且不会泄露部分缓冲区。
    except TimeoutError:
        return extract_provider_error_facts(
            upstream_http_status=response.status_code,
            transport_kind="none",
            raw_body=None,
            body_truncated=True,
            known_body_length=None,
        )
    # 已被其他兼容调用方消费的 stream 与传输读取失败同样无法证明长度；只降级正文事实，
    # response context 的退出异常仍由外层正常传播，不能被这里吞掉。
    except (httpx.HTTPError, httpx.StreamError):
        return extract_provider_error_facts(
            upstream_http_status=response.status_code,
            transport_kind="none",
            raw_body=None,
            body_truncated=True,
            known_body_length=None,
        )
    if declared_length is not None and declared_length != total:
        return extract_provider_error_facts(
            upstream_http_status=response.status_code,
            transport_kind="none",
            raw_body=None,
            body_truncated=True,
            known_body_length=total,
        )
    return extract_provider_error_facts(
        upstream_http_status=response.status_code,
        transport_kind="none",
        raw_body=bytes(body),
        body_truncated=False,
        known_body_length=total,
    )


def _facts_from_buffered_response(
    response: httpx.Response | None,
    *,
    upstream_http_status: int | None,
    transport_kind: str,
) -> ProviderErrorFacts:
    """兼容同步 ``normalize_error`` 调用，只使用已经缓冲且不超过 64 KiB 的正文。"""

    body: bytes | None = None
    truncated = False
    if response is not None:
        try:
            candidate = response.content
        except httpx.ResponseNotRead:
            candidate = None
        # 兼容同步路径只使用完整且有界的已缓冲正文，超限内容全部丢弃并标记截断。
        if candidate is not None and len(candidate) <= MAX_ERROR_BODY_BYTES:
            body = candidate
        elif candidate is not None:
            truncated = True
    return extract_provider_error_facts(
        upstream_http_status=upstream_http_status,
        transport_kind=transport_kind,
        raw_body=body,
        body_truncated=truncated,
        known_body_length=len(body) if body is not None else None,
    )


def _transport_kind(exc: Exception | None, response: httpx.Response | None) -> str:
    """按异常类型映射固定 transport_kind，不读取可能包含 Endpoint 或请求信息的异常文本。"""

    if response is not None or exc is None:
        return "none"
    # URL、协议和本地请求格式错误属于配置问题，不记录可能包含 Endpoint 的异常文本。
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        return "local_configuration"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    # 连接和网络错误统一为固定类别，原异常消息可能含地址、代理或证书信息，不能持久化。
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return "network"
    return "other"


def _safe_provider_error_log_type(error: NormalizedProviderError) -> str:
    """把内存诊断类型映射为可安全落日志的固定枚举。

    参数：已归一化错误。返回值：不含上游任意字符串的稳定分类。未知或未来错误码统一为
    ``unknown``，避免新增 Provider 方言在未审查前意外扩大敏感日志面。
    """

    return _SAFE_PROVIDER_ERROR_LOG_TYPES.get(error.code, "unknown")


def _upstream_error_category(code: str, http_status: int | None) -> str:
    """把 Adapter 错误收敛为 Fallback 允许比较的四类安全枚举。"""

    # 超时可能没有 HTTP 状态码，必须优先依据 Adapter 的固定错误码识别。
    if code == "gateway.upstream_timeout":
        return "timeout"
    # 只有明确的 429 限流响应可触发 rate_limited，其他 429 映射异常按 unknown 处理。
    if http_status == 429 and code == "gateway.upstream_rate_limited":
        return "rate_limited"
    if http_status is not None and 500 <= http_status <= 599:
        return "upstream_5xx"
    return "other"


def upstream_headers(
    spec: ProviderAdapterSpec,
    auth_type: str,
    selected_key: SelectedProviderKey | None = None,
) -> dict[str, str]:
    """按冻结 spec 构造唯一允许的上游认证 Header。

    参数：已校验 spec、规范化 auth 和仅存于内存的选中 Key。返回：新 Header 字典。
    约束：none 不得携带凭据；bearer 的 Header 名与 scheme 只能来自发布制品，不能接受租户输入。
    """

    headers = {"Content-Type": "application/json"}
    # none 是明确的无凭据合同，必须在检查选中 Key 前直接返回，防止误发 Authorization。
    if auth_type == "none":
        return headers
    if (
        auth_type != "bearer"
        or selected_key is None
        or spec.credential_header is None
        or spec.credential_scheme is None
    ):
        # 该分支只处理进程内合同漂移，不回显 auth、Header、Endpoint 或 Key。
        raise ApiError("gateway.provider_adapter_spec_invalid")
    headers[spec.credential_header] = f"{spec.credential_scheme} {selected_key.plain_value}"
    return headers


def _validated_adapter_spec(
    route: GatewayRoute,
    adapter_name: str,
    adapter_version: str,
) -> ProviderAdapterSpec:
    """复核路由鉴权与当前 Adapter 制品是否匹配共享 spec。

    工厂已做首次门禁，但兼容入口和测试可以直接实例化 Adapter，因此构造请求前必须再次验证。
    返回冻结 spec；任何未知类型、auth、名称或版本漂移都映射为固定 Gateway 错误且不含原值。
    """

    try:
        spec = validate_provider_auth(route.provider_type, route.provider_auth_type)
    # 直接实例化 Adapter 的兼容入口也要把 Shared 领域异常收敛为 Gateway 固定错误。
    except ProviderAdapterSpecError as exc:
        code = (
            "gateway.provider_adapter_spec_missing"
            if exc.code == "provider.adapter_spec_missing"
            else "gateway.provider_adapter_spec_invalid"
        )
        # 不向 ASGI 错误处理中间件传播可能随 Shared 演进增加的内部诊断上下文。
        raise ApiError(code) from None
    if spec.adapter_name != adapter_name or spec.adapter_version != adapter_version:
        # 名称或版本漂移表示发布制品不一致，必须在构造 URL/Header 和联网前阻断。
        raise ApiError("gateway.provider_adapter_spec_invalid")
    return spec


def upstream_url(route: GatewayRoute, endpoint: str) -> str:
    """实现 Provider 与 KeyPool 管理 的 upstream url 逻辑，保持职责、输入边界和错误码口径清晰。

    边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
    """

    return f"{route.provider_base_url.rstrip('/')}/{endpoint.lstrip('/')}"


async def post_json(
    route: GatewayRoute, endpoint: str, payload: dict[str, object]
) -> dict[str, object]:
    """兼容旧调用点：向 OpenAI-compatible Provider 发起非流式 JSON 请求。"""

    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(route, endpoint, dict(payload))
    response = await adapter.send_non_stream(request)
    return response.payload


async def open_stream(
    route: GatewayRoute,
    endpoint: str,
    payload: dict[str, object],
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """兼容旧调用点：打开 SSE 上游连接。"""

    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(route, endpoint, dict(payload))
    stream_response = await adapter.send_stream(request)
    stream_response.response.extensions["metahub_stream_context"] = stream_response.stream_context
    return stream_response.client, stream_response.response


async def iter_sse(client: httpx.AsyncClient, response: httpx.Response) -> AsyncIterator[bytes]:
    """实现 Provider 与 KeyPool 管理 的 iter sse 逻辑，保持职责、输入边界和错误码口径清晰。

    边界：后续修改必须同时考虑租户隔离、request_id 追踪和敏感信息脱敏。
    """

    stream_context = response.extensions.get("metahub_stream_context")
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        try:
            if stream_context:
                await stream_context.__aexit__(None, None, None)
        # 兼容 context 的退出异常仍需传播，但不能因此泄漏独占 HTTP client。
        finally:
            await client.aclose()


def _selected_provider_key(
    route: GatewayRoute, config: AppConfig | None
) -> SelectedProviderKey | None:
    """为当前租户路由选择 Provider Key。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if route.provider_auth_type == "none":
        return None
    if config is not None:
        # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
        return select_provider_key(config, route)
    # 没有 AppConfig 就无法校验环境 Key 的租户、Provider 身份和基准 Endpoint；必须失败关闭，
    # 不能为了兼容直接调用 Adapter 而重新读取进程环境变量，绕开 Gateway 的租户边界。
    raise ApiError("provider_key.unavailable")


def _int_usage(value: object) -> int:
    """在 Provider 与 KeyPool 管理 内部复用 int usage 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    try:
        return max(int(value or 0), 0)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except (TypeError, ValueError):
        return 0


def _provider_usage_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """按稳定优先级提取 Provider usage 子对象，绝不回退到整个响应正文。"""

    for key in ("usage", "usage_metadata", "usageMetadata"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            return candidate
    output = payload.get("output")
    # 部分 Responses-compatible Endpoint 把 usage 放在 output 对象内，仍只提取该子对象。
    if isinstance(output, Mapping) and isinstance(output.get("usage"), Mapping):
        return output["usage"]
    return {}


def _token_field(
    raw_usage: Mapping[str, Any],
    keys: tuple[str, ...],
    warning: str,
    warnings: list[str],
) -> int:
    """读取一组 Provider Token 候选字段，非法或负数归零并记录稳定警告代码。"""

    for key in keys:
        # 候选字段按 Provider 明确优先级读取，缺失值继续尝试下一种方言。
        if key not in raw_usage or raw_usage.get(key) is None:
            continue
        value = raw_usage.get(key)
        try:
            parsed = int(value)
        # 非数字 Token 不能参与计费，归零并留下稳定告警供用量详情排查。
        except (TypeError, ValueError):
            warnings.append(warning)
            return 0
        if parsed < 0:
            warnings.append(warning)
            return 0
        return parsed
    return 0


def _cache_tokens(raw_usage: Mapping[str, Any], warnings: list[str] | None = None) -> int:
    """从 OpenAI、Anthropic 和 Gemini 私有扩展中提取缓存 Token。

    直接字段优先；Anthropic 的读取/创建 Token 相加；非法值归零并进入 normalization warning。
    """

    warning_list = warnings if warnings is not None else []
    direct = _token_field(
        raw_usage,
        ("cache_tokens", "cached_tokens", "cachedContentTokenCount", "cached_content_token_count"),
        "invalid_cache_tokens",
        warning_list,
    )
    if direct:
        return direct
    anthropic_cache = sum(
        _token_field(raw_usage, (key,), "invalid_cache_tokens", warning_list)
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )
    if anthropic_cache:
        return anthropic_cache
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = raw_usage.get(details_key)
        if not isinstance(details, Mapping):
            continue
        cached = _token_field(
            details,
            ("cached_tokens", "cache_read_tokens", "cache_creation_tokens"),
            "invalid_cache_tokens",
            warning_list,
        )
        if cached:
            return cached
    return 0


def _provider_cost(
    raw_usage: Mapping[str, Any],
    warnings: list[str],
) -> tuple[Decimal | None, str | None, Decimal | None]:
    """提取 Provider 自报成本和币种，只有明确 CNY 才生成 provider cost_cny。

    USD 或其他币种仅保留原币金额，不做未经配置的隐式汇率换算。
    """

    cost_key = next(
        (
            key
            for key in ("cost_cny", "cost", "total_cost", "cost_amount")
            if raw_usage.get(key) is not None
        ),
        None,
    )
    # 未自报成本是合法情况，内部 PricePlan 仍会独立计算成本，因此无需生成告警。
    if cost_key is None:
        return None, None, None
    try:
        amount = Decimal(str(raw_usage.get(cost_key)))
    # 非法 Provider 金额只丢弃自报口径，不影响内部 PricePlan 成本入账。
    except (InvalidOperation, TypeError, ValueError):
        warnings.append("invalid_provider_cost")
        return None, None, None
    if not amount.is_finite() or amount < 0:
        warnings.append("invalid_provider_cost")
        return None, None, None
    currency_value = raw_usage.get("currency")
    # 某些 Provider 将币种放入 billing 子对象，仅在顶层缺失时读取该兼容字段。
    if currency_value is None and isinstance(raw_usage.get("billing"), Mapping):
        currency_value = raw_usage["billing"].get("currency")
    currency = (
        "CNY" if cost_key == "cost_cny" else str(currency_value or "").strip().upper() or None
    )
    # 明确人民币金额可以直接进入 provider_cost_cny，无需汇率换算。
    if currency == "CNY":
        return amount, currency, amount
    # MVP 没有汇率快照，美元金额只能原币保留，禁止伪造人民币成本。
    if currency == "USD":
        warnings.append("currency_conversion_unavailable")
        return amount, currency, None
    warnings.append("unsupported_provider_currency" if currency else "provider_currency_missing")
    return amount, currency, None


def _log_usage_normalized(request: ProviderRequest, usage: NormalizedUsage) -> None:
    """记录 `usage.normalized` 安全结构化事件，不包含 raw usage 或请求/响应正文。"""

    logger.info(
        "usage.normalized",
        extra={
            "event": "usage.normalized",
            "request_id": request.request_id,
            "trace_id": request.trace_id,
            "tenant_id": request.tenant_id,
            "object_id": request.model_id,
            "payload": {
                "provider_id": request.provider_id,
                "provider_code": request.provider_code,
                "adapter": request.adapter_name,
                "adapter_version": request.adapter_version,
                "adapter_catalog_hash": request.adapter_catalog_hash,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "cache_tokens": usage.cache_tokens,
                "estimated": usage.estimated,
                "provider_cost_currency": usage.currency,
                "normalization_warnings": list(usage.normalization_warnings),
            },
        },
    )


def _estimate_missing_usage(
    request_payload: Mapping[str, Any],
    response_payload: Mapping[str, Any],
    base_usage: NormalizedUsage,
) -> NormalizedUsage:
    """在上游没有返回 usage 时生成本地估算值。

    估算只基于请求与响应文本长度，不保留任何原文；后续账本用
    estimated/estimate_reason 明确区分真实 usage 与估算 usage。
    """

    input_tokens = _rough_token_estimate(_request_texts(request_payload))
    output_tokens = _rough_token_estimate(_response_texts(response_payload))
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_tokens=base_usage.cache_tokens,
        estimated=True,
        estimate_reason=base_usage.estimate_reason or "missing_upstream_usage",
        cost_cny=base_usage.cost_cny,
        currency=base_usage.currency,
        provider_cost_amount=base_usage.provider_cost_amount,
        normalization_warnings=base_usage.normalization_warnings,
        # usage 子对象即使缺少 Token 字段，也可能携带 Provider 自报成本；
        # 必须保留脱敏副本供财务排障。
        raw_usage=base_usage.provider_raw_usage,
    )


def estimate_stream_usage(
    request_payload: Mapping[str, Any],
    output_tokens: int,
    base_usage: NormalizedUsage,
) -> NormalizedUsage:
    """为缺少最终 usage chunk 的流式响应生成本地估算。

    API 层在逐事件转发时只累计字符类别并换算出 ``output_tokens``，不会把完整输出正文
    传入或保存到计量层。本函数仅扫描当前内存中的请求体计算输入长度，返回值也只包含
    数值和已经脱敏的 Provider usage 子对象。
    """

    input_tokens = _rough_token_estimate(_request_texts(request_payload))
    safe_output_tokens = max(int(output_tokens), 0)
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=safe_output_tokens,
        total_tokens=input_tokens + safe_output_tokens,
        cache_tokens=base_usage.cache_tokens,
        estimated=True,
        estimate_reason=base_usage.estimate_reason or "missing_upstream_usage",
        cost_cny=base_usage.cost_cny,
        currency=base_usage.currency,
        provider_cost_amount=base_usage.provider_cost_amount,
        normalization_warnings=base_usage.normalization_warnings,
        raw_usage=base_usage.provider_raw_usage,
    )


def _request_texts(payload: Mapping[str, Any]) -> list[str]:
    """提取请求中可估算 Token 的文本片段，调用方负责保证这些文本不会被落库。"""

    texts: list[str] = []
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                texts.extend(_content_texts(message.get("content")))
    texts.extend(_content_texts(payload.get("input")))
    return texts


def _response_texts(payload: Mapping[str, Any]) -> list[str]:
    """提取响应中可估算 Token 的文本片段，只在内存里用于粗略计数。"""

    texts: list[str] = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
            texts.extend(
                _content_texts(message.get("content") if isinstance(message, Mapping) else None)
            )
            texts.extend(
                _content_texts(delta.get("content") if isinstance(delta, Mapping) else None)
            )
            texts.extend(_content_texts(choice.get("text")))
    return texts


def _content_texts(value: object) -> list[str]:
    """把 OpenAI-compatible content/input 结构展开成文本片段用于估算。"""

    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, Mapping):
                texts.extend(_content_texts(item.get("text")))
        return texts
    return []


def _rough_token_estimate(texts: list[str]) -> int:
    """用中文字符数加非中文字符四字符一 Token 的方式做 M0 粗略估算。"""

    cjk_chars = 0
    non_cjk_chars = 0
    for text in texts:
        for char in text:
            if char.isspace():
                continue
            if "\u4e00" <= char <= "\u9fff":
                cjk_chars += 1
            else:
                non_cjk_chars += 1
    return cjk_chars + (non_cjk_chars + 3) // 4
