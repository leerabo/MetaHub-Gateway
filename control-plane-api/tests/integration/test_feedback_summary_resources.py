"""PRD-049 Feedback Summary 的并发门禁、绝对 deadline 与索引专项测试。

本文件与主流程测试分离，集中验证报表资源合同：同步 SQLite 工作不阻塞 ASGI 事件循环，
同一进程只运行一条 Summary，认证后的 query 解析/排队/线程/DTO 共用 250ms，且 SQL
先用可信 tenant/time scope 命中 0038 定义且由 0039 保留的索引。测试不记录
Session Token 或任何业务正文。
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import metahub_control.feedback.routes as feedback_routes
import metahub_control.feedback.service as feedback_service
import metahub_shared.feedback.storage as feedback_storage
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth import AuthContext, require_feedback_summary_user
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import authenticate_bearer
from metahub_control.feedback.routes import router as feedback_router
from metahub_control.feedback.summary_runtime import FeedbackSummaryRuntime
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.feedback import FeedbackSummaryResult, FeedbackSummaryTotals
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 口令仅用于 pytest 临时库管理员登录，不能写入断言、反馈事实或失败日志。
ADMIN_PASSWORD = "SummaryResource!2026"
# 固定时间窗口既满足 canonical UTC 合同，也让 EXPLAIN 参数与事实内容无关。
FROM_TIME = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
TO_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _sqlite_url(path: Path) -> str:
    """把临时路径转换为项目数据库连接层接受的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """创建完整 0039 临时库并返回数据库 URL 与默认可信 tenant_id。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd049-summary-resources.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd049-summary-resource-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    # tenant_id 来自真实 seed 结果，测试不能从 query 或自造常量恢复授权边界。
    return database_url, seed.tenant_id


def _build_app():
    """构造只注册认证与 Feedback 的独立 Control API 测试应用。"""

    app = create_service_app("control-plane-api", "PRD-049 Summary Resource Test API")
    app.include_router(auth_router)
    app.include_router(feedback_router)
    return app


async def _login_admin(client: AsyncClient) -> str:
    """登录临时管理员并仅在当前测试协程内返回 Session Token。"""

    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "default",
            "email": "admin@metahub.local",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    # 明文 Token 仅保留在函数返回值和当前请求头中，不写测试日志或持久化断言。
    return str(response.json()["access_token"])


def _empty_summary(
    *,
    from_time: datetime,
    to_time: datetime,
    limit: int,
    offset: int,
) -> FeedbackSummaryResult:
    """构造不含租户、actor 或敏感字段的空 Summary 领域结果。"""

    return FeedbackSummaryResult(
        scope_type="tenant",
        from_time=from_time,
        to_time=to_time,
        summary=FeedbackSummaryTotals(
            feedback_count=0,
            rating_sum=0,
            average_rating_milli=0,
            accepted_count=0,
            adoption_rate_bps=0,
            rating_distribution={str(rating): 0 for rating in range(1, 6)},
            tag_counts=(),
        ),
        models=(),
        total=0,
        limit=limit,
        offset=offset,
    )


@pytest.mark.anyio
async def test_summary_runtime_serializes_scans_without_blocking_event_loop() -> None:
    """并发门禁：首条阻塞扫描在线程运行，第二条排队超时且不会进入 operation。"""

    runtime = FeedbackSummaryRuntime()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_calls = 0

    def first_operation(monotonic_deadline: float) -> str:
        """占用唯一资源槽，模拟可由 SQLite progress handler 终止的同步扫描。"""

        assert monotonic_deadline > time.monotonic()
        first_entered.set()
        assert release_first.wait(timeout=1.0)
        return "first-finished"

    def second_operation(monotonic_deadline: float) -> str:
        """记录错误的并发放行；正确门禁下本函数不会被调用。"""

        nonlocal second_calls
        second_calls += 1
        return f"unexpected-{monotonic_deadline}"

    first_task = asyncio.create_task(
        runtime.run(first_operation, monotonic_deadline=time.monotonic() + 1.0)
    )
    try:
        for _ in range(200):
            # 只用异步让出事件循环观察线程信号，避免另一个 to_thread 掩盖被测行为。
            if first_entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert first_entered.is_set()

        # 首条 operation 正在同步等待，但事件循环仍能按时执行 heartbeat。
        heartbeat_started = time.monotonic()
        await asyncio.sleep(0.010)
        assert time.monotonic() - heartbeat_started < 0.100
        assert not release_first.is_set()

        with pytest.raises(ApiError) as captured:
            await runtime.run(
                second_operation,
                monotonic_deadline=time.monotonic() + 0.030,
            )
        assert captured.value.code == "feedback.unavailable"
        assert second_calls == 0
    finally:
        # 无论断言是否通过都释放测试线程，避免把默认线程池留到 pytest 进程退出。
        release_first.set()
    assert await first_task == "first-finished"


@pytest.mark.anyio
async def test_summary_runtime_retains_slot_until_timed_out_worker_really_exits() -> None:
    """超时门禁：HTTP waiter 结束后，遗留同步 worker 退出前仍不能放入下一条扫描。"""

    runtime = FeedbackSummaryRuntime()
    worker_entered = threading.Event()
    release_worker = threading.Event()
    later_calls = 0

    def slow_operation(_monotonic_deadline: float) -> str:
        """故意忽略 deadline 模拟不可立即取消的文件系统调用，直到测试显式释放。"""

        worker_entered.set()
        assert release_worker.wait(timeout=1.0)
        # 固定结果不携带内部线程、数据库或租户状态。
        return "late-worker-finished"

    def later_operation(_monotonic_deadline: float) -> str:
        """统计后续 operation 是否在遗留 worker 仍运行时被错误放行。"""

        nonlocal later_calls
        later_calls += 1
        # 固定结果只用于确认旧 worker 退出后槽位可以恢复服务。
        return "later-finished"

    try:
        with pytest.raises(ApiError) as first_timeout:
            await runtime.run(
                slow_operation,
                monotonic_deadline=time.monotonic() + 0.030,
            )
        assert first_timeout.value.code == "feedback.unavailable"
        assert worker_entered.is_set()

        with pytest.raises(ApiError) as queued_timeout:
            await runtime.run(
                later_operation,
                monotonic_deadline=time.monotonic() + 0.030,
            )
        assert queued_timeout.value.code == "feedback.unavailable"
        assert later_calls == 0
    finally:
        # 释放不响应取消的测试 worker，让 done callback 在真实退出后归还唯一槽位。
        release_worker.set()

    recovered = await runtime.run(
        later_operation,
        monotonic_deadline=time.monotonic() + 0.200,
    )
    assert recovered == "later-finished"
    assert later_calls == 1


def test_shared_summary_absolute_deadline_and_query_plans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shared 门限：耗尽的 endpoint 预算不开连接，两个 scope 使用 0039 仍保留的预期反馈索引。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    connect_attempted = False

    def fail_if_connected(_database_url: str | None, *, error_code: str):
        """记录 deadline 耗尽后错误打开连接的行为，并立即让测试失败。"""

        nonlocal connect_attempted
        connect_attempted = True
        # 固定异常只标识资源合同破坏，不包含数据库 URL、tenant 或底层驱动信息。
        raise AssertionError(f"expired summary opened database: {error_code}")

    real_connect = feedback_storage._connect
    monkeypatch.setattr(feedback_storage, "_connect", fail_if_connected)
    with pytest.raises(ApiError) as captured:
        feedback_storage.query_feedback_summary(
            database_url,
            tenant_id=tenant_id,
            from_time=FROM_TIME,
            to_time=TO_TIME,
            monotonic_deadline=time.monotonic() - 0.001,
        )
    assert captured.value.code == "feedback.unavailable"
    assert connect_attempted is False
    monkeypatch.setattr(feedback_storage, "_connect", real_connect)

    connection = connect_database(database_url)
    try:
        tenant_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COUNT(*), COALESCE(SUM(rating), 0)
            FROM request_feedbacks
            WHERE tenant_id=? AND created_at>=? AND created_at<?
            """,
            (tenant_id, "2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"),
        ).fetchall()
        model_id = str(
            connection.execute(
                "SELECT model_id FROM models WHERE tenant_id=? ORDER BY model_id LIMIT 1",
                (tenant_id,),
            ).fetchone()["model_id"]
        )
        model_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COUNT(*), COALESCE(SUM(rating), 0)
            FROM request_feedbacks
            WHERE tenant_id=? AND created_at>=? AND created_at<? AND model_id=?
            """,
            (
                tenant_id,
                "2026-07-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
                model_id,
            ),
        ).fetchall()
    finally:
        connection.close()

    tenant_details = " ".join(str(row["detail"]) for row in tenant_plan)
    model_details = " ".join(str(row["detail"]) for row in model_plan)
    assert "idx_request_feedback_tenant_time_prd049" in tenant_details
    assert "idx_request_feedback_model_time_prd049" in model_details


@pytest.mark.anyio
async def test_summary_endpoint_uses_trusted_tenant_and_one_entry_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """HTTP 合同：tenant 来自 Session，畸形注入与 deadline 错误保留 code/request_id。"""

    _database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    captured_tenants: list[str] = []
    captured_deadlines: list[float] = []

    def fake_query_summary(
        database_url: str | None,
        *,
        tenant_id: str,
        from_time: datetime,
        to_time: datetime,
        model_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        deadline_seconds: float = 0.250,
        monotonic_deadline: float | None = None,
        authorize=None,
    ) -> FeedbackSummaryResult:
        """捕获 service 恢复的可信 tenant 和绝对 deadline，并返回安全空报表。"""

        assert database_url is not None
        assert model_id is None
        assert deadline_seconds == 0.250
        assert monotonic_deadline is not None
        assert callable(authorize)
        captured_tenants.append(tenant_id)
        captured_deadlines.append(monotonic_deadline)
        return _empty_summary(
            from_time=from_time,
            to_time=to_time,
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(feedback_service, "query_feedback_summary", fake_query_summary)
    app = _build_app()
    # 生产合同固定 250ms，且起点位于认证依赖之前的共享中间件。
    assert feedback_routes.FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS == 0.250
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login_admin(client)
        authorization = {"Authorization": f"Bearer {token}"}
        # 先用真实 Session/RBAC 恢复可信租户，再以异步依赖替身隔离本节点的路由预算。
        # Windows 沙箱可能把同步依赖线程调度延迟计入 250ms，造成与业务逻辑无关的抖动；
        # 紧邻的认证锁测试仍走真实依赖，专门证明认证和 last_seen 共用同一绝对 deadline。
        trusted_context = authenticate_bearer(app.state.config, authorization["Authorization"])

        async def trusted_summary_user() -> AuthContext:
            """返回经真实数据库校验的上下文，避免测试调度延迟污染路由预算断言。"""

            return trusted_context

        app.dependency_overrides[require_feedback_summary_user] = trusted_summary_user
        success = await client.get(
            "/feedback/summary",
            headers={
                **authorization,
                "X-Request-ID": "summary_resource_success",
                "X-Trace-ID": "trace_summary_resource_success",
            },
            params={
                "from_time": "2026-07-01T00:00:00Z",
                "to_time": "2026-08-01T00:00:00Z",
            },
        )
        injected_tenant = await client.get(
            "/feedback/summary?tenant_id=another_tenant",
            headers={**authorization, "X-Request-ID": "summary_resource_tenant_injection"},
        )

        real_query_parser = feedback_routes._read_summary_query

        def slow_query_parser(request):
            """消耗完整 250ms 预算，证明查询解析不能获得认证后的新时钟。"""

            time.sleep(0.270)
            return real_query_parser(request)

        monkeypatch.setattr(feedback_routes, "_read_summary_query", slow_query_parser)
        deadline_error = await client.get(
            "/feedback/summary",
            headers={
                **authorization,
                "X-Request-ID": "summary_resource_deadline",
                "X-Trace-ID": "trace_summary_resource_deadline",
            },
            params={
                "from_time": "2026-07-01T00:00:00Z",
                "to_time": "2026-08-01T00:00:00Z",
            },
        )
        app.dependency_overrides.clear()

    assert success.status_code == 200, success.text
    assert success.json()["request_id"] == "summary_resource_success"
    assert success.json()["trace_id"] == "trace_summary_resource_success"
    assert captured_tenants == [tenant_id]
    assert len(captured_deadlines) == 1
    assert "tenant_id" not in success.json()

    assert injected_tenant.status_code == 422
    assert injected_tenant.json()["code"] == "feedback.query_invalid"
    assert injected_tenant.json()["request_id"] == "summary_resource_tenant_injection"
    assert "another_tenant" not in injected_tenant.text
    assert captured_tenants == [tenant_id]

    assert deadline_error.status_code == 503
    assert deadline_error.json()["code"] == "feedback.unavailable"
    assert deadline_error.json()["request_id"] == "summary_resource_deadline"
    assert deadline_error.json()["trace_id"] == "trace_summary_resource_deadline"
    assert "tenant_id" not in deadline_error.json()
    # 解析耗尽预算后门禁不得启动新线程，也就不能第二次触达 tenant-scoped service。
    assert captured_tenants == [tenant_id]
    assert feedback_routes.FEEDBACK_SUMMARY_ENDPOINT_DEADLINE_SECONDS == 0.250


@pytest.mark.anyio
async def test_summary_authentication_lock_uses_same_endpoint_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """认证写锁不得沿用 SQLite 默认 5 秒等待，必须在 250ms 内返回统一 503。"""

    database_url, _tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login_admin(client)
        lock_connection = connect_database(database_url)
        try:
            # 独立 writer 持有 RESERVED 锁；认证 SELECT 可读，但 last_seen UPDATE 必须等待。
            lock_connection.execute("PRAGMA busy_timeout=0")
            lock_connection.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            response = await client.get(
                "/feedback/summary",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Request-ID": "summary_auth_lock_deadline",
                    "X-Trace-ID": "trace_summary_auth_lock_deadline",
                },
            )

            elapsed = time.monotonic() - started
        finally:
            lock_connection.rollback()
            lock_connection.close()

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "feedback.unavailable"
    assert response.json()["request_id"] == "summary_auth_lock_deadline"
    assert response.json()["trace_id"] == "trace_summary_auth_lock_deadline"
    # Windows CI 留出调度余量，但必须远小于修复前实际复现的约 5.6 秒。
    assert elapsed < 0.750
