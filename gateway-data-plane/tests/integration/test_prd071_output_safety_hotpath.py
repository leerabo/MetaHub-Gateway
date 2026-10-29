"""PRD-071 Gateway 输出安全热路径聚焦集成测试。

测试使用真实非流式编排、SSE framer 与滑窗扫描器，仅替换数据库、Provider 网络和事件队列
边界。所有替身都严格核对 tenant/request/trace owner，观测记录不得保存 Prompt、Response、
规则 pattern 或 Provider Key 明文。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import metahub_gateway.api.openai_compatible as openai_api
import pytest
from metahub_gateway.output_safety import (
    DETECTOR_VERSION,
    OutputSafetyEventRecord,
    OutputSafetyFinalization,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
)
from metahub_gateway.providers.base import (
    NormalizedResponse,
    NormalizedUsage,
    ProviderRequest,
)
from metahub_gateway.providers.openai_compatible import OpenAICompatibleAdapter
from metahub_gateway.routing.attempts import RoutingAttempt
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.streaming.sessions import StreamSession
from metahub_gateway.usage.ledger import RequestLogContext, UsageLedgerWriteResult
from metahub_shared.output_safety import OutputRule, OutputRuleSnapshot
from metahub_shared.runtime.errors import ApiError

# 合成身份和文本只用于内存测试；观测替身必须证明这些正文不会进入日志、账本或 Webhook。
TENANT_ID = "tenant_prd071_hotpath"
PROVIDER_ID = "provider_prd071_hotpath"
MODEL_ID = "model_prd071_hotpath"
PRIVATE_PROMPT = "仅测试提示正文"
BLOCK_MARKER = "机密"
MATCH_MARKER = "审计"
SAFE_EIGHT_TOKENS = "甲乙丙丁戊己庚辛"
BLOCK_TWELVE_TOKENS = "机密壬癸子丑寅卯辰巳午未"
DONE_FRAME = b"data: [DONE]\n\n"


@dataclass
class _ObservedState:
    """集中保存无正文热路径观测，便于核对调用顺序与租户 owner。"""

    durability: str = "committed"
    order: list[str] = field(default_factory=list)
    attempts: list[dict[str, object]] = field(default_factory=list)
    selections: list[dict[str, object]] = field(default_factory=list)
    request_logs: list[tuple[RequestLogContext, dict[str, object]]] = field(default_factory=list)
    usages: list[tuple[RequestLogContext, object, dict[str, object]]] = field(default_factory=list)
    webhook_events: list[object] = field(default_factory=list)
    terminals: list[OutputSafetyTerminalState | None] = field(default_factory=list)
    finishes: list[dict[str, object]] = field(default_factory=list)


class _RequestStub:
    """提供 Gateway 热路径所需的最小 app.state 与可选 Header 容器。"""

    def __init__(self) -> None:
        """创建不声明租户探针的请求替身，避免响应额外暴露租户字段。"""

        self.headers: dict[str, str] = {}
        self.state = SimpleNamespace()
        self.app = SimpleNamespace(state=SimpleNamespace())


class _ScriptedStream:
    """按给定 raw chunk 边界产生上游字节，并记录幂等关闭次数。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """保存合成网络分块；正文只存在于当前测试对象的短期内存。"""

        self.chunks = chunks
        self.close_calls = 0
        self._closed = False

    async def iter_bytes(self):
        """按原始网络边界逐块输出，不主动拼接或解析 SSE 内容。"""

        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        """幂等关闭测试流，用于验证阻断和正常完成都释放上游资源。"""

        if self._closed:
            return
        self._closed = True
        self.close_calls += 1


class _NonStreamAdapter(OpenAICompatibleAdapter):
    """返回预置规范化响应的非流式 Provider Adapter 替身。"""

    def __init__(self, response: NormalizedResponse) -> None:
        """保存单次规范化响应，不记录 ProviderRequest 的敏感字段。"""

        self.response = response

    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse:
        """返回预置响应；请求 owner 由调用前的严格构造替身单独校验。"""

        del request
        return self.response


class _StreamAdapter(OpenAICompatibleAdapter):
    """把预置 raw chunk 流接入真实 Gateway SSE 热路径并保留网络分块边界。"""

    def __init__(self, stream: _ScriptedStream) -> None:
        """保存单次流对象，不复制或记录其中可能出现的模型文本。"""

        self.stream = stream

    async def send_stream(self, request: ProviderRequest) -> _ScriptedStream:
        """返回预置流；ProviderRequest 只用于保持真实调用签名。"""

        del request
        return self.stream


