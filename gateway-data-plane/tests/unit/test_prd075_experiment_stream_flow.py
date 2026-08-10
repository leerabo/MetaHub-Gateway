"""验证 PRD-075 实验流的终止标记、持久化失败、超时和断连收口。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from metahub_gateway.api import openai_compatible as api_module
from metahub_gateway.providers.base import (
    NormalizedResponse,
    NormalizedUsage,
    ProviderRequest,
)
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.streaming.sessions import StreamSession
from metahub_gateway.usage import ledger as ledger_module
from metahub_gateway.usage.ledger import RequestLogContext, UsageLedgerWriteResult
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError


class _ScriptedStream:
    """按固定字节序列模拟 Provider SSE，并记录显式关闭次数。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。"""
        self.chunks = chunks
        self.close_calls = 0

    async def iter_bytes(self):
        """逐块让出预置 SSE，不执行网络访问。"""

        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self) -> None:
        """记录测试流或锁的幂等释放，确保异常路径不会遗留后台资源。

        记录幂等关闭事实。
        """

        self.close_calls += 1


class _HangingStream(_ScriptedStream):
    """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。

    模拟永远不返回首块的 Provider 流。
    """

    def __init__(self) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。"""
        super().__init__([])
        self.never = asyncio.Event()

    async def iter_bytes(self):
        """模拟受控流式调用阶段，用于验证握手、取消和发送围栏的持久化顺序。

        等待状态机取消内部读取 task。
        """

        await self.never.wait()
        yield b"unreachable"


class _Adapter(OpenAICompatibleAdapter):
    """只返回测试流对象的 Adapter，不接触真实 Provider。"""

    def __init__(self, stream: object) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。"""
        self.stream = stream

    async def send_stream(self, _request: ProviderRequest):
        """模拟受控流式调用阶段，用于验证握手、取消和发送围栏的持久化顺序。

        模拟已取得 2xx 流握手。
        """

        return self.stream


class _Guard:
    """记录实验热路径调用，并可注入截止或 Outcome CAS 失败。"""

    def __init__(self, *, interrupt_on_second_check: bool = False, finalizes: bool = True) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。"""
        self.interrupt_on_second_check = interrupt_on_second_check
        self.finalizes = finalizes
        self.raise_calls = 0
        self.bound_stream_id: str | None = None
        self.handshake_seen = False
        self.terminal_facts: list[dict[str, Any]] = []
        self.final_intents: list[dict[str, Any]] = []
        self.interruption_code: str | None = None
        self.cancelled_owner_task: asyncio.Task[Any] | None = None
        self.owner_task: asyncio.Task[Any] | None = None
        self._terminal_committed = False
        self._terminal_intent: dict[str, Any] | None = None
        self.stop_calls = 0

    @property
    def remaining_seconds(self) -> float:
        """返回短但正数的等待窗口，第二次检查负责确定性触发实验截止。"""

        return 0.01 if self.interrupt_on_second_check else 1.0

    @property
    def interruption_error_code(self) -> str | None:
        """测试替身不依赖后台 heartbeat，截止由显式检查产生。"""

        return self.interruption_code

    @property
    def terminal_committed(self) -> bool:
        """返回 Outcome CAS 是否已由测试替身提交。"""

        return self._terminal_committed

    @property
    def terminal_intent(self) -> dict[str, Any] | None:
        """返回实际提交的安全终态参数。"""

        return self._terminal_intent

    def consume_owner_cancellation(self, owner: asyncio.Task[Any]) -> bool:
        """只允许精确 body owner 消费测试 Guard 发出的一次 cancel。"""

        if self.cancelled_owner_task is not owner:
            return False
        self.cancelled_owner_task = None
        return True

    def bind_stream(self, stream_session_id: str) -> None:
        """模拟受控流式调用阶段，用于验证握手、取消和发送围栏的持久化顺序。

        保存不可变 StreamSession owner。
        """

        self.bound_stream_id = stream_session_id

    async def acquire_send_permission(self) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。

        模拟最终发送围栏 CAS 成功。
        """

        return None

    async def run_provider_call(self, call):
        """模拟受控流式调用阶段，用于验证握手、取消和发送围栏的持久化顺序。

        围栏后才构造并等待 Provider 握手。
        """

        return await call()

    def mark_stream_handshake(self) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。

        记录 Provider 已确认收到请求。
        """

        self.handshake_seen = True

    def attach_current_task(self) -> None:
        """直接消费 body_iterator 时将 owner 转移到当前测试 task。"""

        self.owner_task = asyncio.current_task()
        assert self.owner_task is not None

    def raise_if_interrupted(self) -> None:
        """在第二次读取检查时模拟 absolute deadline 到期。"""

        self.raise_calls += 1
        if self.interrupt_on_second_check and self.raise_calls >= 2:
            # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
            raise ApiError("gateway.experiment_execution_timeout")

    def mark_stream_terminal(self, **facts: Any) -> None:
        """保存独立于 Gateway 成败的 Provider 质量候选事实。"""

        self.terminal_facts.append(facts)

    async def finalize(self, **intent: Any) -> bool:
        """保存最终 Gateway Outcome，并返回测试指定的 owner CAS 结果。"""

        self.final_intents.append(intent)
        if self.finalizes:
            self._terminal_intent = intent
            self._terminal_committed = True
        return self.finalizes

    async def stop(self) -> None:
        """记录 RequestLog 不一致时停止 heartbeat、交给 reaper 的事实。"""

        self.stop_calls += 1


