"""验证 PRD-037 流式状态机、资源释放和安全计量闭环。

测试使用可控异步流，不访问真实 Provider，也不把 Prompt、输出正文或 Provider Key 写入
断言日志。StreamSession 持久化在独立测试覆盖；本文件聚焦并发、取消和编排顺序。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from metahub_gateway.api import openai_compatible as api_module
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    ProviderRequest,
    UpstreamApiError,
)
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.streaming.sessions import StreamSession
from metahub_gateway.usage.ledger import RequestLogContext


class _ScriptedStream:
    """按延迟脚本产生字节或异常，并统计显式关闭次数。"""

    def __init__(self, steps: list[tuple[float, bytes | BaseException]]) -> None:
        """保存不可变测试步骤；每一步先等待 delay，再产生值或抛出异常。"""

        self.steps = steps
        self.close_calls = 0
        self._closed = False

    async def iter_bytes(self):
        """执行脚本，模拟正常分片、慢分片和中途读失败。"""

        for delay, value in self.steps:
            await asyncio.sleep(delay)
            if isinstance(value, BaseException):
                raise value
            yield value

    async def aclose(self) -> None:
        """记录关闭调用；测试替身允许多次调用以验证编排器是否幂等依赖接口。"""

        if self._closed:
            return
        self._closed = True
        self.close_calls += 1


class _CloseFailingStream(_ScriptedStream):
    """在第一次关闭时抛受控本地异常，验证后续终态和账本仍会收口。"""

    async def aclose(self) -> None:
        """记录一次关闭后抛异常；私有正文用于验证日志只保留异常类型。"""

        if self._closed:
            return
        self._closed = True
        self.close_calls += 1
        # 关闭异常可能携带底层连接详情，业务日志不得输出异常字符串。
        raise RuntimeError("private close failure with provider key")


class _HangingStream:
    """永不产生首块的上游流，用于复现僵尸连接。"""

    def __init__(self) -> None:
        """创建永不置位的事件，并初始化关闭计数。"""

        self._never = asyncio.Event()
        self.close_calls = 0
        self._closed = False

    async def iter_bytes(self):
        """等待到被任务取消；保留不可达 yield 使其成为异步生成器。"""

        await self._never.wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        """统计看门狗或断连路径的显式关闭。"""

        if self._closed:
            return
        self._closed = True
        self.close_calls += 1


class _Adapter(OpenAICompatibleAdapter):
    """返回注入流对象的 Adapter，不执行网络访问。"""

    def __init__(self, stream: object) -> None:
        """保存单次测试指定的流响应。"""

        self.stream = stream

    async def send_stream(self, request: ProviderRequest):
        """返回测试流；ProviderRequest 只用于保持真实编排契约。"""

        return self.stream


class _FailingAdapter(OpenAICompatibleAdapter):
    """在流握手阶段抛出指定异常，用于验证上游与本地归因分离。"""

    def __init__(self, error: BaseException) -> None:
        """保存单次受控异常；异常正文只存在测试进程内存。"""

        self.error = error

    async def send_stream(self, request: ProviderRequest):
        """模拟尚未返回上游响应头时的失败，不执行任何网络访问。"""

        # 受控异常只用于验证编排归因，异常正文不得进入客户端响应或结构化日志。
        raise self.error


class _Lease:
    """记录并发限流租约释放次数。"""

    def __init__(self) -> None:
        """每个测试使用独立计数，避免类属性跨用例污染。"""

        self.release_calls = 0

    async def release(self) -> None:
        """增加释放计数，确保所有终止路径都恰好归还一次额度。"""

        self.release_calls += 1


class _ClientRequest:
    """提供事件队列所需 app.state 的安全客户端请求测试替身。"""

    def __init__(self, *, disconnected: bool = False) -> None:
        """设置客户端状态，并提供运行时事件队列所需的 app.state。"""

        self.disconnected = disconnected
        self.app = SimpleNamespace(state=SimpleNamespace())

    async def is_disconnected(self) -> bool:
        """返回当前断开状态，不读取或记录任何请求正文。"""

        return self.disconnected


def _config(*, timeout_seconds: float = 0.05) -> SimpleNamespace:
    """构造状态机只需要的流式配置，测试可使用亚秒阈值。"""

    return SimpleNamespace(
        stream_inter_token_timeout_seconds=timeout_seconds,
    )


def _context(request_id: str) -> RequestLogContext:
    """构造不含正文和 Key 的完整流式调用日志上下文。"""

    return RequestLogContext(
        tenant_id="tenant-stream-test",
        request_id=request_id,
        trace_id=f"trc-{request_id}",
        provider_id="provider-stream-test",
        provider_code="openai",
        model_id="model-stream-test",
    )


def _provider_request() -> ProviderRequest:
    """构造内存 ProviderRequest；敏感值只用于验证日志不会泄露。"""

    return ProviderRequest(
        provider_id="provider-stream-test",
        provider_code="openai",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="https://provider.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer sk-provider-must-not-log"},
        payload={
            "model": "provider-model",
            "messages": [{"role": "user", "content": "private prompt must not log"}],
            "stream": True,
        },
        tenant_id="tenant-stream-test",
        model_id="model-stream-test",
    )


def _install_state_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换持久化和事件边界，返回可断言的会话、日志、Usage 与事件列表。"""

    state: dict[str, Any] = {
        "sessions": [],
        "finishes": [],
        "request_logs": [],
        "usage": [],
        "events": [],
        "quality_eligibility": [],
    }

    def create_session(_config_value, context, *, inter_token_timeout_ms: int) -> StreamSession:
        """模拟强制会话创建，并保留超时阈值快照。"""

        session = StreamSession(
            stream_session_id=f"str-{context.request_id}",
            tenant_id=context.tenant_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            model_id=context.model_id,
            provider_id=context.provider_id,
            provider_code=context.provider_code,
            inter_token_timeout_ms=inter_token_timeout_ms,
            started_at=datetime.now(UTC),
        )
        state["sessions"].append(session)
        # 返回的内存会话代表已通过测试持久化闸门，供后续状态机推进。
        return session

    def mark_active(_config_value, session: StreamSession) -> None:
        """模拟上游握手成功后的 active 审计状态推进过程。"""

        session.status = "active"

    def finish(
        _config_value,
        session: StreamSession,
        *,
        status: str,
        error_code: str | None,
        quality_observation_eligible: bool = False,
    ) -> bool:
        """记录首次终态及质量资格，并模拟两者在同一事务提交成功。"""

        session.status = status
        state["finishes"].append((status, error_code, session.chunk_count, session.bytes_sent))
        state["quality_eligibility"].append(quality_observation_eligible)
        return True

    monkeypatch.setattr(api_module, "create_stream_session", create_session)
    monkeypatch.setattr(api_module, "mark_stream_session_active", mark_active)
    monkeypatch.setattr(api_module, "safe_finish_stream_session", finish)
    monkeypatch.setattr(
        api_module,
        "safe_record_request_log",
        lambda _config_value, context, **kwargs: state["request_logs"].append((context, kwargs)),
    )
    monkeypatch.setattr(
        api_module,
        "safe_record_usage_ledger",
        lambda _config_value, context, usage: state["usage"].append((context, usage)),
    )
    monkeypatch.setattr(
        api_module,
        "enqueue_runtime_event",
        lambda _app_state, event: state["events"].append(event) or True,
    )
    return state


