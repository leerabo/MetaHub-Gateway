"""PRD-077 管理助手 Control API 的权限、租户、wire、审计与并发专项。"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.admin_assistant import repository as assistant_repository
from metahub_control.admin_assistant import routes as assistant_routes
from metahub_control.admin_assistant import scheduler as assistant_scheduler
from metahub_control.admin_assistant import service as assistant_service
from metahub_control.auth import service as auth_service
from metahub_control.auth.routes import router as auth_router
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from starlette.background import BackgroundTask

# 本组固定测试凭据、时间和角色身份只存在隔离临时数据库，不代表任何部署环境秘密。
ADMIN_PASSWORD = "MetaHubLocal!2026"
ROLE_PASSWORD = "Prd077RoleOnly!2026"
AUTH_SECRET = "prd077-control-auth-" + "a" * 48
NOW = "2026-08-11T00:00:00Z"

ROLE_USERS = {
    "finance_admin": ("user_prd077_finance", "finance@prd077.local"),
    "security_admin": ("user_prd077_security", "security@prd077.local"),
    "developer": ("user_prd077_developer", "developer@prd077.local"),
    "viewer": ("user_prd077_viewer", "viewer@prd077.local"),
    "ops_admin": ("user_prd077_ops", "ops@prd077.local"),
}


def _url(path: Path) -> str:
    """把仓库内隔离 SQLite 路径转换为应用数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _insert_request_fact(
    connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    error_code: str | None,
) -> None:
    """写入不含正文的 RequestLog owner，用于验证 request_id 的租户优先查询。"""

    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,NULL,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, NOW),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,status,error_code,latency_ms,
          created_at,updated_at
        ) VALUES(?,?,?,'failed',?,17,?,?)
        """,
        (request_id, tenant_id, trace_id, error_code, NOW, NOW),
    )


@pytest.fixture(scope="module")
def prd077_control_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """使用当前完整结构和权限目录，后续每个用例复制为独立事实库。"""

    database = tmp_path_factory.mktemp("prd077-control-template") / "template.sqlite3"
    database_url = _url(database)
    previous_password = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        # 0070 introduced this feature; later migrations also update the RBAC
        # catalog checked by today's authentication code. A historical 0070
        # database cannot exercise the current HTTP runtime faithfully.
        assert "0070" in apply_migrations(database_url).applied
        seed = apply_seed_data(database_url)
    finally:
        if previous_password is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous_password

    connection = connect_database(database_url)
    try:
        # 六角色共用同一默认矩阵，但身份、Session 与限流 owner 必须保持逐用户隔离。
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    user_id,
                    seed.tenant_id,
                    email,
                    f"PRD077 {role}",
                    hash_password(ROLE_PASSWORD),
                    role,
                    NOW,
                    NOW,
                )
                for role, (user_id, email) in ROLE_USERS.items()
            ),
        )
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES(
              'tenant_prd077_foreign','prd077-foreign','PRD077 Foreign',
              'internal_only','active',?,?
            )
            """,
            (NOW, NOW),
        )
        # 同 ID 两租户使用不同安全错误分类，回答若越过 tenant-first 条件会立即暴露。
        _insert_request_fact(
            connection,
            tenant_id=seed.tenant_id,
            request_id="req_prd077_shared",
            trace_id="trc_prd077_shared_local",
            error_code="gateway.upstream_timeout",
        )
        _insert_request_fact(
            connection,
            tenant_id="tenant_prd077_foreign",
            request_id="req_prd077_shared",
            trace_id="trc_prd077_shared_foreign",
            error_code="auth.permission_denied",
        )
        _insert_request_fact(
            connection,
            tenant_id="tenant_prd077_foreign",
            request_id="req_prd077_foreign_only",
            trace_id="trc_prd077_foreign_only",
            error_code="gateway.upstream_rate_limited",
        )
        connection.commit()
    finally:
        connection.close()

    return database


def _clear_rate_limiter() -> None:
    """清空进程级计数，避免独立 HTTP 场景共享一分钟窗口。"""

    with assistant_service._limiter_lock:
        assistant_service._actor_attempts.clear()
        assistant_service._tenant_attempts.clear()


@pytest.fixture(autouse=True)
def clear_prd077_rate_limiter() -> None:
    """在每个测试入口恢复独立的 actor/tenant 限流 owner。"""

    _clear_rate_limiter()


def _clone_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    """复制模板并把数据库、密钥、调度器和日志限定在当前测试进程。"""

    database = tmp_path / "prd077-control.sqlite3"
    shutil.copy2(template, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED", "false")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    return database_url


def _build_app():
    """构造真实错误中间件、认证路由和 PRD-077 endpoint 的隔离应用。"""

    app = create_service_app("control-plane-api", "PRD-077 Control Test")

    @app.middleware("http")
    async def private_assistant_boundary(request: Request, call_next):
        """为测试 endpoint 施加与正式应用一致的私有缓存响应头边界。"""

        # 依赖和 wire parser 都可能在 endpoint 返回前失败，隐私 Header 必须覆盖整条路径。
        response = await call_next(request)
        if request.url.path == "/admin-assistant/query":
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Vary"] = "Authorization"
        return response

    app.include_router(auth_router)
    app.include_router(assistant_routes.router)
    return app


def _current_execution_runtime(app):
    """读取当前测试 loop 的 PAGE-107 runtime，并拒绝跨循环复用 admission。"""

    runtimes = getattr(app.state, assistant_service._EXECUTION_RUNTIMES_STATE)
    runtime = runtimes.get(asyncio.get_running_loop())
    assert runtime is not None
    return runtime


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录合成用户，Bearer Token 只在当前测试调用栈中使用。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只返回当前异步用例调用栈，不写日志、文件或失败断言正文。
    return str(response.json()["access_token"])


def _headers(token: str, suffix: str) -> dict[str, str]:
    """构造不含客户数据的单值认证与链路 Header。"""

    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": f"req_prd077_{suffix}",
        "X-Trace-ID": f"trc_prd077_{suffix}",
    }


async def _ask(
    client: AsyncClient,
    token: str,
    suffix: str,
    question: str,
) -> Response:
    """发送 exact 单字段正文，调用方只断言安全结构和固定机器值。"""

    return await client.post(
        "/admin-assistant/query",
        json={"question": question},
        headers=_headers(token, suffix),
    )


def _assert_private_response(response: Response) -> None:
    """成功和失败都不得被浏览器或中间缓存保存。"""

    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "Authorization"
    assert response.headers["x-request-id"]
    assert response.headers["x-trace-id"]


def _assert_error(response: Response, code: str, status: int) -> None:
    """统一错误必须同时携带固定 code、request_id 与 trace_id。"""

    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["trace_id"] == response.headers["x-trace-id"]
    _assert_private_response(response)


class _CleanupFailureConnection(sqlite3.Connection):
    """按操作计数注入一次 SQLite 状态清理失败，供长期连接退休攻击测试复用。"""

    fail_progress_install = False
    fail_progress_clear = False
    fail_busy_restore = False
    fail_query_only_restore = False
    fail_rollback = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        """初始化真实 SQLite 连接，并记录状态安装与恢复动作的精确次数。"""

        super().__init__(*args, **kwargs)
        self.busy_assignments = 0
        self.progress_install_attempts = 0
        self.progress_clear_attempts = 0

    def execute(self, sql: str, parameters=()):
        """在 busy_timeout 或 query_only 恢复点按开关注入固定 SQLite 失败。"""

        normalized = " ".join(sql.strip().upper().split())
        if normalized.startswith("PRAGMA BUSY_TIMEOUT="):
            self.busy_assignments += 1
            if self.fail_busy_restore and self.busy_assignments >= 2:
                # 第二次赋值对应恢复原 busy_timeout，失败时连接必须由 owner 退休。
                raise sqlite3.OperationalError("fixed busy restore failure")
        if normalized == "PRAGMA QUERY_ONLY=OFF" and self.fail_query_only_restore:
            # query_only 无法恢复会污染长期连接，必须触发失效通知而非继续复用。
            raise sqlite3.OperationalError("fixed query-only restore failure")
        return super().execute(sql, parameters)

    def set_progress_handler(self, progress_handler, n: int) -> None:
        """分别在 deadline handler 安装或清除阶段注入失败，验证其余恢复仍执行。"""

        if progress_handler is None:
            self.progress_clear_attempts += 1
            if self.fail_progress_clear:
                # 清除失败表示回调可能残留，连接必须退休并禁止回到请求池。
                raise sqlite3.OperationalError("fixed progress clear failure")
        else:
            self.progress_install_attempts += 1
            if self.fail_progress_install:
                # handler 未成功安装时仍应尝试 rollback、busy 与 query_only 的完整恢复。
                raise sqlite3.OperationalError("fixed progress install failure")
        return super().set_progress_handler(progress_handler, n)

    def rollback(self) -> None:
        """在事务回滚阶段注入固定失败，验证连接污染会触发 owner 失效处理。"""

        if self.fail_rollback:
            # 回滚失败意味着事务边界未知，测试要求连接不再被任何后续请求复用。
            raise sqlite3.OperationalError("fixed rollback failure")
        return super().rollback()


def test_expired_middleware_deadline_is_never_refreshed() -> None:
    """已存在但过期的入口时钟必须失败关闭，dependency 不能从当前时刻重开预算。"""

    request = SimpleNamespace(
        app=FastAPI(),
        state=SimpleNamespace(request_started_monotonic=time.monotonic() - 2.0),
    )
    with pytest.raises(ApiError) as raised:
        assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
    assert raised.value.code == "admin_assistant.source_unavailable"
    assert not hasattr(request.state, assistant_service._REQUEST_BUDGET_STATE)