class _Lease:
    """统计流结束后并发额度释放次数。"""

    def __init__(self) -> None:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。"""
        self.release_calls = 0

    async def release(self) -> None:
        """记录测试流或锁的幂等释放，确保异常路径不会遗留后台资源。

        记录一次释放。
        """

        self.release_calls += 1


class _BlockingLease(_Lease):
    """在 Outcome 已提交后的资源释放点提供可控取消窗口。"""

    def __init__(self) -> None:
        """初始化 Outcome 提交后的异步释放屏障，并记录租约清理调用次数。"""

        super().__init__()
        self.release_started = asyncio.Event()
        self.allow_release = asyncio.Event()

    async def release(self) -> None:
        """记录释放并等待测试允许清理结束。"""

        self.release_calls += 1
        self.release_started.set()
        await self.allow_release.wait()


class _BlockingCloseStream(_ScriptedStream):
    """在上游 2xx 后的关闭点提供可控取消窗口，并记录连接真正关闭。"""

    def __init__(self) -> None:
        """初始化关闭屏障和完成事实。"""

        super().__init__([])
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_completed = False

    async def aclose(self) -> None:
        """等待测试放行后完成关闭；调用方取消不能中断独立清理 task。"""

        self.close_calls += 1
        self.close_started.set()
        await self.allow_close.wait()
        self.close_completed = True


def _context(request_id: str) -> RequestLogContext:
    """构造只含安全元数据的租户请求上下文。"""

    return RequestLogContext(
        tenant_id="tenant-prd075-stream",
        request_id=request_id,
        trace_id=f"trc-{request_id}",
        provider_id="provider-prd075-stream",
        provider_code="local_openai",
        model_id="model-prd075-stream",
    )


def _provider_request(*, termination_marker: str | None = "[DONE]") -> ProviderRequest:
    """构造仅驻留内存的最小流请求。"""

    return ProviderRequest(
        provider_id="provider-prd075-stream",
        provider_code="local_openai",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="https://provider.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer test-only-provider-key"},
        payload={"model": "upstream-a", "messages": [], "stream": True},
        tenant_id="tenant-prd075-stream",
        model_id="model-prd075-stream",
        stream_termination_marker=termination_marker,
    )


def _session(context: RequestLogContext) -> StreamSession:
    """构造与当前 request owner 完全一致的内存 StreamSession。"""

    # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
    return StreamSession(
        stream_session_id=f"str-{context.request_id}",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        model_id=context.model_id or "",
        provider_id=context.provider_id or "",
        provider_code=context.provider_code or "",
        inter_token_timeout_ms=1000,
        started_at=datetime.now(UTC),
    )


def _install_persistence_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_finalized: bool = True,
    request_log_error: str | None = None,
    usage_durability: str = "committed",
) -> dict[str, Any]:
    """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。

    安装 StreamSession、RequestLog 和 Usage 持久化替身。
    """

    state: dict[str, Any] = {"finishes": [], "logs": [], "usage": []}

    def create_session(_config, context, *, inter_token_timeout_ms: int) -> StreamSession:
        """返回 owner 一致的会话，并核验部署超时已换算为毫秒。"""

        assert inter_token_timeout_ms == 1000
        # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
        return _session(context)

    def finish_session(_config, _session_value, **terminal: Any) -> bool:
        """保存会话终态并返回可控提交结果。"""

        state["finishes"].append(terminal)
        # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
        return session_finalized

    def record_log(_config, context, **terminal: Any) -> None:
        """保存最终日志；需要时抛固定领域错误模拟主库失败。"""

        state["logs"].append((context, terminal))
        # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
        if request_log_error is not None:
            # 流式测试阶段失配时立即失败，确保发送围栏、取消和用量记录顺序可证。
            raise ApiError(request_log_error)

    def record_usage(_config, context, usage, **_kwargs: Any) -> UsageLedgerWriteResult:
        """构造流式实验测试替身，只保存同步状态和调用次数，不保留请求或响应正文。

        返回 committed、spooled 或 lost 三态，不保存 Token 或正文。
        """

        state["usage"].append((context, usage))
        return UsageLedgerWriteResult(
            durability=usage_durability,  # type: ignore[arg-type] - 参数由本文件固定用例控制。
            committed=None,
        )

    monkeypatch.setattr(api_module, "create_stream_session", create_session)
    monkeypatch.setattr(api_module, "mark_stream_session_active", lambda *_args: None)
    monkeypatch.setattr(api_module, "safe_finish_stream_session", finish_session)
    monkeypatch.setattr(api_module, "record_request_log", record_log)
    monkeypatch.setattr(api_module, "safe_record_request_log", record_log)
    monkeypatch.setattr(api_module, "safe_record_usage_ledger", record_usage)
    monkeypatch.setattr(
        api_module,
        "safe_finalize_cache_affinity_selection",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(api_module, "_enqueue_finops_ledger_event", lambda *_args: None)
    return state


@pytest.mark.parametrize(
    "failure",
    (
        ApiError("gateway.request_log_unavailable"),
        DbError("db.connection_failed", "synthetic failure", "retry"),
        RuntimeError("synthetic request log failure"),
    ),
)
def test_non_stream_experiment_requires_strict_request_log_terminal(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """实验非流日志失败仍须处理 Usage，最终才阻断正文并返回固定领域码。"""

    usage_calls = 0
    log_calls = 0

    def fail_required_log(*_args, **_kwargs) -> None:
        """模拟严格 RequestLog 的领域错误或未知本地异常。"""

        nonlocal log_calls
        log_calls += 1
        raise failure

    def record_usage(*_args, **_kwargs) -> None:
        """记录 Provider 已发生后仍必须执行的 Usage 主库或 spool 尝试。"""

        nonlocal usage_calls
        usage_calls += 1
        return None

    monkeypatch.setattr(api_module, "record_request_log", fail_required_log)
    monkeypatch.setattr(api_module, "safe_record_usage_ledger", record_usage)
    context = _context("req-prd075-non-stream-strict")
    response = NormalizedResponse(
        payload={"choices": []},
        usage=NormalizedUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        status_code=200,
    )

    with pytest.raises(ApiError) as unavailable:
        api_module._complete_non_stream_response(
            SimpleNamespace(),
            context,
            response,
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            request_started=time.perf_counter(),
            output_safety_scan=None,
            require_request_log=True,
        )

    assert unavailable.value.code == "gateway.request_log_unavailable"
    assert usage_calls == 1
    # Usage 完成后允许一次幂等终态重试；永久故障仍不得返回正文。
    assert log_calls == 2


def test_non_experiment_non_stream_keeps_best_effort_request_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通请求继续使用 best-effort writer，不启用实验严格写入语义。"""

    calls: list[str] = []

    def fail_if_strict(*_args, **_kwargs) -> None:
        """普通路径若错误调用严格 writer 就立即暴露。"""

        # 普通请求不具备实验终态 owner，进入严格 writer 会破坏历史兼容合同。
        raise AssertionError("strict writer used for ordinary request")

    def record_best_effort(*_args, **_kwargs) -> None:
        """记录普通请求仍选择兼容 writer，证明实验严格路径没有扩散到旧流量。"""

        calls.append("request_log")

    def record_usage(*_args, **_kwargs) -> UsageLedgerWriteResult:
        """返回可发布响应的确定 Usage durability，并记录账本步骤真实发生一次。"""

        calls.append("usage")
        return UsageLedgerWriteResult(durability="committed", committed=None)

    monkeypatch.setattr(api_module, "record_request_log", fail_if_strict)
    monkeypatch.setattr(api_module, "safe_record_request_log", record_best_effort)
    monkeypatch.setattr(api_module, "safe_record_usage_ledger", record_usage)
    monkeypatch.setattr(api_module, "_enqueue_finops_ledger_event", lambda *_args: None)
    context = _context("req-prd075-non-stream-best-effort")
    normalized = NormalizedResponse(
        payload={"choices": []},
        usage=NormalizedUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        status_code=200,
    )

    response = api_module._complete_non_stream_response(
        SimpleNamespace(),
        context,
        normalized,
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        request_started=time.perf_counter(),
        output_safety_scan=None,
    )

    assert response.status_code == 200
    assert calls == ["request_log", "usage"]