class _FailingStreamAdapter(OpenAICompatibleAdapter):
    """在 Provider 握手阶段抛统一错误，验证 configured scan 的收口。"""

    def __init__(self, error: ApiError) -> None:
        """保存不含 Provider 原始正文的统一错误对象。"""

        self.error = error

    async def send_stream(self, request: ProviderRequest) -> object:
        """在尚未返回响应头时失败，不产生任何模型输出或 Usage。"""

        del request
        # 握手失败只能抛统一错误，不能把 Provider 响应或凭据附加到异常正文。
        raise self.error


class _RateLimitLease:
    """记录流结束后的并发租约释放次数。"""

    def __init__(self) -> None:
        """初始化尚未释放的单请求租约。"""

        self.release_calls = 0

    async def release(self) -> None:
        """记录一次资源归还；真实状态机应保证最终只调用一次。"""

        self.release_calls += 1


def _config() -> SimpleNamespace:
    """构造热路径使用的最小配置，并采用允许的最小扫描间隔和窗口。"""

    return SimpleNamespace(
        output_safety_max_full_text_code_points=4_096,
        output_safety_interval_tokens=8,
        output_safety_window_tokens=32,
        stream_inter_token_timeout_seconds=1.0,
    )


def _context(suffix: str) -> RequestLogContext:
    """构造只含可信 owner 与路由元数据的 Gateway 请求上下文。"""

    return RequestLogContext(
        tenant_id=TENANT_ID,
        request_id=f"req_prd071_hotpath_{suffix}",
        trace_id=f"trc_prd071_hotpath_{suffix}",
        virtual_key_id="vk_prd071_hotpath",
        provider_id=PROVIDER_ID,
        provider_code="deepseek",
        model_id=MODEL_ID,
        endpoint="/v1/chat/completions",
        request_model="company-chat",
        resolved_model="company-chat",
    )


def _route() -> GatewayRoute:
    """构造同租户单候选路由，Provider 地址与模型名只存在内存。"""

    return GatewayRoute(
        tenant_id=TENANT_ID,
        model_id=MODEL_ID,
        model_alias="company-chat",
        upstream_model_name="provider-model",
        capabilities={"chat": True, "streaming": True},
        provider_id=PROVIDER_ID,
        provider_code="deepseek",
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="PRD-071 synthetic provider",
        provider_base_url="https://provider.invalid/v1",
        requested_model="company-chat",
    )


def _provider_request(
    context: RequestLogContext,
    *,
    stream: bool,
    selection: object,
) -> ProviderRequest:
    """构造包含合成敏感值的内存请求，以证明观测面不会泄露正文或 Key。"""

    return ProviderRequest(
        provider_id=PROVIDER_ID,
        provider_code="deepseek",
        provider_type="openai_compatible",
        adapter_name="openai_compatible",
        endpoint="chat/completions",
        url="https://provider.invalid/v1/chat/completions",
        headers={"Authorization": "Bearer sk-prd071-must-not-observe"},
        payload={
            "model": "provider-model",
            "messages": [{"role": "user", "content": PRIVATE_PROMPT}],
            "stream": stream,
        },
        tenant_id=context.tenant_id,
        model_id=MODEL_ID,
        request_id=context.request_id,
        trace_id=context.trace_id,
        cache_affinity_selection=selection,
    )


def _rule(pattern: str, *, action: str, suffix: str) -> OutputRule:
    """构造冻结输出规则；pattern 使用 repr=False 且不得进入任何观测记录。"""

    digest = hashlib.sha256(suffix.encode()).hexdigest()[:32]
    return OutputRule(
        dlp_rule_id=f"dlp_{digest}",
        rule_type="keyword",
        action=action,
        priority=10,
        pattern=pattern,
    )


def _scan(
    context: RequestLogContext,
    rules: tuple[OutputRule, ...],
    *,
    stream_session_id: str | None = None,
) -> PreparedOutputSafetyScan:
    """构造严格绑定 tenant/request/trace 的 pending 或零规则扫描句柄。"""

    digest = hashlib.sha256(context.request_id.encode()).hexdigest()
    mode = "stream_window" if stream_session_id is not None else "non_stream_full"
    return PreparedOutputSafetyScan(
        output_safety_scan_id=f"oss_{digest[:32]}",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        stream_session_id=stream_session_id,
        scan_mode=mode,
        scan_status="pending" if rules else "not_configured",
        interval_tokens=8 if stream_session_id is not None else None,
        window_tokens=32 if stream_session_id is not None else None,
        snapshot=OutputRuleSnapshot(
            tenant_id=context.tenant_id,
            ruleset_fingerprint=f"sha256:{digest}",
            detector_version=DETECTOR_VERSION,
            rules=rules,
        ),
    )