def test_rate_limit_key_registries_are_bounded_reclaim_expired_and_stay_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限流 key 过期后可回收，容量压力和线程竞争都必须关闭式且原子。"""

    monkeypatch.setattr(assistant_service, "_RATE_LIMIT_ACTOR_KEY_CAPACITY", 1)
    monkeypatch.setattr(assistant_service, "_RATE_LIMIT_TENANT_KEY_CAPACITY", 1)
    old_context = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_old"),
        user=SimpleNamespace(user_id="user_prd077_rate_old"),
    )
    assert assistant_service._consume_rate_limit(old_context) is True
    expired_at = time.monotonic() - assistant_service._RATE_LIMIT_WINDOW_SECONDS - 1.0
    with assistant_service._limiter_lock:
        assistant_service._actor_attempts[("tenant_prd077_rate_old", "user_prd077_rate_old")][0] = (
            expired_at
        )
        assistant_service._tenant_attempts["tenant_prd077_rate_old"][0] = expired_at

    current_context = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_current"),
        user=SimpleNamespace(user_id="user_prd077_rate_current"),
    )
    assert assistant_service._consume_rate_limit(current_context) is True
    with assistant_service._limiter_lock:
        assert set(assistant_service._actor_attempts) == {
            ("tenant_prd077_rate_current", "user_prd077_rate_current")
        }
        assert set(assistant_service._tenant_attempts) == {"tenant_prd077_rate_current"}

    second_actor = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_current"),
        user=SimpleNamespace(user_id="user_prd077_rate_second"),
    )
    assert assistant_service._consume_rate_limit(second_actor) is False
    with assistant_service._limiter_lock:
        assert len(assistant_service._actor_attempts) == 1
        assert len(assistant_service._tenant_attempts["tenant_prd077_rate_current"]) == 1
        assistant_service._actor_attempts.clear()

    second_tenant = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_second"),
        user=SimpleNamespace(user_id="user_prd077_rate_second"),
    )
    assert assistant_service._consume_rate_limit(second_tenant) is False
    with assistant_service._limiter_lock:
        # tenant registry 已满时不能先留下 actor 半状态，否则后续容量会被永久误占。
        assert assistant_service._actor_attempts == {}
        assistant_service._tenant_attempts.clear()

    barrier = threading.Barrier(2)
    results: list[bool] = []

    def consume_concurrently(context: SimpleNamespace) -> None:
        """让两个可信主体同时竞争唯一 registry 容量，验证进程锁原子性。"""

        barrier.wait(timeout=2)
        results.append(assistant_service._consume_rate_limit(context))

    contexts = (
        SimpleNamespace(
            tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_race_a"),
            user=SimpleNamespace(user_id="user_prd077_rate_race_a"),
        ),
        SimpleNamespace(
            tenant=SimpleNamespace(tenant_id="tenant_prd077_rate_race_b"),
            user=SimpleNamespace(user_id="user_prd077_rate_race_b"),
        ),
    )
    threads = [
        threading.Thread(target=consume_concurrently, args=(context,)) for context in contexts
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(results) == [False, True]
    with assistant_service._limiter_lock:
        assert len(assistant_service._actor_attempts) == 1
        assert {key[0] for key in assistant_service._actor_attempts} == set(
            assistant_service._tenant_attempts
        )


def test_execution_primitives_are_partitioned_by_running_event_loop() -> None:
    """同一 app 的并发 event loop 只共享 SQLite 权威层，不共享 asyncio 锁或 AnyIO limiter。"""

    app = FastAPI()
    barrier = threading.Barrier(2)
    results: list[tuple[object, asyncio.Lock]] = []
    failures: list[BaseException] = []

    def build_runtime() -> None:
        """在线程专属事件循环内创建 runtime，并捕获任何跨循环装配失败。"""

        async def run() -> None:
            """构造当前 loop 的请求预算和 tenant 锁，供线程间身份比较。"""

            request = SimpleNamespace(
                app=app,
                state=SimpleNamespace(request_started_monotonic=time.monotonic()),
            )
            _, runtime = assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
            lock = assistant_service._tenant_execution_lock(runtime, "tenant_prd077_loop")
            barrier.wait(timeout=2)
            results.append((runtime, lock))
            assistant_service.release_admin_assistant_request(request)  # type: ignore[arg-type]

        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=build_runtime) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == 2
    assert results[0][0] is not results[1][0]
    assert results[0][1] is not results[1][1]


@pytest.mark.anyio
async def test_attempt_gate_is_bounded_and_authentication_failure_releases_owner(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """gate 达到硬上限时拒绝新 admission，认证失败也不能给 shutdown 留下 owner。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    monkeypatch.setattr(assistant_service, "_IN_FLIGHT_ATTEMPT_CAPACITY", 1)
    app = _build_app()
    first = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    _, runtime = assistant_service.prepare_admin_assistant_request(first)  # type: ignore[arg-type]
    second = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    with pytest.raises(ApiError) as raised:
        assistant_service.prepare_admin_assistant_request(second)  # type: ignore[arg-type]
    assert raised.value.code == "admin_assistant.source_unavailable"
    assert len(runtime.attempt_owners) == 1
    assistant_service.release_admin_assistant_request(first)  # type: ignore[arg-type]
    assert runtime.attempt_owners == {}

    # 真实 FastAPI dependency 在无凭据时不会进入 endpoint；异常边界仍须释放 owner。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/admin-assistant/query",
            json={"question": "usage last 7 days"},
            headers={"X-Request-ID": "req_prd077_auth_gate", "X-Trace-ID": "trc_prd077_auth_gate"},
        )
    assert response.status_code == 401
    assert runtime.attempt_owners == {}
    await assistant_service.clear_admin_assistant_execution_runtime(
        app,
        asyncio.get_running_loop(),
    )


