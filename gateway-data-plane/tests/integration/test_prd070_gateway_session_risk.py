"""PRD-070 Gateway 会话风险主编排专项测试。

本模块只验证 DLP/Intent/SessionRisk/Rate/Budget/Provider 的相对顺序与错误优先级；Store
原子性和 SQLite pending/ACK 已由独立单元测试覆盖。合成正文和 HMAC 均为测试常量，
不会连接外部 Provider、Redis 或 Control API。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import metahub_gateway.api.openai_compatible as openai_api
import pytest
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.dlp import DlpBlockedError
from metahub_gateway.dlp.models import DlpDecision
from metahub_gateway.intent_security import IntentBlockedError
from metahub_gateway.intent_security.models import IntentRiskDecision
from metahub_gateway.policies.cache import PolicySnapshotLeaseDecision
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.session_risk import (
    SessionRiskPreflightResult,
    SessionRiskRequestIdentity,
    SessionRiskSnapshot,
    SessionRiskTurnResult,
)
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.intent_security import (
    DETECTOR_VERSION,
    RISK_SCORE_SCALE,
    RULESET_VERSION,
    IntentClassification,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.session_risk import (
    SESSION_RISK_THRESHOLD_VERSION,
    SessionIntentClassification,
    SessionRiskHmacKey,
    SessionRiskScope,
)
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse


class _RequestStub:
    """提供主编排所需的最小可信 Request 表面。"""

    def __init__(self, suffix: str) -> None:
        """创建带服务端 request/trace ID 且不含客户端身份 Header 的请求替身。"""

        self.headers = Headers()
        self.state = SimpleNamespace(
            request_id=f"req_prd070_{suffix}",
            trace_id=f"trc_prd070_{suffix}",
        )
        self.app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace()))


def _auth() -> GatewayAuthContext:
    """返回只含服务端可信 owner 的合成 Gateway 鉴权上下文。"""

    # 固定 owner 模拟完成鉴权后的上下文，Chat payload 不参与租户或 Virtual Key 选择。
    return GatewayAuthContext(
        tenant_id="tenant_prd070_gateway",
        virtual_key_id="vk_prd070_gateway",
        key_prefix="mhv_prd070",
        environment="dev",
        environment_id="env_prd070_gateway",
    )


def _route() -> GatewayRoute:
    """返回不会真实连接 Provider 的合成 Chat 路由。"""

    return GatewayRoute(
        tenant_id="tenant_prd070_gateway",
        model_id="model_prd070_gateway",
        model_alias="company-default",
        upstream_model_name="upstream-model",
        capabilities={"chat": True},
        provider_id="provider_prd070_gateway",
        provider_code="local_openai",
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="Local Test",
        provider_base_url="http://127.0.0.1.invalid",
        requested_model="company-default",
        environment="dev",
        environment_id="env_prd070_gateway",
    )


def _identity() -> SessionRiskRequestIdentity:
    """构造由企业 Chat grant 派生的可信 session/turn 身份。"""

    chat_session_id = "chat_session_prd070_gateway"
    # grant 派生的 Chat scope 与 turn 摘要均固定，公开 Header 不能覆盖这些可信字段。
    return SessionRiskRequestIdentity(
        scope=SessionRiskScope(
            tenant_id="tenant_prd070_gateway",
            virtual_key_id="vk_prd070_gateway",
            scope_type="enterprise_chat",
            scope_key=chat_session_id,
            owner_slot="srh_" + "a" * 64,
            hmac_key_version=1,
            chat_session_id=chat_session_id,
        ),
        turn_key_digest="srh_" + "b" * 64,
        hmac_key=SessionRiskHmacKey(version=1, secret="k" * 32),
        chat_message_id="chm_" + "f" * 32,
    )


def _payload(text: str, *, stream: bool = False) -> dict[str, object]:
    """构造只含一条合成 user turn 的最小 Chat payload，不携带 tenant 或会话 owner。"""

    return {
        "model": "company-default",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
    }


def _trusted(
    payload: dict[str, object],
    candidates: tuple[SessionRiskRequestIdentity, ...] = (),
) -> openai_api.TrustedGatewayInvocation:
    """构造测试专用的可信请求上下文，用于固定会话风险校验的输入边界。

    Build the complete Enterprise Chat trusted-invocation boundary.
    """

    return openai_api.TrustedGatewayInvocation(
        source="enterprise_chat",
        auth_context=_auth(),
        payload=payload,
        session_risk_candidates=candidates,
    )


def _clean_intent() -> IntentClassification:
    """返回符合 Shared 合同的零风险意图摘要。"""

    return IntentClassification(
        risk_type="none",
        risk_score=0,
        risk_score_scale=RISK_SCORE_SCALE,
        action="allow",
        reason_code="no_risk_detected",
        detector_version=DETECTOR_VERSION,
        ruleset_version=RULESET_VERSION,
        input_code_points=12,
    )


def _observed_intent() -> IntentClassification:
    """返回只观察、不触发同步 strong block 的 current-turn 风险摘要。"""

    return IntentClassification(
        risk_type="jailbreak",
        risk_score=8_200,
        risk_score_scale=RISK_SCORE_SCALE,
        action="observe",
        reason_code="jailbreak_instruction_override",
        detector_version=DETECTOR_VERSION,
        ruleset_version=RULESET_VERSION,
        input_code_points=64,
    )


def _frozen_snapshot() -> SessionRiskSnapshot:
    """返回第三个风险 turn 已提交后的冻结快照。"""

    # 固定 revision 与三次风险计数，确保主链路只能在证据提交后观察到冻结状态。
    return SessionRiskSnapshot(
        score_id="srs_" + "c" * 32,
        score=10_000,
        risky_turn_count=3,
        jailbreak_count=3,
        sensitive_count=0,
        tool_inducement_count=0,
        state="frozen",
        revision=3,
        window_started_at_epoch=1_786_000_000,
        last_risk_at_epoch=1_786_000_030,
        expires_at_epoch=None,
        hmac_key_version=1,
        threshold_version=SESSION_RISK_THRESHOLD_VERSION,
    )


def _monitoring_snapshot() -> SessionRiskSnapshot:
    """返回首个风险 turn 已提交、仍允许进入 Provider 的活动快照。"""

    # 首轮风险保持 monitoring，用来证明未达阈值时 Provider 主链路不会被提前截断。
    return SessionRiskSnapshot(
        score_id="srs_" + "9" * 32,
        score=2_600,
        risky_turn_count=1,
        jailbreak_count=1,
        sensitive_count=0,
        tool_inducement_count=0,
        state="monitoring",
        revision=1,
        window_started_at_epoch=1_786_000_000,
        last_risk_at_epoch=1_786_000_000,
        expires_at_epoch=1_786_001_800,
        hmac_key_version=1,
        threshold_version=SESSION_RISK_THRESHOLD_VERSION,
    )


def _install_baseline(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
    logged: list[tuple[RequestLogContext, str]],
    preflight: SessionRiskPreflightResult,
) -> None:
    """隔离数据库、网络和后续治理阶段，只保留本组测试关注的顺序事实。"""

    async def acquire_error_map(_request: object, _tenant_id: str) -> None:
        """模拟取得同租户错误映射租约。"""

        return None

    async def acquire_policy(
        _request: object,
        _tenant_id: str,
    ) -> PolicySnapshotLeaseDecision:
        """返回可执行且禁用动态同步的固定 Policy lease。"""

        return PolicySnapshotLeaseDecision(
            executable=True,
            snapshot=None,
            snapshot_version=None,
            status="disabled",
            error_code=None,
            acquired_monotonic=0.0,
        )

    async def preflight_risk(*_args: object, **_kwargs: object) -> SessionRiskPreflightResult:
        """返回测试指定的读取正文前门禁结果。"""

        order.append("preflight")
        return preflight

    async def forbidden_rate(*_args: object, **_kwargs: object) -> None:
        """冻结或更早安全阻断后调用 Rate 即视为测试失败。"""

        # 安全阻断必须早于限流执行，任何调用都说明阶段顺序发生了高风险回归。
        raise AssertionError("rate must not run after a security block")

    def forbidden_late(*_args: object, **_kwargs: object) -> None:
        """冻结或更早安全阻断后调用 Budget/Provider 即视为测试失败。"""

        # 冻结请求绝不能触达预算或上游阶段，否则既会产生费用也可能泄露敏感正文。
        raise AssertionError("late gateway stage must not run after a security block")

    route = _route()
    monkeypatch.setattr(openai_api, "_acquire_provider_error_map_lease", acquire_error_map)
    monkeypatch.setattr(openai_api, "_acquire_policy_snapshot_lease", acquire_policy)
    monkeypatch.setattr(openai_api, "_ensure_environment_active", lambda _auth: None)
    monkeypatch.setattr(
        openai_api,
        "claim_gateway_request",
        lambda _config, _context: order.append("claim"),
    )
    monkeypatch.setattr(openai_api, "preflight_session_risk", preflight_risk)
    monkeypatch.setattr(
        openai_api,
        "validate_requested_model_authority",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        openai_api,
        "evaluate_policy_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(decision="allow", target_model_id=None),
    )
    monkeypatch.setattr(
        openai_api,
        "apply_policy_execution_result",
        lambda context, _result: context,
    )
    monkeypatch.setattr(openai_api, "policy_execution_error", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        openai_api,
        "apply_policy_mask_payload",
        lambda _config, context, _result, payload, **_kwargs: (context, payload),
    )
    monkeypatch.setattr(
        openai_api,
        "resolve_model_route_plan",
        lambda *_args, **_kwargs: GatewayRoutePlan(
            initial_route=route,
            initial_role="primary",
        ),
    )
    monkeypatch.setattr(openai_api, "_validate_route_capability", lambda *_args: None)
    monkeypatch.setattr(openai_api, "enforce_virtual_key_rate_limit", forbidden_rate)
    monkeypatch.setattr(openai_api, "enforce_budget_guard", forbidden_late)
    monkeypatch.setattr(openai_api, "adapter_for_route", forbidden_late)
    monkeypatch.setattr(
        openai_api,
        "safe_record_request_log",
        lambda _config, context, **kwargs: logged.append((context, kwargs["error_code"]))
        or order.append("log"),
    )


@pytest.mark.anyio
async def test_dlp_block_uses_zero_intent_calls_and_preserves_error_on_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：DLP block 只提交当前轮 DLP；Store 503 不得覆盖原 DLP 错误。"""

    order: list[str] = []
    logged: list[tuple[RequestLogContext, str]] = []
    identity = _identity()
    _install_baseline(
        monkeypatch,
        order,
        logged,
        SessionRiskPreflightResult(status="clean", identity=identity),
    )

    def block_dlp(*_args: object, **_kwargs: object) -> None:
        """返回只含固定摘要的当前轮 DLP 阻断。"""

        order.append("dlp")
        # 注入不含命中文本的固定阻断，验证会话累计只消费 DLP 安全摘要。
        raise DlpBlockedError(
            DlpDecision(
                status="blocked",
                rule_id="dlp_prd070_gateway",
                action="block",
                match_count=1,
                reason_code="dlp.keyword_matched",
                current_turn_match_count=1,
                current_turn_action="block",
            )
        )

    def forbidden_intent(*_args: object, **_kwargs: object) -> None:
        """DLP 阻断后任何意图分类调用都立即失败。"""

        # DLP 已决定拒绝后再分类会扩大正文处理面，因此以硬断言保护零调用合同。
        raise AssertionError("intent classifier must not run after DLP block")

    async def fail_store(
        _config: object,
        _state: object,
        _identity: object,
        assessment: object,
    ) -> None:
        """验证 assessment 只含本轮 DLP 信号后注入固定 Store 故障。"""

        order.append("session")
        assert assessment.dlp_action == "block"
        assert assessment.dlp_current_turn_match_count == 1
        assert assessment.intent_risk_type == "none"
        assert assessment.tool_inducement_detected is False
        # 摘要检查通过后注入固定依赖故障，确认公开响应仍优先保留 DLP 错误。
        raise ApiError("gateway.session_risk_unavailable")

    monkeypatch.setattr(openai_api, "enforce_dlp_guard", block_dlp)
    monkeypatch.setattr(openai_api, "enforce_session_intent_guard", forbidden_intent)
    monkeypatch.setattr(openai_api, "record_session_risk_turn", fail_store)

    with pytest.raises(DlpBlockedError) as blocked:
        await openai_api._handle_gateway_request(
            _RequestStub("dlp"),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted(_payload("synthetic dlp value"), (identity,)),
        )
    assert blocked.value.code == "gateway.dlp_blocked"
    assert order == ["claim", "preflight", "dlp", "session", "log"]
    context, error_code = logged[0]
    assert error_code == "gateway.dlp_blocked"
    assert context.session_risk_status == "failed"
    assert context.session_risk_reason_code == "store_unavailable"