def test_experiment_terminal_writer_maps_real_connect_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不替换目标 writer，验证真实连接边界的 DbError 固定映射。"""

    def fail_connect(_config) -> None:
        """在 ledger 的真实连接入口注入不可连接故障。"""

        # 合成连接故障只携带固定测试文本，用于验证底层异常不会进入公开错误。
        raise DbError("db.connection_failed", "synthetic failure", "retry")

    monkeypatch.setattr(ledger_module, "_connect", fail_connect)
    error_code = api_module._record_experiment_request_log_terminal(
        SimpleNamespace(),
        _context("req-prd075-real-connect-failure"),
        status="failed",
        error_code="gateway.experiment_unavailable",
        latency_ms=1,
    )

    assert error_code == "gateway.request_log_unavailable"


def test_non_stream_transient_log_failure_retries_after_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次严格日志失败后先处理 Usage，再以幂等重试恢复成功终态。"""

    calls: list[str] = []
    usage_calls = 0

    def transient_log(_config, _context, **terminal: Any) -> None:
        """只让第一次连接失败，第二次记录最终状态。"""

        calls.append(str(terminal["status"]))
        if len(calls) == 1:
            # 首次严格日志写入模拟瞬态连接失败，后续重试必须使用同一安全终态。
            raise DbError("db.connection_failed", "synthetic failure", "retry")

    def record_usage(*_args, **_kwargs) -> None:
        """记录首次日志故障后 Usage 仍被处理。"""

        nonlocal usage_calls
        usage_calls += 1
        return None

    monkeypatch.setattr(api_module, "record_request_log", transient_log)
    monkeypatch.setattr(api_module, "safe_record_usage_ledger", record_usage)
    monkeypatch.setattr(api_module, "_enqueue_finops_ledger_event", lambda *_args: None)
    response = api_module._complete_non_stream_response(
        SimpleNamespace(),
        _context("req-prd075-non-stream-transient-log"),
        NormalizedResponse(
            payload={"choices": []},
            usage=NormalizedUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            status_code=200,
        ),
        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        request_started=time.perf_counter(),
        output_safety_scan=None,
        require_request_log=True,
    )

    assert response.status_code == 200
    assert calls == ["success", "success"]
    assert usage_calls == 1