@pytest.mark.anyio
async def test_loop_runtime_reuses_two_connections_and_closes_them_at_teardown(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """认证/来源和最终写各复用一条连接，lifespan 清理后文件句柄失效。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    opened: list[tuple[str | None, bool]] = []
    original_connect = assistant_service.connect_database

    def counted_connect(
        url: str | None,
        *,
        check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        """记录长期连接的线程检查参数，并委托真实 SQLite 连接工厂。"""

        opened.append((url, check_same_thread))
        return original_connect(url, check_same_thread=check_same_thread)

    monkeypatch.setattr(assistant_service, "connect_database", counted_connect)
    app = _build_app()
    assistant_scheduler.install_admin_assistant_scheduler(app)
    runtime = None
    read_connection = None
    write_connection = None
    async with app.router.lifespan_context(app):
        runtime = _current_execution_runtime(app)
        read_connection = runtime.read_connection
        write_connection = runtime.write_connection
        assert read_connection is not None
        assert write_connection is not None
        assert read_connection is not write_connection
        assert opened == [(database_url, False), (database_url, False)]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            first = await _ask(client, token, "runtime_reuse_1", "usage last 7 days")
            second = await _ask(client, token, "runtime_reuse_2", "usage last 7 days")
        assert first.status_code == second.status_code == 200
        assert runtime.auth_slots is runtime.source_slots is runtime.read_slots
        assert runtime.read_connection is read_connection
        assert runtime.write_connection is write_connection
        assert opened == [(database_url, False), (database_url, False)]
        assert read_connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert write_connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert int(read_connection.execute("PRAGMA synchronous").fetchone()[0]) == 2
        assert int(write_connection.execute("PRAGMA synchronous").fetchone()[0]) == 2

    assert runtime is not None
    assert runtime.closed is True
    assert read_connection is not None
    assert write_connection is not None
    with pytest.raises(sqlite3.ProgrammingError):
        read_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        write_connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_reused_connections_restore_busy_timeout_and_shutdown_removes_wal(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """连续成功/异常请求都恢复锁预算，lifespan 正常退出后不遗留 WAL/SHM。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    app = _build_app()
    assistant_scheduler.install_admin_assistant_scheduler(app)
    original_reader = assistant_repository._read_query_values
    original_audit = assistant_repository.record_audit_log

    async with app.router.lifespan_context(app):
        runtime = _current_execution_runtime(app)
        assert runtime.read_connection is not None
        assert runtime.write_connection is not None
        read_timeout = int(runtime.read_connection.execute("PRAGMA busy_timeout").fetchone()[0])
        write_timeout = int(runtime.write_connection.execute("PRAGMA busy_timeout").fetchone()[0])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            for suffix in ("timeout_restore_1", "timeout_restore_2"):
                response = await _ask(client, token, suffix, "usage last 7 days")
                assert response.status_code == 200, response.text
                assert runtime.attempt_owners == {}

            def fail_source(*args: object, **kwargs: object):
                """模拟来源 SQLite 异常，验证 finally 恢复长期 read connection。"""

                # 固定异常正文只用于测试，API 必须折叠为统一 source_unavailable 错误。
                raise sqlite3.OperationalError("fixed source failure")

            monkeypatch.setattr(assistant_repository, "_read_query_values", fail_source)
            source_failed = await _ask(
                client,
                token,
                "timeout_restore_source_failure",
                "usage last 7 days",
            )
            _assert_error(source_failed, "admin_assistant.source_unavailable", 503)
            monkeypatch.setattr(assistant_repository, "_read_query_values", original_reader)

            def fail_audit(*args: object, **kwargs: object):
                """模拟最终审计 SQLite 异常，验证 write connection 状态同样恢复。"""

                # 审计写入失败不能泄露 SQLite 正文，也不能留下未回滚的写连接状态。
                raise sqlite3.OperationalError("fixed audit failure")

            monkeypatch.setattr(assistant_repository, "record_audit_log", fail_audit)
            audit_failed = await _ask(
                client,
                token,
                "timeout_restore_audit_failure",
                "usage last 7 days",
            )
            _assert_error(audit_failed, "admin_assistant.audit_unavailable", 503)
            monkeypatch.setattr(assistant_repository, "record_audit_log", original_audit)

        assert int(runtime.read_connection.execute("PRAGMA busy_timeout").fetchone()[0]) == (
            read_timeout
        )
        assert int(runtime.write_connection.execute("PRAGMA busy_timeout").fetchone()[0]) == (
            write_timeout
        )
        assert runtime.attempt_owners == {}

    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()
    verification = connect_database(database_url)
    try:
        assert verification.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        verification.close()


@pytest.mark.parametrize(
    "failure_flag",
    ("fail_progress_clear", "fail_query_only_restore", "fail_busy_restore", "fail_rollback"),
)
def test_repository_cleanup_failure_retires_exact_runtime_connection(
    failure_flag: str,
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """任一仓储清理失败都按 identity 摘除并关闭旧句柄，替换引用绝不能被误清。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    connection = sqlite3.connect(
        str(database_path),
        check_same_thread=False,
        factory=_CleanupFailureConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    setattr(connection, failure_flag, True)
    tenant_id = str(
        connection.execute(
            "SELECT tenant_id FROM users WHERE email='admin@metahub.local'"
        ).fetchone()[0]
    )
    context = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id),
        user=SimpleNamespace(user_id="unused", role="admin"),
        session=SimpleNamespace(session_id="unused"),
    )
    replacement = connect_database(database_url, check_same_thread=False)
    runtime = SimpleNamespace(read_connection=connection)

    def invalidate(current: sqlite3.Connection) -> None:
        """模拟 service helper 的 identity CAS，并在回调中制造并发替换引用。"""

        runtime.read_connection = replacement
        assistant_service.retire_admin_assistant_runtime_read_connection(runtime, current)

    monkeypatch.setattr(
        assistant_repository,
        "refresh_transaction_authentication_fingerprint",
        lambda *_args, **_kwargs: context,
    )
    monkeypatch.setattr(assistant_repository, "require_permission", lambda *_args: None)
    monkeypatch.setattr(assistant_repository, "_require_live_permission_codes", lambda *_args: None)
    monkeypatch.setattr(
        assistant_repository,
        "_read_query_values",
        lambda *_args, **_kwargs: ({"request_count": 0}, {"overview": 0}),
    )
    try:
        with pytest.raises(ApiError) as raised:
            assistant_repository.read_source_snapshot(
                SimpleNamespace(),  # type: ignore[arg-type]
                context,  # type: ignore[arg-type]
                query_type="usage_statistics",
                target_request_id=None,
                window_started_at=datetime.now(UTC) - timedelta(days=1),
                window_ended_at=datetime.now(UTC),
                deadline=time.monotonic() + 1,
                connection=connection,
                invalidate_connection=invalidate,
            )
        assert raised.value.code == "admin_assistant.source_unavailable"
        assert runtime.read_connection is replacement
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        assert replacement.execute("SELECT 1").fetchone()[0] == 1
    finally:
        replacement.close()


def test_persist_cleanup_failure_retires_writer_and_next_call_opens_clean_connection(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """写连接 rollback 清理失败后被退休；同一 runtime 下一次只能取得新的干净句柄。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    failed_connection = sqlite3.connect(
        str(database_path),
        check_same_thread=False,
        factory=_CleanupFailureConnection,
    )
    failed_connection.row_factory = sqlite3.Row
    failed_connection.execute("PRAGMA foreign_keys=ON")
    failed_connection.fail_rollback = True
    runtime = SimpleNamespace(write_connection=failed_connection, closed=False)
    monkeypatch.setattr(
        assistant_repository,
        "refresh_transaction_authentication_fingerprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ApiError("auth.permission_denied")),
    )
    context = SimpleNamespace(
        tenant=SimpleNamespace(tenant_id="unused"),
        user=SimpleNamespace(user_id="unused", role="admin"),
        session=SimpleNamespace(session_id="unused"),
    )

    with pytest.raises(ApiError) as raised:
        assistant_repository.persist_attempt(
            SimpleNamespace(),  # type: ignore[arg-type]
            context,  # type: ignore[arg-type]
            query_type="usage_statistics",
            target_request_id=None,
            window_started_at=None,
            window_ended_at=None,
            outcome="failed",
            failure_code="auth.permission_denied",
            required_permission_codes=("admin_assistant.query",),
            source_types=(),
            claim_count=0,
            source_count=0,
            redaction_count=0,
            request_id="req_prd077_writer_retire",
            trace_id="trc_prd077_writer_retire",
            request_meta=SimpleNamespace(),  # type: ignore[arg-type]
            started_monotonic=time.monotonic(),
            deadline=time.monotonic() + 1,
            expected_authorization_fingerprint=None,
            enforce_permissions=False,
            connection=failed_connection,
            invalidate_connection=lambda current: (
                assistant_service._retire_admin_assistant_runtime_write_connection(runtime, current)
            ),
        )
    assert raised.value.code == "admin_assistant.audit_unavailable"
    assert runtime.write_connection is None
    with pytest.raises(sqlite3.ProgrammingError):
        failed_connection.execute("SELECT 1")

    clean_connection = assistant_service._admin_assistant_runtime_write_connection(
        runtime,  # type: ignore[arg-type]
        SimpleNamespace(database_url=database_url),  # type: ignore[arg-type]
        deadline=time.monotonic() + 1,
    )
    try:
        assert clean_connection is runtime.write_connection
        assert clean_connection is not failed_connection
        assert int(clean_connection.execute("PRAGMA query_only").fetchone()[0]) == 0
    finally:
        clean_connection.close()
        runtime.write_connection = None


@pytest.mark.anyio
async def test_lifespan_warmup_failure_closes_partial_runtime(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ready 前第二条连接失败时关闭首条连接，并移除当前 loop runtime。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    original_open = assistant_service._open_runtime_connection
    captured: list[sqlite3.Connection] = []

    def fail_second_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        """保留首条真实连接，第二条用固定领域错误模拟 startup 失败。"""

        if captured:
            # 第二条连接失败模拟 audit runtime 未就绪，首条 read 连接必须随启动回滚关闭。
            raise ApiError("admin_assistant.audit_unavailable")
        connection = original_open(*args, **kwargs)  # type: ignore[arg-type]
        captured.append(connection)
        return connection

    monkeypatch.setattr(
        assistant_service,
        "_open_runtime_connection",
        fail_second_connection,
    )
    app = _build_app()
    assistant_scheduler.install_admin_assistant_scheduler(app)
    with pytest.raises(ApiError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("failed warmup must not yield a ready application")
    assert raised.value.code == "admin_assistant.audit_unavailable"
    assert len(captured) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].execute("SELECT 1")
    runtimes = getattr(app.state, assistant_service._EXECUTION_RUNTIMES_STATE)
    assert asyncio.get_running_loop() not in runtimes


@pytest.mark.anyio
async def test_cancelled_teardown_waits_for_slots_and_prevents_connection_reopen() -> None:
    """teardown 取消不抢关活跃连接，closing runtime 也不能建立孤儿连接。"""

    app = FastAPI()
    request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    _, runtime = assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
    read_connection = sqlite3.connect(":memory:", check_same_thread=False)
    write_connection = sqlite3.connect(":memory:", check_same_thread=False)
    runtime.read_connection = read_connection
    runtime.write_connection = write_connection
    assistant_service.release_admin_assistant_request(request)  # type: ignore[arg-type]

    await runtime.read_slots.acquire()
    cleanup = asyncio.create_task(
        assistant_service.clear_admin_assistant_execution_runtime(
            app,
            asyncio.get_running_loop(),
        )
    )
    while not runtime.closing:
        await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0.050)
    assert not cleanup.done()
    stale_request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    with pytest.raises(ApiError) as raised:
        assistant_service.prepare_admin_assistant_request(stale_request)  # type: ignore[arg-type]
    assert raised.value.code == "admin_assistant.source_unavailable"

    runtime.read_slots.release()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    assert runtime.closed is True
    with pytest.raises(sqlite3.ProgrammingError):
        read_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        write_connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_teardown_close_failure_attempts_both_connections_and_stays_failed() -> None:
    """一条连接 close 失败时仍关闭另一条，且 runtime 不得被标成 closed 或移除。"""

    class FailingCloseConnection(sqlite3.Connection):
        """仅为本测试在第一次 runtime close 上制造稳定 SQLite 失败。"""

        close_attempts = 0

        def close(self) -> None:
            """记录每次关闭尝试并持续失败，验证 teardown 仍会处理另一条连接。"""

            self.close_attempts += 1
            # 关闭失败意味着句柄状态未证明终止，runtime 不得被错误标记为 closed。
            raise sqlite3.OperationalError("fixed close failure")

    app = FastAPI()
    request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    _, runtime = assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
    read_connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=FailingCloseConnection,
    )
    write_connection = sqlite3.connect(":memory:", check_same_thread=False)
    runtime.read_connection = read_connection
    runtime.write_connection = write_connection
    assistant_service.release_admin_assistant_request(request)  # type: ignore[arg-type]

    try:
        with pytest.raises(ApiError) as raised:
            await assistant_service.clear_admin_assistant_execution_runtime(
                app,
                asyncio.get_running_loop(),
            )
        assert raised.value.code == "admin_assistant.audit_unavailable"
        assert read_connection.close_attempts == 1
        with pytest.raises(sqlite3.ProgrammingError):
            write_connection.execute("SELECT 1")
        assert runtime.closed is False
        assert runtime.closing is True
        runtimes = getattr(app.state, assistant_service._EXECUTION_RUNTIMES_STATE)
        assert runtimes.get(asyncio.get_running_loop()) is runtime
    finally:
        # 绕过测试 override 释放真实本地句柄，避免测试自身制造资源泄漏。
        sqlite3.Connection.close(read_connection)


@pytest.mark.anyio
async def test_teardown_never_runs_database_wide_checkpoint(
    tmp_path: Path,
) -> None:
    """子系统只关闭自身连接，不执行共享数据库的全局 checkpoint。"""

    class FailingCheckpointConnection(sqlite3.Connection):
        """只拒绝 shutdown 的正规 checkpoint，其他 SQLite 操作保持真实。"""

        checkpoint_attempts = 0

        def execute(self, sql: str, parameters=()):
            """仅在 WAL checkpoint 语句注入失败，其他 SQL 保持真实 SQLite 行为。"""

            if sql.strip().upper().startswith("PRAGMA WAL_CHECKPOINT"):
                self.checkpoint_attempts += 1
                # checkpoint 失败后仍需尽力关闭 read/write 句柄，但不能报告成功 teardown。
                raise sqlite3.OperationalError("fixed checkpoint failure")
            return super().execute(sql, parameters)

    database_path = tmp_path / "checkpoint-failure.sqlite3"
    read_connection = sqlite3.connect(str(database_path), check_same_thread=False)
    write_connection = sqlite3.connect(
        str(database_path),
        check_same_thread=False,
        factory=FailingCheckpointConnection,
    )
    assert read_connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    app = FastAPI()
    request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    _, runtime = assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
    runtime.read_connection = read_connection
    runtime.write_connection = write_connection
    assistant_service.release_admin_assistant_request(request)  # type: ignore[arg-type]

    await assistant_service.clear_admin_assistant_execution_runtime(
        app,
        asyncio.get_running_loop(),
    )
    assert write_connection.checkpoint_attempts == 0
    for connection in (read_connection, write_connection):
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    assert runtime.closed is True
    assert runtime.closing is True
    runtimes = getattr(app.state, assistant_service._EXECUTION_RUNTIMES_STATE)
    assert asyncio.get_running_loop() not in runtimes


@pytest.mark.anyio
async def test_registered_persistence_waiter_finishes_before_teardown_closes_runtime(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """closing 拒绝新登记，但必须让既有 write FIFO waiter 先完成再关连接。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    request = SimpleNamespace(
        app=app,
        state=SimpleNamespace(request_started_monotonic=time.monotonic()),
    )
    budget, runtime = assistant_service.prepare_admin_assistant_request(request)  # type: ignore[arg-type]
    await runtime.write_slots.acquire()
    worker_started = threading.Event()

    def inspect_connection(connection: sqlite3.Connection) -> int:
        """在已登记 waiter 取得 slot 后证明 closing 没有拒绝其长期连接。"""

        worker_started.set()
        return int(connection.execute("SELECT 1").fetchone()[0])

    waiter = asyncio.create_task(
        assistant_service.run_admin_assistant_persistence_worker(
            inspect_connection,
            runtime=runtime,
            config=app.state.config,
            deadline=budget.governance_deadline,
        )
    )
    while runtime.write_slots.statistics().tasks_waiting < 1:
        await asyncio.sleep(0)
    assistant_service.release_admin_assistant_request(request)  # type: ignore[arg-type]

    cleanup = asyncio.create_task(
        assistant_service.clear_admin_assistant_execution_runtime(
            app,
            asyncio.get_running_loop(),
        )
    )
    while not runtime.closing:
        await asyncio.sleep(0)
    assert not waiter.done()

    runtime.write_slots.release()
    assert await asyncio.wait_for(waiter, timeout=1) == 1
    await asyncio.wait_for(cleanup, timeout=1)
    assert worker_started.is_set()
    assert runtime.closed is True


@pytest.mark.anyio
async def test_cancelled_lifespan_waits_for_scheduler_and_closes_runtime(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """宿主取消 lifespan 时，后台线程先退出，两条长期连接随后完整关闭。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    monkeypatch.setenv("METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED", "true")
    scheduler_started = threading.Event()
    scheduler_release = threading.Event()

    def blocking_scheduler_iteration(
        _config,
        *,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """阻塞一次 scheduler worker，验证 lifespan 取消会等待线程真实退出。"""

        assert isinstance(stop_event, threading.Event)
        scheduler_started.set()
        assert scheduler_release.wait(timeout=3)
        return False

    monkeypatch.setattr(
        assistant_scheduler,
        "run_admin_assistant_event_once",
        blocking_scheduler_iteration,
    )
    app = _build_app()
    assistant_scheduler.install_admin_assistant_scheduler(app)
    entered = asyncio.Event()
    captured: dict[str, object] = {}

    async def own_lifespan() -> None:
        """持有应用 lifespan 直到外部取消，用于观察 scheduler 与连接清理顺序。"""

        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
                response = await _ask(
                    client,
                    token,
                    "cancelled_lifespan",
                    "usage last 7 days",
                )
                assert response.status_code == 200, response.text
            runtime = _current_execution_runtime(app)
            captured["runtime"] = runtime
            captured["read"] = runtime.read_connection
            captured["write"] = runtime.write_connection
            entered.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(own_lifespan())
    try:
        assert await asyncio.to_thread(scheduler_started.wait, 1)
        await asyncio.wait_for(entered.wait(), timeout=2)
        owner.cancel()
        await asyncio.sleep(0.050)
        assert not owner.done()
    finally:
        scheduler_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, timeout=3)

    runtime = captured["runtime"]
    assert runtime.closed is True  # type: ignore[union-attr]
    assert app.state.admin_assistant_wake_event is None
    assert app.state.admin_assistant_event_loop is None
    for key in ("read", "write"):
        connection = captured[key]
        assert isinstance(connection, sqlite3.Connection)
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_lifespan_seals_new_admission_but_drains_inflight_attempt(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """shutdown 与来源读取竞争时，既有可信 attempt 必须先形成审计终态再关连接。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    source_started = threading.Event()
    source_release = threading.Event()
    original_reader = assistant_service.read_source_snapshot

    def blocked_reader(*args: object, **kwargs: object):
        """暂停真实来源读取，让 lifespan 在已登记 owner 存在时进入 closing。"""

        source_started.set()
        assert source_release.wait(timeout=1)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(assistant_service, "read_source_snapshot", blocked_reader)
    app = _build_app()
    assistant_scheduler.install_admin_assistant_scheduler(app)
    entered = asyncio.Event()
    stop_lifespan = asyncio.Event()
    captured: dict[str, object] = {}

    async def own_lifespan() -> None:
        """在单一 task 内持有真实 lifespan，外部事件只触发其正常退出。"""

        async with app.router.lifespan_context(app):
            captured["runtime"] = _current_execution_runtime(app)
            entered.set()
            await stop_lifespan.wait()

    lifespan_owner = asyncio.create_task(own_lifespan())
    request_task: asyncio.Task[Response] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        runtime = captured["runtime"]
        assert isinstance(runtime, assistant_service._LoopRuntime)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            request_task = asyncio.create_task(
                _ask(client, token, "shutdown_drain", "usage last 7 days")
            )
            assert await asyncio.to_thread(source_started.wait, 0.5)
            assert len(runtime.attempt_owners) == 1

            stop_lifespan.set()
            while not runtime.closing:
                await asyncio.sleep(0)
            assert not lifespan_owner.done()
            stale_request = SimpleNamespace(
                app=app,
                state=SimpleNamespace(request_started_monotonic=time.monotonic()),
            )
            with pytest.raises(ApiError) as raised:
                assistant_service.prepare_admin_assistant_request(stale_request)  # type: ignore[arg-type]
            assert raised.value.code == "admin_assistant.source_unavailable"

            source_release.set()
            response = await asyncio.wait_for(request_task, timeout=1)
            assert response.status_code == 200, response.text
            assert runtime.attempt_owners == {}
        await asyncio.wait_for(lifespan_owner, timeout=1)
    finally:
        source_release.set()
        stop_lifespan.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        if not lifespan_owner.done():
            await asyncio.wait_for(lifespan_owner, timeout=1)

    runtime = captured["runtime"]
    assert runtime.closed is True  # type: ignore[union-attr]
    verification = connect_database(database_url)
    try:
        graph = verification.execute(
            """
            SELECT outcome,COUNT(*) FROM assistant_queries
            WHERE operation_request_id='req_prd077_shutdown_drain'
            GROUP BY outcome
            """
        ).fetchone()
        assert tuple(graph) == ("answered", 1)
    finally:
        verification.close()


@pytest.mark.anyio
async def test_authentication_progress_deadline_interrupts_and_clears_handler(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """长 RBAC SQL 响应 source deadline，失败后同一长期连接不遗留 handler。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

    original_snapshot = auth_service._load_permission_snapshot

    def slow_snapshot(*args, **kwargs):
        """在真实权限快照前执行长 SQL，验证认证 progress handler 能按期中断。"""

        connection = args[0]
        connection.execute(
            """
            WITH RECURSIVE counter(value) AS (
              SELECT 1 UNION ALL SELECT value+1 FROM counter WHERE value<10000000
            )
            SELECT SUM(value) FROM counter
            """
        ).fetchone()
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(auth_service, "_load_permission_snapshot", slow_snapshot)
    connection = connect_database(database_url, check_same_thread=False)
    original_busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    started = time.monotonic()
    try:
        with pytest.raises(ApiError) as raised:
            await asyncio.to_thread(
                auth_service.authenticate_bearer,
                app.state.config,
                f"Bearer {token}",
                monotonic_deadline=time.monotonic() + 0.075,
                deadline_error_code="admin_assistant.source_unavailable",
                connection=connection,
                update_last_seen=False,
            )
        assert raised.value.code == "admin_assistant.source_unavailable"
        assert time.monotonic() - started < 0.750
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("PRAGMA busy_timeout")
        # deadline 中断属于 SQLite 错误路径；没有 runtime callback 时也必须关闭外部长期句柄。
        assert original_busy_timeout >= 0
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("failure_flag", "expected_clear_attempts", "expected_busy_assignments"),
    (
        ("fail_progress_install", 1, 2),
        ("fail_progress_clear", 1, 2),
        ("fail_busy_restore", 1, 2),
    ),
)
def test_auth_deadline_state_failure_restores_best_effort_and_invalidates(
    failure_flag: str,
    expected_clear_attempts: int,
    expected_busy_assignments: int,
) -> None:
    """handler 安装/清除或 busy 恢复失败均继续其余恢复，并通知 owner 退休连接。"""

    connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=_CleanupFailureConnection,
    )
    setattr(connection, failure_flag, True)
    invalidations = 0

    def invalidate() -> None:
        """记录 deadline 清理失败后的连接失效通知，确保 owner 只收到一次退休请求。"""

        nonlocal invalidations
        invalidations += 1

    try:
        with pytest.raises(sqlite3.Error):
            with auth_service._authentication_sqlite_deadline(
                connection,
                deadline=time.monotonic() + 1,
                error_code="admin_assistant.source_unavailable",
                invalidate_connection=invalidate,
            ):
                pass
        assert invalidations >= 1
        assert connection.progress_clear_attempts == expected_clear_attempts
        assert connection.busy_assignments == expected_busy_assignments
    finally:
        sqlite3.Connection.close(connection)


def test_auth_rollback_failure_returns_fixed_error_and_retires_runtime_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """认证 SQL 与 rollback 同时失败时只返回固定码，污染句柄从 runtime 精确摘除。"""

    connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=_CleanupFailureConnection,
    )
    connection.fail_rollback = True
    runtime = SimpleNamespace(read_connection=connection)
    monkeypatch.setattr(auth_service, "_extract_bearer", lambda _value: "fixed-test-token")
    monkeypatch.setattr(
        auth_service,
        "decode_access_token",
        lambda *_args: SimpleNamespace(),
    )
    monkeypatch.setattr(
        auth_service,
        "_authenticate_bearer_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("fixed authentication failure")
        ),
    )

    with pytest.raises(ApiError) as raised:
        auth_service.authenticate_bearer(
            SimpleNamespace(auth_token_secret="fixed-test-secret"),  # type: ignore[arg-type]
            "Bearer fixed-test-token",
            monotonic_deadline=time.monotonic() + 1,
            deadline_error_code="admin_assistant.source_unavailable",
            connection=connection,
            update_last_seen=False,
            invalidate_connection=lambda current: (
                assistant_service.retire_admin_assistant_runtime_read_connection(runtime, current)
            ),
        )
    assert raised.value.code == "admin_assistant.source_unavailable"
    assert runtime.read_connection is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_authentication_rejects_python_work_finishing_after_deadline(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限快照后的 Python 校验也计入入口预算，不能返回迟到认证结果。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

    original_snapshot = auth_service._load_permission_snapshot

    def late_snapshot(*args, **kwargs):
        """在真实权限扫描后消耗 Python 时间，验证返回前仍复核绝对期限。"""

        snapshot = original_snapshot(*args, **kwargs)
        time.sleep(0.075)
        return snapshot

    monkeypatch.setattr(auth_service, "_load_permission_snapshot", late_snapshot)
    connection = connect_database(database_url, check_same_thread=False)
    try:
        with pytest.raises(ApiError) as raised:
            await asyncio.to_thread(
                auth_service.authenticate_bearer,
                app.state.config,
                f"Bearer {token}",
                monotonic_deadline=time.monotonic() + 0.050,
                deadline_error_code="admin_assistant.source_unavailable",
                connection=connection,
                update_last_seen=False,
            )
        assert raised.value.code == "admin_assistant.source_unavailable"
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    finally:
        connection.close()


@pytest.mark.anyio
async def test_assistant_expired_auth_is_read_only_but_normal_auth_marks_expired(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PAGE-107 不在 read-stage 改 Session，普通认证仍收敛 expired 状态。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    runtime = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        connection = connect_database(database_url)
        try:
            session_id = str(
                connection.execute(
                    """
                    SELECT session_id FROM sessions
                    WHERE user_id=(SELECT user_id FROM users WHERE email='admin@metahub.local')
                      AND status='active'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE sessions SET expires_at=? WHERE session_id=?",
                ("2020-01-01T00:00:00Z", session_id),
            )
            connection.commit()
        finally:
            connection.close()

        assistant_response = await _ask(
            client,
            token,
            "expired_read_only",
            "usage last 7 days",
        )
        _assert_error(assistant_response, "auth.token_expired", 401)
        runtime = _current_execution_runtime(app)

        connection = connect_database(database_url)
        try:
            assert (
                connection.execute(
                    "SELECT status FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                == "active"
            )
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM assistant_queries
                WHERE operation_request_id='req_prd077_expired_read_only'
                """
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()

        normal_response = await client.get(
            "/auth/me",
            headers=_headers(token, "expired_normal_auth"),
        )
        assert normal_response.status_code == 401
        assert normal_response.json()["code"] == "auth.token_expired"

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT status FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            == "expired"
        )
    finally:
        connection.close()
        if runtime is not None:
            await assistant_service.clear_admin_assistant_execution_runtime(
                app,
                asyncio.get_running_loop(),
            )


@pytest.mark.anyio
async def test_last_seen_commits_and_rolls_back_with_governance_graph(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """last_seen 与四类治理事实同事务提交，审计失败时一起回滚且 writer 可恢复。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    old_time = "2020-01-01T00:00:00Z"
    app = _build_app()
    runtime = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        connection = connect_database(database_url)
        try:
            session_id = str(
                connection.execute(
                    """
                    SELECT session_id FROM sessions
                    WHERE user_id=(SELECT user_id FROM users WHERE email='admin@metahub.local')
                      AND status='active'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE sessions SET last_seen_at=?,updated_at=? WHERE session_id=?",
                (old_time, old_time, session_id),
            )
            connection.commit()
        finally:
            connection.close()

        committed = await _ask(client, token, "last_seen_commit", "usage last 7 days")
        assert committed.status_code == 200, committed.text
        runtime = _current_execution_runtime(app)
        writer_identity = id(runtime.write_connection)

        connection = connect_database(database_url)
        try:
            committed_times = connection.execute(
                """
                SELECT session.last_seen_at,query.created_at
                FROM sessions session
                JOIN assistant_queries query
                  ON query.actor_session_id=session.session_id
                WHERE query.operation_request_id='req_prd077_last_seen_commit'
                """
            ).fetchone()
            assert tuple(committed_times)[0] == tuple(committed_times)[1]
            connection.execute(
                "UPDATE sessions SET last_seen_at=?,updated_at=? WHERE session_id=?",
                (old_time, old_time, session_id),
            )
            connection.commit()
        finally:
            connection.close()

        with monkeypatch.context() as scoped:
            scoped.setattr(
                assistant_repository,
                "record_audit_log",
                lambda *_args, **_kwargs: None,
            )
            rolled_back = await _ask(
                client,
                token,
                "last_seen_rollback",
                "usage last 7 days",
            )
        _assert_error(rolled_back, "admin_assistant.audit_unavailable", 503)

        connection = connect_database(database_url)
        try:
            assert (
                connection.execute(
                    "SELECT last_seen_at FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                == old_time
            )
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM assistant_queries
                WHERE operation_request_id='req_prd077_last_seen_rollback'
                """
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()

        recovered = await _ask(client, token, "last_seen_recovered", "usage last 7 days")
        assert recovered.status_code == 200, recovered.text
        assert id(runtime.write_connection) == writer_identity

    connection = connect_database(database_url)
    try:
        recovered_times = connection.execute(
            """
            SELECT session.last_seen_at,query.created_at
            FROM sessions session
            JOIN assistant_queries query ON query.actor_session_id=session.session_id
            WHERE query.operation_request_id='req_prd077_last_seen_recovered'
            """
        ).fetchone()
        assert tuple(recovered_times)[0] == tuple(recovered_times)[1]
    finally:
        connection.close()
        if runtime is not None:
            await assistant_service.clear_admin_assistant_execution_runtime(
                app,
                asyncio.get_running_loop(),
            )


@pytest.mark.anyio
async def test_started_worker_holds_admission_until_real_thread_exit() -> None:
    """请求取消只能取消响应等待，已启动同步写 worker 退出前不得释放 admission。"""

    slots = anyio.CapacityLimiter(1)
    dispatcher = anyio.CapacityLimiter(math.inf)
    started = threading.Event()
    release = threading.Event()

    def operation() -> str:
        """阻塞已启动同步 worker，验证取消期间 admission 仍保持占用。"""

        started.set()
        assert release.wait(timeout=2)
        return "completed"

    task = asyncio.create_task(
        assistant_service.run_admin_assistant_worker(
            operation,
            slots=slots,
            dispatcher=dispatcher,
            deadline=time.monotonic() + 2.0,
            error_code="admin_assistant.audit_unavailable",
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.050)
    assert not task.done()
    assert slots.borrowed_tokens == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slots.borrowed_tokens == 0


@pytest.mark.anyio
async def test_client_cancel_after_authentication_still_commits_one_audit_graph(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """可信 actor 建立后取消只终止响应，不得跳过 Query/Audit/AuditLog/event。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    source_started = threading.Event()
    source_release = threading.Event()
    original_reader = assistant_service.read_source_snapshot

    def blocked_reader(*args, **kwargs):
        """暂停真实来源读取，让客户端取消发生在可信 actor 建立之后。"""

        source_started.set()
        assert source_release.wait(timeout=2)
        return original_reader(*args, **kwargs)

    monkeypatch.setattr(assistant_service, "read_source_snapshot", blocked_reader)
    app = _build_app()
    runtime = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            request_task = asyncio.create_task(
                _ask(client, token, "client_cancel", "usage last 7 days")
            )
            assert await asyncio.to_thread(source_started.wait, 1)
            request_task.cancel()
            await asyncio.sleep(0.050)
            assert not request_task.done()
            source_release.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            runtime = _current_execution_runtime(app)
    finally:
        source_release.set()

    connection = connect_database(database_url)
    try:
        graph = connection.execute(
            """
            SELECT query.outcome,query.claim_count,query.source_count,
                   COUNT(DISTINCT audit.assistant_audit_id),
                   COUNT(DISTINCT audit.assistant_event_id),
                   COUNT(DISTINCT log.audit_log_id)
            FROM assistant_queries query
            JOIN assistant_audits audit
              ON audit.tenant_id=query.tenant_id
             AND audit.assistant_query_id=query.assistant_query_id
            JOIN audit_logs log
              ON log.tenant_id=audit.tenant_id AND log.audit_log_id=audit.audit_log_id
            WHERE query.operation_request_id='req_prd077_client_cancel'
            GROUP BY query.outcome,query.claim_count,query.source_count
            """
        ).fetchone()
        assert graph["outcome"] == "answered"
        assert int(graph["claim_count"]) > 0
        assert int(graph["source_count"]) > 0
        assert tuple(graph)[3:] == (1, 1, 1)
    finally:
        connection.close()
        if runtime is not None:
            await assistant_service.clear_admin_assistant_execution_runtime(
                app,
                asyncio.get_running_loop(),
            )


def test_scheduler_wake_ignores_only_closed_loop_race() -> None:
    """is_closed 预检后的标准 RuntimeError 不得把已提交回答反向改成 500。"""

    app = FastAPI()
    calls = 0

    class ClosingLoop:
        """模拟预检仍开放但线程安全调度时已经关闭的事件循环竞态。"""

        def is_closed(self) -> bool:
            """在预检阶段报告开放，以进入真实关闭竞态分支。"""

            return False

        def call_soon_threadsafe(self, _callback) -> None:
            """模拟提交后唤醒 scheduler 时 loop 恰好关闭的标准异常。"""

            nonlocal calls
            calls += 1
            # 仅该标准关闭异常允许 wake helper 忽略，其他异常必须继续暴露。
            raise RuntimeError("Event loop is closed")

    app.state.admin_assistant_wake_event = threading.Event()
    app.state.admin_assistant_event_loop = ClosingLoop()
    assistant_scheduler.wake_admin_assistant_scheduler(app)
    assert calls == 1


@pytest.mark.anyio
async def test_four_query_types_return_tenant_sources_and_content_free_audits(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """四类回答的 claim/recommendation 必须全部指向本次有界来源图。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    cases = (
        ("usage", "usage in the last 7 days", "usage_statistics"),
        ("errors", "explain errors in the last 24 hours", "error_explanation"),
        ("cost", "cost advice for the last 30 days", "cost_advice"),
        ("policy", "explain the current policy", "policy_explanation"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        responses = [await _ask(client, token, suffix, question) for suffix, question, _ in cases]

    for response, (_, _, query_type) in zip(responses, cases, strict=True):
        assert response.status_code == 200, response.text
        _assert_private_response(response)
        body = response.json()
        assert set(body) == {
            "assistant_query_id",
            "assistant_audit_id",
            "query_type",
            "answer",
            "sources",
            "redactions",
            "request_id",
            "trace_id",
        }
        assert body["query_type"] == query_type
        source_ids = {source["source_id"] for source in body["sources"]}
        assert source_ids
        assert all(source["href"].startswith("/#") for source in body["sources"])
        for claim in body["answer"]["claims"]:
            assert set(claim["source_ids"]) <= source_ids
        for recommendation in body["answer"]["recommendations"]:
            assert set(recommendation["source_ids"]) <= source_ids

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT query.query_type,query.outcome,query.model_call_count,
                   query.model_input_tokens,query.model_output_tokens,
                   query.model_cost_cny_micros,query.model_cost_status,
                   audit.assistant_audit_id,log.audit_log_id,log.action,
                   audit.event_dispatch_status
            FROM assistant_queries query
            JOIN assistant_audits audit
              ON audit.tenant_id=query.tenant_id
             AND audit.assistant_query_id=query.assistant_query_id
            JOIN audit_logs log
              ON log.tenant_id=audit.tenant_id
             AND log.audit_log_id=audit.audit_log_id
            WHERE query.operation_request_id LIKE 'req_prd077_%'
            ORDER BY query.query_type
            """
        ).fetchall()
        assert len(rows) == 4
        assert {row["query_type"] for row in rows} == {case[2] for case in cases}
        for row in rows:
            assert row["outcome"] == "answered"
            assert tuple(
                row[key]
                for key in (
                    "model_call_count",
                    "model_input_tokens",
                    "model_output_tokens",
                    "model_cost_cny_micros",
                )
            ) == (0, 0, 0, 0)
            assert row["model_cost_status"] == "not_applicable_local"
            assert row["action"] == "admin_assistant.queried"
            assert row["event_dispatch_status"] == "pending"
        # 专用表只能含元数据列，正文连可选容器列都不存在。
        columns = {
            row[1]
            for table in ("assistant_queries", "assistant_audits")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        assert columns.isdisjoint({"question", "answer", "prompt", "response", "token"})
    finally:
        connection.close()

    # 真实回答已形成 Query/Audit/AuditLog 三类不可变治理事实，down 必须在首个 DDL 前原子阻断。
    with pytest.raises(DbError, match="prd077_down_business_facts_present"):
        rollback_last_migration(database_url)
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM assistant_queries WHERE outcome='answered'"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM assistant_audits WHERE outcome='answered'"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='admin_assistant.queried'"
            ).fetchone()[0]
            == 4
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
async def test_role_boundaries_are_enforced_by_backend_source_permissions(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """finance/security 只读各自来源域，其他三个角色连 base permission 都没有。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        finance = await _login(client, ROLE_USERS["finance_admin"][1], ROLE_PASSWORD)
        security = await _login(client, ROLE_USERS["security_admin"][1], ROLE_PASSWORD)
        restricted = {
            role: await _login(client, ROLE_USERS[role][1], ROLE_PASSWORD)
            for role in ("developer", "viewer", "ops_admin")
        }

        finance_usage = await _ask(client, finance, "finance_usage", "usage last 7 days")
        finance_cost = await _ask(client, finance, "finance_cost", "cost advice last 30 days")
        finance_error = await _ask(client, finance, "finance_error", "explain recent errors")
        finance_policy = await _ask(client, finance, "finance_policy", "explain current policy")
        security_error = await _ask(client, security, "security_error", "explain recent errors")
        security_policy = await _ask(client, security, "security_policy", "explain current policy")
        security_usage = await _ask(client, security, "security_usage", "usage last 7 days")
        security_cost = await _ask(client, security, "security_cost", "cost advice last 30 days")
        base_denied = {
            role: await _ask(client, token, f"{role}_base", "usage last 7 days")
            for role, token in restricted.items()
        }

    for response in (finance_usage, finance_cost, security_error, security_policy):
        assert response.status_code == 200, response.text
    for response in (finance_error, finance_policy, security_usage, security_cost):
        _assert_error(response, "auth.permission_denied", 403)
    for response in base_denied.values():
        _assert_error(response, "auth.permission_denied", 403)


@pytest.mark.anyio
async def test_target_request_id_is_tenant_first_and_cross_tenant_is_404(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同 ID 只读取当前租户；外租户 ID 与不存在 ID 必须保持同形 404。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        local = await _ask(
            client,
            token,
            "tenant_local",
            "explain error for req_prd077_shared in the last 24 hours",
        )
        foreign = await _ask(
            client,
            token,
            "tenant_foreign",
            "explain error for req_prd077_foreign_only in the last 24 hours",
        )
        missing = await _ask(
            client,
            token,
            "tenant_missing",
            "explain error for req_prd077_missing in the last 24 hours",
        )

    assert local.status_code == 200, local.text
    claims = {claim["claim_id"]: claim["value"] for claim in local.json()["answer"]["claims"]}
    assert claims["error_code"] == "gateway.upstream_timeout"
    assert claims["error_reason"]
    assert claims["error_suggestion"]
    _assert_error(foreign, "admin_assistant.source_not_found", 404)
    _assert_error(missing, "admin_assistant.source_not_found", 404)
    assert set(foreign.json()) == set(missing.json())

    connection = connect_database(database_url)
    try:
        failed_targets = connection.execute(
            """
            SELECT target_request_id,outcome,failure_code,claim_count,source_count
            FROM assistant_queries
            WHERE operation_request_id IN(
              'req_prd077_tenant_foreign','req_prd077_tenant_missing'
            ) ORDER BY target_request_id
            """
        ).fetchall()
        assert [tuple(row) for row in failed_targets] == [
            (
                "req_prd077_foreign_only",
                "failed",
                "admin_assistant.source_not_found",
                0,
                0,
            ),
            (
                "req_prd077_missing",
                "failed",
                "admin_assistant.source_not_found",
                0,
                0,
            ),
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_single_request_unknown_and_null_error_codes_never_leave_catalog_boundary(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """指定请求的未知值和 NULL 都映射固定码，诊断字段不得回显数据库 canary。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    canary = "private_provider_response_canary_prd077"
    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM users WHERE email='admin@metahub.local'"
            ).fetchone()[0]
        )
        _insert_request_fact(
            connection,
            tenant_id=tenant_id,
            request_id="req_prd077_unknown_error_value",
            trace_id="trc_prd077_unknown_error_value",
            error_code=canary,
        )
        _insert_request_fact(
            connection,
            tenant_id=tenant_id,
            request_id="req_prd077_null_error_value",
            trace_id="trc_prd077_null_error_value",
            error_code=None,
        )
        connection.commit()
    finally:
        connection.close()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        unknown = await _ask(
            client,
            token,
            "unknown_error_response",
            "explain error for req_prd077_unknown_error_value in the last 24 hours",
        )
        null_value = await _ask(
            client,
            token,
            "null_error_response",
            "explain error for req_prd077_null_error_value in the last 24 hours",
        )

    for response in (unknown, null_value):
        assert response.status_code == 200, response.text
        claims = {
            claim["claim_id"]: claim["value"] for claim in response.json()["answer"]["claims"]
        }
        assert claims["error_code"] == "infra.unhandled"
        assert claims["error_reason"]
        assert claims["error_suggestion"]
        assert canary not in response.text
        assert "unknown_error" not in str(claims["error_reason"])

    # 服务投影也要独立防御被测试桩/未来仓储实现污染的快照，诊断器不得看到原值。
    projected = assistant_service._error_graph({"request_status": "failed", "error_code": canary})
    assert {claim.claim_id: claim.value for claim in projected}["error_code"] == ("infra.unhandled")
    assert canary not in repr(projected)


def test_answer_projection_includes_cost_concentration_and_request_snapshot_facts() -> None:
    """冻结实现后自审补齐的成本集中度和指定请求策略快照投影。"""

    cost_values = {
        "request_count": 10,
        "failure_count": 1,
        "usage_count": 5,
        "estimated_count": 1,
        "gross_debit_cost_cny_micros": 1_000_000,
        "top_cost_model_alias": "default-chat",
        "top_cost_model_cny_micros": 700_000,
        "active_budget_count": 1,
        "open_anomaly_count": 0,
        "model_price_count": 1,
    }
    cost_claims = {
        claim.claim_id: claim.value for claim in assistant_service._cost_graph(cost_values)
    }
    assert cost_claims["gross_debit_cost_cny"] == "1"
    assert cost_claims["top_cost_model"] == "default-chat"
    assert cost_claims["top_cost_model_share"] == "70"
    assert any(
        item.code == "high_model_cost_concentration"
        for item in assistant_service._cost_recommendations(cost_values)
    )

    policy_values = {
        "active_policy_count": 1,
        "priority_collision_count": 0,
        "published_snapshot_version": 4,
        "action_counts": (),
        "request_policy_decision": "allow",
        "request_policy_reason_code": "policy.allowed",
        "request_policy_snapshot_status": "published",
        "request_snapshot_version": 4,
    }
    policy_claims = {
        claim.claim_id: claim.value for claim in assistant_service._policy_graph(policy_values)
    }
    assert policy_claims["request_policy_snapshot_status"] == "published"
    assert policy_claims["request_snapshot_version"] == 4


@pytest.mark.anyio
async def test_auth_and_base_rbac_precede_body_while_raw_wire_fails_closed(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """匿名/缺 base 调用不解析正文，可信 admin 才进入 exact wire 校验。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    invalid_body = b'{"question":"usage last 7 days","question":"duplicate"}'
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        developer = await _login(client, ROLE_USERS["developer"][1], ROLE_PASSWORD)
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        anonymous = await client.post(
            "/admin-assistant/query",
            content=invalid_body,
            headers={"Content-Type": "application/json", "X-Request-ID": "req_prd077_anon"},
        )
        bad_token = await client.post(
            "/admin-assistant/query",
            content=invalid_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer invalid-prd077-token",
                "X-Request-ID": "req_prd077_bad_token",
            },
        )
        base_denied = await client.post(
            "/admin-assistant/query",
            content=invalid_body,
            headers={
                **_headers(developer, "base_before_body"),
                "Content-Type": "application/json",
            },
        )
        duplicate_json = await client.post(
            "/admin-assistant/query",
            content=invalid_body,
            headers={**_headers(admin, "duplicate_json"), "Content-Type": "application/json"},
        )
        idempotency = await client.post(
            "/admin-assistant/query",
            json={"question": "usage last 7 days"},
            headers={**_headers(admin, "idempotency"), "Idempotency-Key": "not-supported"},
        )
        query_string = await client.post(
            "/admin-assistant/query?mode=debug",
            json={"question": "usage last 7 days"},
            headers=_headers(admin, "query_string"),
        )
        duplicate_auth = await client.post(
            "/admin-assistant/query",
            content=json.dumps({"question": "usage last 7 days"}).encode(),
            headers=[
                ("Authorization", f"Bearer {admin}"),
                ("Authorization", f"Bearer {admin}"),
                ("Content-Type", "application/json"),
                ("X-Request-ID", "req_prd077_duplicate_auth"),
            ],
        )

    _assert_error(anonymous, "auth.token_missing", 401)
    _assert_error(bad_token, "auth.token_invalid", 401)
    _assert_error(base_denied, "auth.permission_denied", 403)
    for response in (duplicate_json, idempotency, query_string, duplicate_auth):
        _assert_error(response, "admin_assistant.request_invalid", 422)


def _revoke_role_permission(
    database_url: str,
    role_code: str,
    permission_code: str,
    *,
    sync_metadata: bool,
) -> None:
    """删除真实 grant；可选同步 revision/hash 以分别验证指纹层和 exact-grant 层。"""

    connection = connect_database(database_url)
    try:
        role = connection.execute(
            "SELECT role_id,tenant_id FROM roles WHERE code=? ORDER BY tenant_id LIMIT 1",
            (role_code,),
        ).fetchone()
        assert role is not None
        connection.execute(
            """
            DELETE FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=(
              SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?
            )
            """,
            (role["tenant_id"], role["role_id"], role["tenant_id"], permission_code),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        if sync_metadata:
            remaining = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT permission.code FROM role_permissions relation
                    JOIN permissions permission
                      ON permission.tenant_id=relation.tenant_id
                     AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                    ORDER BY permission.code
                    """,
                    (role["tenant_id"], role["role_id"]),
                ).fetchall()
            )
            connection.execute(
                """
                UPDATE roles
                SET permission_revision=permission_revision+1,matrix_sha256=?,updated_at=?
                WHERE tenant_id=? AND role_id=?
                """,
                (
                    permission_matrix_sha256(remaining),
                    NOW,
                    role["tenant_id"],
                    role["role_id"],
                ),
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ("source_transaction", "final_audit_transaction"))
@pytest.mark.parametrize("sync_metadata", (True, False), ids=("revision", "exact-grant"))
async def test_dynamic_revocation_never_returns_a_late_answer(
    phase: str,
    sync_metadata: bool,
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """来源读取前或回答持久化前撤权，都只落零 claim/source 的拒绝审计。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    original = assistant_service.read_source_snapshot
    revoked = False

    def revoke_around_snapshot(*args, **kwargs):
        """在来源事务前或最终审计事务前精确撤销一个真实来源权限。"""

        nonlocal revoked
        if phase == "source_transaction" and not revoked:
            _revoke_role_permission(
                database_url,
                "finance_admin",
                "billing.usage.read",
                sync_metadata=sync_metadata,
            )
            revoked = True
        snapshot = original(*args, **kwargs)
        if phase == "final_audit_transaction" and not revoked:
            _revoke_role_permission(
                database_url,
                "finance_admin",
                "billing.usage.read",
                sync_metadata=sync_metadata,
            )
            revoked = True
        return snapshot

    monkeypatch.setattr(assistant_service, "read_source_snapshot", revoke_around_snapshot)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, ROLE_USERS["finance_admin"][1], ROLE_PASSWORD)
        suffix = f"revoke_{phase}_{'revision' if sync_metadata else 'exact'}"
        response = await _ask(client, token, suffix, "usage last 7 days")

    _assert_error(response, "auth.permission_denied", 403)
    assert revoked is True
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count,redaction_count
            FROM assistant_queries WHERE operation_request_id=?
            """,
            (f"req_prd077_{suffix}",),
        ).fetchone()
        assert tuple(row) == ("permission_denied", "auth.permission_denied", 0, 0, 0)
    finally:
        connection.close()


@pytest.mark.anyio
async def test_twenty_concurrent_attempts_create_twenty_unique_audit_graphs(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """写 FIFO 超过决策期限时，每个 HTTP attempt 仍形成独立治理事实。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    original_persist = assistant_service.persist_attempt

    def delayed_persist(*args, **kwargs):
        """让二十个串行短写累计超过 1.5 秒，固定复现旧治理 admission 缺口。"""

        time.sleep(0.090)
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(assistant_service, "persist_attempt", delayed_persist)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        responses = await asyncio.gather(
            *(
                _ask(client, token, f"concurrent_{index:02d}", "usage last 7 days")
                for index in range(20)
            )
        )

    # 绝对来源期限允许过载 attempt 固定失败；治理 FIFO 已被确定性拉长到决策期限之外，
    # 二十次调用仍不能因 timeout、短写事务竞争或缺少幂等键而合并、覆盖或丢失。
    assert {response.status_code for response in responses} <= {200, 503}
    successes = [response for response in responses if response.status_code == 200]
    unavailable = [response for response in responses if response.status_code == 503]
    assert successes, [
        (response.status_code, response.json().get("code")) for response in responses
    ]
    for response in unavailable:
        _assert_error(response, "admin_assistant.source_unavailable", 503)
    assert len({response.json()["assistant_query_id"] for response in successes}) == len(successes)
    assert len({response.json()["assistant_audit_id"] for response in successes}) == len(successes)
    connection = connect_database(database_url)
    try:
        graph = connection.execute(
            """
            SELECT COUNT(*) AS query_count,
                   COUNT(DISTINCT query.assistant_query_id) AS unique_queries,
                   COUNT(DISTINCT audit.assistant_audit_id) AS unique_audits,
                   COUNT(DISTINCT audit.assistant_event_id) AS unique_events,
                   COUNT(DISTINCT log.audit_log_id) AS unique_logs
            FROM assistant_queries query
            JOIN assistant_audits audit
              ON audit.tenant_id=query.tenant_id
             AND audit.assistant_query_id=query.assistant_query_id
            JOIN audit_logs log
              ON log.tenant_id=audit.tenant_id AND log.audit_log_id=audit.audit_log_id
            WHERE query.operation_request_id LIKE 'req_prd077_concurrent_%'
            """
        ).fetchone()
        assert tuple(graph) == (20, 20, 20, 20, 20)
    finally:
        connection.close()


def test_error_summary_counts_all_failures_while_limiting_categories_to_five(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """错误分类只显示 Top-5，但总失败数和来源计数必须覆盖第六类以后。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM users WHERE email='admin@metahub.local'"
            ).fetchone()[0]
        )
        start = "2026-08-10T00:00:00Z"
        end = "2026-08-12T00:00:00Z"
        baseline = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM request_logs
                WHERE tenant_id=? AND status='failed'
                  AND julianday(created_at)>=julianday(?)
                  AND julianday(created_at)<julianday(?)
                """,
                (tenant_id, start, end),
            ).fetchone()[0]
        )
        known_codes = (
            "auth.permission_denied",
            "gateway.model_not_found",
            "gateway.upstream_error",
            "gateway.upstream_rate_limited",
            "gateway.upstream_timeout",
            "infra.unhandled",
        )
        assert all(code in ERROR_SPECS for code in known_codes)
        for index, code in enumerate(known_codes):
            _insert_request_fact(
                connection,
                tenant_id=tenant_id,
                request_id=f"req_prd077_error_class_{index}",
                trace_id=f"trc_prd077_error_class_{index}",
                error_code=code,
            )
        connection.commit()

        values, counts = assistant_repository._error_values(
            connection,
            tenant_id,
            None,
            start,
            end,
        )
        top_errors = tuple(values["top_errors"])
        assert len(top_errors) == 5
        assert int(values["failure_count"]) == baseline + 6
        assert counts["request_logs"] == baseline + 6
        assert sum(int(item[1]) for item in top_errors) < int(values["failure_count"])
    finally:
        connection.close()


def test_error_summary_maps_canaries_before_aggregation_and_preserves_total(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """多个未知分类和 NULL 必须先合并成安全桶，Top-N 计数与总失败数保持一致。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    canaries = (
        "private_error_canary_alpha_prd077",
        "private_error_canary_beta_prd077",
        None,
    )
    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM users WHERE email='admin@metahub.local'"
            ).fetchone()[0]
        )
        start = "2026-08-10T00:00:00Z"
        end = "2026-08-12T00:00:00Z"
        baseline = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM request_logs
                WHERE tenant_id=? AND status='failed'
                  AND julianday(created_at)>=julianday(?)
                  AND julianday(created_at)<julianday(?)
                """,
                (tenant_id, start, end),
            ).fetchone()[0]
        )
        for index, value in enumerate(canaries):
            _insert_request_fact(
                connection,
                tenant_id=tenant_id,
                request_id=f"req_prd077_safe_bucket_{index}",
                trace_id=f"trc_prd077_safe_bucket_{index}",
                error_code=value,
            )
        connection.commit()

        values, counts = assistant_repository._error_values(
            connection,
            tenant_id,
            None,
            start,
            end,
        )
        top_errors = dict(values["top_errors"])
        assert set(top_errors) <= set(ERROR_SPECS)
        assert top_errors["infra.unhandled"] == 3
        assert int(values["failure_count"]) == baseline + 3
        assert counts["request_logs"] == baseline + 3
        assert sum(int(count) for count in top_errors.values()) == baseline + 3
        rendered = json.dumps(values, ensure_ascii=True)
        assert all(value is None or value not in rendered for value in canaries)
    finally:
        connection.close()


@pytest.mark.anyio
async def test_final_write_lock_wait_is_async_and_included_in_duration(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """最终写锁等待不阻塞事件循环，并在锁取得后进入持久审计耗时。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    holder = sqlite3.connect(str(database_path), check_same_thread=False)
    lock_acquired = threading.Event()
    original_snapshot_reader = assistant_service.read_source_snapshot

    def snapshot_then_lock(*args, **kwargs):
        """认证和来源读取完成后才占写锁，精确阻塞最终审计事务。"""

        snapshot = original_snapshot_reader(*args, **kwargs)
        holder.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        return snapshot

    monkeypatch.setattr(assistant_service, "read_source_snapshot", snapshot_then_lock)
    app = _build_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)

            async def release_lock() -> None:
                """等待最终写锁被测试 worker 请求后再延迟释放，形成可计时等待。"""

                while not lock_acquired.is_set():
                    await asyncio.sleep(0.005)
                await asyncio.sleep(0.150)
                holder.rollback()

            release = asyncio.create_task(release_lock())
            response = await _ask(client, token, "lock_duration", "usage last 7 days")
            await release
        assert response.status_code == 200, response.text
        connection = connect_database(database_url)
        try:
            duration_ms = int(
                connection.execute(
                    """
                    SELECT query_duration_ms FROM assistant_queries
                    WHERE operation_request_id='req_prd077_lock_duration'
                    """
                ).fetchone()[0]
            )
            assert 100 <= duration_ms < 1_500
        finally:
            connection.close()
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()