def _source_event(
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
) -> OutputSafetyEventRecord:
    """从无正文 blocked 终态构造已提交 source event，供真实 Webhook 转换。"""

    digest = hashlib.sha256(f"event:{scan.request_id}".encode()).hexdigest()[:32]
    assert terminal.dlp_rule_id is not None
    assert terminal.exposure_state in {"none", "partial"}
    return OutputSafetyEventRecord(
        event_id=f"osevt_{digest}",
        tenant_id=scan.tenant_id,
        output_safety_scan_id=scan.output_safety_scan_id,
        request_id=scan.request_id,
        trace_id=scan.trace_id,
        event_type="security.output_blocked",
        schema_version=1,
        scan_mode=scan.scan_mode,
        scan_status="completed",
        decision="blocked",
        error_code="content_policy_violation",
        exposure_state=terminal.exposure_state,
        emitted_token_count=terminal.emitted_token_count,
        checkpoint_count=terminal.checkpoint_count,
        dlp_rule_id=terminal.dlp_rule_id,
        detector_version=DETECTOR_VERSION,
        billing_disposition="dispute_pending",
        occurred_at="2026-08-04T08:00:00Z",
    )


def _assert_owner(context: RequestLogContext) -> None:
    """核对所有观测上下文仍属于当前可信租户和请求链路。"""

    assert context.tenant_id == TENANT_ID
    assert context.request_id.startswith("req_prd071_hotpath_")
    assert context.trace_id.startswith("trc_prd071_hotpath_")


def _install_common_spies(
    monkeypatch: pytest.MonkeyPatch,
    state: _ObservedState,
) -> None:
    """替换日志、Usage、FinOps 与 Webhook 边界，并保留严格调用顺序。"""

    def record_request_log(
        _config_value: object,
        context: RequestLogContext,
        **kwargs: object,
    ) -> None:
        """记录无正文 RequestLog 调用，同时复核可信 tenant owner。"""

        _assert_owner(context)
        state.order.append("request_log")
        state.request_logs.append((context, kwargs))

    def record_usage(
        _config_value: object,
        context: RequestLogContext,
        usage: object,
        **kwargs: object,
    ) -> UsageLedgerWriteResult:
        """返回显式 durability，并证明 Usage 只含数字和稳定估算元数据。"""

        _assert_owner(context)
        assert isinstance(usage, dict)
        assert "messages" not in usage and "choices" not in usage
        state.order.append("usage_durable")
        state.usages.append((context, usage, kwargs))
        committed = (
            SimpleNamespace(usage_ledger_id=f"ulg_{context.request_id}")
            if state.durability == "committed"
            else None
        )
        # 显式结果让调用方必须先确认 committed/spooled，再发布输出安全事件。
        return UsageLedgerWriteResult(
            durability=state.durability,  # type: ignore[arg-type]
            committed=committed,
        )

    def record_finops_event(
        _app_state: object,
        _committed: object,
        context: RequestLogContext,
    ) -> None:
        """记录 FinOps 机会性事件，顺序必须晚于 Usage durability。"""

        _assert_owner(context)
        state.order.append("finops_event")

    def enqueue_event(_app_state: object, event: object) -> bool:
        """记录已通过账单门禁的 Webhook，并拒绝正文进入测试观测面。"""

        state.order.append("safety_event")
        state.webhook_events.append(event)
        return True

    def finalize_selection(
        _config_value: object,
        selection: object,
        *,
        succeeded: bool,
    ) -> bool:
        """记录 Provider 选择结果，输出策略阻断不能把成功上游记为失败。"""

        state.selections.append({"selection": selection, "succeeded": succeeded})
        return True

    monkeypatch.setattr(openai_api, "safe_record_request_log", record_request_log)
    monkeypatch.setattr(openai_api, "safe_record_usage_ledger", record_usage)
    monkeypatch.setattr(openai_api, "_enqueue_finops_ledger_event", record_finops_event)
    monkeypatch.setattr(openai_api, "enqueue_runtime_event", enqueue_event)
    monkeypatch.setattr(
        openai_api,
        "safe_finalize_cache_affinity_selection",
        finalize_selection,
    )


