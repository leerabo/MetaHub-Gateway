"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_provider_adapters.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from metahub_gateway.cache_affinity.models import CacheAffinitySelectionHandle
from metahub_gateway.providers import openai_compatible as openai_compatible_module
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    ProviderRequest,
    ProviderStreamResponse,
    is_quality_observation_eligible_error,
    is_upstream_cost_exposure_possible,
    quality_observation_error,
)
from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.providers.generic_http import GenericHttpConfigAdapter
from metahub_gateway.providers.keypool import SelectedProviderKey
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing.models import GatewayRoute
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.usage import sanitize_provider_usage


def route(
    *,
    provider_type: str = "openai_compatible",
    base_url: str = "http://provider.test/v1",
    provider_auth_type: str = "bearer",
) -> GatewayRoute:
    """route：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return GatewayRoute(
        tenant_id="tenant_1",
        model_id="model_1",
        model_alias="internal-chat",
        upstream_model_name="provider-chat-model",
        capabilities={"chat": True, "stream": True},
        provider_id="provider_1",
        provider_code="local_openai",
        provider_type=provider_type,
        provider_auth_type=provider_auth_type,
        provider_name="Local Provider",
        provider_base_url=base_url,
    )


def cache_affinity_selection() -> CacheAffinitySelectionHandle:
    """构造不含明文 Key 的已提交亲和 Selection 句柄，供 Adapter 边界测试复用。"""

    return CacheAffinitySelectionHandle(
        cache_affinity_selection_id="cas_test_adapter",
        tenant_id="tenant_1",
        request_id="req_test_adapter",
        trace_id="trc_test_adapter",
        attempt_sequence=1,
        routing_attempt_id="rat_test_adapter",
        stream_session_id=None,
        cache_affinity_binding_id="cab_test_adapter",
        target_kind="managed",
        target_generation=2,
        pending_lease_generation=3,
        model_endpoint_id="model_1",
        provider_id="provider_1",
        endpoint_revision_digest="endpoint_revision_test",
        provider_credential_id="credential_test_adapter",
    )


def stream_provider_request() -> ProviderRequest:
    """构造不含真实 Endpoint、Prompt 或 Key 的流式 Provider 请求测试对象。"""

    return ProviderRequest(
        provider_id="provider_1",
        provider_code="local_openai",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="http://provider.test/v1/chat/completions",
        headers={},
        payload={"model": "upstream", "messages": [], "stream": True},
        tenant_id="tenant_1",
        model_id="model_1",
    )


@pytest.fixture()
def fake_provider_endpoint():
    """fake_provider_endpoint：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    captured: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        """Handler：说明该定义的测试或脚本职责。
        
        修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
        """

        def do_POST(self) -> None:  # noqa: N802
            """do_POST：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            captured.append(
                {
                    "path": self.path,
                    "body": body,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
            if self.path == "/v1/error":
                self._json({"error": {"type": "rate_limit", "message": "too many"}}, status_code=429)
                return
            # 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
            if self.path == "/v1/missing_usage":
                self._json(
                    {
                        "id": "chatcmpl_missing_usage",
                        "object": "chat.completion",
                        "model": body.get("model"),
                        "choices": [{"message": {"role": "assistant", "content": "你好 ok"}}],
                    }
                )
                return
            self._json(
                {
                    "id": "chatcmpl_adapter",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
            )

        def _json(self, payload: dict[str, Any], status_code: int = 200) -> None:
            """_json：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, format: str, *args: object) -> None:
            """log_message：说明该定义的测试或脚本职责。
            
            修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
            """

            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", captured
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_openai_adapter_build_request_replaces_model_and_adds_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """主流程：Adapter 构造上游请求时替换内部模型 ID，并只把 Key 放进请求头。"""

    monkeypatch.setattr(
        openai_compatible_module,
        "_selected_provider_key",
        lambda _route, _config: SelectedProviderKey(None, "sk-adapter-secret", "test"),
    )
    adapter = OpenAICompatibleAdapter()

    request = adapter.build_request(
        route(),
        "chat/completions",
        {"model": "internal-chat", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert request.url == "http://provider.test/v1/chat/completions"
    assert request.payload["model"] == "provider-chat-model"
    assert request.headers["Authorization"] == "Bearer sk-adapter-secret"
    assert request.provider_code == "local_openai"


def test_selected_provider_key_repr_hides_all_credential_fields() -> None:
    """安全：SelectedProviderKey 的 ID、明文和短前缀均不得通过异常 repr 泄露。"""

    selected = SelectedProviderKey(
        "credential-repr-sentinel",
        "tiny-secret",
        "database",
        "tiny-secret",
    )

    rendered = repr(selected)

    assert "database" in rendered
    assert "credential-repr-sentinel" not in rendered
    assert "tiny-secret" not in rendered
    assert "provider_credential_id" not in rendered
    assert "key_prefix" not in rendered


def test_cache_affinity_missing_explicit_key_fails_before_legacy_keypool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全：亲和 Selection 已提交后，认证路由缺 Key 必须失败而不能重新选择凭据。"""

    def reject_legacy_selection(_route: GatewayRoute, _config: object) -> SelectedProviderKey:
        """旧 KeyPool 若被误调用就立即失败，证明测试覆盖的是二次选择边界。"""

        # 该异常证明亲和请求没有绕回非事务 KeyPool，否则会破坏已提交目标。
        raise AssertionError("cache affinity must not reselect a provider key")

    monkeypatch.setattr(openai_compatible_module, "_selected_provider_key", reject_legacy_selection)

    with pytest.raises(ApiError) as exc:
        OpenAICompatibleAdapter().build_request(
            route(),
            "chat/completions",
            {"model": "internal-chat"},
            selected_key=None,
            cache_affinity_selection=cache_affinity_selection(),
        )

    assert exc.value.code == "provider_key.unavailable"


def test_cache_affinity_none_auth_keeps_keyless_request_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容：无认证 Provider 即使携带亲和 Selection，也应保持无 Authorization 的旧语义。"""

    def reject_legacy_selection(_route: GatewayRoute, _config: object) -> SelectedProviderKey:
        """无认证路由不应调用任何旧 KeyPool 选择函数。"""

        # 无认证端点不存在凭据选择步骤，进入这里即代表兼容语义发生回归。
        raise AssertionError("none auth route must not select a provider key")

    monkeypatch.setattr(openai_compatible_module, "_selected_provider_key", reject_legacy_selection)
    selection = cache_affinity_selection()

    request = OpenAICompatibleAdapter().build_request(
        route(provider_auth_type="none"),
        "chat/completions",
        {"model": "internal-chat"},
        selected_key=None,
        cache_affinity_selection=selection,
    )

    assert "Authorization" not in request.headers
    assert request.provider_credential_id is None
    assert request.cache_affinity_selection is selection


@pytest.mark.anyio
async def test_stream_adapter_uses_bounded_handshake_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式 Adapter 限制连接、写入和池等待，正文 read timeout 留给块间看门狗。"""

    captured: dict[str, Any] = {}

    class _StreamContext:
        """返回 200 响应并验证流式握手参数的异步上下文替身。"""

        async def __aenter__(self) -> httpx.Response:
            """模拟成功完成上游响应头握手。"""

            return httpx.Response(200)

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            """模拟关闭上游响应上下文且不执行任何真实网络操作。"""

    class _Client:
        """捕获 Adapter 传入的 httpx.Timeout 和代理开关参数。"""

        def __init__(self, *, timeout, trust_env: bool) -> None:
            """保存 timeout，并断言没有使用系统代理。"""

            captured["timeout"] = timeout
            captured["trust_env"] = trust_env

        def stream(self, *_args, **_kwargs) -> _StreamContext:
            """返回可进入的成功响应上下文以完成 Adapter 握手流程。"""

            return _StreamContext()

        async def aclose(self) -> None:
            """模拟关闭 HTTP client 且不访问任何真实 Provider。"""

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    request = ProviderRequest(
        provider_id="provider_1",
        provider_code="local_openai",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="http://provider.test/v1/chat/completions",
        headers={},
        payload={"model": "upstream", "messages": [], "stream": True},
        tenant_id="tenant_1",
        model_id="model_1",
        stream_connect_timeout_seconds=41,
        stream_write_timeout_seconds=42,
        stream_pool_timeout_seconds=7,
    )

    response = await OpenAICompatibleAdapter().send_stream(request)
    await response.aclose()

    timeout = captured["timeout"]
    assert timeout.connect == 41.0
    assert timeout.write == 42.0
    assert timeout.pool == 7.0
    assert timeout.read is None
    assert captured["trust_env"] is False


@pytest.mark.anyio
async def test_stream_adapter_cancelled_handshake_closes_resources_within_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消流程：响应头握手被取消时有界关闭 context/client，并继续传播 CancelledError。"""

    entered = asyncio.Event()
    never_finishes = asyncio.Event()
    captured = {"context_exit_calls": 0, "client_close_calls": 0}

    class _CancelledStreamContext:
        """模拟长期握手与长期 context 清理，验证 Adapter 的取消和超时收口。"""

        async def __aenter__(self) -> httpx.Response:
            """进入握手后阻塞，直到测试任务发出真实 asyncio 取消。"""

            entered.set()
            await never_finishes.wait()
            # Event 永不主动置位，只有取消失效时才可能落到该不可达保护分支。
            raise AssertionError("cancelled handshake unexpectedly resumed")

        async def __aexit__(self, exc_type, _exc, _traceback) -> None:
            """记录退出参数后持续阻塞，迫使 Adapter 使用清理时间上限。"""

            captured["context_exit_calls"] += 1
            captured["context_exit_type"] = exc_type
            await never_finishes.wait()

    stream_context = _CancelledStreamContext()

    class _CancelledClient:
        """返回取消流替身，并记录独立 client 清理是否仍被执行。"""

        def __init__(self, *, timeout, trust_env: bool) -> None:
            """断言取消测试仍使用生产路径的有界 timeout 与禁代理配置。"""

            assert timeout.connect == 30.0
            assert trust_env is False

        def stream(self, *_args, **_kwargs) -> _CancelledStreamContext:
            """返回同一个握手替身，便于断言它只被清理一次。"""

            return stream_context

        async def aclose(self) -> None:
            """记录 client 关闭；该操作不应被阻塞的 context 清理拖住。"""

            captured["client_close_calls"] += 1

    monkeypatch.setattr(httpx, "AsyncClient", _CancelledClient)
    monkeypatch.setattr(
        openai_compatible_module,
        "_STREAM_HANDSHAKE_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )
    request = ProviderRequest(
        provider_id="provider_1",
        provider_code="local_openai",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="http://provider.test/v1/chat/completions",
        headers={},
        payload={"model": "upstream", "messages": [], "stream": True},
        tenant_id="tenant_1",
        model_id="model_1",
    )
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(OpenAICompatibleAdapter().send_stream(request))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    started_at = loop.time()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
    await asyncio.sleep(0)

    assert loop.time() - started_at < 0.5
    assert captured["context_exit_calls"] == 1
    assert captured["context_exit_type"] is asyncio.CancelledError
    assert captured["client_close_calls"] == 1


@pytest.mark.anyio
async def test_stream_adapter_runtime_handshake_error_closes_resources_without_logging(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：未预期握手异常关闭 context/client、原样传播且不记录异常正文。"""

    captured = {"context_exit_calls": 0, "client_close_calls": 0}
    runtime_error = RuntimeError("private-runtime-endpoint-sentinel")

    class _RuntimeErrorStreamContext:
        """在响应头握手处抛出非 httpx 异常，并记录退出清理参数。"""

        async def __aenter__(self) -> httpx.Response:
            """模拟第三方 Adapter 在握手中产生未预期运行时异常。"""

            # 第三方异常可能携带 URL 或响应片段，只允许原样交给上层安全边界。
            raise runtime_error

        async def __aexit__(self, exc_type, _exc, _traceback) -> None:
            """记录握手失败后 context 已被尽力退出。"""

            captured["context_exit_calls"] += 1
            captured["context_exit_type"] = exc_type

    stream_context = _RuntimeErrorStreamContext()

    class _RuntimeErrorClient:
        """提供运行时异常流并记录独占 HTTP client 的关闭次数。"""

        def __init__(self, *, timeout, trust_env: bool) -> None:
            """验证异常流程仍使用禁代理的生产初始化参数。"""

            assert timeout.connect == 30.0
            assert trust_env is False

        def stream(self, *_args, **_kwargs) -> _RuntimeErrorStreamContext:
            """返回会在进入阶段失败的 context，确保不会发起任何真实网络连接。"""

            return stream_context

        async def aclose(self) -> None:
            """记录 client 已关闭且不执行真实网络操作。"""

            captured["client_close_calls"] += 1

    monkeypatch.setattr(httpx, "AsyncClient", _RuntimeErrorClient)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError) as exc:
        await OpenAICompatibleAdapter().send_stream(stream_provider_request())

    assert exc.value is runtime_error
    assert captured["context_exit_calls"] == 1
    assert captured["context_exit_type"] is RuntimeError
    assert captured["client_close_calls"] == 1
    assert "private-runtime-endpoint-sentinel" not in caplog.text


@pytest.mark.anyio
async def test_stream_adapter_non_2xx_context_exit_error_still_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：非 2xx 响应的 context 退出失败不能跳过 client 清理或落异常正文。"""

    captured = {"context_exit_calls": 0, "client_close_calls": 0}
    cleanup_error = RuntimeError("private-non-2xx-cleanup-sentinel")

    class _NonSuccessStreamContext:
        """返回非成功响应，并在 context 退出时模拟第三方清理异常。"""

        async def __aenter__(self) -> httpx.Response:
            """返回不含真实上游正文的 503 响应。"""

            return httpx.Response(503)

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            """记录 context 退出并抛出清理错误，验证 finally 仍关闭 client。"""

            captured["context_exit_calls"] += 1
            # 清理异常正文不可信，必须只传播而不能写入 Adapter 日志。
            raise cleanup_error

    stream_context = _NonSuccessStreamContext()

    class _NonSuccessClient:
        """提供非成功响应流并记录 context 失败后的 client 关闭。"""

        def __init__(self, *, timeout, trust_env: bool) -> None:
            """验证非成功响应仍禁用环境代理。"""

            assert timeout.connect == 30.0
            assert trust_env is False

        def stream(self, *_args, **_kwargs) -> _NonSuccessStreamContext:
            """返回固定非成功响应 context，供非 2xx 资源清理分支使用。"""

            return stream_context

        async def aclose(self) -> None:
            """记录 finally 已释放 client，证明连接池资源没有遗留到后续请求。"""

            captured["client_close_calls"] += 1

    monkeypatch.setattr(httpx, "AsyncClient", _NonSuccessClient)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError) as exc:
        await OpenAICompatibleAdapter().send_stream(stream_provider_request())

    assert exc.value is cleanup_error
    assert captured["context_exit_calls"] == 1
    assert captured["client_close_calls"] == 1
    assert "private-non-2xx-cleanup-sentinel" not in caplog.text


@pytest.mark.anyio
async def test_compat_iter_sse_context_exit_error_still_closes_client() -> None:
    """兼容入口：SSE 迭代结束时 context 退出失败仍必须关闭独占 client。"""

    captured = {"context_exit_calls": 0, "client_close_calls": 0}
    cleanup_error = RuntimeError("private-compat-cleanup-sentinel")

    class _CompatStreamContext:
        """模拟兼容入口保存到 response.extensions 的旧 stream context 资源句柄。"""

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            """记录退出后抛错，验证 client 清理位于 finally。"""

            captured["context_exit_calls"] += 1
            # 兼容 context 的清理异常仍向调用方传播，但不能截断 client 释放。
            raise cleanup_error

    class _CompatResponse:
        """提供一个非空 SSE 数据块和旧扩展字段的响应替身。"""

        def __init__(self) -> None:
            """保存兼容 stream context，不包含 URL、Header 或响应正文。"""

            self.extensions = {"metahub_stream_context": _CompatStreamContext()}

        async def aiter_bytes(self):
            """仅产生一个固定测试数据块，随后触发 iter_sse 的资源 finally。"""

            yield b"data: {}\n\n"

    class _CompatClient:
        """记录兼容入口最终关闭 HTTP client 的次数。"""

        async def aclose(self) -> None:
            """模拟释放旧调用方持有的 client，并记录最终资源关闭次数。"""

            captured["client_close_calls"] += 1

    chunks: list[bytes] = []
    with pytest.raises(RuntimeError) as exc:
        async for chunk in openai_compatible_module.iter_sse(
            _CompatClient(),  # type: ignore[arg-type]
            _CompatResponse(),  # type: ignore[arg-type]
        ):
            chunks.append(chunk)

    assert exc.value is cleanup_error
    assert chunks == [b"data: {}\n\n"]
    assert captured["context_exit_calls"] == 1
    assert captured["client_close_calls"] == 1


@pytest.mark.anyio
async def test_provider_stream_response_concurrent_close_is_idempotent() -> None:
    """资源边界：看门狗和 finally 并发关闭时，底层响应上下文与 client 各执行一次。"""

    class _StreamContext:
        """记录响应上下文退出次数并验证并发幂等语义的测试替身。"""

        calls = 0

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            """模拟 httpx stream context 退出并主动让出一次事件循环。"""

            self.calls += 1
            await asyncio.sleep(0)

    class _Client:
        """记录 HTTP client 关闭次数并验证底层只释放一次的替身。"""

        calls = 0

        async def aclose(self) -> None:
            """模拟 AsyncClient.aclose 并累计真实资源关闭次数。"""

            self.calls += 1

    stream_context = _StreamContext()
    client = _Client()
    response = ProviderStreamResponse(
        client=client,  # type: ignore[arg-type]
        response=object(),  # type: ignore[arg-type]
        stream_context=stream_context,
    )

    await asyncio.gather(response.aclose(), response.aclose(), response.aclose())

    assert stream_context.calls == 1
    assert client.calls == 1


def test_adapter_parse_usage_supports_openai_and_generic_fields() -> None:
    """主流程：usage 归一化兼容 OpenAI、通用和 Gemini 字段。"""

    adapter = OpenAICompatibleAdapter()

    openai_usage = adapter.parse_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}})
    generic_usage = adapter.parse_usage({"usage": {"input_tokens": 4, "output_tokens": 6}})
    cached_usage = adapter.parse_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}}
    )
    missing_usage = adapter.parse_usage({})
    gemini_usage = adapter.parse_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 8,
                "totalTokenCount": 20,
                "cachedContentTokenCount": 3,
            }
        }
    )

    assert openai_usage.input_tokens == 3
    assert openai_usage.output_tokens == 5
    assert openai_usage.total_tokens == 8
    assert generic_usage.input_tokens == 4
    assert generic_usage.output_tokens == 6
    assert generic_usage.total_tokens == 10
    assert cached_usage.cache_tokens == 4
    assert gemini_usage.input_tokens == 12
    assert gemini_usage.output_tokens == 8
    assert gemini_usage.total_tokens == 20
    assert gemini_usage.cache_tokens == 3
    assert missing_usage.estimated is True
    assert missing_usage.estimate_reason == "missing_upstream_usage"