@pytest.mark.anyio
async def test_final_write_lock_honors_governance_deadline_without_blocking_loop(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """锁未释放时审计在治理绝对预算内失败关闭，等待期间 event loop 仍可运行。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    holder = sqlite3.connect(str(database_path), check_same_thread=False)
    lock_acquired = threading.Event()
    original_snapshot_reader = assistant_service.read_source_snapshot
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.750)
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.500)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.750)

    def snapshot_then_lock(*args, **kwargs):
        """在来源快照后保持写锁，直到 endpoint 已返回固定审计错误。"""

        snapshot = original_snapshot_reader(*args, **kwargs)
        holder.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        return snapshot

    monkeypatch.setattr(assistant_service, "read_source_snapshot", snapshot_then_lock)
    app = _build_app()
    ticks = 0
    stop_ticker = asyncio.Event()

    async def tick_event_loop() -> None:
        """持续记录事件循环调度次数，证明同步 SQLite 锁等待未阻塞 loop。"""

        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.010)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            ticker = asyncio.create_task(tick_event_loop())
            started = time.monotonic()
            response = await _ask(client, token, "lock_deadline", "usage last 7 days")
            elapsed = time.monotonic() - started
            stop_ticker.set()
            await ticker
        _assert_error(response, "admin_assistant.audit_unavailable", 503)
        assert lock_acquired.is_set()
        assert elapsed < 1.5
        assert ticks >= 5
        connection = connect_database(database_url)
        try:
            assert (
                connection.execute(
                    """
                SELECT COUNT(*) FROM assistant_queries
                WHERE operation_request_id='req_prd077_lock_deadline'
                """
                ).fetchone()[0]
                == 0
            )
        finally:
            connection.close()
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()


@pytest.mark.anyio
async def test_cold_wal_initialization_lock_uses_source_deadline_and_recovers(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首次 WAL 切换遇 writer lock 固定失败，释放后同一 runtime 可重新初始化。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.250)
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.550)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.550)
    holder = sqlite3.connect(str(database_path), check_same_thread=False)
    app = _build_app()
    runtime = None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            holder.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            blocked = await _ask(client, token, "cold_wal_lock", "usage last 7 days")
            elapsed = time.monotonic() - started
            _assert_error(blocked, "admin_assistant.source_unavailable", 503)
            # journal_mode 可立即返回非 WAL，也可等待 busy_timeout；两种都不得越过总预算。
            assert elapsed < 0.700
            runtime = _current_execution_runtime(app)
            assert runtime.read_connection is None
            assert runtime.write_connection is None

            holder.rollback()
            monkeypatch.setattr(
                assistant_service,
                "SOURCE_DEADLINE_SECONDS",
                assistant_service.ADMIN_ASSISTANT_SOURCE_DEADLINE_SECONDS,
            )
            monkeypatch.setattr(
                assistant_service,
                "DECISION_DEADLINE_SECONDS",
                assistant_service.ADMIN_ASSISTANT_ENDPOINT_DEADLINE_SECONDS,
            )
            monkeypatch.setattr(
                assistant_service,
                "GOVERNANCE_DEADLINE_SECONDS",
                assistant_service.ADMIN_ASSISTANT_GOVERNANCE_DEADLINE_SECONDS,
            )
            recovered = await _ask(
                client,
                token,
                "cold_wal_recovered",
                "usage last 7 days",
            )
            assert recovered.status_code == 200, recovered.text
            assert runtime.read_connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert int(runtime.write_connection.execute("PRAGMA synchronous").fetchone()[0]) == 2
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()
        if runtime is not None:
            await assistant_service.clear_admin_assistant_execution_runtime(
                app,
                asyncio.get_running_loop(),
            )

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM assistant_queries
            WHERE operation_request_id='req_prd077_cold_wal_lock'
            """
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM assistant_queries
            WHERE operation_request_id='req_prd077_cold_wal_recovered'
            """
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_initial_authentication_stays_read_only_under_writer_lock(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """warmup 后认证/来源可越过 writer lock，最终审计仍遵守总入口期限。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    database_path = tmp_path / "prd077-control.sqlite3"
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.250)
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.550)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.550)
    holder = sqlite3.connect(str(database_path), check_same_thread=False)
    app = _build_app()
    ticks = 0
    stop_ticker = asyncio.Event()

    async def tick_event_loop() -> None:
        """持续记录事件循环调度次数，证明只读认证不会阻塞当前 loop。"""

        nonlocal ticks
        while not stop_ticker.is_set():
            ticks += 1
            await asyncio.sleep(0.010)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            warmup = await _ask(client, token, "auth_read_warmup", "usage last 7 days")
            assert warmup.status_code == 200, warmup.text
            holder.execute("BEGIN IMMEDIATE")
            ticker = asyncio.create_task(tick_event_loop())
            started = time.monotonic()
            response = await _ask(client, token, "auth_entry_deadline", "usage last 7 days")
            elapsed = time.monotonic() - started
            stop_ticker.set()
            await ticker
        _assert_error(response, "admin_assistant.audit_unavailable", 503)
        assert 0.400 <= elapsed < 0.800
        assert ticks >= 5
    finally:
        if holder.in_transaction:
            holder.rollback()
        holder.close()

    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM assistant_queries
            WHERE operation_request_id='req_prd077_auth_entry_deadline'
            """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_tenant_waiter_does_not_refresh_deadline_and_is_audited(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """tenant owner 排队只到来源期限，失败 duration 从 middleware 入口计入等待。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.250)
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.600)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.600)
    owner = asyncio.Lock()
    await owner.acquire()
    monkeypatch.setattr(
        assistant_service,
        "_tenant_execution_lock",
        lambda _runtime, _tenant_id, *, attempt_owner=None: owner,
    )
    app = _build_app()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            started = time.monotonic()
            response = await _ask(client, token, "tenant_wait_deadline", "usage last 7 days")
            elapsed = time.monotonic() - started
    finally:
        owner.release()

    _assert_error(response, "admin_assistant.source_unavailable", 503)
    assert 0.150 <= elapsed < 0.750
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count,query_duration_ms
            FROM assistant_queries
            WHERE operation_request_id='req_prd077_tenant_wait_deadline'
            """
        ).fetchone()
        assert tuple(row[:4]) == (
            "failed",
            "admin_assistant.source_unavailable",
            0,
            0,
        )
        assert int(row[4]) >= 150
    finally:
        connection.close()


@pytest.mark.anyio
async def test_source_admission_timeout_never_starts_reader_and_remains_auditable(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """source slot 在绝对期限内拿不到时不启动线程，并用独立 write slot 落零事实。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.400)
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.800)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.800)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        warmup = await _ask(client, token, "source_slot_warmup", "usage last 7 days")
        assert warmup.status_code == 200, warmup.text
        runtime = _current_execution_runtime(app)
        authenticated = asyncio.Event()
        continue_body = asyncio.Event()
        calls = 0
        original = assistant_service.read_source_snapshot

        async def pause_after_authentication(_request: Request) -> str:
            """在认证与正文之间暂停请求，使测试可独占后续 read-stage slot。"""

            authenticated.set()
            await continue_body.wait()
            return "usage last 7 days"

        def counted_reader(*args, **kwargs):
            """统计来源 worker 是否被启动，并在允许时委托真实仓储读取。"""

            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(
            assistant_service,
            "read_assistant_question",
            pause_after_authentication,
        )
        monkeypatch.setattr(assistant_service, "read_source_snapshot", counted_reader)
        request_task = asyncio.create_task(
            _ask(client, token, "source_slot_timeout", "usage last 7 days")
        )
        await asyncio.wait_for(authenticated.wait(), timeout=0.5)
        await runtime.source_slots.acquire()
        continue_body.set()
        try:
            response = await asyncio.wait_for(request_task, timeout=1.2)
        finally:
            runtime.source_slots.release()

    _assert_error(response, "admin_assistant.source_unavailable", 503)
    assert calls == 0
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count
            FROM assistant_queries
            WHERE operation_request_id='req_prd077_source_slot_timeout'
            """
        ).fetchone()
        assert tuple(row) == (
            "failed",
            "admin_assistant.source_unavailable",
            0,
            0,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_write_admission_timeout_never_starts_persist_or_leaves_partial_facts(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """write slot 到治理期限仍不可用时固定失败，persist 函数和四表事实都保持为零。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.400)
    monkeypatch.setattr(assistant_service, "DECISION_DEADLINE_SECONDS", 0.700)
    monkeypatch.setattr(assistant_service, "GOVERNANCE_DEADLINE_SECONDS", 0.700)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        warmup = await _ask(client, token, "write_slot_warmup", "usage last 7 days")
        assert warmup.status_code == 200, warmup.text
        runtime = _current_execution_runtime(app)
        await runtime.write_slots.acquire()
        calls = 0
        original = assistant_service.persist_attempt

        def counted_persist(*args, **kwargs):
            """统计最终写 worker 是否被启动，并在允许时委托真实事务。"""

            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(assistant_service, "persist_attempt", counted_persist)
        try:
            request_task = asyncio.create_task(
                _ask(client, token, "write_slot_timeout", "usage last 7 days")
            )
            response = await asyncio.wait_for(request_task, timeout=1.2)
        finally:
            runtime.write_slots.release()

    _assert_error(response, "admin_assistant.audit_unavailable", 503)
    assert calls == 0
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM assistant_queries
            WHERE operation_request_id='req_prd077_write_slot_timeout'
            """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_write_waiter_crosses_decision_deadline_but_commits_before_governance_deadline(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """write slot 持有超过 1.5 秒后释放，回答和完整治理事实仍必须一起提交。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        warmup = await _ask(client, token, "governance_wait_warmup", "usage last 7 days")
        assert warmup.status_code == 200, warmup.text
        runtime = _current_execution_runtime(app)
        await runtime.write_slots.acquire()
        started = time.monotonic()
        request_task = asyncio.create_task(
            _ask(client, token, "governance_wait", "usage last 7 days")
        )
        try:
            await asyncio.sleep(1.600)
            waited_past_decision = not request_task.done()
            queued_writers = runtime.write_slots.statistics().tasks_waiting
        finally:
            runtime.write_slots.release()
        response = await asyncio.wait_for(request_task, timeout=2.5)
        elapsed = time.monotonic() - started

    assert waited_past_decision is True
    assert queued_writers == 1
    assert 1.5 <= elapsed < 5.0
    assert response.status_code == 200, response.text
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count
            FROM assistant_queries
            WHERE operation_request_id='req_prd077_governance_wait'
            """
        ).fetchone()
        assert row is not None
        assert row[0] == "answered"
        assert row[1] is None
        assert int(row[2]) > 0
        assert int(row[3]) > 0
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("suffix", "question", "expected_status", "expected_code"),
    (
        ("background_success", "usage last 7 days", 200, None),
        (
            "background_error",
            "tell me a joke",
            422,
            "admin_assistant.query_unsupported",
        ),
    ),
)
async def test_generic_audit_dispatch_runs_in_background_for_success_and_error(
    suffix: str,
    question: str,
    expected_status: int,
    expected_code: str | None,
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """阻塞通用审计不能阻塞 event loop；成功和 ApiError 都必须先 wake 再派发。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    event_loop_thread = threading.get_ident()
    order: list[str] = []
    dispatch_threads: list[int] = []
    dispatch_request_ids: list[str] = []
    dispatch_started = threading.Event()
    dispatch_release = threading.Event()
    dispatch_finished = threading.Event()

    def wake(_app) -> None:
        """记录 durable source scheduler 的同步唤醒顺序，不执行真实调度。"""

        order.append("wake")

    def dispatch(*_args, **kwargs) -> None:
        """在线程中阻塞通用审计派发，验证 HTTP event loop 仍可服务健康检查。"""

        order.append("dispatch")
        dispatch_threads.append(threading.get_ident())
        dispatch_request_ids.append(str(kwargs["request_id"]))
        dispatch_started.set()
        try:
            assert dispatch_release.wait(timeout=3)
        finally:
            dispatch_finished.set()

    monkeypatch.setattr(assistant_scheduler, "wake_admin_assistant_scheduler", wake)
    monkeypatch.setattr(assistant_service, "dispatch_generic_audit", dispatch)
    app = _build_app()
    transport = ASGITransport(app=app)
    try:
        async with (
            AsyncClient(transport=transport, base_url="http://test") as client,
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as health_client,
        ):
            token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
            started = time.monotonic()
            request_task = asyncio.create_task(_ask(client, token, suffix, question))
            is_started = await asyncio.wait_for(
                asyncio.to_thread(dispatch_started.wait, 2),
                timeout=2.5,
            )
            assert is_started is True
            assert time.monotonic() - started < 2.5
            assert order == ["wake", "dispatch"]
            assert not dispatch_finished.is_set()
            assert not request_task.done()

            health = await asyncio.wait_for(health_client.get("/health"), timeout=1)
            assert health.status_code == 200
            assert not dispatch_finished.is_set()

            dispatch_release.set()
            response = await asyncio.wait_for(request_task, timeout=2)
    finally:
        dispatch_release.set()

    if expected_code is None:
        assert response.status_code == expected_status, response.text
        _assert_private_response(response)
    else:
        _assert_error(response, expected_code, expected_status)
    assert dispatch_finished.is_set()
    assert dispatch_request_ids == [f"req_prd077_{suffix}"]
    assert dispatch_threads and all(value != event_loop_thread for value in dispatch_threads)