def _install_non_stream_spies(
    monkeypatch: pytest.MonkeyPatch,
    state: _ObservedState,
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan,
    adapter: _NonStreamAdapter,
    provider_request: ProviderRequest,
) -> GatewayRoutePlan:
    """安装非流式 attempt、Provider 请求和 scan 终结的严格边界替身。"""

    route = _route()
    plan = GatewayRoutePlan(initial_route=route, initial_role="primary")
    attempt = RoutingAttempt(
        routing_attempt_id=f"rta_{hashlib.sha256(context.request_id.encode()).hexdigest()[:32]}",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        attempt_sequence=1,
        attempt_role="primary",
        route=route,
    )

    def create_attempt(
        _config_value: object,
        current_plan: GatewayRoutePlan,
        current_route: GatewayRoute,
        **kwargs: object,
    ) -> RoutingAttempt:
        """验证 attempt 在 Provider 前使用当前租户与同一 request/trace 建立。"""

        assert current_plan is plan and current_route is route
        assert kwargs["request_id"] == context.request_id
        assert kwargs["trace_id"] == context.trace_id
        state.order.append("attempt_started")
        return attempt

    def complete_attempt(
        _config_value: object,
        current_attempt: RoutingAttempt,
        **kwargs: object,
    ) -> bool:
        """记录 Provider attempt 终态，输出扫描结果不得反向处罚 Provider。"""

        assert current_attempt is attempt
        state.order.append("attempt_completed")
        state.attempts.append(dict(kwargs))
        return True

    def build_provider_request(*_args: object, **_kwargs: object) -> ProviderRequest:
        """返回已绑定 owner 的内存请求，不让测试替身记录 Prompt 或 Provider Key。"""

        assert provider_request.tenant_id == context.tenant_id
        assert provider_request.request_id == context.request_id
        assert provider_request.trace_id == context.trace_id
        return provider_request

    def finalize_scan(
        _config_value: object,
        prepared: PreparedOutputSafetyScan,
        terminal: OutputSafetyTerminalState,
    ) -> OutputSafetyFinalization:
        """模拟 scan 事务提交，并仅为 blocked 结果生成无正文 source event。"""

        assert prepared is scan
        assert prepared.tenant_id == context.tenant_id
        assert prepared.request_id == context.request_id
        assert prepared.trace_id == context.trace_id
        state.order.append("scan_finalized")
        state.terminals.append(terminal)
        source = _source_event(prepared, terminal) if terminal.decision == "blocked" else None
        return OutputSafetyFinalization(
            scan=prepared,
            terminal=terminal,
            source_event=source,
            idempotent_replay=False,
        )

    _install_common_spies(monkeypatch, state)
    monkeypatch.setattr(openai_api, "create_routing_attempt", create_attempt)
    monkeypatch.setattr(openai_api, "safe_complete_routing_attempt", complete_attempt)
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route_value: adapter)
    monkeypatch.setattr(openai_api, "_provider_request_with_affinity", build_provider_request)
    monkeypatch.setattr(openai_api, "finalize_output_safety_scan", finalize_scan)
    return plan