def test_adapter_normalizes_provider_cost_currency_and_sanitizes_raw_usage() -> None:
    """成本/安全：CNY 可归一，USD 不隐式换汇，raw usage 的认证字段必须脱敏。"""

    adapter = OpenAICompatibleAdapter()
    cny_usage = adapter.parse_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 2,
                "cost_cny": "0.125",
                "api_key": "sk-provider-raw-secret",
                "nested": {"authorization": "Bearer raw-secret"},
            }
        }
    )
    usd_usage = adapter.parse_usage(
        {"usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0.01", "currency": "USD"}}
    )
    invalid_usage = adapter.parse_usage(
        {"usage": {"input_tokens": -1, "output_tokens": "bad", "total_tokens": 1, "cost": "NaN"}}
    )
    cost_only_usage = adapter.parse_usage({"usage": {"cost_cny": "0.2", "api_key": "cost-only-secret"}})

    assert str(cny_usage.cost_cny) == "0.125"
    assert str(cny_usage.provider_cost_amount) == "0.125"
    assert cny_usage.currency == "CNY"
    assert cny_usage.provider_raw_usage["input_tokens"] == 10
    assert cny_usage.provider_raw_usage["api_key"] == "[REDACTED]"
    assert cny_usage.provider_raw_usage["nested"]["authorization"] == "[REDACTED]"
    assert "sk-provider-raw-secret" not in json.dumps(cny_usage.provider_raw_usage)
    assert usd_usage.cost_cny is None
    assert usd_usage.currency == "USD"
    assert "currency_conversion_unavailable" in usd_usage.normalization_warnings
    assert invalid_usage.input_tokens == 0
    assert invalid_usage.output_tokens == 0
    assert "invalid_input_tokens" in invalid_usage.normalization_warnings
    assert "invalid_output_tokens" in invalid_usage.normalization_warnings
    assert "invalid_provider_cost" in invalid_usage.normalization_warnings
    assert cost_only_usage.estimated is True
    assert cost_only_usage.estimate_reason == "missing_upstream_token_fields"
    assert cost_only_usage.provider_raw_usage["api_key"] == "[REDACTED]"


