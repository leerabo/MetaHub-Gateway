"""PRD-049 反馈写路径的真实并发、错误重分类与提交后副作用测试。

本模块刻意使用完整迁移和多个真实 SQLite connection，不用顺序 mock 冒充并发。测试只
保存稳定对象 ID，不记录 Session Token、Virtual Key 明文、Prompt、Response 或上游密钥。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from metahub_control.feedback import service as control_feedback_service
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.feedback import service as gateway_feedback_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.feedback import (
    FeedbackActor,
    FeedbackCreateResult,
    create_request_feedback,
    normalize_feedback_payload,
)
from metahub_shared.runtime.errors import ApiError

# 每个场景使用不同源调用，避免一个场景形成的不可变反馈改变另一个场景的裁决结果。
SAME_INTENT_REQUEST_ID = "req_prd049_concurrent_same_intent"
DIFFERENT_INTENT_REQUEST_ID = "req_prd049_concurrent_different_intent"
CROSS_ENTRY_REQUEST_ID = "req_prd049_concurrent_cross_entry"
UNKNOWN_INTEGRITY_REQUEST_ID = "req_prd049_unknown_integrity"
LOCKED_REQUEST_ID = "req_prd049_database_locked"
QUEUE_FULL_REQUEST_ID = "req_prd049_queue_full"
WORKER_UNAVAILABLE_REQUEST_ID = "req_prd049_worker_unavailable"
WEBHOOK_FAILURE_REQUEST_ID = "req_prd049_webhook_failure"


@dataclass(frozen=True, slots=True)
class _FeedbackFixture:
    """保存临时数据库中的可信租户、actor 和最终归因对象 ID。

    这些值均由迁移与 seed 后的数据库读取，不接受反馈请求体覆盖；测试不把任何认证明文
    放进该结构，避免失败断言或对象 repr 扩大敏感信息暴露面。
    """

    database_url: str
    tenant_id: str
    admin_user_id: str
    virtual_key_id: str
    model_id: str
    provider_id: str


@dataclass(frozen=True, slots=True)
class _CallOutcome:
    """承载并发调用的成功值或统一领域错误，确保线程异常可被精确比较。"""

    value: object | None
    error: ApiError | None


def _sqlite_url(path: Path) -> str:
    """把 pytest 临时路径转换为项目数据库层支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(tmp_path: Path, request_ids: Sequence[str]) -> _FeedbackFixture:
    """应用完整迁移、写入 seed，并创建指定的可反馈成功 RequestLog。

    参数 ``request_ids`` 只包含测试固定 ASCII ID；每条 RequestLog 都先写 claim，再从同
    tenant 的 Model/Provider 恢复归因，保持与 Gateway 权威写入顺序一致。
    """

    database_url = _sqlite_url(tmp_path / "metahub-prd049-concurrency.db")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        admin = connection.execute(
            """
            SELECT user_id FROM users
            WHERE tenant_id=? AND email=? AND role='admin' AND status='active'
            """,
            (seed.tenant_id, seed.admin_email),
        ).fetchone()
        provider = connection.execute(
            """
            SELECT provider_id FROM providers
            WHERE tenant_id=? AND provider_code=? AND status='active'
            """,
            (seed.tenant_id, seed.provider_code),
        ).fetchone()
        model = connection.execute(
            """
            SELECT model_id FROM models
            WHERE tenant_id=? AND model_alias=? AND status='active'
            """,
            (seed.tenant_id, seed.model_alias),
        ).fetchone()
        virtual_key = connection.execute(
            """
            SELECT virtual_key_id FROM virtual_keys
            WHERE tenant_id=? AND key_prefix=? AND status='active'
            ORDER BY created_at, virtual_key_id LIMIT 1
            """,
            (seed.tenant_id, seed.virtual_key_prefix),
        ).fetchone()
        assert admin is not None
        assert provider is not None
        assert model is not None
        assert virtual_key is not None
        for request_id in request_ids:
            _insert_success_request_log(
                connection,
                tenant_id=seed.tenant_id,
                request_id=request_id,
                virtual_key_id=str(virtual_key["virtual_key_id"]),
                provider_id=str(provider["provider_id"]),
                model_id=str(model["model_id"]),
            )
        connection.commit()
        return _FeedbackFixture(
            database_url=database_url,
            tenant_id=seed.tenant_id,
            admin_user_id=str(admin["user_id"]),
            virtual_key_id=str(virtual_key["virtual_key_id"]),
            model_id=str(model["model_id"]),
            provider_id=str(provider["provider_id"]),
        )
    finally:
        # 即使夹具断言失败也释放 SQLite 句柄，避免后续专项测试误报 database locked。
        connection.close()


