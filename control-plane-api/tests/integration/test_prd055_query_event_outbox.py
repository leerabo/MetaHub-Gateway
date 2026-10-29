"""PRD-055 查询事件 source outbox、lease/CAS 和调度器生命周期专项测试。

测试应用真实迁移、租户/用户/Session 外键与 0045 状态触发器。网络边界仅替换公共
dispatcher，既核验稳定签名信封，又保证专项测试不访问任何外部地址。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from metahub_control.request_logs import outbox, scheduler
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

# 固定时间与虚构身份只存在测试数据库，不包含 filters 值、密钥或真实企业数据。
EVENT_TIME = "2026-07-28T03:00:00Z"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换成项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str, str]:
    """应用全部迁移，并创建 QueryEvent 所需的同租户用户与 active Session。"""

    database_url = _sqlite_url(tmp_path / "prd055-query-outbox.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "Prd055LocalAdmin!2026")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd055-outbox-test-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        user = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? ORDER BY created_at LIMIT 1",
            (seed.tenant_id,),
        ).fetchone()
        assert user is not None
        user_id = str(user["user_id"])
        session_id = "ses_prd055_query_outbox"
        connection.execute(
            """
            INSERT INTO sessions(
              session_id, tenant_id, user_id, token_jti, token_hash, status,
              expires_at, created_at, updated_at
            ) VALUES(?, ?, ?, 'jti-prd055-query-outbox',
                     'sha256-prd055-query-outbox', 'active',
                     '2026-07-29T03:00:00Z', '2026-07-28T02:59:00Z',
                     '2026-07-28T02:59:00Z')
            """,
            (session_id, seed.tenant_id, user_id),
        )
        connection.commit()
    finally:
        connection.close()
    # 返回的仅是测试数据库 URL 与稳定归属 ID，不包含 Session Token 或任何业务正文。
    return database_url, seed.tenant_id, user_id, session_id


def _insert_query_event(
    database_url: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    *,
    suffix: str,
) -> tuple[str, str]:
    """插入一条合法 pending 查询事实；字段值摘要只使用固定测试指纹。"""

    query_event_id = "lqe_" + suffix * 32
    event_id = "lqevt_" + suffix * 32
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO request_log_query_events(
              query_event_id, event_id, tenant_id, actor_user_id, actor_session_id,
              filter_version, filter_fingerprint, filter_keys_json, saved_filter_id,
              sort_by, sort_direction, query_limit, query_offset, matched_count,
              returned_count, has_more, query_duration_ms, operation_request_id,
              operation_trace_id, event_dispatch_status, event_dispatch_attempts,
              event_next_attempt_at, executed_at, created_at, updated_at
            ) VALUES(
              ?, ?, ?, ?, ?, 1, ?, '["model","status","terminal_stage"]', NULL,
              'created_at', 'desc', 20, 20, 41, 20, 1, 7,
              ?, ?, 'pending', 0, ?, ?, ?, ?
            )
            """,
            (
                query_event_id,
                event_id,
                tenant_id,
                user_id,
                session_id,
                suffix * 64,
                f"req_prd055_query_{suffix}",
                f"trc_prd055_query_{suffix}",
                EVENT_TIME,
                EVENT_TIME,
                EVENT_TIME,
                EVENT_TIME,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return query_event_id, event_id


def _row(database_url: str, query_event_id: str) -> Any:
    """读取单条 source 状态，测试不查询 Webhook payload 或 Endpoint 数据。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT * FROM request_log_query_events WHERE query_event_id=?",
            (query_event_id,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _parse_time(value: object) -> datetime:
    """把数据库 canonical UTC 时间转换成时区感知 datetime 供 lease 断言使用。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def test_prd055_query_outbox_stable_event_backoff_and_tenth_lease_reaper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证稳定事件、lease 接管、CAS、退避、十次终态和崩溃恢复。"""

    database_url, tenant_id, user_id, session_id = _prepare_database(
        monkeypatch, tmp_path
    )
    config = load_app_config("control-plane-api")
    query_event_id, event_id = _insert_query_event(
        database_url, tenant_id, user_id, session_id, suffix="a"
    )
    captured: list[Any] = []

    def successful_dispatch(_config: Any, actual_tenant: str, event: Any, **kwargs: Any):
        """捕获安全信封而不访问网络，并核对恢复投递使用稳定内部请求 ID。"""

        assert actual_tenant == tenant_id
        assert kwargs["resume_existing_pending"] is True
        assert kwargs["request_id"] == f"req_{event_id}"
        captured.append(event)
        return []

    monkeypatch.setattr(outbox, "dispatch_webhook_event", successful_dispatch)
    due = _parse_time(_row(database_url, query_event_id)["event_next_attempt_at"])
    stale_claim = outbox.claim_next_request_log_query_event(
        config, worker_id="worker-prd055-stale", now=due
    )
    assert stale_claim is not None
    assert stale_claim.event_dispatch_attempts == 1
    recovered_claim = outbox.claim_next_request_log_query_event(
        config,
        worker_id="worker-prd055-recovered",
        now=stale_claim.event_lease_expires_at,
    )
    assert recovered_claim is not None
    assert recovered_claim.query_event_id == query_event_id
    assert recovered_claim.event_id == event_id
    assert recovered_claim.event_dispatch_attempts == 2

    # 旧 worker 即使已完成远端调用也不能覆盖新 owner；at-least-once 允许同 event_id 重复到达。
    assert outbox.dispatch_claimed_request_log_query_event(
        config,
        stale_claim,
        now=stale_claim.event_lease_expires_at + timedelta(seconds=1),
    ) is False
    assert outbox.dispatch_claimed_request_log_query_event(
        config,
        recovered_claim,
        now=recovered_claim.event_lease_expires_at - timedelta(seconds=29),
    ) is True
    assert len(captured) == 2
    assert {event.event_id for event in captured} == {event_id}
    payload = captured[-1].payload
    assert payload["filter_keys"] == ["model", "status", "terminal_stage"]
    assert payload["has_more"] is True
    assert payload["offset"] + payload["returned_count"] < payload["matched_count"]
    assert "filters" not in payload and "filter_json" not in payload
    assert _row(database_url, query_event_id)["event_dispatch_status"] == "dispatched"

    def failed_dispatch(*_args: Any, **_kwargs: Any):
        """模拟公共 dispatcher 固定失败，不携带 URL 或远端响应正文。"""

        # 只抛共享固定 code，确保测试不会把任意异常文本误当生产持久错误。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(outbox, "dispatch_webhook_event", failed_dispatch)
    retry_id, retry_event_id = _insert_query_event(
        database_url, tenant_id, user_id, session_id, suffix="b"
    )
    due = _parse_time(_row(database_url, retry_id)["event_next_attempt_at"])
    for expected_attempt in range(1, 11):
        claim = outbox.claim_next_request_log_query_event(
            config,
            worker_id=f"worker-prd055-retry-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.event_dispatch_attempts == expected_attempt
        assert claim.event_id == retry_event_id
        completed_at = claim.event_lease_expires_at - timedelta(seconds=29)
        assert outbox.dispatch_claimed_request_log_query_event(
            config, claim, now=completed_at
        ) is True
        state = _row(database_url, retry_id)
        if expected_attempt < 10:
            expected_delay = (5, 30, 300)[min(expected_attempt - 1, 2)]
            assert state["event_dispatch_status"] == "pending"
            due = _parse_time(state["event_next_attempt_at"])
            assert due - completed_at == timedelta(seconds=expected_delay)
        else:
            assert state["event_dispatch_status"] == "failed"
            assert state["event_dispatch_attempts"] == 10
            assert state["event_last_error_code"] == "webhook.delivery_unavailable"

    # 第三个事实模拟每次 claim 后都在 CAS 前崩溃；第十次 lease 到期只做终结，不再外呼。
    orphan_id, orphan_event_id = _insert_query_event(
        database_url, tenant_id, user_id, session_id, suffix="c"
    )
    due = _parse_time(_row(database_url, orphan_id)["event_next_attempt_at"])
    for expected_attempt in range(1, 11):
        claim = outbox.claim_next_request_log_query_event(
            config,
            worker_id=f"worker-prd055-orphan-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.event_id == orphan_event_id
        assert claim.event_dispatch_attempts == expected_attempt
        due = claim.event_lease_expires_at
    assert outbox.claim_next_request_log_query_event(
        config, worker_id="worker-prd055-orphan-reaper", now=due
    ) is None
    orphan = _row(database_url, orphan_id)
    assert orphan["event_dispatch_status"] == "failed"
    assert orphan["event_dispatch_attempts"] == 10
    assert orphan["event_last_error_code"] == "request_log.query_event_unavailable"
    assert orphan["event_claim_token"] is None
    assert orphan["event_lease_owner_id"] is None
    assert orphan["event_lease_expires_at"] is None


def test_prd055_corrupted_filter_keys_roll_back_claim_and_close_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """模拟物理坏行，验证映射失败回滚 claim 且只返回固定 source 错误码。"""

    database_url, tenant_id, user_id, session_id = _prepare_database(
        monkeypatch, tmp_path
    )
    query_event_id, _ = _insert_query_event(
        database_url, tenant_id, user_id, session_id, suffix="d"
    )
    connection = connect_database(database_url)
    try:
        # 测试临时绕过 CHECK/不可变触发器等价模拟磁盘或旧版本造成的坏行；生产迁移仍会
        # 拒绝该写入。worker 连接也仅在本用例开启 ignore_check_constraints，以便覆盖
        # “状态更新成功后 DTO 映射失败必须整体回滚”的事务边界。
        connection.execute("DROP TRIGGER trg_request_log_query_events_state_update_prd055")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE request_log_query_events SET filter_keys_json='{' WHERE query_event_id=?",
            (query_event_id,),
        )
        connection.commit()
    finally:
        connection.close()

    original_connect = outbox._connect

    def corrupted_connect(config: Any):
        """创建真实 worker 连接，只为物理坏行专项继续忽略表级 CHECK。"""

        worker_connection = original_connect(config)
        worker_connection.execute("PRAGMA ignore_check_constraints=ON")
        return worker_connection

    monkeypatch.setattr(outbox, "_connect", corrupted_connect)
    with pytest.raises(ApiError) as failure:
        outbox.claim_next_request_log_query_event(
            load_app_config("control-plane-api"),
            worker_id="worker-prd055-corrupt-json",
            now=datetime(2026, 7, 28, 3, tzinfo=UTC),
        )
    assert failure.value.code == "request_log.query_event_unavailable"
    state = _row(database_url, query_event_id)
    assert state["event_dispatch_status"] == "pending"
    assert state["event_dispatch_attempts"] == 0

    connection = connect_database(database_url)
    try:
        # 能立即取得 EXCLUSIVE 锁，证明异常路径已回滚并关闭 worker 连接。
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd055_scheduler_composes_lifespan_and_waits_for_active_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 scheduler 不覆盖既有 lifespan，关闭时等待已开始的同步 tick。"""

    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    @asynccontextmanager
    async def original_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """记录被组合生命周期的进入和退出顺序。"""

        events.append("original.enter")
        try:
            yield
        finally:
            events.append("original.exit")

    def blocking_tick(_config: Any) -> bool:
        """模拟已进入 SQLite/网络边界且不能用 task.cancel 中断的一轮工作。"""

        events.append("worker.start")
        started.set()
        assert release.wait(timeout=3)
        events.append("worker.finish")
        return False

    app = FastAPI(lifespan=original_lifespan)
    app.state.config = SimpleNamespace(
        request_log_query_event_enabled=True,
        request_log_query_event_poll_interval_seconds=60.0,
    )
    monkeypatch.setattr(scheduler, "run_request_log_query_event_once", blocking_tick)
    scheduler.install_request_log_query_event_scheduler(app)

    manager = app.router.lifespan_context(app)
    await manager.__aenter__()
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
    await asyncio.sleep(0.02)
    assert exit_task.done() is False
    assert "original.exit" not in events
    release.set()
    await asyncio.wait_for(exit_task, timeout=2)
    assert events == [
        "original.enter",
        "worker.start",
        "worker.finish",
        "original.exit",
    ]


def test_prd055_scheduler_logs_only_fixed_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """调度迭代失败日志不得包含数据库路径、filters、URL 或任意异常正文。"""

    def unsafe_failure(*_args: Any, **_kwargs: Any) -> bool:
        """模拟可能携带敏感正文的未知底层异常。"""

        # 虚构敏感文本仅用于证明 scheduler 丢弃异常正文，不能出现在日志断言之外。
        raise RuntimeError("filters=secret https://receiver.invalid D:/private.db")

    monkeypatch.setattr(scheduler, "drain_request_log_query_event_once", unsafe_failure)
    with caplog.at_level("WARNING"):
        assert scheduler.run_request_log_query_event_once(SimpleNamespace()) is False
    text = caplog.text
    assert "request log query event iteration failed" in text
    assert "filters=secret" not in text
    assert "receiver.invalid" not in text
    assert "private.db" not in text