def test_provider_raw_usage_sanitizer_bounds_nested_and_large_values() -> None:
    """安全边界：超深、超长和超大数组会截断，正常 Token 字段仍完整保留。"""

    sanitized = sanitize_provider_usage(
        {
            "input_tokens": 123,
            "content": "private response text",
            "long_text": "x" * 400,
            "items": list(range(40)),
            "nested": {"level2": {"level3": {"level4": {"secret": "must-not-leak"}}}},
        }
    )

    assert sanitized["input_tokens"] == 123
    assert sanitized["content"] == "[REDACTED]"
    assert "[TRUNCATED]" in sanitized["long_text"]
    assert sanitized["items"][-1] == "[TRUNCATED]"
    assert "must-not-leak" not in json.dumps(sanitized)


def test_adapter_factory_selects_generic_for_custom_provider() -> None:
    """扩展性：自定义 Provider 走通用 HTTP Adapter，不需要修改数据面主流程。"""

    assert isinstance(adapter_for_route(route(provider_type="custom")), GenericHttpConfigAdapter)
    assert isinstance(adapter_for_route(route(provider_type="openai_compatible")), OpenAICompatibleAdapter)


def test_adapter_normalize_error_maps_actionable_upstream_codes() -> None:
    """主流程：Provider Adapter 将典型上游状态映射成可操作的统一错误码。"""

    adapter = OpenAICompatibleAdapter()

    auth_error = adapter.normalize_error(
        response=httpx.Response(401, json={"error": {"type": "invalid_api_key", "message": "sk-secret"}})
    )
    rate_error = adapter.normalize_error(
        response=httpx.Response(429, json={"error": {"type": "rate_limit", "message": "too many"}})
    )
    quota_error = adapter.normalize_error(
        response=httpx.Response(429, json={"error": {"type": "insufficient_quota", "message": "quota"}})
    )
    model_error = adapter.normalize_error(
        response=httpx.Response(404, json={"error": {"code": "model_not_found", "message": "missing"}})
    )
    request_error = adapter.normalize_error(
        response=httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad"}})
    )
    timeout_error = adapter.normalize_error(exc=httpx.TimeoutException("timeout with sk-secret"))
    server_error = adapter.normalize_error(response=httpx.Response(503, json={"error": {}}))
    invalid_url = quality_observation_error(
        adapter.normalize_error(exc=httpx.InvalidURL("private invalid endpoint"))
    )
    transport_error = quality_observation_error(
        adapter.normalize_error(exc=httpx.ConnectError("private connection failure"))
    )

    assert auth_error.code == "gateway.upstream_auth_failed"
    assert auth_error.message == "Upstream provider authentication failed."
    assert "sk-secret" not in auth_error.message
    assert rate_error.code == "gateway.upstream_rate_limited"
    assert rate_error.retryable is True
    assert quota_error.code == "gateway.upstream_quota_exceeded"
    assert quota_error.retryable is False
    assert model_error.code == "gateway.upstream_model_not_found"
    assert request_error.code == "gateway.upstream_request_invalid"
    assert timeout_error.code == "gateway.upstream_timeout"
    assert timeout_error.retryable is True
    assert server_error.error_category == "upstream_5xx"
    assert invalid_url.code == "gateway.upstream_unavailable"
    assert transport_error.code == "gateway.upstream_transport_failed"
    assert is_upstream_cost_exposure_possible(invalid_url) is False
    assert is_upstream_cost_exposure_possible(transport_error) is True
    assert is_upstream_cost_exposure_possible(auth_error) is True
    assert is_quality_observation_eligible_error(rate_error) is True
    assert is_quality_observation_eligible_error(timeout_error) is True
    assert is_quality_observation_eligible_error(server_error) is True
    assert is_quality_observation_eligible_error(transport_error) is True
    for excluded in (auth_error, quota_error, model_error, request_error, invalid_url):
        assert is_quality_observation_eligible_error(excluded) is False
    local_collision = NormalizedProviderError(
        code="gateway.upstream_unavailable",
        message="",
        http_status=None,
        retryable=False,
        provider_error_type="gateway",
        error_category="other",
    )
    # generic 本地 ApiError 不经过 quality_observation_error，不能仅因同名 code 碰撞计分。
    assert is_quality_observation_eligible_error(local_collision) is False