async def _build_and_consume(
    stream: object,
    guard: _Guard | None,
    lease: _Lease,
    *,
    termination_marker: str | None = "[DONE]",
) -> tuple[object, list[bytes]]:
    """执行真实流状态机并收集生成器交付的字节边界。"""

    context = _context("req-prd075-stream")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(termination_marker=termination_marker),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )
    chunks = [chunk async for chunk in response.body_iterator]
    return response, chunks


@pytest.mark.anyio
async def test_openai_profile_eof_without_done_commits_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI SSE 未观察到冻结 `[DONE]` 时不得把 clean EOF 冒充客户端成功。"""

    state = _install_persistence_spies(monkeypatch)
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(_ScriptedStream([]), guard, lease)
    payload = b"".join(chunks)

    assert payload.count(b"event: error") == 1
    assert b"gateway.upstream_unavailable" in payload
    assert b"data: [DONE]" not in payload
    assert state["finishes"][-1]["status"] == "upstream_failed"
    assert state["logs"][-1][1]["status"] == "failed"
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"
    assert guard.terminal_facts[-1]["quality_succeeded"] is False
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_openai_eof_usage_loss_emits_one_final_experiment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺终止标记后 Usage 再降级时，只交付持久化收口得到的唯一最终错误。"""

    state = _install_persistence_spies(monkeypatch, usage_durability="lost")
    guard = _Guard()

    _response, chunks = await _build_and_consume(_ScriptedStream([]), guard, _Lease())
    payload = b"".join(chunks)

    assert payload.count(b"event: error") == 1
    assert b"finops.usage_record_unavailable" in payload
    assert b"gateway.upstream_unavailable" not in payload
    assert b"data: [DONE]" not in payload
    assert state["logs"][-1][1]["error_code"] == "finops.usage_record_unavailable"
    assert guard.final_intents[-1]["error_code"] == "finops.usage_record_unavailable"