def _non_stream_payload(outcome: str) -> dict[str, object]:
    """为 clean、matched、blocked 与 unavailable 构造最小规范化响应载荷。"""

    if outcome == "clean":
        content: object = "普通安全输出"
    elif outcome == "matched":
        content = f"普通输出{MATCH_MARKER}"
    elif outcome == "blocked":
        content = f"普通输出{BLOCK_MARKER}"
    else:
        # message 结构损坏必须失败关闭，但异常和日志不能包含该原始对象。
        return {"choices": [{"index": 0, "message": "invalid-message-shape"}]}
    return {
        "id": f"chatcmpl-{outcome}",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


async def _exercise_non_stream(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    *,
    durability: str,
    budget_reservation_outstanding_cny: str | None = None,
) -> tuple[_ObservedState, object | None, ApiError | None, PreparedOutputSafetyScan]:
    """执行真实单候选非流式编排，并把公开响应或统一错误返回给断言。"""

    context = replace(
        _context(f"non_stream_{outcome}_{durability}"),
        budget_reservation_outstanding_cny=budget_reservation_outstanding_cny,
    )
    rule = (
        _rule(MATCH_MARKER, action="log_only", suffix=context.request_id)
        if outcome == "matched"
        else _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    )
    scan = _scan(context, (rule,))
    normalized = NormalizedResponse(
        payload=_non_stream_payload(outcome),
        usage=NormalizedUsage(input_tokens=3, output_tokens=12, total_tokens=15),
        status_code=200,
    )
    adapter = _NonStreamAdapter(normalized)
    selection = object()
    provider_request = _provider_request(context, stream=False, selection=selection)
    state = _ObservedState(durability=durability)
    plan = _install_non_stream_spies(
        monkeypatch,
        state,
        context,
        scan,
        adapter,
        provider_request,
    )
    try:
        response = await openai_api._non_stream_response_with_fallback(
            _config(),
            context,
            plan,
            endpoint="chat/completions",
            payload=provider_request.payload,
            request=_RequestStub(),
            request_started=time.perf_counter(),
            budget_guard_result=None,
            output_safety_scan=scan,
        )
    # 统一错误对象是本测试需要核对的公开合同，不能把异常 cause 或响应正文写入 state。
    except ApiError as error:
        return state, None, error, scan
    return state, response, None, scan


def _session_id(context: RequestLogContext) -> str:
    """根据 request_id 生成稳定 StreamSession ID，便于严格 owner 断言。"""

    return f"str_{hashlib.sha256(context.request_id.encode()).hexdigest()[:32]}"


def _install_stream_spies(
    monkeypatch: pytest.MonkeyPatch,
    state: _ObservedState,
    context: RequestLogContext,
    scan: PreparedOutputSafetyScan,
) -> StreamSession:
    """安装真实 SSE 热路径外侧的 Session、scan、Usage 与事件严格替身。"""

    session = StreamSession(
        stream_session_id=_session_id(context),
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        model_id=MODEL_ID,
        provider_id=PROVIDER_ID,
        provider_code="deepseek",
        inter_token_timeout_ms=1_000,
        started_at=datetime.now(UTC),
    )

    def create_session(
        _config_value: object,
        current_context: RequestLogContext,
        *,
        inter_token_timeout_ms: int,
    ) -> StreamSession:
        """验证 starting Session 在扫描和 Provider 握手之前绑定可信 owner。"""

        _assert_owner(current_context)
        assert current_context.request_id == context.request_id
        assert inter_token_timeout_ms == 1_000
        state.order.append("session_created")
        # 返回同一内存 Session 才能证明后续 active、终结和 scan owner 没有被重新拼接。
        return session

    def prepare_scan(
        _config_value: object,
        **kwargs: object,
    ) -> PreparedOutputSafetyScan:
        """验证 stream scan 与 Session 使用完全相同的租户、请求和链路身份。"""

        assert kwargs["tenant_id"] == session.tenant_id
        assert kwargs["request_id"] == session.request_id
        assert kwargs["trace_id"] == session.trace_id
        assert kwargs["stream_session_id"] == session.stream_session_id
        assert kwargs["scan_mode"] == "stream_window"
        state.order.append("scan_prepared")
        # 返回值已由测试构造器冻结，任何 owner 错配都会在上述断言中立即失败。
        return scan

    def mark_active(_config_value: object, current_session: StreamSession) -> None:
        """模拟上游握手成功后的 active 审计闸门提交。"""

        assert current_session is session
        current_session.status = "active"
        state.order.append("session_active")

    def finish_session(
        _config_value: object,
        current_session: StreamSession,
        *,
        status: str,
        error_code: str | None,
        quality_observation_eligible: bool,
        **kwargs: object,
    ) -> bool:
        """模拟 Session 与 scan/source event 原子终结，并记录无正文终态。"""

        assert current_session is session
        prepared = kwargs.get("output_safety_scan")
        terminal = kwargs.get("output_safety_terminal")
        assert (prepared is None) == (terminal is None)
        if prepared is not None:
            assert prepared is scan
            assert isinstance(terminal, OutputSafetyTerminalState)
            assert scan.tenant_id == session.tenant_id
            assert scan.request_id == session.request_id
            assert scan.trace_id == session.trace_id
            assert scan.stream_session_id == session.stream_session_id
        state.order.append("session_finished")
        state.terminals.append(
            terminal if isinstance(terminal, OutputSafetyTerminalState) else None
        )
        state.finishes.append(
            {
                "status": status,
                "error_code": error_code,
                "quality_observation_eligible": quality_observation_eligible,
            }
        )
        current_session.status = status
        if isinstance(terminal, OutputSafetyTerminalState) and terminal.decision == "blocked":
            current_session.output_safety_event = _source_event(scan, terminal)
        # True 表示 Session 与可选 scan/source event 已按同一事务事实提交。
        return True

    _install_common_spies(monkeypatch, state)
    monkeypatch.setattr(openai_api, "create_stream_session", create_session)
    monkeypatch.setattr(openai_api, "prepare_output_safety_scan", prepare_scan)
    monkeypatch.setattr(openai_api, "mark_stream_session_active", mark_active)
    monkeypatch.setattr(openai_api, "safe_finish_stream_session", finish_session)
    # 调用方只读取最终计数；Session 对象本身不包含任何模型输出或 Prompt 正文。
    return session


def _sse_content(text: str) -> bytes:
    """把合成模型文本编码为单个完整 OpenAI-compatible SSE event。"""

    payload = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode()


def _split_inside_marker(frame: bytes, marker: str) -> list[bytes]:
    """在多字节关键词首字符内部切分 raw chunk，验证完整 event 前不得放行。"""

    marker_bytes = marker.encode()
    start = frame.index(marker_bytes)
    split = start + 1
    return [frame[:split], frame[split:]]


async def _consume_stream(response: object) -> list[bytes]:
    """消费 StreamingResponse 生成器，并保留下游实际收到的事件边界。"""

    chunks: list[bytes] = []
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        chunks.append(chunk)
    return chunks


async def _exercise_stream(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    upstream_chunks: list[bytes],
    *,
    rules: tuple[OutputRule, ...],
    durability: str = "spooled",
) -> tuple[_ObservedState, list[bytes], PreparedOutputSafetyScan, _RateLimitLease, StreamSession]:
    """执行真实 SSE framer/scanner 热路径并返回无正文观测与下游字节。"""

    context = _context(suffix)
    stream_session_id = _session_id(context)
    scan = _scan(context, rules, stream_session_id=stream_session_id)
    state = _ObservedState(durability=durability)
    session = _install_stream_spies(monkeypatch, state, context, scan)
    stream = _ScriptedStream(upstream_chunks)
    adapter = _StreamAdapter(stream)
    selection = object()
    provider_request = _provider_request(context, stream=True, selection=selection)
    lease = _RateLimitLease()
    response = await openai_api._stream_response(
        _config(),
        context,
        adapter,
        provider_request,
        time.perf_counter(),
        lease,
        client_request=_RequestStub(),
        enable_output_safety=True,
    )
    downstream = await _consume_stream(response)
    assert stream.close_calls == 1
    # 返回值把下游字节与无正文观测分开，避免测试 state 意外长期保存 SSE 正文。
    return state, downstream, scan, lease, session


def _error_payload(event: bytes) -> dict[str, object]:
    """解析测试收到的唯一标准 SSE error，返回其无正文 error 对象。"""

    lines = event.decode().splitlines()
    assert lines[0] == "event: error"
    data_line = next(line for line in lines if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    # 标准 SSE error 的公开合同只允许稳定错误元数据，不返回模型或规则正文。
    return payload["error"]


def _assert_no_plaintext(state: _ObservedState) -> None:
    """确认日志、Usage、终态与 Webhook 观测中没有 Prompt、输出或 Key 明文。"""

    observable = repr(
        (
            state.request_logs,
            state.usages,
            state.webhook_events,
            state.terminals,
            state.finishes,
        )
    )
    for forbidden in (
        PRIVATE_PROMPT,
        BLOCK_MARKER,
        MATCH_MARKER,
        "sk-prd071-must-not-observe",
        "provider-model",
    ):
        assert forbidden not in observable


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "durability", "decision", "error_code", "http_status"),
    (
        ("clean", "committed", "clean", None, None),
        ("matched", "committed", "matched", None, None),
        ("blocked", "spooled", "blocked", "content_policy_violation", 403),
        (
            "unavailable",
            "committed",
            "unknown",
            "gateway.output_safety_unavailable",
            503,
        ),
    ),
)
async def test_non_stream_outcomes_keep_provider_success_and_durable_event_gate(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    durability: str,
    decision: str,
    error_code: str | None,
    http_status: int | None,
) -> None:
    """验证非流式四种输出安全结果、403 上下文与 Provider 成功归因。"""

    state, response, error, scan = await _exercise_non_stream(
        monkeypatch,
        outcome,
        durability=durability,
        budget_reservation_outstanding_cny="0",
    )

    assert len(state.attempts) == 1
    assert state.attempts[0]["succeeded"] is True
    assert state.attempts[0]["quality_observation_eligible"] is True
    assert state.selections[0]["succeeded"] is True
    terminal = state.terminals[0]
    assert terminal is not None and terminal.decision == decision
    assert state.usages[0][2] == {"require_durable": True, "return_durability": True}
    # clean/matched 才允许返回正文；blocked/unavailable 必须进入统一错误并携带同一 owner。
    if error_code is None:
        assert error is None and response is not None
        assert state.request_logs[0][1]["status"] == "success"
    else:
        assert response is None and error is not None
        assert error.code == error_code and error.http_status == http_status
        attached = error.request_log_context
        assert attached.tenant_id == TENANT_ID
        assert attached.request_id == scan.request_id
        assert attached.trace_id == scan.trace_id
        assert attached.output_safety_scan_id == scan.output_safety_scan_id
        assert attached.budget_reservation_outstanding_cny == (
            "0" if durability == "committed" else None
        )
        assert state.request_logs[0][1] == {
            "status": "failed",
            "error_code": error_code,
            "latency_ms": state.request_logs[0][1]["latency_ms"],
        }
    assert state.order.index("usage_durable") < state.order.index("finops_event")
    if outcome == "blocked":
        assert state.order.index("usage_durable") < state.order.index("safety_event")
        assert len(state.webhook_events) == 1
        event = state.webhook_events[0]
        assert event.tenant_id == TENANT_ID
        assert event.payload["request_id"] == scan.request_id
        assert event.trace_id == scan.trace_id
    else:
        assert state.webhook_events == []
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_non_stream_lost_usage_fails_before_body_or_safety_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证非流式 Usage durability 丢失必须升级 FinOps 503 且不得返回正文。"""

    state, response, error, _scan_value = await _exercise_non_stream(
        monkeypatch,
        "clean",
        durability="lost",
        budget_reservation_outstanding_cny="0",
    )

    assert response is None
    assert error is not None
    assert error.code == "finops.usage_record_unavailable"
    assert error.http_status == 503
    assert error.request_log_context.budget_reservation_outstanding_cny is None
    assert state.webhook_events == []
    assert "safety_event" not in state.order
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_enabled_stream_reassembles_complete_event_before_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证启用规则时跨 raw chunk 的 UTF-8 event 完整后才会向下游发送。"""

    context = _context("stream_complete_event")
    rule = _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    safe_frame = _sse_content(SAFE_EIGHT_TOKENS)
    raw_chunks = _split_inside_marker(safe_frame, SAFE_EIGHT_TOKENS[0]) + [DONE_FRAME]

    state, downstream, _scan_value, lease, session = await _exercise_stream(
        monkeypatch,
        "stream_complete_event",
        raw_chunks,
        rules=(rule,),
    )

    assert downstream == [safe_frame, DONE_FRAME]
    terminal = state.terminals[0]
    assert terminal is not None
    assert (terminal.scan_status, terminal.decision) == ("completed", "clean")
    assert terminal.observed_output_token_count == 8
    assert terminal.emitted_token_count == 8
    assert terminal.emitted_sse_event_count == 1
    assert session.chunk_count == 2
    assert lease.release_calls == 1
    assert state.webhook_events == []
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_stream_counts_only_emitted_events_with_model_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 keepalive、usage-only 与 DONE 均发送但不计入正文 SSE event 暴露数。"""

    context = _context("stream_body_event_count")
    rule = _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    keepalive = b": keepalive\n\n"
    usage_only = (
        b'data: {"choices":[],"usage":{"prompt_tokens":3,'
        b'"completion_tokens":8,"total_tokens":11}}\n\n'
    )
    body_frame = _sse_content(SAFE_EIGHT_TOKENS)

    state, downstream, _scan_value, lease, session = await _exercise_stream(
        monkeypatch,
        "stream_body_event_count",
        [keepalive, usage_only, body_frame, DONE_FRAME],
        rules=(rule,),
    )

    assert downstream == [keepalive, usage_only, body_frame, DONE_FRAME]
    terminal = state.terminals[0]
    assert terminal is not None
    assert terminal.emitted_sse_event_count == 1
    assert terminal.emitted_token_count == 8
    assert session.chunk_count == 4
    assert lease.release_calls == 1
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_cross_raw_block_separates_observed_scanned_and_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证跨 chunk 阻断不发送命中 event，并分离 observed、scanned、emitted。"""

    context = _context("stream_cross_raw_block")
    rule = _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    safe_frame = _sse_content(SAFE_EIGHT_TOKENS)
    blocked_frame = _sse_content(BLOCK_TWELVE_TOKENS)
    raw_chunks = [safe_frame, *_split_inside_marker(blocked_frame, BLOCK_MARKER), DONE_FRAME]

    state, downstream, scan, lease, _session = await _exercise_stream(
        monkeypatch,
        "stream_cross_raw_block",
        raw_chunks,
        rules=(rule,),
    )

    assert downstream[0] == safe_frame
    assert blocked_frame not in downstream
    assert b"".join(downstream).count(b"event: error\n") == 1
    assert len(downstream) == 2
    error = _error_payload(downstream[-1])
    assert error == {
        "code": "content_policy_violation",
        "message": "Model output was blocked by content policy.",
        "request_id": scan.request_id,
        "trace_id": scan.trace_id,
        "output_safety_scan_id": scan.output_safety_scan_id,
        "already_emitted": True,
        "emitted_token_count": 8,
    }
    terminal = state.terminals[0]
    assert terminal is not None
    assert terminal.observed_output_token_count == 20
    assert terminal.scanned_token_count == 16
    assert terminal.emitted_token_count == 8
    assert terminal.emitted_sse_event_count == 1
    assert terminal.emitted_bytes == len(safe_frame)
    assert state.usages[0][1]["output_tokens"] == 20
    assert state.finishes[0]["quality_observation_eligible"] is False
    assert state.selections[0]["succeeded"] is True
    assert state.order.index("usage_durable") < state.order.index("safety_event")
    assert len(state.webhook_events) == 1
    assert lease.release_calls == 1
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_tail_window_block_omits_done_and_emits_one_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证不足检查间隔的尾窗命中会省略 DONE，并只追加一个标准安全错误。"""

    context = _context("stream_tail_block")
    rule = _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    exposed_frame = _sse_content(BLOCK_MARKER)

    state, downstream, scan, lease, _session = await _exercise_stream(
        monkeypatch,
        "stream_tail_block",
        [exposed_frame, DONE_FRAME],
        rules=(rule,),
    )

    assert downstream[0] == exposed_frame
    assert DONE_FRAME not in downstream
    assert b"".join(downstream).count(b"event: error\n") == 1
    assert len(downstream) == 2
    error = _error_payload(downstream[-1])
    assert error["code"] == "content_policy_violation"
    assert error["request_id"] == scan.request_id
    assert error["trace_id"] == scan.trace_id
    assert error["already_emitted"] is True
    assert error["emitted_token_count"] == 2
    terminal = state.terminals[0]
    assert terminal is not None
    assert terminal.observed_output_token_count == 2
    assert terminal.scanned_token_count == 2
    assert terminal.emitted_token_count == 2
    assert terminal.exposure_state == "partial"
    assert state.usages[0][1]["output_tokens"] == 2
    assert lease.release_calls == 1
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_zero_rules_preserve_exact_raw_chunk_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证零规则不启用严格 framer，畸形和非 UTF-8 Provider chunk 仍原样透传。"""

    raw_chunks = [
        b"data: {broken",
        b"-json}\n\n",
        b"data: \xff\xfe\n\n",
        DONE_FRAME,
    ]
    state, downstream, scan, lease, session = await _exercise_stream(
        monkeypatch,
        "stream_zero_rules",
        raw_chunks,
        rules=(),
    )

    assert scan.scan_status == "not_configured"
    assert downstream == raw_chunks
    assert state.terminals == [None]
    assert state.finishes[0]["status"] == "completed"
    logged_context = state.request_logs[0][0]
    assert logged_context.output_safety_scan_id == scan.output_safety_scan_id
    assert logged_context.output_safety_scan_status == "not_configured"
    assert logged_context.output_safety_decision == "none"
    assert session.chunk_count == len(raw_chunks)
    assert state.webhook_events == []
    assert lease.release_calls == 1
    _assert_no_plaintext(state)