@pytest.mark.anyio
async def test_slow_request_body_uses_source_budget_and_keeps_failure_auditable(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """慢正文读取在 1.2 秒子预算处取消，并使用剩余总预算落零事实审计。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        warmup = await _ask(client, token, "slow_body_warmup", "usage last 7 days")
        assert warmup.status_code == 200, warmup.text

        async def slow_body(_request: Request) -> str:
            """模拟超过 source 子预算的正文读取，不保存或回显用户问题。"""

            await asyncio.sleep(1.0)
            return "usage last 7 days"

        monkeypatch.setattr(assistant_service, "SOURCE_DEADLINE_SECONDS", 0.050)
        monkeypatch.setattr(assistant_service, "read_assistant_question", slow_body)
        started = time.monotonic()
        response = await _ask(client, token, "slow_body", "usage last 7 days")
        elapsed = time.monotonic() - started

    _assert_error(response, "admin_assistant.source_unavailable", 503)
    assert elapsed < 1.0
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT outcome,failure_code,claim_count,source_count
            FROM assistant_queries WHERE operation_request_id='req_prd077_slow_body'
            """
        ).fetchone()
        assert tuple(row) == (
            "failed",
            "admin_assistant.source_unavailable",
            0,
            0,
        )
    finally:
        connection.close()