@pytest.mark.anyio
async def test_trusted_session_classifies_once_and_freezes_before_all_late_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主流程：可信 session 仅同步分类一次，第三个风险 turn 在 SSE/Rate/Budget/Provider 前冻结。"""

    order: list[str] = []
    logged: list[tuple[RequestLogContext, str]] = []
    identity = _identity()
    classification = SessionIntentClassification(
        strong_guard=_clean_intent(),
        current_turn=_observed_intent(),
        input_truncated=False,
    )
    _install_baseline(
        monkeypatch,
        order,
        logged,
        SessionRiskPreflightResult(status="clean", identity=identity),
    )
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        # 未配置 DLP 规则是常见主流程；会话引擎必须把空决策映射为 allow/零命中。
        lambda *_args, **_kwargs: order.append("dlp") or None,
    )

    def classify_once(
        _config: object,
        _state: object,
        context: RequestLogContext,
        _payload_value: object,
    ) -> tuple[RequestLogContext, SessionIntentClassification]:
        """模拟一次同步会话分类完成，并返回仅含当前轮固定字段的风险摘要。"""

        order.append("intent")
        return (
            replace(
                context,
                intent_risk_result_id="irr_" + "d" * 32,
                intent_risk_schedule_status="completed",
                intent_risk_mode="sync_session",
                intent_risk_type="jailbreak",
                intent_risk_score=8_200,
                intent_risk_action="observe",
                intent_risk_reason_code="jailbreak_instruction_override",
                intent_risk_detector_version=DETECTOR_VERSION,
            ),
            classification,
        )

    async def freeze_turn(
        _config: object,
        _state: object,
        _identity: object,
        assessment: object,
    ) -> SessionRiskTurnResult:
        """验证一次组合 assessment 后返回已提交的冻结快照。"""

        order.append("session")
        assert assessment.intent_risk_type == "jailbreak"
        assert assessment.turn_delta > 0
        assert assessment.dlp_action == "allow"
        assert assessment.dlp_current_turn_match_count == 0
        assert assessment.source_request_id == "req_prd070_freeze"
        # 第三轮提交直接返回冻结快照，验证网关在后续限流与上游调用前短路。
        return SessionRiskTurnResult(
            status="frozen",
            snapshot=_frozen_snapshot(),
            freeze_transition=True,
        )

    monkeypatch.setattr(openai_api, "enforce_session_intent_guard", classify_once)
    monkeypatch.setattr(openai_api, "record_session_risk_turn", freeze_turn)

    with pytest.raises(ApiError) as frozen:
        await openai_api._handle_gateway_request(
            _RequestStub("freeze"),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted(
                _payload("synthetic observed turn", stream=True), (identity,)
            ),
        )
    assert frozen.value.code == "gateway.session_risk_frozen"
    assert order == ["claim", "preflight", "dlp", "intent", "session", "log"]
    context, error_code = logged[0]
    assert error_code == "gateway.session_risk_frozen"
    assert context.session_risk_status == "frozen"
    assert context.session_risk_score_id == "srs_" + "c" * 32
    assert context.session_risk_reason_code == "session_frozen"


@pytest.mark.anyio
async def test_chat_without_session_keeps_prd069_path_and_records_disabled_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼容流程：无成对标识不累计 score，继续原 Intent 路径并记录 no_session_identifiers。"""

    order: list[str] = []
    logged: list[tuple[RequestLogContext, str]] = []
    _install_baseline(
        monkeypatch,
        order,
        logged,
        SessionRiskPreflightResult(status="not_applicable", identity=None),
    )
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        lambda *_args, **_kwargs: order.append("dlp")
        or DlpDecision(
            status="passed",
            rule_id=None,
            action="allow",
            match_count=0,
            reason_code=None,
        ),
    )
    decision = IntentRiskDecision(
        intent_risk_result_id="irr_" + "e" * 32,
        schedule_status="blocked",
        classification_mode="sync_strong",
        risk_type="jailbreak",
        risk_score=9_800,
        action="block",
        reason_code="jailbreak_instruction_override",
        detector_version=DETECTOR_VERSION,
        input_truncated=False,
    )

    def block_original_intent(*_args: object, **_kwargs: object) -> None:
        """证明无 session 时仍调用 PRD-069 原闸门，而非 sync_session。"""

        order.append("intent")
        # 缺少可信会话标识时必须沿用原有同步强阻断，禁止创建可串扰的匿名会话状态。
        raise IntentBlockedError("gateway.intent_blocked", decision)

    monkeypatch.setattr(openai_api, "enforce_intent_guard", block_original_intent)
    monkeypatch.setattr(
        openai_api,
        "enforce_session_intent_guard",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sync_session must not run without trusted identifiers")
        ),
    )
    monkeypatch.setattr(
        openai_api,
        "record_session_risk_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("session score must not be created without trusted identifiers")
        ),
    )

    with pytest.raises(IntentBlockedError):
        await openai_api._handle_gateway_request(
            _RequestStub("no_session"),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted(_payload("synthetic strong intent")),
        )
    assert order == ["claim", "preflight", "dlp", "intent", "log"]
    context, error_code = logged[0]
    assert error_code == "gateway.intent_blocked"
    assert context.session_risk_status == "disabled"
    assert context.session_risk_reason_code == "no_session_identifiers"