def _insert_success_request_log(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    virtual_key_id: str,
    provider_id: str,
    model_id: str,
) -> None:
    """按 claim -> RequestLog 顺序写入零 attempt 的成功调用。

    该 legacy 形态只有在 ``routing_attempt_count=0`` 且 final attempt 为空时才允许使用
    RequestLog 的模型和 Provider；因此它也覆盖反馈资格状态机的严格回退前提。
    """

    trace_id = f"trc_{request_id}"
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id, request_id, trace_id, virtual_key_id,
          snapshot_version, policy_snapshot_status, claimed_at
        )
        VALUES(?, ?, ?, ?, NULL, 'disabled', '2026-07-20T01:00:00Z')
        """,
        (tenant_id, request_id, trace_id, virtual_key_id),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
          status, endpoint, request_model, resolved_model, provider_code,
          routing_attempt_count, created_at, updated_at
        )
        SELECT ?, ?, ?, ?, ?, ?, 'success', '/v1/chat/completions',
               'company-default', model.model_alias, provider.provider_code, 0,
               '2026-07-20T01:00:00Z', '2026-07-20T01:00:01Z'
        FROM models AS model
        JOIN providers AS provider
          ON provider.tenant_id=model.tenant_id
         AND provider.provider_id=model.provider_id
        WHERE model.tenant_id=? AND model.model_id=? AND provider.provider_id=?
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            virtual_key_id,
            provider_id,
            model_id,
            tenant_id,
            model_id,
            provider_id,
        ),
    )


def _session_actor(fixture: _FeedbackFixture) -> FeedbackActor:
    """从 seed 管理员恢复 Session actor，不允许测试正文伪造 tenant 或 user。"""

    return FeedbackActor(
        tenant_id=fixture.tenant_id,
        actor_type="session_user",
        actor_user_id=fixture.admin_user_id,
    )


def _virtual_key_context(fixture: _FeedbackFixture) -> GatewayAuthContext:
    """构造只含脱敏对象 ID 的 Gateway 鉴权上下文。"""

    # Key 前缀仅使用固定遮罩；租户和 Key 对象 ID 必须来自数据库夹具的可信归属。
    return GatewayAuthContext(
        tenant_id=fixture.tenant_id,
        virtual_key_id=fixture.virtual_key_id,
        key_prefix="masked",
        environment="dev",
    )


def _intent(request_id: str, *, rating: int = 5):
    """通过公开 shared 规范化器生成固定标签顺序和 v1 指纹的反馈意图。"""

    return normalize_feedback_payload(
        {
            "request_id": request_id,
            "rating": rating,
            "accepted": True,
            "tags": ["helpful", "accurate"],
        }
    )


def _capture_call(start: threading.Barrier, call: Callable[[], object]) -> _CallOutcome:
    """等待线程屏障后执行一次领域调用，只捕获预期的统一 ApiError。"""

    start.wait(timeout=5)
    try:
        return _CallOutcome(value=call(), error=None)
    # 领域竞争只允许通过统一错误码返回；数据库或线程异常仍由 Future 原样传播。
    except ApiError as exc:
        # 其他异常必须由 Future 原样抛出使测试失败，不能被误分类成领域竞争结果。
        return _CallOutcome(value=None, error=exc)


def _run_concurrently(
    first: Callable[[], object],
    second: Callable[[], object],
) -> tuple[_CallOutcome, _CallOutcome]:
    """用两个工作线程和三方屏障同时发起真实双连接写竞争。

    主线程只有在两个 worker 都到达屏障后才放行；每个领域函数在各自线程内新建 SQLite
    connection，因此不会复用同一连接串行执行或触发 sqlite 的跨线程句柄限制。
    """

    start = threading.Barrier(3)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="prd049-feedback") as executor:
        first_future = executor.submit(_capture_call, start, first)
        second_future = executor.submit(_capture_call, start, second)
        start.wait(timeout=5)
        return first_future.result(timeout=10), second_future.result(timeout=10)


def _assert_persisted_graph(fixture: _FeedbackFixture, expected_count: int) -> None:
    """核对 Feedback、Event 与派生统计同租户同计数且外键完整。"""

    connection = connect_database(fixture.database_url)
    try:
        feedback_count = connection.execute(
            "SELECT COUNT(*) AS total FROM request_feedbacks WHERE tenant_id=?",
            (fixture.tenant_id,),
        ).fetchone()["total"]
        event_count = connection.execute(
            "SELECT COUNT(*) AS total FROM request_feedback_events WHERE tenant_id=?",
            (fixture.tenant_id,),
        ).fetchone()["total"]
        aggregate_count = connection.execute(
            """
            SELECT COALESCE(SUM(feedback_count), 0) AS total
            FROM model_feedback_statistics WHERE tenant_id=?
            """,
            (fixture.tenant_id,),
        ).fetchone()["total"]
        foreign_key_failures = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    assert feedback_count == event_count == aggregate_count == expected_count
    assert foreign_key_failures == []


def test_same_actor_same_operation_real_concurrency_replays_single_fact(
    tmp_path: Path,
) -> None:
    """同 actor、同 operation、同意图并发时一方创建，另一方恢复同一事件。"""

    fixture = _prepare_database(tmp_path, [SAME_INTENT_REQUEST_ID])
    actor = _session_actor(fixture)
    intent = _intent(SAME_INTENT_REQUEST_ID)
    first, second = _run_concurrently(
        lambda: create_request_feedback(
            fixture.database_url,
            actor=actor,
            operation_request_id="op_prd049_same_intent",
            trace_id="trace_prd049_same_intent_a",
            intent=intent,
        ),
        lambda: create_request_feedback(
            fixture.database_url,
            actor=actor,
            operation_request_id="op_prd049_same_intent",
            trace_id="trace_prd049_same_intent_b",
            intent=intent,
        ),
    )

    assert first.error is None and second.error is None
    assert isinstance(first.value, FeedbackCreateResult)
    assert isinstance(second.value, FeedbackCreateResult)
    assert sorted([first.value.idempotent_replay, second.value.idempotent_replay]) == [False, True]
    assert first.value.feedback.request_feedback_id == second.value.feedback.request_feedback_id
    assert first.value.event.feedback_event_id == second.value.event.feedback_event_id
    _assert_persisted_graph(fixture, 1)


def test_same_actor_operation_different_intent_real_concurrency_conflicts(
    tmp_path: Path,
) -> None:
    """同 actor、同 operation 的异意图竞争只保留胜者，并稳定返回 request_conflict。"""

    fixture = _prepare_database(tmp_path, [DIFFERENT_INTENT_REQUEST_ID])
    actor = _session_actor(fixture)
    first, second = _run_concurrently(
        lambda: create_request_feedback(
            fixture.database_url,
            actor=actor,
            operation_request_id="op_prd049_different_intent",
            trace_id="trace_prd049_different_intent_a",
            intent=_intent(DIFFERENT_INTENT_REQUEST_ID, rating=4),
        ),
        lambda: create_request_feedback(
            fixture.database_url,
            actor=actor,
            operation_request_id="op_prd049_different_intent",
            trace_id="trace_prd049_different_intent_b",
            intent=_intent(DIFFERENT_INTENT_REQUEST_ID, rating=5),
        ),
    )

    outcomes = (first, second)
    successes = [item.value for item in outcomes if item.value is not None]
    errors = [item.error for item in outcomes if item.error is not None]
    assert len(successes) == len(errors) == 1
    assert isinstance(successes[0], FeedbackCreateResult)
    assert errors[0] is not None and errors[0].code == "feedback.request_conflict"
    connection = connect_database(fixture.database_url)
    try:
        persisted = connection.execute(
            """
            SELECT rating FROM request_feedbacks
            WHERE tenant_id=? AND source_request_id=?
            """,
            (fixture.tenant_id, DIFFERENT_INTENT_REQUEST_ID),
        ).fetchone()
    finally:
        connection.close()
    assert persisted is not None
    assert persisted["rating"] == successes[0].feedback.rating
    _assert_persisted_graph(fixture, 1)


def test_control_and_gateway_real_concurrency_share_one_source_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control Session 与 Gateway Key 同时评价同一源调用时全局只形成一条事实。"""

    fixture = _prepare_database(tmp_path, [CROSS_ENTRY_REQUEST_ID])
    config = SimpleNamespace(database_url=fixture.database_url)
    control_context = SimpleNamespace(
        user=SimpleNamespace(tenant_id=fixture.tenant_id, user_id=fixture.admin_user_id)
    )
    gateway_context = _virtual_key_context(fixture)
    app_state = SimpleNamespace(runtime_event_queue=None)
    # 本测试只裁决双入口数据库竞争；外部投递分别由后续副作用测试使用真实边界验证。
    monkeypatch.setattr(control_feedback_service, "dispatch_webhook_event", Mock(return_value=None))
    monkeypatch.setattr(gateway_feedback_service, "enqueue_runtime_event", Mock(return_value=True))
    intent = _intent(CROSS_ENTRY_REQUEST_ID)

    control, gateway = _run_concurrently(
        lambda: control_feedback_service.create_session_feedback(
            config,
            control_context,
            operation_request_id="op_prd049_cross_control",
            trace_id="trace_prd049_cross_control",
            intent=intent,
        ),
        lambda: gateway_feedback_service.create_virtual_key_feedback(
            config,
            app_state,
            gateway_context,
            operation_request_id="op_prd049_cross_gateway",
            trace_id="trace_prd049_cross_gateway",
            intent=intent,
        ),
    )

    outcomes = (control, gateway)
    successes = [item.value for item in outcomes if item.value is not None]
    errors = [item.error for item in outcomes if item.error is not None]
    assert len(successes) == len(errors) == 1
    assert errors[0] is not None and errors[0].code == "feedback.already_exists"
    assert successes[0].feedback.request_id == CROSS_ENTRY_REQUEST_ID
    _assert_persisted_graph(fixture, 1)