@pytest.mark.anyio
async def test_configured_stream_handshake_failure_finishes_scan_not_applicable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 configured scan 在 Provider 握手失败时与 starting Session 一起收口。"""

    context = _context("stream_handshake_failure")
    rule = _rule(BLOCK_MARKER, action="block", suffix=context.request_id)
    scan = _scan(context, (rule,), stream_session_id=_session_id(context))
    state = _ObservedState()
    _install_stream_spies(monkeypatch, state, context, scan)
    selection = object()
    provider_request = _provider_request(context, stream=True, selection=selection)

    with pytest.raises(ApiError) as raised:
        await openai_api._stream_response(
            _config(),
            context,
            _FailingStreamAdapter(ApiError("provider_key.unavailable")),
            provider_request,
            time.perf_counter(),
            None,
            client_request=_RequestStub(),
            enable_output_safety=True,
        )

    assert raised.value.code == "provider_key.unavailable"
    attached = raised.value.request_log_context
    assert attached.tenant_id == TENANT_ID
    assert attached.request_id == context.request_id
    assert attached.trace_id == context.trace_id
    assert attached.output_safety_scan_id == scan.output_safety_scan_id
    assert attached.output_safety_scan_status == "not_applicable"
    assert attached.output_safety_decision == "none"
    terminal = state.terminals[0]
    assert terminal is not None
    assert (terminal.scan_status, terminal.decision) == ("not_applicable", "none")
    assert terminal.observed_output_token_count == 0
    assert terminal.emitted_token_count == 0
    assert state.finishes == [
        {
            "status": "upstream_failed",
            "error_code": "provider_key.unavailable",
            "quality_observation_eligible": False,
        }
    ]
    assert state.selections == [{"selection": selection, "succeeded": False}]
    assert state.usages == []
    assert state.webhook_events == []
    _assert_no_plaintext(state)