async def _consume(response) -> list[bytes]:
    """消费 StreamingResponse body，返回按生成器边界收到的字节块。"""

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return chunks


@pytest.mark.anyio
async def test_stream_normal_chunks_reset_watchdog_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主流程：多个低于阈值的分片原样输出，并完成会话、Usage 和租约释放。"""

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream(
        [
            (0.005, b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'),
            (0.02, b"data: [DONE]\n\n"),
        ]
    )
    lease = _Lease()
    response = await api_module._stream_response(
        _config(timeout_seconds=0.04),
        _context("req-normal"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )

    chunks = await _consume(response)

    assert chunks == [step[1] for step in stream.steps]
    assert state["finishes"] == [("completed", None, 2, sum(len(item) for item in chunks))]
    assert state["request_logs"][0][1]["status"] == "success"
    assert state["request_logs"][0][0].stream_status == "completed"
    assert state["usage"][0][1]["estimated"] is True
    assert state["events"] == []
    assert state["quality_eligibility"] == [True]
    assert stream.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_stream_usage_finish_failure_keeps_ledger_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """最终 Usage 解析故障使用最低估算，不能跳过成功日志、账本或并发租约。

    参数：正常上游流、故障 finish 和日志捕获。返回值：无。关键约束：本地计量异常不改变
    completed 质量事实，也不把异常正文、Prompt、Response 或 Provider Key 写入日志。
    """

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream(
        [(0, b'data: {"choices":[{"delta":{"content":"visible"}}]}\n\n')]
    )
    lease = _Lease()
    caplog.set_level(logging.WARNING, logger="metahub.gateway")

    def fail_finish(_collector, _payload) -> None:
        """模拟流已结束后的本地 Adapter/估算故障，异常正文不得外泄。"""

        # 故障字符串故意包含敏感词，只能用于证明结构化告警保留异常类型而非正文。
        raise RuntimeError("private usage finish failure with prompt")

    monkeypatch.setattr(api_module._SseUsageCollector, "finish", fail_finish)
    response = await api_module._stream_response(
        _config(),
        _context("req-finish-failure"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )

    assert len(await _consume(response)) == 1
    assert state["finishes"][0][:2] == ("completed", None)
    assert state["quality_eligibility"] == [True]
    assert state["request_logs"][0][1]["status"] == "success"
    assert state["usage"][0][1]["estimated"] is True
    assert state["usage"][0][1]["estimate_reason"] == "stream_usage_finalize_failed"
    assert state["usage"][0][1]["output_tokens"] > 0
    assert lease.release_calls == 1
    assert "private usage finish failure" not in caplog.text
    assert "private prompt must not log" not in caplog.text


@pytest.mark.anyio
async def test_stream_close_failure_does_not_skip_terminal_accounting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """上游关闭异常只形成安全告警，Session、调用日志、Usage 和租约仍全部完成。"""

    state = _install_state_spies(monkeypatch)
    stream = _CloseFailingStream([(0, b"data: [DONE]\n\n")])
    lease = _Lease()
    caplog.set_level(logging.WARNING, logger="metahub.gateway")
    response = await api_module._stream_response(
        _config(),
        _context("req-close-failure"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )

    assert await _consume(response) == [b"data: [DONE]\n\n"]
    assert state["finishes"][0][:2] == ("completed", None)
    assert state["request_logs"][0][1]["status"] == "success"
    assert len(state["usage"]) == 1
    assert stream.close_calls == 1
    assert lease.release_calls == 1
    assert "private close failure" not in caplog.text
    assert "sk-provider-must-not-log" not in caplog.text


@pytest.mark.anyio
async def test_stream_first_chunk_timeout_closes_upstream_and_emits_safe_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：首块超时主动关闭僵尸连接、提交会话并发布一次安全事件。"""

    state = _install_state_spies(monkeypatch)
    stream = _HangingStream()
    lease = _Lease()
    caplog.set_level(logging.WARNING, logger="metahub.gateway")
    response = await api_module._stream_response(
        _config(timeout_seconds=0.01),
        _context("req-timeout"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )

    chunks = await _consume(response)

    assert len(chunks) == 1
    assert chunks[0].startswith(b"event: error\n")
    assert b"gateway.upstream_stream_timeout" in chunks[0]
    assert b"private prompt" not in chunks[0]
    assert state["finishes"] == [("timed_out", "gateway.upstream_stream_timeout", 0, 0)]
    assert state["request_logs"][0][1]["error_code"] == "gateway.upstream_stream_timeout"
    assert state["events"][0].event_type == "stream.timeout"
    assert state["events"][0].payload["request_id"] == "req-timeout"
    assert state["quality_eligibility"] == [True]
    assert stream.close_calls == 1
    assert lease.release_calls == 1
    assert "sk-provider-must-not-log" not in caplog.text
    assert "private prompt must not log" not in caplog.text


@pytest.mark.anyio
async def test_client_disconnect_while_upstream_idle_releases_resources_without_timeout_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断连流程：上游无块时仍能及时侦测客户端断开，并且不误发 timeout 事件。"""

    state = _install_state_spies(monkeypatch)
    stream = _HangingStream()
    lease = _Lease()
    response = await api_module._stream_response(
        _config(timeout_seconds=0.2),
        _context("req-disconnected"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(disconnected=True),
    )
    # 直接消费 body_iterator 不会执行 StreamingResponse.__call__，因此显式模拟 ASGI 断开事件。
    response.disconnect_event.set()

    assert await _consume(response) == []
    assert state["finishes"] == [("client_disconnected", "gateway.client_disconnected", 0, 0)]
    assert state["request_logs"][0][1]["error_code"] == "gateway.client_disconnected"
    assert state["events"] == []
    assert state["quality_eligibility"] == [False]
    assert stream.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_stream_read_exception_returns_standard_sse_error_and_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游中途异常不穿透原始错误，保留已输出分片并追加统一 SSE error。"""

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream(
        [
            (0, b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'),
            (0, RuntimeError("synthetic upstream secret failure")),
        ]
    )
    response = await api_module._stream_response(
        _config(),
        _context("req-failure"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        None,
        client_request=_ClientRequest(),
    )

    chunks = await _consume(response)

    assert b"partial" in chunks[0]
    assert chunks[1].startswith(b"event: error\n")
    assert b"gateway.upstream_unavailable" in chunks[1]
    assert b"synthetic upstream secret failure" not in chunks[1]
    assert state["finishes"][0][:2] == ("upstream_failed", "gateway.upstream_stream_failed")
    assert state["quality_eligibility"] == [True]
    assert state["usage"][0][1]["output_tokens"] > 0
    assert stream.close_calls == 1


@pytest.mark.anyio
async def test_stream_generator_close_records_client_disconnect_and_safe_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方关闭响应生成器时继续传播 GeneratorExit 语义，并完成清理和部分计量。"""

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream(
        [
            (0, b'data: {"choices":[{"delta":{"content":"private output"}}]}\n\n'),
            (0, b"data: [DONE]\n\n"),
        ]
    )
    lease = _Lease()
    response = await api_module._stream_response(
        _config(),
        _context("req-close"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )
    iterator = response.body_iterator
    first = await anext(iterator)
    await iterator.aclose()

    assert b"private output" in first
    assert state["finishes"][0][:2] == ("client_disconnected", "gateway.client_disconnected")
    assert state["quality_eligibility"] == [False]
    assert state["usage"][0][1]["output_tokens"] > 0
    assert stream.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_stream_task_cancellation_records_disconnect_and_preserves_cancel_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI 响应任务取消要落客户端断开、排除质量统计，并继续传播 CancelledError。

    参数：永不返回首块的上游流和可计数租约。返回值：无。关键约束：取消不能转换为
    上游超时/失败，资源与租约仍各释放一次，Session 终态只提交一次。
    """

    state = _install_state_spies(monkeypatch)
    stream = _HangingStream()
    lease = _Lease()
    response = await api_module._stream_response(
        _config(timeout_seconds=5),
        _context("req-cancelled"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=_ClientRequest(),
    )
    consume_task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.sleep(0)
    consume_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consume_task

    assert state["finishes"] == [
        ("client_disconnected", "gateway.client_disconnected", 0, 0)
    ]
    assert state["quality_eligibility"] == [False]
    assert stream.close_calls == 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_local_stream_usage_parser_failure_does_not_penalize_model_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地 usage 解析异常虽终止流，但不得冒充上游读取失败计入质量错误。

    参数：一条正常上游分片及故障注入的本地 collector。返回值：无。关键约束：对外仍只
    返回统一 SSE 错误，session 可记录 upstream_failed 以结束资源，但质量资格必须为 false。
    """

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream(
        [(0, b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')]
    )

    def fail_local_parse(_collector, _chunk: bytes) -> None:
        """模拟网关本地计量解析故障，异常正文不得进入响应或质量统计。"""

        # 故障正文故意包含私有描述，用于证明统一错误响应与质量统计都不会泄露它。
        raise RuntimeError("private local parser failure")

    monkeypatch.setattr(api_module._SseUsageCollector, "feed", fail_local_parse)
    response = await api_module._stream_response(
        _config(),
        _context("req-local-parser-failure"),
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        None,
        client_request=_ClientRequest(),
    )

    chunks = await _consume(response)

    assert len(chunks) == 1
    assert chunks[0].startswith(b"event: error\n")
    assert b"gateway.stream_processing_unavailable" in chunks[0]
    assert b"private local parser failure" not in chunks[0]
    assert state["finishes"][0][:2] == (
        "upstream_failed",
        "gateway.stream_processing_unavailable",
    )
    assert state["quality_eligibility"] == [False]


@pytest.mark.anyio
async def test_stream_handshake_distinguishes_normalized_upstream_and_local_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """握手失败持久化明确归因码；客户端仍获得对应的统一安全错误。

    参数：一个已归一化上游异常和一个本地未归一化异常。返回值：无。关键约束：只有
    ``UpstreamApiError`` 进入质量统计，旧歧义码不再写入新的 StreamSession。
    """

    upstream_state = _install_state_spies(monkeypatch)
    normalized = NormalizedProviderError(
        code="gateway.upstream_unavailable",
        message="Upstream provider is unavailable.",
        http_status=None,
        retryable=True,
        provider_error_type="connection",
        error_category="network",
    )
    with pytest.raises(UpstreamApiError) as upstream_failure:
        await api_module._stream_response(
            _config(),
            _context("req-handshake-upstream"),
            _FailingAdapter(UpstreamApiError(normalized)),
            _provider_request(),
            time.perf_counter(),
            None,
            client_request=_ClientRequest(),
        )

    assert upstream_failure.value.code == "gateway.upstream_unavailable"
    assert upstream_state["finishes"][0][:2] == (
        "upstream_failed",
        "gateway.upstream_stream_failed",
    )
    assert upstream_state["quality_eligibility"] == [True]

    excluded_upstream_errors = (
        ("auth", "gateway.upstream_auth_failed", 401),
        ("quota", "gateway.upstream_quota_exceeded", 429),
    )
    for label, code, http_status in excluded_upstream_errors:
        excluded_state = _install_state_spies(monkeypatch)
        excluded = NormalizedProviderError(
            code=code,
            message="Safe normalized upstream failure.",
            http_status=http_status,
            retryable=False,
            provider_error_type=label,
            error_category="other",
        )
        with pytest.raises(UpstreamApiError) as excluded_failure:
            await api_module._stream_response(
                _config(),
                _context(f"req-handshake-{label}"),
                _FailingAdapter(UpstreamApiError(excluded)),
                _provider_request(),
                time.perf_counter(),
                None,
                client_request=_ClientRequest(),
            )

        assert excluded_failure.value.code == code
        assert excluded_state["finishes"][0][:2] == ("upstream_failed", code)
        # 上游认证和配额属于账号/Key 治理事实，不能因为异常类型是 UpstreamApiError 就计质量。
        assert excluded_state["quality_eligibility"] == [False]

    local_api_state = _install_state_spies(monkeypatch)
    with pytest.raises(api_module.ApiError) as local_api_failure:
        await api_module._stream_response(
            _config(),
            _context("req-handshake-local-api"),
            _FailingAdapter(api_module.ApiError("gateway.stream_session_unavailable")),
            _provider_request(),
            time.perf_counter(),
            None,
            client_request=_ClientRequest(),
        )

    assert local_api_failure.value.code == "gateway.stream_session_unavailable"
    assert local_api_state["finishes"][0][:2] == (
        "upstream_failed",
        "gateway.stream_session_unavailable",
    )
    assert local_api_state["quality_eligibility"] == [False]

    local_state = _install_state_spies(monkeypatch)
    with pytest.raises(api_module.ApiError) as local_failure:
        await api_module._stream_response(
            _config(),
            _context("req-handshake-local"),
            _FailingAdapter(RuntimeError("private local handshake failure")),
            _provider_request(),
            time.perf_counter(),
            None,
            client_request=_ClientRequest(),
        )

    assert local_failure.value.code == "gateway.stream_processing_unavailable"
    assert local_state["finishes"][0][:2] == (
        "upstream_failed",
        "gateway.stream_processing_unavailable",
    )
    assert local_state["quality_eligibility"] == [False]


@pytest.mark.anyio
async def test_stream_handshake_budget_distinguishes_transport_and_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流握手预算按是否可能到达上游结算，不能复用模型质量资格或异常父类。

    参数：同一冻结预留下的 ConnectError 与 InvalidURL。返回值：无。关键约束：前者可能
    产生厂商费用而保留估算，后者由本地 URL parser 在发网前拒绝，必须把 outstanding 清零。
    """

    frozen_cost = Decimal("2.500000")
    base_context = replace(
        _context("req-stream-budget-base"),
        budget_reservation_outstanding_cny=format(frozen_cost, "f"),
    )
    cases = (
        ("transport", "ConnectError", format(frozen_cost, "f"), True),
        ("invalid-url", "InvalidURL", "0", False),
    )
    for label, provider_error_type, expected_outstanding, expected_quality in cases:
        state = _install_state_spies(monkeypatch)
        normalized = NormalizedProviderError(
            code="gateway.upstream_unavailable",
            message="Upstream provider is unavailable.",
            http_status=None,
            retryable=True,
            provider_error_type=provider_error_type,
            error_category="network" if expected_quality else "other",
        )
        with pytest.raises(UpstreamApiError) as failure:
            await api_module._stream_response(
                _config(),
                replace(
                    base_context,
                    request_id=f"req-stream-budget-{label}",
                    trace_id=f"trc-stream-budget-{label}",
                ),
                _FailingAdapter(UpstreamApiError(normalized)),
                _provider_request(),
                time.perf_counter(),
                None,
                client_request=_ClientRequest(),
                budget_initial_estimated_cost_cny=frozen_cost,
            )

        assert failure.value.code == "gateway.upstream_unavailable"
        assert failure.value.request_log_context.budget_reservation_outstanding_cny == expected_outstanding
        assert state["quality_eligibility"] == [expected_quality]


@pytest.mark.anyio
async def test_stream_active_audit_gate_failure_closes_upstream_before_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active 审计闸门失败时不创建 SSE 响应，并以本地故障终结且排除质量统计。

    参数：已握手上游流和故障注入的 active 持久化函数。返回值：无。关键约束：连接只关闭
    一次，终态上下文只在提交成功后关联 Session，外层统一错误处理再据此写失败 request_log。
    """

    state = _install_state_spies(monkeypatch)
    stream = _ScriptedStream([])

    def fail_mark_active(_config_value, _session: StreamSession) -> None:
        """模拟 active 审计持久化不可用，不输出数据库异常或请求正文。"""

        # 使用公开固定码触发审计 Fail Closed，避免测试依赖任意 SQLite 异常文本。
        raise api_module.ApiError("gateway.stream_session_unavailable")

    monkeypatch.setattr(api_module, "mark_stream_session_active", fail_mark_active)
    with pytest.raises(api_module.ApiError) as failure:
        await api_module._stream_response(
            _config(),
            _context("req-active-gate-failure"),
            _Adapter(stream),
            _provider_request(),
            time.perf_counter(),
            None,
            client_request=_ClientRequest(),
        )

    assert failure.value.code == "gateway.stream_session_unavailable"
    assert failure.value.request_log_context.stream_status == "upstream_failed"
    assert failure.value.request_log_context.stream_session_id == "str-req-active-gate-failure"
    assert state["finishes"] == [
        ("upstream_failed", "gateway.stream_session_unavailable", 0, 0)
    ]
    assert state["quality_eligibility"] == [False]
    assert state["request_logs"] == []
    assert state["usage"] == []
    assert state["events"] == []
    assert stream.close_calls == 1