def test_locked_database_and_unknown_integrity_keep_distinct_error_codes(
    tmp_path: Path,
) -> None:
    """写锁超时映射 unavailable，未知 IntegrityError 则失败关闭为 record_failed。"""

    fixture = _prepare_database(
        tmp_path,
        [UNKNOWN_INTEGRITY_REQUEST_ID, LOCKED_REQUEST_ID],
    )
    actor = _session_actor(fixture)
    connection = connect_database(fixture.database_url)
    try:
        # 测试 trigger 模拟随机 PK/未知 graph 约束；错误正文不得被猜成 already_exists。
        connection.execute(
            """
            CREATE TRIGGER prd049_test_unknown_feedback_integrity
            BEFORE INSERT ON request_feedbacks
            BEGIN
              SELECT RAISE(ABORT, 'prd049_test_private_integrity_detail');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ApiError) as unknown_error:
        create_request_feedback(
            fixture.database_url,
            actor=actor,
            operation_request_id="op_prd049_unknown_integrity",
            trace_id="trace_prd049_unknown_integrity",
            intent=_intent(UNKNOWN_INTEGRITY_REQUEST_ID),
        )
    assert unknown_error.value.code == "feedback.record_failed"
    assert unknown_error.value.http_status == 500
    assert "prd049_test_private_integrity_detail" not in unknown_error.value.message
    _assert_persisted_graph(fixture, 0)

    connection = connect_database(fixture.database_url)
    try:
        connection.execute("DROP TRIGGER prd049_test_unknown_feedback_integrity")
        connection.commit()
    finally:
        connection.close()

    lock_holder = connect_database(fixture.database_url)
    try:
        # BEGIN IMMEDIATE 持有真实写锁；被测连接只能等待领域固定的 250ms，不能无限阻塞。
        lock_holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(ApiError) as locked_error:
            create_request_feedback(
                fixture.database_url,
                actor=actor,
                operation_request_id="op_prd049_database_locked",
                trace_id="trace_prd049_database_locked",
                intent=_intent(LOCKED_REQUEST_ID),
            )
        assert locked_error.value.code == "feedback.unavailable"
        assert locked_error.value.http_status == 503
    finally:
        lock_holder.rollback()
        lock_holder.close()
    _assert_persisted_graph(fixture, 0)


def test_gateway_queue_full_and_worker_unavailable_do_not_rollback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gateway 队列满或 worker 缺失时仍提交 Feedback、聚合和持久 Event。"""

    fixture = _prepare_database(
        tmp_path,
        [QUEUE_FULL_REQUEST_ID, WORKER_UNAVAILABLE_REQUEST_ID],
    )
    config = SimpleNamespace(database_url=fixture.database_url)
    context = _virtual_key_context(fixture)
    queue: asyncio.Queue[object] = asyncio.Queue(maxsize=1)
    # 预占唯一槽位以真实触发 put_nowait QueueFull，而不是 mock dispatcher 返回值。
    queue.put_nowait(object())
    caplog.set_level(logging.WARNING, logger="metahub.gateway.events")

    queue_full = gateway_feedback_service.create_virtual_key_feedback(
        config,
        SimpleNamespace(runtime_event_queue=queue),
        context,
        operation_request_id="op_prd049_queue_full",
        trace_id="trace_prd049_queue_full",
        intent=_intent(QUEUE_FULL_REQUEST_ID),
    )
    worker_unavailable = gateway_feedback_service.create_virtual_key_feedback(
        config,
        SimpleNamespace(runtime_event_queue=None),
        context,
        operation_request_id="op_prd049_worker_unavailable",
        trace_id="trace_prd049_worker_unavailable",
        intent=_intent(WORKER_UNAVAILABLE_REQUEST_ID),
    )

    assert queue_full.idempotent_replay is False
    assert worker_unavailable.idempotent_replay is False
    assert queue.qsize() == 1
    drop_reasons = {
        payload["reason"]
        for record in caplog.records
        if record.name == "metahub.gateway.events"
        and isinstance((payload := getattr(record, "payload", None)), dict)
        and payload.get("reason") in {"queue_full", "worker_unavailable"}
    }
    assert drop_reasons == {"queue_full", "worker_unavailable"}
    _assert_persisted_graph(fixture, 2)


def test_control_webhook_dispatch_failure_does_not_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Control Webhook 提交后异常只告警，不能改写已经提交的成功反馈。"""

    fixture = _prepare_database(tmp_path, [WEBHOOK_FAILURE_REQUEST_ID])
    config = SimpleNamespace(database_url=fixture.database_url)
    context = SimpleNamespace(
        user=SimpleNamespace(tenant_id=fixture.tenant_id, user_id=fixture.admin_user_id)
    )
    # 故障正文模拟外部 Endpoint 细节；服务日志只能保留固定分类，不能传播该文本。
    dispatch = Mock(side_effect=RuntimeError("private-endpoint-token must stay redacted"))
    monkeypatch.setattr(control_feedback_service, "dispatch_webhook_event", dispatch)
    caplog.set_level(logging.WARNING, logger="metahub.control.feedback")

    result = control_feedback_service.create_session_feedback(
        config,
        context,
        operation_request_id="op_prd049_webhook_failure",
        trace_id="trace_prd049_webhook_failure",
        intent=_intent(WEBHOOK_FAILURE_REQUEST_ID),
    )

    assert result.idempotent_replay is False
    assert result.feedback.request_id == WEBHOOK_FAILURE_REQUEST_ID
    dispatch.assert_called_once()
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "feedback.webhook_dispatch_failed"
    ]
    assert len(warnings) == 1
    assert getattr(warnings[0], "error_code", None) == "webhook.delivery_unavailable"
    assert "private-endpoint-token" not in str(vars(warnings[0]))
    _assert_persisted_graph(fixture, 1)
