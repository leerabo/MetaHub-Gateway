"""PRD-069 Gateway 敏感意图闸门、旁路队列与 Usage spool v3 聚焦测试。

测试使用隔离 SQLite、合成安全语句和随机领域 ID，不连接 Provider/Control，不保存真实
Prompt、Key 或 Token。所有数据库断言均以 tenant_id 与 request_id 作为首要 owner 条件。
"""

from __future__ import annotations

import asyncio
import gc
import threading
import weakref
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from time import perf_counter_ns
from types import SimpleNamespace

import metahub_gateway.api.openai_compatible as openai_api
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.dlp import DlpBlockedError, extract_user_texts
from metahub_gateway.dlp.models import DlpDecision
from metahub_gateway.events import enqueue_runtime_event
from metahub_gateway.intent_security import (
    IntentBlockedError,
    IntentClassificationJob,
    IntentClassifierUnavailableError,
    IntentResultStoreUnavailableError,
    apply_intent_decision,
    enforce_intent_guard,
    install_intent_security_dispatcher,
    process_intent_job,
)
from metahub_gateway.intent_security import dispatcher as intent_dispatcher
from metahub_gateway.intent_security import service as intent_service
from metahub_gateway.policies.cache import PolicySnapshotLeaseDecision
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.session_risk import SessionRiskPreflightResult
from metahub_gateway.usage.ledger import (
    RequestLogContext,
    UsageLedgerIntent,
    _deserialize_usage_spool_record,
    _ensure_replay_request_log,
    _serialize_usage_spool_record,
    claim_gateway_request,
    record_request_log,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.intent_security import classify_intent, classify_strong_intent
from metahub_shared.runtime.errors import ApiError
from starlette.datastructures import Headers


@pytest.fixture(scope="module")
def intent_config(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """创建完成最新迁移的隔离租户数据库，并读取可信 Virtual Key owner。"""

    root = tmp_path_factory.mktemp("prd069-gateway-intent")
    database_url = f"sqlite:///{(root / 'intent.db').as_posix()}"
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT virtual_key_id
            FROM virtual_keys
            WHERE tenant_id=? AND status='active'
            ORDER BY virtual_key_id
            LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert row is not None
        virtual_key_id = str(row["virtual_key_id"])
    finally:
        connection.close()
    return SimpleNamespace(
        database_url=database_url,
        tenant_id=seed.tenant_id,
        virtual_key_id=virtual_key_id,
    )


def _claimed_context(config: SimpleNamespace, suffix: str) -> RequestLogContext:
    """创建并 claim 一次 tenant-qualified Gateway 请求，供结果 trigger 验证 owner。"""

    context = RequestLogContext(
        tenant_id=config.tenant_id,
        request_id=f"req_prd069_{suffix}",
        trace_id=f"trc_prd069_{suffix}",
        virtual_key_id=config.virtual_key_id,
        endpoint="/v1/chat/completions",
        request_model="company-default",
    )
    claim_gateway_request(config, context)
    return context


def test_legacy_claim_without_complete_scope_skips_experiment_scope_owner(
    intent_config: SimpleNamespace,
) -> None:
    """旧普通调用缺 Project/Environment 时仍 claim，但绝不能获得实验 scope owner。"""

    context = _claimed_context(intent_config, "legacy_scope_compat")
    connection = connect_database(intent_config.database_url)
    try:
        claim = connection.execute(
            """
            SELECT 1 FROM gateway_request_claims
            WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, context.request_id),
        ).fetchone()
        scope = connection.execute(
            """
            SELECT 1 FROM gateway_request_scope_claims
            WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, context.request_id),
        ).fetchone()
    finally:
        connection.close()
    assert claim is not None
    assert scope is None


def _chat(text: str) -> dict[str, object]:
    """构造只含单条合成 user 文本的最小 Chat payload。"""

    return {
        "model": "company-default",
        "messages": [{"role": "user", "content": text}],
    }


def _result_row(config: SimpleNamespace, request_id: str):
    """按 tenant 与 source request 查询唯一意图结果，禁止全局 ID 查找。"""

    connection = connect_database(config.database_url)
    try:
        return connection.execute(
            """
            SELECT * FROM intent_risk_results
            WHERE tenant_id=? AND source_kind='gateway' AND source_request_id=?
            """,
            (config.tenant_id, request_id),
        ).fetchone()
    finally:
        connection.close()


def test_public_extractor_preserves_dlp_boundary_and_many_messages() -> None:
    """边界流程：DLP 与 intent 共用 user/content-part/20k 抽取，65+ 消息不被额外截断。"""

    many_messages = [
        {"role": "user", "content": [{"type": "text", "text": "a"}]}
        for _ in range(70)
    ]
    many_messages.insert(1, {"role": "assistant", "content": "ignored"})
    texts, truncated = extract_user_texts({"messages": many_messages})
    assert len(texts) == 70
    assert truncated is False
    assert all(text == "a" for _, text in texts)

    bounded, truncated = extract_user_texts(
        {"messages": [{"role": "user", "content": "x" * 20_001}]}
    )
    assert sum(len(text) for _, text in bounded) == 20_000
    assert truncated is True


def test_20k_classifier_performance_percentiles_stay_within_budget(
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    """性能门禁：三类 20k code points 输入在热身后的 P95/P99 均保持线性低延迟。"""

    samples = {
        "chinese": ("企业治理安全审计" * 2_500)[:20_000],
        "english": ("enterprisegovernanceaudit" * 1_000)[:20_000],
        "separators": ("-_./\\|:：,，;；'\"`~!！?？ \t\n" * 1_000)[:20_000],
    }
    classifiers = {
        "strong": classify_strong_intent,
        "light": classify_intent,
    }
    violations: list[str] = []
    for sample_name, sample in samples.items():
        assert len(sample) == 20_000
        for classifier_name, classifier in classifiers.items():
            # 每组先独立热身，避免正则、Unicode 数据页或 CPU 冷启动污染分位数。
            for _ in range(10):
                classifier((sample,))
            durations_ms: list[float] = []
            for _ in range(100):
                started = perf_counter_ns()
                classifier((sample,))
                durations_ms.append((perf_counter_ns() - started) / 1_000_000)
            ordered = sorted(durations_ms)
            # 固定 100 个样本时采用 nearest-rank：P95/P99 分别是第 95/99 个观测值。
            p95_ms = ordered[94]
            p99_ms = ordered[98]
            metric_prefix = f"{classifier_name}_{sample_name}"
            record_testsuite_property(f"{metric_prefix}_p95_ms", f"{p95_ms:.6f}")
            record_testsuite_property(f"{metric_prefix}_p99_ms", f"{p99_ms:.6f}")
            if p95_ms > 10.0:
                violations.append(f"{metric_prefix} P95={p95_ms:.3f}ms")
            if p99_ms > 25.0:
                violations.append(f"{metric_prefix} P99={p99_ms:.3f}ms")
    assert violations == [], "; ".join(violations)


def test_queue_unavailable_and_full_are_fail_open_without_running_light_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：worker 缺失/队列满只标记 skipped，完整分类器不在请求线程执行。"""

    calls: list[tuple[str, ...]] = []

    def forbidden_light_classifier(texts: tuple[str, ...]):
        """若请求线程错误等待完整分类则立即留下可见调用事实。"""

        calls.append(texts)
        # 该分支一旦执行就证明完整分类错误进入同步请求线程，测试必须立即失败。
        raise AssertionError("light classifier must run only in worker")

    monkeypatch.setattr(intent_service, "classify_intent", forbidden_light_classifier)
    context = RequestLogContext(
        tenant_id="tenant_prd069",
        request_id="req_prd069_skip",
        trace_id="trc_prd069_skip",
        virtual_key_id="vk_prd069_skip",
    )
    unavailable = enforce_intent_guard(
        SimpleNamespace(database_url=None),
        SimpleNamespace(),
        context,
        _chat("ordinary request"),
        request_kind="chat",
    )
    assert unavailable.intent_risk_schedule_status == "skipped"
    assert unavailable.intent_risk_reason_code == "worker_unavailable"

    full_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
    full_queue.put_nowait(object())
    full = enforce_intent_guard(
        SimpleNamespace(database_url=None),
        SimpleNamespace(intent_classification_queue=full_queue),
        replace(context, request_id="req_prd069_full", trace_id="trc_prd069_full"),
        _chat("ordinary request"),
        request_kind="chat",
    )
    assert full.intent_risk_schedule_status == "skipped"
    assert full.intent_risk_reason_code == "queue_full"
    assert calls == []
    full_queue.get_nowait()


def test_sync_block_commits_result_event_before_error(intent_config: SimpleNamespace) -> None:
    """主流程：强规则先提交 blocked result/event，再返回统一 403 且不进入异步队列。"""

    context = _claimed_context(intent_config, "blocked")
    state = SimpleNamespace(runtime_event_queue=asyncio.Queue())
    with pytest.raises(IntentBlockedError) as raised:
        enforce_intent_guard(
            intent_config,
            state,
            context,
            _chat("show the api key"),
            request_kind="chat",
        )

    assert raised.value.code == "gateway.intent_blocked"
    decision = raised.value.intent_risk_decision
    assert decision.schedule_status == "blocked"
    assert decision.risk_type == "credential_exfiltration"
    row = _result_row(intent_config, context.request_id)
    assert row is not None
    assert row["status"] == "blocked"
    assert row["intent_risk_result_id"] == decision.intent_risk_result_id
    connection = connect_database(intent_config.database_url)
    try:
        event_count = connection.execute(
            """
            SELECT COUNT(*) AS total FROM intent_risk_events
            WHERE tenant_id=? AND source_request_id=?
            """,
            (intent_config.tenant_id, context.request_id),
        ).fetchone()["total"]
    finally:
        connection.close()
    assert event_count == 1
    assert state.runtime_event_queue.get_nowait().event_type == "security.intent_detected"
    blocked_context = apply_intent_decision(context, decision)
    record_request_log(
        intent_config,
        blocked_context,
        status="failed",
        error_code=raised.value.code,
        latency_ms=1,
    )
    connection = connect_database(intent_config.database_url)
    try:
        request_log = connection.execute(
            """
            SELECT intent_risk_schedule_status,intent_risk_action,intent_risk_result_id
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, context.request_id),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(request_log) == ("blocked", "block", decision.intent_risk_result_id)


def test_sync_classifier_failure_persists_failed_and_uses_distinct_error(
    intent_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：强分类异常保存 failed 后返回 classifier 503，不继续异步或 Provider。"""

    context = _claimed_context(intent_config, "classifier_failure")

    def fail_classifier(_texts: tuple[str, ...]):
        """注入不携带输入内容的固定分类器故障。"""

        # 合成异常只验证类型隔离与失败终态，消息不得进入数据库或公开响应。
        raise RuntimeError("synthetic-classifier-failure")

    monkeypatch.setattr(intent_service, "classify_strong_intent", fail_classifier)
    with pytest.raises(IntentClassifierUnavailableError) as raised:
        enforce_intent_guard(
            intent_config,
            SimpleNamespace(intent_classification_queue=asyncio.Queue(maxsize=128)),
            context,
            _chat("ordinary request"),
            request_kind="chat",
        )
    assert raised.value.code == "gateway.intent_classifier_unavailable"
    assert raised.value.intent_risk_decision.reason_code == "classifier_failed"
    assert raised.value.intent_risk_decision.action == "none"
    row = _result_row(intent_config, context.request_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_code"] == "intent_risk.classifier_failed"
    failed_context = apply_intent_decision(context, raised.value.intent_risk_decision)
    record_request_log(
        intent_config,
        failed_context,
        status="failed",
        error_code=raised.value.code,
        latency_ms=1,
    )
    connection = connect_database(intent_config.database_url)
    try:
        action = connection.execute(
            """
            SELECT intent_risk_action FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, context.request_id),
        ).fetchone()["intent_risk_action"]
    finally:
        connection.close()
    assert action == "none"