@pytest.mark.anyio
async def test_rate_limit_and_source_failure_are_audited_with_fixed_codes(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """失败请求同样可追踪，且数据库异常不能把内部错误正文返回客户端。"""

    database_url = _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        responses = [
            await _ask(client, token, f"rate_{index:02d}", "usage last 7 days")
            for index in range(31)
        ]
    assert [response.status_code for response in responses[:30]] == [200] * 30
    _assert_error(responses[30], "admin_assistant.rate_limited", 429)

    # 新用例阶段不重建应用，只清限流并把来源读取替换为固定域错误，验证错误脱敏与审计提升。
    _clear_rate_limiter()

    def unavailable(*args, **kwargs):
        """模拟已归一的来源故障，确保响应和审计都只使用固定机器码。"""

        # stub 不附带数据库路径、SQL 或上游正文，专门验证固定错误提升。
        raise ApiError("admin_assistant.source_unavailable")

    monkeypatch.setattr(assistant_service, "read_source_snapshot", unavailable)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        failed = await _ask(client, token, "source_unavailable", "usage last 7 days")
    _assert_error(failed, "admin_assistant.source_unavailable", 503)
    assert "sqlite" not in failed.text.lower()

    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT operation_request_id,outcome,failure_code,claim_count,source_count
            FROM assistant_queries
            WHERE operation_request_id IN(
              'req_prd077_rate_30','req_prd077_source_unavailable'
            ) ORDER BY operation_request_id
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("req_prd077_rate_30", "rate_limited", "admin_assistant.rate_limited", 0, 0),
            (
                "req_prd077_source_unavailable",
                "failed",
                "admin_assistant.source_unavailable",
                0,
                0,
            ),
        ]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_unhandled_assistant_error_keeps_private_headers_and_chain_ids(
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证最外层 500 的私有 Header、链路信封和无异常正文日志合同。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    app = _build_app()
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    event_loop_thread = threading.get_ident()
    marker_threads: list[int] = []
    private_canary = "PRD077_PRIVATE_PROMPT_RESPONSE_CANARY"

    def marker() -> None:
        """记录错误响应 BackgroundTasks 的执行线程，避免同步阻塞 event loop。"""

        marker_threads.append(threading.get_ident())

    async def crash_after_authentication(_request, _context, background_tasks):
        """模拟携带问题/回答正文的未知异常，验证日志异常链同样失败关闭。"""

        # 先登记任务再抛异常，验证 500 handler 从 request.state 接回同一受控容器。
        background_tasks.add_task(marker)
        raise RuntimeError(private_canary)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        monkeypatch.setattr(
            assistant_routes,
            "execute_admin_assistant_query",
            crash_after_authentication,
        )
        response = await _ask(client, token, "unhandled", "usage last 7 days")

    _assert_error(response, "infra.unhandled", 500)
    assert marker_threads and all(value != event_loop_thread for value in marker_threads)
    assert private_canary not in response.text
    failed_records = [
        record
        for record in caplog.records
        if record.name == "metahub.runtime" and record.getMessage() == "request.failed"
    ]
    assert len(failed_records) == 2
    assert all(record.exc_info is None for record in failed_records)
    failed_record = next(record for record in failed_records if record.levelno == logging.ERROR)
    assert failed_record.path == "/admin-assistant/query"
    assert failed_record.error_code == "infra.unhandled"
    assert failed_record.request_id == "req_prd077_unhandled"
    assert failed_record.trace_id == "trc_prd077_unhandled"
    assert private_canary not in "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )


@pytest.mark.anyio
@pytest.mark.parametrize("state_kind", ("callable", "single-task"))
async def test_error_handler_rejects_non_container_background_state(
    state_kind: str,
    prd077_control_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """request.state 即使放入可 await callable，也不能绕过 BackgroundTasks 类型白名单。"""

    _clone_environment(prd077_control_template, monkeypatch, tmp_path)
    calls = 0

    class CallableTrap:
        """模拟 request.state 中攻击者可控的异步 callable，验证类型白名单。"""

        async def __call__(self) -> None:
            """若错误 handler 错误调用陷阱则递增计数，使越界行为可观测。"""

            nonlocal calls
            calls += 1

    def sync_trap() -> None:
        """模拟单个 BackgroundTask 内的同步 callable，不能冒充任务容器。"""

        nonlocal calls
        calls += 1

    trap = CallableTrap() if state_kind == "callable" else BackgroundTask(sync_trap)

    async def fail_with_trap(request, _context, _background_tasks):
        """把非容器陷阱写入 request.state 后抛固定领域错误，验证 handler 拒绝。"""

        setattr(
            request.state,
            assistant_routes.ERROR_RESPONSE_BACKGROUND_TASKS_STATE,
            trap,
        )
        # 固定错误只用于触发统一 handler，不能执行 state 中未知 callable。
        raise ApiError("admin_assistant.source_unavailable")

    app = _build_app()
    monkeypatch.setattr(
        assistant_routes,
        "execute_admin_assistant_query",
        fail_with_trap,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await _ask(client, token, "background_type_trap", "usage last 7 days")

    _assert_error(response, "admin_assistant.source_unavailable", 503)
    assert calls == 0