@pytest.mark.anyio
async def test_openai_profile_body_then_eof_commits_failed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI raw SSE 的正文子串不能冒充 `[DONE]`，EOF 必须提交失败。"""

    state = _install_persistence_spies(monkeypatch)
    body = b'data: {"choices":[{"delta":{"content":"synthetic [DONE]"}}]}\n\n'
    lease = _Lease()

    _response, chunks = await _build_and_consume(_ScriptedStream([body]), None, lease)
    payload = b"".join(chunks)

    assert payload.startswith(body)
    assert payload.count(b"event: error") == 1
    assert b"gateway.upstream_unavailable" in payload
    assert b"data: [DONE]" not in payload
    assert state["finishes"][-1]["status"] == "upstream_failed"
    assert state["logs"][-1][1]["status"] == "failed"
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_generic_profile_clean_eof_remains_success_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic/custom profile 没有终止标记时，clean EOF 仍是其冻结成功合同。"""

    state = _install_persistence_spies(monkeypatch)
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(
        _ScriptedStream([]),
        guard,
        lease,
        termination_marker=None,
    )

    assert chunks == []
    assert state["finishes"][-1]["status"] == "completed"
    assert state["logs"][-1][1]["status"] == "success"
    assert guard.final_intents[-1]["gateway_outcome"] == "success"
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_openai_raw_passthrough_observes_split_done_without_reframing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通 OpenAI raw 流跨块识别完整 `[DONE]`，但交付块边界保持不变。"""

    state = _install_persistence_spies(monkeypatch)
    chunks = [
        b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n',
        b"data: [DO",
        b"NE]\r\n\r\n",
    ]
    lease = _Lease()

    _response, emitted = await _build_and_consume(
        _ScriptedStream(chunks),
        None,
        lease,
    )

    assert emitted == chunks
    assert b"event: error" not in b"".join(emitted)
    assert state["finishes"][-1]["status"] == "completed"
    assert state["logs"][-1][1]["status"] == "success"
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_done_is_released_only_after_all_experiment_terminals_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功流先提交 Session、RequestLog、Usage 和 Outcome，最后才发送 `[DONE]`。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _ScriptedStream(
        [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(stream, guard, lease)

    assert chunks[-1] == b"data: [DONE]\n\n"
    assert b"event: error" not in b"".join(chunks)
    assert state["finishes"][0]["status"] == "completed"
    assert state["logs"][-1][1]["status"] == "success"
    assert len(state["usage"]) == 1
    assert guard.final_intents[-1]["gateway_outcome"] == "success"
    assert guard.terminal_facts[-1]["quality_succeeded"] is True
    assert guard.bound_stream_id == "str-req-prd075-stream"
    assert guard.handshake_seen is True
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_session_terminal_failure_withholds_done_and_returns_sse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StreamSession 未提交时保留 Provider 质量成功，但 Gateway Outcome 必须失败。"""

    state = _install_persistence_spies(monkeypatch, session_finalized=False)
    stream = _ScriptedStream(
        [b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"]
    )
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(stream, guard, lease)
    payload = b"".join(chunks)

    assert b"data: [DONE]" not in payload
    assert b"gateway.stream_processing_unavailable" in payload
    assert state["logs"][-1][1]["status"] == "failed"
    assert state["logs"][-1][0].stream_session_id is None
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"
    assert guard.terminal_facts[-1]["quality_succeeded"] is True
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_request_log_failure_defers_outcome_and_returns_sse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 RequestLog 不可用时不得发送 `[DONE]` 或提交不一致 Outcome。"""

    _install_persistence_spies(
        monkeypatch,
        request_log_error="gateway.request_log_unavailable",
    )
    stream = _ScriptedStream([b"data: [DONE]\n\n"])
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(stream, guard, lease)
    payload = b"".join(chunks)

    assert b"data: [DONE]" not in payload
    assert b"gateway.request_log_unavailable" in payload
    assert guard.final_intents == []
    assert guard.stop_calls == 1


@pytest.mark.anyio
async def test_stream_transient_request_log_failure_retries_before_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次日志连接失败可在 Usage 后恢复，但 Outcome 必须采用失败终态。"""

    state = _install_persistence_spies(monkeypatch)
    calls = 0

    def transient_log(_config, context, **terminal: Any) -> None:
        """第一次失败、最终严格重试成功，并保存安全终态调用。"""

        nonlocal calls
        calls += 1
        state["logs"].append((context, terminal))
        if calls == 1:
            # 首次流式日志写入故障用于证明 Usage 处理后仍会执行严格失败重试。
            raise DbError("db.connection_failed", "synthetic failure", "retry")

    monkeypatch.setattr(api_module, "record_request_log", transient_log)
    guard = _Guard()
    _response, chunks = await _build_and_consume(
        _ScriptedStream([b"data: [DONE]\n\n"]),
        guard,
        _Lease(),
    )

    payload = b"".join(chunks)
    assert b"gateway.request_log_unavailable" in payload
    assert b"data: [DONE]" not in payload
    assert [item[1]["status"] for item in state["logs"]] == ["success", "failed"]
    assert len(state["usage"]) == 1
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"
    assert guard.final_intents[-1]["error_code"] == "gateway.request_log_unavailable"


@pytest.mark.anyio
async def test_stream_usage_failure_rewrite_failure_does_not_commit_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage 降级后若失败日志也不可写，只能交给 reaper，不能形成矛盾 Outcome。"""

    _install_persistence_spies(monkeypatch, usage_durability="lost")
    durable_status: str | None = None
    calls = 0

    def fail_failure_rewrite(_config, _context, **terminal: Any) -> None:
        """持久化候选 success，但让 Usage 后的严格 failure 改写失败。"""

        nonlocal calls, durable_status
        calls += 1
        if calls == 1:
            durable_status = str(terminal["status"])
            return
        # 第二次固定失败反写也不可用时必须交给 Reaper，禁止测试替身伪造成功。
        raise DbError("db.connection_failed", "synthetic failure", "retry")

    monkeypatch.setattr(api_module, "record_request_log", fail_failure_rewrite)
    guard = _Guard()
    _response, chunks = await _build_and_consume(
        _ScriptedStream([b"data: [DONE]\n\n"]),
        guard,
        _Lease(),
    )

    payload = b"".join(chunks)
    assert durable_status == "success"
    assert calls == 2
    assert guard.final_intents == []
    assert guard.stop_calls == 1
    assert b"gateway.request_log_unavailable" in payload
    assert b"data: [DONE]" not in payload


@pytest.mark.anyio
async def test_usage_durability_loss_overrides_success_and_withholds_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage 主库与 spool 均不可证明时，以 FinOps 错误覆盖 Provider 成功。"""

    state = _install_persistence_spies(monkeypatch, usage_durability="lost")
    stream = _ScriptedStream([b"data: [DONE]\n\n"])
    guard = _Guard()
    lease = _Lease()

    _response, chunks = await _build_and_consume(stream, guard, lease)
    payload = b"".join(chunks)

    assert b"data: [DONE]" not in payload
    assert b"finops.usage_record_unavailable" in payload
    assert state["logs"][-1][1]["error_code"] == "finops.usage_record_unavailable"
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"


@pytest.mark.anyio
async def test_outcome_cas_loss_rewrites_request_log_and_returns_sse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 generation 不能提交 Outcome，也不能向客户端发送暂存 `[DONE]`。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _ScriptedStream([b"data: [DONE]\n\n"])
    guard = _Guard(finalizes=False)
    lease = _Lease()

    _response, chunks = await _build_and_consume(stream, guard, lease)
    payload = b"".join(chunks)

    assert b"data: [DONE]" not in payload
    assert b"gateway.experiment_unavailable" in payload
    assert [item[1]["status"] for item in state["logs"]] == [
        "success",
        "success",
        "failed",
    ]
    assert state["logs"][-1][1]["error_code"] == "gateway.experiment_unavailable"


@pytest.mark.anyio
async def test_absolute_deadline_after_headers_returns_200_sse_error_without_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """响应头已建立后的实验截止关闭上游，并用 SSE 504 语义收口。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _HangingStream()
    guard = _Guard(interrupt_on_second_check=True)
    lease = _Lease()

    response, chunks = await _build_and_consume(stream, guard, lease)
    payload = b"".join(chunks)

    assert response.status_code == 200
    assert b"gateway.experiment_execution_timeout" in payload
    assert b"data: [DONE]" not in payload
    assert state["finishes"][-1]["status"] == "timed_out"
    assert state["logs"][-1][1]["status"] == "failed"
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"
    assert guard.terminal_facts[-1]["quality_eligible"] is False
    assert stream.close_calls >= 1


@pytest.mark.anyio
async def test_upstream_timeout_log_failure_defers_one_experiment_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游超时后的日志降级必须先收口 owner，再发送一次最终错误。"""

    _install_persistence_spies(
        monkeypatch,
        request_log_error="gateway.request_log_unavailable",
    )
    stream = _HangingStream()
    guard = _Guard()
    lease = _Lease()
    context = _context("req-prd075-stream-timeout-log-failure")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=None,
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )

    first = await anext(response.body_iterator)
    # 第一段客户端错误可见时，finalizer 必须已经停止无法形成一致 Outcome 的 Guard owner。
    assert guard.final_intents == []
    assert guard.stop_calls == 1
    remaining = [chunk async for chunk in response.body_iterator]
    payload = first + b"".join(remaining)

    assert payload.count(b"event: error") == 1
    assert b"gateway.request_log_unavailable" in payload
    assert b"gateway.upstream_stream_timeout" not in payload
    assert b"data: [DONE]" not in payload
    assert stream.close_calls >= 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_ordinary_timeout_persistence_failure_does_not_repeat_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通流保留即时超时错误，后续持久化失败不得再发送第二个终态事件。"""

    state = _install_persistence_spies(monkeypatch)

    def fail_usage(*_args: Any, **_kwargs: Any) -> UsageLedgerWriteResult:
        """模拟普通流首次错误交付之后发生固定 Usage 持久化失败。"""

        # 受控故障只用于证明 finalizer 不能在已有 SSE 错误后再次发送降级错误。
        raise ApiError("finops.usage_record_unavailable")

    monkeypatch.setattr(api_module, "safe_record_usage_ledger", fail_usage)
    stream = _HangingStream()
    lease = _Lease()
    context = _context("req-prd075-ordinary-timeout-persistence-failure")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=None,
        experiment_guard=None,
    )

    first = await anext(response.body_iterator)
    assert b"gateway.upstream_stream_timeout" in first
    assert state["logs"] == []
    assert lease.release_calls == 0
    remaining = [chunk async for chunk in response.body_iterator]
    payload = first + b"".join(remaining)

    assert payload.count(b"event: error") == 1
    assert b"finops.usage_record_unavailable" not in payload
    assert len(state["logs"]) == 1
    assert stream.close_calls >= 1
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_confirmed_disconnect_records_failure_without_sending_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """客户端明确断开后只保存数据库终态，不能继续发送 SSE 字节。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _HangingStream()
    guard = _Guard()
    lease = _Lease()
    context = _context("req-prd075-stream")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )
    response.disconnect_event.set()

    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == []
    assert state["finishes"][-1]["status"] == "client_disconnected"
    assert state["logs"][-1][1]["error_code"] == "gateway.client_disconnected"
    assert guard.final_intents[-1]["gateway_outcome"] == "failed"
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_stream_body_guard_cancel_emits_exact_sse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 对 body owner 的唯一 cancel 可消费，并在清理后发送固定 SSE 错误。"""

    _install_persistence_spies(monkeypatch)
    stream = _HangingStream()
    guard = _Guard()
    lease = _Lease()
    context = _context("req-prd075-stream-guard-cancel")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )

    async def consume() -> list[bytes]:
        """在独立 body task 中收集唯一终态错误。"""

        return [chunk async for chunk in response.body_iterator]

    body_task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    guard.interruption_code = "gateway.experiment_unavailable"
    guard.cancelled_owner_task = body_task
    body_task.cancel()
    chunks = await body_task

    payload = b"".join(chunks)
    assert body_task.cancelled() is False
    assert body_task.cancelling() == 0
    assert b"gateway.experiment_unavailable" in payload
    assert b"data: [DONE]" not in payload


@pytest.mark.anyio
async def test_stream_body_multiple_cancels_preserve_external_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard 与外部 cancel 同时到达时不得转换为正常完成的实验 SSE。"""

    _install_persistence_spies(monkeypatch)
    stream = _HangingStream()
    guard = _Guard()
    lease = _Lease()
    context = _context("req-prd075-stream-multi-cancel")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )

    async def consume() -> list[bytes]:
        """等待上游块，使两个取消在同一 body await 点被观察。"""

        return [chunk async for chunk in response.body_iterator]

    body_task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    guard.interruption_code = "gateway.experiment_unavailable"
    guard.cancelled_owner_task = body_task
    body_task.cancel()
    body_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await body_task
    assert body_task.cancelled() is True
    # helper 只减去 Guard 的一次，至少一个外部取消仍由 task 保留并传播。
    assert body_task.cancelling() == 1