@pytest.mark.anyio
async def test_openai_adapter_send_non_stream_returns_normalized_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_provider_endpoint,
) -> None:
    """主流程：非流式发送返回标准化响应和 usage。"""

    base_url, captured = fake_provider_endpoint
    monkeypatch.setattr(
        openai_compatible_module,
        "_selected_provider_key",
        lambda _route, _config: SelectedProviderKey(None, "sk-adapter-secret", "test"),
    )
    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(route(base_url=base_url), "chat/completions", {"model": "internal-chat"})
    request.request_id = "req_usage_normalized"
    request.trace_id = "trc_usage_normalized"
    caplog.set_level(logging.INFO, logger="metahub.provider_adapter")

    response = await adapter.send_non_stream(request)

    assert response.payload["model"] == "provider-chat-model"
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert captured[-1]["authorization"] == "Bearer sk-adapter-secret"
    events = [record for record in caplog.records if record.getMessage() == "usage.normalized"]
    assert events
    assert events[-1].request_id == "req_usage_normalized"
    assert events[-1].payload["total_tokens"] == 18
    assert "raw_usage" not in events[-1].payload


@pytest.mark.anyio
async def test_openai_adapter_estimates_usage_when_upstream_omits_usage(
    monkeypatch: pytest.MonkeyPatch,
    fake_provider_endpoint,
) -> None:
    """异常/估算流程：上游缺少 usage 字段时 Adapter 只按文本长度估算 Token，不保留明文内容。"""

    base_url, _captured = fake_provider_endpoint
    monkeypatch.setattr(
        openai_compatible_module,
        "_selected_provider_key",
        lambda _route, _config: SelectedProviderKey(None, "sk-adapter-secret", "test"),
    )
    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(
        route(base_url=base_url),
        "missing_usage",
        {"model": "internal-chat", "messages": [{"role": "user", "content": "hello world"}]},
    )

    response = await adapter.send_non_stream(request)

    assert response.usage.estimated is True
    assert response.usage.estimate_reason == "missing_upstream_usage"
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    assert response.usage.raw_usage == {}