@pytest.mark.anyio
async def test_trusted_session_scores_once_before_successful_sse_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流式主流程：一次同步分类和一次累计均先于 Rate/Budget/Adapter/SSE 握手。"""

    order: list[str] = []
    logged: list[tuple[RequestLogContext, str]] = []
    identity = _identity()
    classification = SessionIntentClassification(
        strong_guard=_clean_intent(),
        current_turn=_observed_intent(),
        input_truncated=False,
    )
    _install_baseline(
        monkeypatch,
        order,
        logged,
        SessionRiskPreflightResult(status="clean", identity=identity),
    )
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        lambda *_args, **_kwargs: order.append("dlp")
        or DlpDecision(
            status="passed",
            rule_id=None,
            action="allow",
            match_count=0,
            reason_code=None,
        ),
    )

    def classify_once(
        _config: object,
        _state: object,
        context: RequestLogContext,
        _payload_value: object,
    ) -> tuple[RequestLogContext, SessionIntentClassification]:
        """返回一次 sync_session 摘要，重复调用会使顺序断言失败。"""

        order.append("intent")
        return context, classification

    async def record_once(
        _config: object,
        _state: object,
        _identity: object,
        assessment: object,
    ) -> SessionRiskTurnResult:
        """返回未达阈值的已持久快照，并核对当前 turn 只形成一个 assessment。"""

        order.append("session")
        assert assessment.turn_delta == 2_600
        # 未达阈值时返回 monitoring，后续顺序断言应继续观察 Rate、Budget 与 Provider。
        return SessionRiskTurnResult(
            status="monitoring",
            snapshot=_monitoring_snapshot(),
        )

    async def rate_limit(*_args: object, **_kwargs: object) -> None:
        """记录 Rate 位于会话风险提交之后。"""

        order.append("rate")

    def budget(*_args: object, **_kwargs: object) -> None:
        """记录 Budget 位于会话风险提交和 Rate 之后。"""

        order.append("budget")

    adapter = object()

    def resolve_adapter(_route_value: object) -> object:
        """返回不连接网络的适配器哨兵。"""

        order.append("adapter")
        return adapter

    async def stream_response(*args: object, **_kwargs: object) -> StreamingResponse:
        """验证握手拿到已提交的 monitoring 上下文，且不再次执行分类或计分。"""

        order.append("stream")
        context = args[1]
        assert isinstance(context, RequestLogContext)
        assert context.session_risk_status == "monitoring"
        assert context.session_risk_score_id == "srs_" + "9" * 32
        return StreamingResponse(iter((b"data: [DONE]\n\n",)), media_type="text/event-stream")

    monkeypatch.setattr(openai_api, "enforce_session_intent_guard", classify_once)
    monkeypatch.setattr(openai_api, "record_session_risk_turn", record_once)
    monkeypatch.setattr(openai_api, "enforce_virtual_key_rate_limit", rate_limit)
    monkeypatch.setattr(openai_api, "enforce_budget_guard", budget)
    monkeypatch.setattr(openai_api, "adapter_for_route", resolve_adapter)
    monkeypatch.setattr(openai_api, "_stream_response", stream_response)
    monkeypatch.setattr(openai_api, "_log_request_received", lambda *_args: None)

    response = await openai_api._handle_gateway_request(
        _RequestStub("stream"),
        endpoint="chat/completions",
        request_kind="chat",
        trusted_invocation=_trusted(
            _payload("synthetic observed stream turn", stream=True), (identity,)
        ),
    )

    assert isinstance(response, StreamingResponse)
    assert order == [
        "claim",
        "preflight",
        "dlp",
        "intent",
        "session",
        "rate",
        "budget",
        "adapter",
        "stream",
    ]
    assert logged == []


@pytest.mark.anyio
async def test_intent_block_preserves_error_when_session_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常优先级：Intent strong block 已成立时，累计故障只写日志摘要而不覆盖原错误。"""

    order: list[str] = []
    logged: list[tuple[RequestLogContext, str]] = []
    identity = _identity()
    classification = SessionIntentClassification(
        strong_guard=replace(_observed_intent(), risk_score=9_800, action="block"),
        current_turn=_observed_intent(),
        input_truncated=False,
    )
    decision = IntentRiskDecision(
        intent_risk_result_id="irr_" + "7" * 32,
        schedule_status="blocked",
        classification_mode="sync_session",
        risk_type="jailbreak",
        risk_score=9_800,
        action="block",
        reason_code="jailbreak_instruction_override",
        detector_version=DETECTOR_VERSION,
        input_truncated=False,
    )
    _install_baseline(
        monkeypatch,
        order,
        logged,
        SessionRiskPreflightResult(status="clean", identity=identity),
    )
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        lambda *_args, **_kwargs: order.append("dlp")
        or DlpDecision(
            status="passed",
            rule_id=None,
            action="allow",
            match_count=0,
            reason_code=None,
        ),
    )

    def block_intent(*_args: object, **_kwargs: object) -> None:
        """模拟 strong guard 已持久化并携带 current-turn 安全摘要。"""

        order.append("intent")
        # 原始强阻断必须保持最高公开错误优先级，同时允许下游累计可信当前轮摘要。
        raise IntentBlockedError(
            "gateway.intent_blocked",
            decision,
            session_classification=classification,
        )

    async def fail_store(*_args: object, **_kwargs: object) -> None:
        """模拟 Intent 阻断后的会话累计依赖故障。"""

        order.append("session")
        # 累计故障不得覆盖已经成立的意图阻断，测试通过固定错误验证优先级合同。
        raise ApiError("gateway.session_risk_unavailable")

    monkeypatch.setattr(openai_api, "enforce_session_intent_guard", block_intent)
    monkeypatch.setattr(openai_api, "record_session_risk_turn", fail_store)

    with pytest.raises(IntentBlockedError) as blocked:
        await openai_api._handle_gateway_request(
            _RequestStub("intent"),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted(_payload("synthetic strong intent"), (identity,)),
        )

    assert blocked.value.code == "gateway.intent_blocked"
    assert order == ["claim", "preflight", "dlp", "intent", "session", "log"]
    context, error_code = logged[0]
    assert error_code == "gateway.intent_blocked"
    assert context.session_risk_status == "failed"
    assert context.session_risk_reason_code == "store_unavailable"