@pytest.mark.anyio
async def test_cancel_during_stream_cleanup_commits_failed_outcome_before_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理已开始但 Outcome CAS 未执行时，晚到取消必须冻结为 failed/cancelled。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _ScriptedStream([b"data: [DONE]\n\n"])
    guard = _Guard()
    lease = _Lease()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    original_close = api_module._safe_close_stream_response

    async def blocking_close(upstream_response: Any, context: RequestLogContext) -> None:
        """在任何持久化终态前暂停 shielded cleanup，提供可重复的取消线性化窗口。"""

        close_started.set()
        await allow_close.wait()
        await original_close(upstream_response, context)

    monkeypatch.setattr(api_module, "_safe_close_stream_response", blocking_close)
    context = _context("req-prd075-stream-cancel-before-cas")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )
    delivered: list[bytes] = []

    async def consume() -> None:
        """保存真正交付的 SSE 字节并保留取消语义。"""

        async for chunk in response.body_iterator:
            delivered.append(chunk)

    body_task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(close_started.wait(), timeout=1)
        assert guard.owner_task is body_task
        body_task.cancel()
        await asyncio.sleep(0)
        assert body_task.done() is False
    finally:
        allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await body_task

    assert delivered == []
    assert b"data: [DONE]" not in b"".join(delivered)
    assert state["logs"][-1][1]["status"] == "failed"
    assert state["logs"][-1][1]["error_code"] == "gateway.client_disconnected"
    assert guard.terminal_committed is True
    assert guard.terminal_intent is not None
    assert guard.terminal_intent["gateway_outcome"] == "failed"
    assert guard.terminal_intent["outcome_reason"] == "cancelled"
    assert guard.terminal_intent["error_code"] == "gateway.client_disconnected"
    assert lease.release_calls == 1