@pytest.mark.anyio
async def test_adapter_normalizes_upstream_errors_without_logging_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fake_provider_endpoint,
) -> None:
    """异常/安全：上游错误归一为 gateway 错误，日志不包含 Provider Key。"""

    base_url, _captured = fake_provider_endpoint
    monkeypatch.setattr(
        openai_compatible_module,
        "_selected_provider_key",
        lambda _route, _config: SelectedProviderKey(None, "sk-adapter-secret", "test"),
    )
    caplog.set_level(logging.WARNING, logger="metahub.provider_adapter")
    adapter = OpenAICompatibleAdapter()
    request = adapter.build_request(route(base_url=base_url), "error", {"model": "internal-chat"})
    request.request_id = "req_adapter_error"
    request.trace_id = "trc_adapter_error"

    with pytest.raises(ApiError) as exc:
        await adapter.send_non_stream(request)

    assert exc.value.code == "gateway.upstream_rate_limited"
    records = [record for record in caplog.records if record.getMessage() == "provider_adapter.error"]
    assert records
    assert records[-1].payload["provider_error_type"] == "rate_limited"
    assert "sk-adapter-secret" not in caplog.text
    assert "Authorization" not in caplog.text

    malicious_response = httpx.Response(
        500,
        json={
            "error": {
                "type": "sk-malicious-secret Authorization private prompt",
                "message": "private response must not log",
            }
        },
    )
    # 日志边界必须独立于上游 type/code 的内容；内存分类可读正文但落盘只能是固定枚举。
    with pytest.raises(ApiError):
        adapter._raise_adapter_error(
            request,
            adapter.normalize_error(response=malicious_response),
        )
    malicious_record = [
        record for record in caplog.records if record.getMessage() == "provider_adapter.error"
    ][-1]
    assert malicious_record.payload["provider_error_type"] == "upstream_error"
    assert "sk-malicious-secret" not in caplog.text
    assert "private prompt" not in caplog.text
    assert "private response must not log" not in caplog.text


def test_openai_adapter_without_config_cannot_fallback_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """安全：缺少 AppConfig 时无法证明租户与 Endpoint 身份，必须在建连前拒绝环境 Key。"""

    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY", "sk-must-not-be-read")
    adapter = OpenAICompatibleAdapter()

    with pytest.raises(ApiError) as exc:
        adapter.build_request(route(), "chat/completions", {"model": "internal-chat"})

    assert exc.value.code == "provider_key.unavailable"