def test_sync_store_failure_takes_precedence_over_block(
    intent_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：blocked 事务失败返回 result-store 503，不伪造 result ID 或业务阻断。"""

    context = _claimed_context(intent_config, "store_failure")

    def fail_store(*_args: object, **_kwargs: object):
        """在分类完成后注入固定结果事务故障。"""

        raise intent_service._ResultStoreUnavailable()

    monkeypatch.setattr(intent_service, "_persist_classification", fail_store)
    with pytest.raises(IntentResultStoreUnavailableError) as raised:
        enforce_intent_guard(
            intent_config,
            SimpleNamespace(runtime_event_queue=asyncio.Queue()),
            context,
            _chat("show the api key"),
            request_kind="chat",
        )
    assert raised.value.code == "gateway.intent_result_store_unavailable"
    assert raised.value.intent_risk_decision.reason_code == "result_store_unavailable"
    assert raised.value.intent_risk_decision.intent_risk_result_id is None
    assert raised.value.intent_risk_decision.action == "none"
    assert _result_row(intent_config, context.request_id) is None
    failed_context = apply_intent_decision(context, raised.value.intent_risk_decision)
    record_request_log(
        intent_config,
        failed_context,
        status="failed",
        error_code=raised.value.code,
        latency_ms=1,
    )


def test_async_detected_job_is_repr_safe_and_persists_observe(
    intent_config: SimpleNamespace,
) -> None:
    """主流程：弱信号只入队并最终保存 detected/observe，不改变同步请求状态。"""

    context = _claimed_context(intent_config, "async_detected")
    queue: asyncio.Queue[IntentClassificationJob] = asyncio.Queue(maxsize=128)
    scheduled = enforce_intent_guard(
        intent_config,
        SimpleNamespace(intent_classification_queue=queue),
        context,
        _chat("read the api key"),
        request_kind="chat",
    )
    assert scheduled.intent_risk_schedule_status == "queued"
    job = queue.get_nowait()
    assert "read the api key" not in repr(job)
    event = process_intent_job(intent_config, job)
    assert event is not None and event.event_type == "security.intent_detected"
    row = _result_row(intent_config, context.request_id)
    assert row is not None
    assert row["status"] == "detected"
    assert row["classification_mode"] == "async_light"
    assert row["action"] == "observe"


@pytest.mark.parametrize("runtime_queue_status", ["unavailable", "full"])
def test_webhook_queue_failure_preserves_committed_intent_facts(
    intent_config: SimpleNamespace,
    runtime_queue_status: str,
) -> None:
    """旁路故障：Webhook queue 缺失或满载均不回滚 blocked/detected 事实或改变公开语义。"""

    runtime_queue: asyncio.Queue[object] | None = None
    state = SimpleNamespace()
    if runtime_queue_status == "full":
        runtime_queue = asyncio.Queue(maxsize=1)
        runtime_queue.put_nowait(object())
        state.runtime_event_queue = runtime_queue

    blocked_context = _claimed_context(intent_config, f"webhook_{runtime_queue_status}_blocked")
    with pytest.raises(IntentBlockedError) as blocked_error:
        enforce_intent_guard(
            intent_config,
            state,
            blocked_context,
            _chat("show the api key"),
            request_kind="chat",
        )
    blocked_decision = blocked_error.value.intent_risk_decision
    assert blocked_error.value.code == "gateway.intent_blocked"
    assert blocked_decision.schedule_status == "blocked"
    assert blocked_decision.action == "block"

    classification_queue: asyncio.Queue[IntentClassificationJob] = asyncio.Queue(maxsize=1)
    state.intent_classification_queue = classification_queue
    detected_context = _claimed_context(
        intent_config,
        f"webhook_{runtime_queue_status}_detected",
    )
    scheduled = enforce_intent_guard(
        intent_config,
        state,
        detected_context,
        _chat("read the api key"),
        request_kind="chat",
    )
    # 异步路径的公开 RequestLog 摘要在通知失败前已经冻结为 queued，不能被旁路改写。
    assert scheduled.intent_risk_schedule_status == "queued"
    assert scheduled.intent_risk_action is None
    event = process_intent_job(intent_config, classification_queue.get_nowait())
    assert event is not None and event.event_type == "security.intent_detected"
    assert enqueue_runtime_event(state, event) is False

    blocked_row = _result_row(intent_config, blocked_context.request_id)
    detected_row = _result_row(intent_config, detected_context.request_id)
    assert blocked_row is not None
    assert (blocked_row["status"], blocked_row["action"]) == ("blocked", "block")
    assert detected_row is not None
    assert (detected_row["status"], detected_row["action"]) == ("detected", "observe")
    connection = connect_database(intent_config.database_url)
    try:
        committed_events = connection.execute(
            """
            SELECT source_request_id,COUNT(*) AS total
            FROM intent_risk_events
            WHERE tenant_id=? AND source_request_id IN (?,?)
            GROUP BY source_request_id
            """,
            (
                intent_config.tenant_id,
                blocked_context.request_id,
                detected_context.request_id,
            ),
        ).fetchall()
    finally:
        connection.close()
    assert {row["source_request_id"]: row["total"] for row in committed_events} == {
        blocked_context.request_id: 1,
        detected_context.request_id: 1,
    }
    if runtime_queue is not None:
        # 满队列中的原占位项仍在，证明两次通知都没有偷偷挤掉旧事件或越界扩容。
        assert runtime_queue.qsize() == 1
        runtime_queue.get_nowait()


def test_async_classifier_failure_is_terminal_only_when_failed_row_commits(
    intent_config: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：worker 分类异常写 failed；该旁路故障不向主请求抛出异常。"""

    context = _claimed_context(intent_config, "async_failure")
    queue: asyncio.Queue[IntentClassificationJob] = asyncio.Queue(maxsize=128)
    enforce_intent_guard(
        intent_config,
        SimpleNamespace(intent_classification_queue=queue),
        context,
        _chat("ordinary request"),
        request_kind="chat",
    )
    job = queue.get_nowait()

    def fail_light(_texts: tuple[str, ...]):
        """注入不包含队列正文的完整分类器异常。"""

        # worker 异常用于验证 best-effort 失败事实，不得反压已经继续的主请求。
        raise RuntimeError("synthetic-light-failure")

    monkeypatch.setattr(intent_service, "classify_intent", fail_light)
    assert process_intent_job(intent_config, job) is None
    row = _result_row(intent_config, context.request_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["classification_mode"] == "async_light"


def test_request_log_freezes_queued_identity(intent_config: SimpleNamespace) -> None:
    """租户边界：RequestLog 可从 disabled 单调升级 queued，后续旧上下文不得覆盖或换 result。"""

    base = _claimed_context(intent_config, "request_log_freeze")
    queued = replace(
        base,
        intent_risk_result_id="irr_" + "a" * 32,
        intent_risk_schedule_status="queued",
        intent_risk_mode="async_light",
        intent_risk_reason_code="queued",
        intent_risk_detector_version="intent-rule-engine-v1",
    )
    record_request_log(intent_config, queued, status="success", error_code=None, latency_ms=1)
    # 模拟 v1/v2 spool 或旧调用点的 disabled 上下文；数据库中已冻结的新事实必须保留。
    record_request_log(intent_config, base, status="success", error_code=None, latency_ms=2)
    connection = connect_database(intent_config.database_url)
    try:
        row = connection.execute(
            """
            SELECT intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, base.request_id),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == ("irr_" + "a" * 32, "queued", "async_light")

    with pytest.raises(ApiError) as conflict:
        record_request_log(
            intent_config,
            replace(queued, intent_risk_result_id="irr_" + "b" * 32),
            status="success",
            error_code=None,
            latency_ms=3,
        )
    assert conflict.value.code == "gateway.request_log_unavailable"


def test_spool_replay_inserts_v4_security_summaries_and_legacy_defaults_cannot_overwrite(
    intent_config: SimpleNamespace,
) -> None:
    """恢复流程：v4 可补建 Intent/会话摘要，旧版安全默认值不得回退已冻结事实。"""

    base = _claimed_context(intent_config, "spool_replay_intent")
    queued = replace(
        base,
        intent_risk_result_id="irr_" + "9" * 32,
        intent_risk_schedule_status="queued",
        intent_risk_mode="async_light",
        intent_risk_reason_code="queued",
        intent_risk_detector_version="intent-rule-engine-v1",
        intent_risk_input_truncated=True,
        session_risk_status="clean",
        session_risk_scope_type="api_session",
        session_risk_reason_code="clean_turn",
    )
    connection = connect_database(intent_config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_replay_request_log(
            connection,
            queued,
            "2026-08-03T00:00:00Z",
        )
        connection.commit()
        # 历史 payload 解码后只有安全默认值；恢复必须保留刚写入的 v4 冻结摘要。
        connection.execute("BEGIN IMMEDIATE")
        _ensure_replay_request_log(
            connection,
            base,
            "2026-08-03T00:00:01Z",
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT intent_risk_result_id,intent_risk_schedule_status,
                   intent_risk_mode,intent_risk_input_truncated,
                   session_risk_status,session_risk_scope_type,session_risk_reason_code
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (intent_config.tenant_id, base.request_id),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (
        "irr_" + "9" * 32,
        "queued",
        "async_light",
        1,
        "clean",
        "api_session",
        "clean_turn",
    )


def test_usage_spool_v4_restores_session_and_legacy_versions_use_safe_defaults() -> None:
    """兼容流程：v4 精确恢复会话摘要，旧版缺失的安全事实只能映射为安全默认值。"""

    context = RequestLogContext(
        tenant_id="tenant_prd069_spool",
        request_id="req_prd069_spool",
        trace_id="trc_prd069_spool",
        virtual_key_id="vk_prd069_spool",
        intent_risk_result_id="irr_" + "c" * 32,
        intent_risk_schedule_status="queued",
        intent_risk_mode="async_light",
        intent_risk_reason_code="queued",
        intent_risk_detector_version="intent-rule-engine-v1",
        intent_risk_input_truncated=True,
        session_risk_status="clean",
        session_risk_scope_type="api_session",
        session_risk_reason_code="clean_turn",
    )
    intent = UsageLedgerIntent(
        usage_ledger_id="usg_" + "d" * 32,
        pricing_observed_at="2026-08-03T00:00:00Z",
        raw_usage_value={},
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        cache_tokens=0,
        estimated=False,
        usage_source="upstream",
        estimate_reason=None,
        provider_cost_amount=None,
        provider_cost_currency=None,
        provider_cost_cny=None,
        normalization_warnings=[],
    )
    current = _serialize_usage_spool_record(context, intent)
    assert current["schema_version"] == 4
    restored, _ = _deserialize_usage_spool_record(current)
    assert restored.intent_risk_schedule_status == "queued"
    assert restored.intent_risk_result_id == context.intent_risk_result_id
    assert restored.intent_risk_input_truncated is True
    assert restored.session_risk_status == "clean"
    assert restored.session_risk_scope_type == "api_session"
    assert restored.session_risk_reason_code == "clean_turn"

    v3 = {
        **current,
        "schema_version": 3,
        "context": {
            key: value
            for key, value in current["context"].items()
            if not key.startswith("session_risk_")
        },
    }
    restored_v3, _ = _deserialize_usage_spool_record(v3)
    assert restored_v3.intent_risk_schedule_status == "queued"
    assert restored_v3.session_risk_status == "not_applicable"
    v2 = {
        **v3,
        "schema_version": 2,
        "context": {
            key: value
            for key, value in v3["context"].items()
            if not key.startswith("intent_risk_")
        },
    }
    restored_v2, _ = _deserialize_usage_spool_record(v2)
    assert restored_v2.intent_risk_schedule_status == "disabled"
    assert restored_v2.session_risk_status == "not_applicable"
    v1 = {
        **v2,
        "schema_version": 1,
        "context": {
            key: value
            for key, value in v2["context"].items()
            if key not in {"policy_mask_applied", "policy_mask_policy_id"}
        },
    }
    restored_v1, _ = _deserialize_usage_spool_record(v1)
    assert restored_v1.intent_risk_schedule_status == "disabled"
    assert restored_v1.session_risk_status == "not_applicable"
    with pytest.raises(ValueError, match="finops_spool_payload_version_invalid"):
        _deserialize_usage_spool_record({**current, "schema_version": 7})
    with pytest.raises(ValueError, match="finops_spool_payload_invalid"):
        _deserialize_usage_spool_record(
            {**current, "context": {**current["context"], "unexpected": "field"}}
        )


@pytest.mark.anyio
async def test_lifespan_drains_intent_before_inner_event_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生命周期：退出先处理 intent job，再退出内层 Webhook dispatcher 生命周期。"""

    order: list[str] = []
    app = FastAPI()
    app.state.config = SimpleNamespace(database_url=None)

    @asynccontextmanager
    async def inner_lifespan(_app: FastAPI):
        """模拟已经安装的 runtime event dispatcher，并记录其停止顺序。"""

        order.append("event_started")
        try:
            yield
        finally:
            order.append("event_stopped")

    def process_job(_config: object, _job: IntentClassificationJob):
        """模拟已接收任务完成，避免生命周期测试访问数据库。"""

        order.append("intent_processed")
        return None

    app.router.lifespan_context = inner_lifespan
    monkeypatch.setattr(intent_dispatcher, "process_intent_job", process_job)
    install_intent_security_dispatcher(app)
    job = IntentClassificationJob(
        tenant_id="tenant_lifecycle",
        request_id="req_lifecycle",
        trace_id="trc_lifecycle",
        virtual_key_id="vk_lifecycle",
        intent_risk_result_id="irr_" + "e" * 32,
        detector_version="intent-rule-engine-v1",
        ruleset_version="intent-rules-2026-08-03-v1",
        input_truncated=False,
        enqueued_at=intent_service._utcnow(),
        texts=("private synthetic text",),
    )
    assert "private synthetic text" not in repr(job)
    async with app.router.lifespan_context(app):
        queue = app.state.intent_classification_queue
        assert queue.maxsize == 128
        queue.put_nowait(job)
    assert order == ["event_started", "intent_processed", "event_stopped"]
    assert app.state.intent_classification_queue is None


@pytest.mark.anyio
async def test_lifespan_timeout_releases_pending_jobs_and_drops_late_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭超时：立即清空 pending，断开入口，并在底层线程返回后释放当前任务且不补投事件。"""

    class WeakText(str):
        """提供可弱引用的 str 测试值，用于观察正文引用生命周期而不读取其内容。"""

    app = FastAPI()
    app.state.config = SimpleNamespace(database_url=None)
    runtime_queue: asyncio.Queue[object] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    process_started = asyncio.Event()
    process_finished = asyncio.Event()
    running_text_released = asyncio.Event()
    release_process = threading.Event()
    enqueue_attempts: list[object] = []

    @asynccontextmanager
    async def inner_lifespan(current_app: FastAPI):
        """模拟内层 Webhook 生命周期，并在退出时断开真实 runtime queue 引用。"""

        current_app.state.runtime_event_queue = runtime_queue
        try:
            yield
        finally:
            current_app.state.runtime_event_queue = None

    def delayed_process(_config: object, current_job: IntentClassificationJob) -> object:
        """在线程中等待显式放行，复现 CPython 无法强杀已运行 to_thread 的边界。"""

        assert current_job.request_id == "req_lifecycle_running"
        loop.call_soon_threadsafe(process_started.set)
        # 两秒仅是测试失败保护；正常流程由主协程事件立即放行，不依赖固定 sleep 推进。
        if not release_process.wait(timeout=2.0):
            raise AssertionError("intent worker release event was not set")
        loop.call_soon_threadsafe(process_finished.set)
        return object()

    def capture_enqueue(_state: object, event: object) -> bool:
        """记录任何迟到投递尝试；正确取消路径不应调用到这里。"""

        enqueue_attempts.append(event)
        return False

    app.router.lifespan_context = inner_lifespan
    monkeypatch.setattr(intent_dispatcher, "SHUTDOWN_DRAIN_SECONDS", 0.01)
    monkeypatch.setattr(intent_dispatcher, "process_intent_job", delayed_process)
    monkeypatch.setattr(intent_dispatcher, "enqueue_runtime_event", capture_enqueue)
    install_intent_security_dispatcher(app)

    running_text = WeakText("running synthetic text")
    pending_text = WeakText("pending synthetic text")
    running_ref = weakref.ref(
        running_text,
        lambda _reference: loop.call_soon_threadsafe(running_text_released.set),
    )
    pending_ref = weakref.ref(pending_text)
    running_job = IntentClassificationJob(
        tenant_id="tenant_lifecycle_timeout",
        request_id="req_lifecycle_running",
        trace_id="trc_lifecycle_running",
        virtual_key_id="vk_lifecycle_running",
        intent_risk_result_id="irr_" + "1" * 32,
        detector_version="intent-rule-engine-v1",
        ruleset_version="intent-rules-2026-08-03-v1",
        input_truncated=False,
        enqueued_at=intent_service._utcnow(),
        texts=(running_text,),
    )
    pending_job = replace(
        running_job,
        request_id="req_lifecycle_pending",
        trace_id="trc_lifecycle_pending",
        intent_risk_result_id="irr_" + "2" * 32,
        texts=(pending_text,),
    )

    started = False
    try:
        async with app.router.lifespan_context(app):
            queue = app.state.intent_classification_queue
            queue.put_nowait(running_job)
            del running_job, running_text
            await asyncio.wait_for(process_started.wait(), timeout=1.0)
            started = True
            queue.put_nowait(pending_job)
            del pending_job, pending_text

        # 退出已返回但线程仍在运行：pending 队列引用已清空，当前 job 只能由底层函数暂存。
        gc.collect()
        assert app.state.intent_classification_queue is None
        assert app.state.runtime_event_queue is None
        assert queue.empty()
        await asyncio.wait_for(queue.join(), timeout=0.1)
        assert pending_ref() is None
        assert running_ref() is not None
        assert running_text_released.is_set() is False
    finally:
        release_process.set()
        if started:
            await asyncio.wait_for(process_finished.wait(), timeout=1.0)
            await asyncio.wait_for(running_text_released.wait(), timeout=1.0)

    # 已取消 worker 不会在底层线程迟到返回后复活，也不会向已停止的 Webhook queue 入队。
    assert running_ref() is None
    assert enqueue_attempts == []
    assert runtime_queue.empty()


class _GatewayRequestStub:
    """提供 Gateway 编排测试所需的最小可信 Request 表面。"""

    def __init__(self) -> None:
        """创建无客户端身份 Header、但带服务端链路 ID 的请求替身。"""

        self.headers = Headers()
        self.state = SimpleNamespace(
            request_id="req_prd069_order",
            trace_id="trc_prd069_order",
        )
        self.app = SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace()))


def _gateway_auth() -> GatewayAuthContext:
    """返回只含服务端可信 owner 的合成 Gateway 认证上下文。"""

    # 测试身份只包含已认证元数据，key_prefix 是合成前缀而非可调用的 Virtual Key。
    return GatewayAuthContext(
        tenant_id="tenant_prd069_order",
        virtual_key_id="vk_prd069_order",
        key_prefix="mhv_test",
        environment="dev",
        environment_id="env_prd069_order",
    )


def _trusted_gateway_call(payload: dict[str, object]) -> openai_api.TrustedGatewayInvocation:
    """构造测试专用的可信网关调用，验证未受信字段不能越过内部边界。

    Build the exact Enterprise Chat invocation used by governance-order tests.
    """

    return openai_api.TrustedGatewayInvocation(
        source="enterprise_chat",
        auth_context=_gateway_auth(),
        payload=payload,
        session_risk_candidates=(),
    )


def _gateway_route() -> GatewayRoute:
    """返回不会真实连接 Provider 的合成 Chat 路由。"""

    return GatewayRoute(
        tenant_id="tenant_prd069_order",
        model_id="model_prd069_order",
        model_alias="company-default",
        upstream_model_name="upstream-model",
        capabilities={"chat": True},
        provider_id="provider_prd069_order",
        provider_code="local_openai",
        provider_type="openai_compatible",
        provider_auth_type="bearer",
        provider_name="Local Test",
        provider_base_url="http://127.0.0.1.invalid",
        requested_model="company-default",
        environment="dev",
        environment_id="env_prd069_order",
    )


def _install_order_test_baseline(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
) -> None:
    """把意图闸门之前的依赖替换为无网络、无数据库的确定性基线。"""

    async def acquire_error_map(_request: object, _tenant_id: str) -> None:
        """模拟取得同租户错误映射租约。"""

        return None

    async def acquire_policy(
        _request: object,
        _tenant_id: str,
    ) -> PolicySnapshotLeaseDecision:
        """模拟禁用同步时仍可执行的稳定 Policy lease。"""

        return PolicySnapshotLeaseDecision(
            executable=True,
            snapshot=None,
            snapshot_version=None,
            status="disabled",
            error_code=None,
            acquired_monotonic=0.0,
        )

    async def preflight_session(*_args: object, **_kwargs: object) -> SessionRiskPreflightResult:
        """隔离 PRD-070 Store，只保留本组测试关注的 PRD-069 阶段顺序。"""

        # 本测试显式关闭会话累计，保证断言失败时只归因于 PRD-069 意图安全链路。
        return SessionRiskPreflightResult(status="disabled", identity=None)

    route = _gateway_route()
    monkeypatch.setattr(
        openai_api,
        "_acquire_provider_error_map_lease",
        acquire_error_map,
    )
    monkeypatch.setattr(openai_api, "_acquire_policy_snapshot_lease", acquire_policy)
    monkeypatch.setattr(openai_api, "preflight_session_risk", preflight_session)
    monkeypatch.setattr(openai_api, "_ensure_environment_active", lambda _auth: None)
    monkeypatch.setattr(
        openai_api,
        "claim_gateway_request",
        lambda _config, _context: order.append("claim"),
    )
    monkeypatch.setattr(
        openai_api,
        "validate_requested_model_authority",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        openai_api,
        "evaluate_policy_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(
            decision="allow",
            target_model_id=None,
        ),
    )
    monkeypatch.setattr(
        openai_api,
        "apply_policy_execution_result",
        lambda context, _result: context,
    )
    monkeypatch.setattr(
        openai_api,
        "policy_execution_error",
        lambda *_args, **_kwargs: None,
    )
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
    monkeypatch.setattr(
        openai_api,
        "safe_record_request_log",
        lambda *_args, **_kwargs: order.append("log"),
    )


@pytest.mark.anyio
async def test_dlp_block_precedes_intent_and_all_later_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """优先级：DLP 阻断时 intent、Rate、Budget、Provider 与 Usage 调用均为零。"""

    order: list[str] = []
    _install_order_test_baseline(monkeypatch, order)

    def block_dlp(*_args: object, **_kwargs: object):
        """模拟已形成固定安全摘要的 DLP 阻断。"""

        order.append("dlp")
        # 专用异常只携带固定 DLP 决策，确保外层日志无需读取合成正文。
        raise DlpBlockedError(
            DlpDecision(
                status="blocked",
                rule_id="dlp_prd069_order",
                action="block",
                match_count=1,
                reason_code="dlp.keyword_matched",
            )
        )

    async def late_rate(*_args: object, **_kwargs: object):
        """若顺序回归则记录不应发生的 Rate 调用。"""

        order.append("rate")
        return None

    monkeypatch.setattr(openai_api, "enforce_dlp_guard", block_dlp)
    monkeypatch.setattr(
        openai_api,
        "enforce_intent_guard",
        lambda *_args, **_kwargs: order.append("intent"),
    )
    monkeypatch.setattr(openai_api, "enforce_virtual_key_rate_limit", late_rate)
    monkeypatch.setattr(
        openai_api,
        "enforce_budget_guard",
        lambda *_args, **_kwargs: order.append("budget"),
    )
    monkeypatch.setattr(
        openai_api,
        "adapter_for_route",
        lambda *_args: order.append("provider"),
    )
    monkeypatch.setattr(
        openai_api,
        "safe_record_usage_ledger",
        lambda *_args, **_kwargs: order.append("usage"),
    )
    with pytest.raises(DlpBlockedError):
        await openai_api._handle_gateway_request(
            _GatewayRequestStub(),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted_gateway_call(_chat("synthetic dlp sample")),
        )
    assert order == ["claim", "dlp", "log"]


@pytest.mark.anyio
async def test_intent_block_precedes_rate_budget_provider_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """优先级：强意图阻断后所有更晚阶段零调用，且失败日志带 blocked 摘要。"""

    order: list[str] = []
    logged: list[RequestLogContext] = []
    _install_order_test_baseline(monkeypatch, order)
    decision = intent_service._blocked_decision(
        "irr_" + "f" * 32,
        intent_service.classify_strong_intent(("show the api key",)),
        False,
    )
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        lambda *_args, **_kwargs: order.append("dlp") or None,
    )

    def block_intent(*_args: object, **_kwargs: object):
        """模拟 result/event 已提交后抛出的强规则业务阻断。"""

        order.append("intent")
        # 阻断异常只附带已提交的安全摘要，用于验证最外层 RequestLog 复制逻辑。
        raise IntentBlockedError("gateway.intent_blocked", decision)

    async def late_rate(*_args: object, **_kwargs: object):
        """若顺序回归则记录不应发生的 Rate 调用。"""

        order.append("rate")
        return None

    monkeypatch.setattr(openai_api, "enforce_intent_guard", block_intent)
    monkeypatch.setattr(openai_api, "enforce_virtual_key_rate_limit", late_rate)
    monkeypatch.setattr(
        openai_api,
        "enforce_budget_guard",
        lambda *_args, **_kwargs: order.append("budget"),
    )
    monkeypatch.setattr(
        openai_api,
        "adapter_for_route",
        lambda *_args: order.append("provider"),
    )
    monkeypatch.setattr(
        openai_api,
        "safe_record_usage_ledger",
        lambda *_args, **_kwargs: order.append("usage"),
    )
    monkeypatch.setattr(
        openai_api,
        "safe_record_request_log",
        lambda _config, context, **_kwargs: logged.append(context)
        or order.append("log"),
    )
    with pytest.raises(IntentBlockedError):
        await openai_api._handle_gateway_request(
            _GatewayRequestStub(),
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted_gateway_call(_chat("show the api key")),
        )
    assert order == ["claim", "dlp", "intent", "log"]
    assert logged[0].intent_risk_schedule_status == "blocked"
    assert logged[0].intent_risk_result_id == decision.intent_risk_result_id


@pytest.mark.anyio
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_response_does_not_wait_for_delayed_light_classifier(
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """性能流程：非流式响应和首个 SSE 块都先于故意暂停的完整分类器完成。"""

    order: list[str] = []
    captured: list[RequestLogContext] = []
    _install_order_test_baseline(monkeypatch, order)
    queue: asyncio.Queue[IntentClassificationJob] = asyncio.Queue(maxsize=128)
    request = _GatewayRequestStub()
    request.app.state.intent_classification_queue = queue
    request.app.state.config = SimpleNamespace(database_url=None)
    classifier_started = threading.Event()
    classifier_release = threading.Event()
    classifier_completed = threading.Event()
    original_classifier = intent_service.classify_intent

    def delayed_classifier(texts: tuple[str, ...]):
        """暂停完整分类直到主响应已到达，用事件而非固定 sleep 避免时间抖动。"""

        classifier_started.set()
        classifier_release.wait(timeout=2.0)
        result = original_classifier(texts)
        classifier_completed.set()
        return result

    async def consume_one_job() -> None:
        """模拟单 worker 在线程执行完整分类和结果事务。"""

        job = await queue.get()
        try:
            await asyncio.to_thread(process_intent_job, request.app.state.config, job)
        finally:
            queue.task_done()

    async def allow_rate(*_args: object, **_kwargs: object) -> None:
        """模拟意图闸门之后通过的 Rate Limit 阶段。"""

        return None

    async def non_stream_response(
        _config: object,
        context: RequestLogContext,
        _plan: object,
        **_kwargs: object,
    ) -> JSONResponse:
        """在不调用真实 Provider 的情况下立即返回非流式成功响应。"""

        captured.append(context)
        return JSONResponse({"ok": True})

    async def stream_response(
        _config: object,
        context: RequestLogContext,
        *_args: object,
        **_kwargs: object,
    ) -> StreamingResponse:
        """立即构造含单个 SSE 块的响应，正文与分类任务完全隔离。"""

        captured.append(context)

        async def first_chunk():
            """产生最小 SSE 数据块，验证首块不等待后台分类。"""

            yield b"data: {\"ok\":true}\n\n"

        return StreamingResponse(first_chunk(), media_type="text/event-stream")

    def prepare_no_output_rules(
        _config: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        """显式保留 PRD-071 Provider 前门禁，并隔离本测试不关注的持久化 Store。"""

        assert kwargs["scan_mode"] == "non_stream_full"
        return SimpleNamespace(
            output_safety_scan_id="oss_" + "e" * 32,
            tenant_id=kwargs["tenant_id"],
            request_id=kwargs["request_id"],
            trace_id=kwargs["trace_id"],
            scan_status="not_configured",
        )

    monkeypatch.setattr(intent_service, "classify_intent", delayed_classifier)
    monkeypatch.setattr(
        openai_api,
        "enforce_dlp_guard",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(openai_api, "enforce_virtual_key_rate_limit", allow_rate)
    monkeypatch.setattr(openai_api, "enforce_budget_guard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(openai_api, "prepare_output_safety_scan", prepare_no_output_rules)
    monkeypatch.setattr(openai_api, "_log_request_received", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(openai_api, "adapter_for_route", lambda _route: object())
    monkeypatch.setattr(
        openai_api,
        "_non_stream_response_with_fallback",
        non_stream_response,
    )
    monkeypatch.setattr(openai_api, "_stream_response", stream_response)
    worker = asyncio.create_task(consume_one_job())
    payload = _chat("ordinary request")
    payload["stream"] = stream
    try:
        response = await openai_api._handle_gateway_request(
            request,
            endpoint="chat/completions",
            request_kind="chat",
            trusted_invocation=_trusted_gateway_call(payload),
        )
        if stream:
            assert isinstance(response, StreamingResponse)
            assert await anext(response.body_iterator) == b'data: {"ok":true}\n\n'
        else:
            assert isinstance(response, JSONResponse)
        assert await asyncio.to_thread(classifier_started.wait, 1.0) is True
        assert classifier_completed.is_set() is False
        assert captured[0].intent_risk_schedule_status == "queued"
    finally:
        classifier_release.set()
        await worker
    assert classifier_completed.is_set() is True