@pytest.mark.anyio
async def test_cancel_during_inactive_session_cleanup_waits_for_all_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active 审计失败后的取消必须等连接、Session 和 Selection 全部收口再传播。"""

    state = _install_persistence_spies(monkeypatch)
    selections: list[bool] = []
    monkeypatch.setattr(
        api_module,
        "mark_stream_session_active",
        lambda *_args: (_ for _ in ()).throw(ApiError("gateway.stream_session_unavailable")),
    )
    monkeypatch.setattr(
        api_module,
        "safe_finalize_cache_affinity_selection",
        lambda *_args, **kwargs: selections.append(bool(kwargs["succeeded"])),
    )
    stream = _BlockingCloseStream()

    build_task = asyncio.create_task(
        api_module._stream_response(
            SimpleNamespace(stream_inter_token_timeout_seconds=1),
            _context("req-prd075-inactive-cancel"),
            _Adapter(stream),
            _provider_request(),
            time.perf_counter(),
            _Lease(),
            client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            experiment_guard=_Guard(),  # type: ignore[arg-type] - 专项替身覆盖公开 Guard 合同。
        )
    )
    try:
        await asyncio.wait_for(stream.close_started.wait(), timeout=1)
        build_task.cancel()
        await asyncio.sleep(0)
        assert build_task.done() is False
    finally:
        stream.allow_close.set()

    with pytest.raises(asyncio.CancelledError):
        await build_task

    assert stream.close_calls == 1
    assert stream.close_completed is True
    assert [item["status"] for item in state["finishes"]] == ["upstream_failed"]
    assert selections == [False]
    assert not any(
        task.get_name() == "gateway-stream-terminal-finalizer" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.anyio
async def test_cancel_after_stream_outcome_cas_does_not_rewrite_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outcome CAS 后到达的取消只能传播，不能反写成功 RequestLog 或 Outcome。"""

    state = _install_persistence_spies(monkeypatch)
    stream = _ScriptedStream([b"data: [DONE]\n\n"])
    guard = _Guard()
    lease = _BlockingLease()
    context = _context("req-prd075-stream-cancel-after-cas")
    response = await api_module._stream_response(
        SimpleNamespace(stream_inter_token_timeout_seconds=1),
        context,
        _Adapter(stream),
        _provider_request(),
        time.perf_counter(),
        lease,
        client_request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
        experiment_guard=guard,  # type: ignore[arg-type] - 专项替身覆盖 Guard 公开调用合同。
    )
    delivered: list[bytes] = []

    async def consume() -> None:
        """保存真正交付的 SSE 字节并等待清理终态。"""

        async for chunk in response.body_iterator:
            delivered.append(chunk)

    body_task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(lease.release_started.wait(), timeout=1)
        assert guard.owner_task is body_task
        assert guard.terminal_committed is True
        assert guard.terminal_intent is not None
        assert guard.terminal_intent["gateway_outcome"] == "success"
        body_task.cancel()
        await asyncio.sleep(0)
        assert body_task.done() is False
    finally:
        lease.allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await body_task

    assert delivered == []
    assert b"data: [DONE]" not in b"".join(delivered)
    assert all(item[1]["status"] == "success" for item in state["logs"])
    assert all(item[1]["error_code"] is None for item in state["logs"])
    assert guard.terminal_intent is not None
    assert guard.terminal_intent["gateway_outcome"] == "success"
    assert guard.terminal_intent["outcome_reason"] == "completed"
    assert guard.terminal_intent["error_code"] is None
    assert lease.release_calls == 1
