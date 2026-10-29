"""PRD-057 lease 失权、数据库错误映射与 OpenAPI 合同定向回归测试。

本文件只使用 pytest ``tmp_path`` 下的独立 SQLite 数据库。测试通过真实认证上下文验证
统一错误响应，通过显式时间注入验证过期 lease，禁止访问开发库或进行真实外部出网。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.webhooks import outbox as webhook_outbox
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import DbError, connect_database
from test_prd057_webhook_dead_letters import _create_failed_source
from test_webhooks import (
    DEVELOPER_PASSWORD,
    RunningWebhookServer,
    build_control_app,
    login,
    prepare_database,
)


def _raise_database_connection_error(_database_url: str) -> Any:
    """模拟底层连接失败。

    参数：``_database_url`` 是服务层传入但测试刻意不读取、不输出的数据库地址。
    返回：本函数始终抛出 ``DbError``，用于验证公开响应只包含领域错误码。
    约束：异常消息使用固定占位内容，避免把临时路径或连接参数带入日志和断言。
    """

    # 固定三段错误内容只用于触发服务层折叠逻辑，不能携带真实数据库地址或测试凭据。
    raise DbError(
        "db.connection_failed",
        "private database location",
        "private infrastructure detail",
    )


def _assert_unavailable_response(response: Response, expected_code: str) -> None:
    """断言统一的服务不可用响应。

    参数：``response`` 是 ASGI 请求结果，``expected_code`` 是本业务动作的公开错误码。
    返回：无；断言失败时由 pytest 报告具体合同差异。
    约束：必须同时检查 HTTP 503 与非空 request_id，防止只修正文案却破坏链路追踪。
    """

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == expected_code
    assert body["request_id"]
    # 底层异常文字包含部署细节，统一错误响应不得把该文字回显给调用方。
    assert "private database" not in response.text.lower()
    assert "private infrastructure" not in response.text.lower()


def _read_replay_claim_snapshot(
    database_url: str,
    tenant_id: str,
    dead_letter_event_id: str,
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """读取 lease 根状态与 replay delivery 摘要，供 no-op 前后做精确比较。

    参数：数据库地址、认证租户 ID 和根死信 ID 均来自当前测试的隔离库。
    返回：第一项是 owner/退避/状态快照，第二项是 replay 投递状态与 attempt 计数。
    约束：查询始终带 tenant_id，避免测试助手本身绕过生产代码要求的租户边界。
    """

    connection = connect_database(database_url)
    try:
        dead_letter = connection.execute(
            """
            SELECT status,current_replay_delivery_id,replay_claim_token,
                   replay_lease_owner_id,replay_lease_expires_at,replay_next_attempt_at,
                   last_replay_error_code,replay_dispatch_attempts,updated_at
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND dead_letter_event_id=?
            """,
            (tenant_id, dead_letter_event_id),
        ).fetchone()
        assert dead_letter is not None
        replay = connection.execute(
            """
            SELECT status,attempt_count,http_status,error_code,updated_at
            FROM webhook_deliveries
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (tenant_id, dead_letter["current_replay_delivery_id"]),
        ).fetchone()
        assert replay is not None
        return tuple(dead_letter), tuple(replay)
    finally:
        connection.close()


def test_process_claimed_replay_rechecks_clock_after_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产路径在出网返回后必须重新取时钟，不能用发送前时间提交已过期 lease。"""

    before_dispatch = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    after_expiry = before_dispatch + timedelta(seconds=2)
    clock = iter((before_dispatch, after_expiry))
    observed_load_times: list[datetime] = []
    observed_finish_times: list[datetime] = []

    class _FakeConnection:
        """提供 process finally 所需的最小连接表面，不访问任何真实数据库。"""

        def rollback(self) -> None:
            """异常路径占位；正常测试流程不会调用回滚。"""

        def close(self) -> None:
            """模拟幂等关闭，确保 worker 的 finally 合同仍被执行。"""

    connection = _FakeConnection()
    claim = webhook_outbox.ReplayClaim(
        tenant_id="tenant_prd057_clock",
        dead_letter_event_id=f"whdl_{'c' * 32}",
        replay_delivery_id=f"whd_{'d' * 32}",
        claim_token="claim_token_prd057_clock",
        worker_id="worker_prd057_clock",
        lease_expires_at=before_dispatch + timedelta(seconds=1),
        dispatch_attempt=1,
    )

    def load_owned(
        _connection: Any,
        _claim: webhook_outbox.ReplayClaim,
        *,
        now: datetime,
    ) -> dict[str, str]:
        """记录发送前 owner 校验时间，并返回同租户 Endpoint 摘要。"""

        observed_load_times.append(now)
        return {"webhook_endpoint_id": "wh_prd057_clock"}

    def finish_claim(
        _connection: Any,
        _claim: webhook_outbox.ReplayClaim,
        *,
        replay_succeeded: bool,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        """记录 finish CAS 时间；跨过 lease 后真实 SQL 将返回未命中。"""

        assert replay_succeeded is True
        assert error_code is None
        observed_finish_times.append(now)
        return False

    monkeypatch.setattr(webhook_outbox, "_connect", lambda _config: connection)
    monkeypatch.setattr(webhook_outbox, "_utcnow", lambda: next(clock))
    monkeypatch.setattr(webhook_outbox, "_load_owned_dead_letter", load_owned)
    monkeypatch.setattr(webhook_outbox, "_finish_claim", finish_claim)
    monkeypatch.setattr(
        webhook_service,
        "_load_delivery_row",
        lambda *_args: {"status": "pending"},
    )
    monkeypatch.setattr(
        webhook_service,
        "_resume_pending_delivery",
        lambda *_args, **_kwargs: SimpleNamespace(status="success", error_code=None),
    )

    assert webhook_outbox.process_claimed_replay(object(), claim) is False
    assert observed_load_times == [before_dispatch]
    assert observed_finish_times == [after_expiry]


@pytest.mark.anyio
async def test_expired_replay_lease_cannot_send_finish_or_reschedule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """过期但尚未接管的旧 claim 必须零出网，finish 与退避写入也必须是 no-op。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            _endpoint_id, source = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 过期 lease 失权",
            )
            # API 仅持久化 replay intent，由本测试在确定时间点手工领取，避免后台时序干扰断言。
            monkeypatch.setattr(webhook_service, "_attempt_replay_worker", lambda *_args: None)
            requested = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_expired_lease"},
            )
            assert requested.status_code == 200, requested.text
            dead_letter_id = requested.json()["dead_letter"]["dead_letter_event_id"]

    claim_time = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    claim = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-expired-owner",
        now=claim_time,
        dead_letter_event_id=dead_letter_id,
    )
    assert claim is not None
    expired_time = claim.lease_expires_at + timedelta(seconds=1)
    before = _read_replay_claim_snapshot(database_url, tenant_id, dead_letter_id)
    outbound_delivery_ids: list[str] = []

    def reject_unexpected_send(
        _config: Any,
        _endpoint: Any,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """记录并拒绝任何旧 owner 出网；正确实现不会调用本替身。"""

        outbound_delivery_ids.append(delivery_id)
        # 一旦到达发送原语就说明过期 owner 绕过了 lease 门禁，必须立即终止本用例。
        raise AssertionError("expired replay lease attempted outbound delivery")

    monkeypatch.setattr(webhook_service, "_send_once", reject_unexpected_send)
    assert (
        webhook_outbox.process_claimed_replay(
            app.state.config,
            claim,
            now=expired_time,
        )
        is False
    )

    connection = connect_database(database_url)
    try:
        # 即使 token/owner 仍留在行上，时间边界也属于 CAS 所有权；到期后不得提交终态。
        assert (
            webhook_outbox._finish_claim(
                connection,
                claim,
                replay_succeeded=True,
                error_code=None,
                now=expired_time,
            )
            is False
        )
    finally:
        connection.close()
    # 到期且无人接管的窗口仍由后续 claim_next_replay 负责接管，旧 worker 不得清 lease 抢跑。
    webhook_outbox._reschedule_claim(
        app.state.config,
        claim,
        error_code="webhook.replay_unavailable",
        now=expired_time,
    )

    after = _read_replay_claim_snapshot(database_url, tenant_id, dead_letter_id)
    assert outbound_delivery_ids == []
    assert after == before


@pytest.mark.anyio
async def test_dead_letter_list_and_archive_connection_failures_use_dead_letter_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """列表和归档在数据库连接失败时都返回 dead-letter 专用错误码及 request_id。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        # 只替换 Webhook 服务模块自己的连接入口，认证仍从隔离测试库恢复真实租户和角色。
        monkeypatch.setattr(webhook_service, "connect_database", _raise_database_connection_error)
        headers = {"Authorization": f"Bearer {token}"}
        listed = await client.get("/webhook-dead-letters", headers=headers)
        archived = await client.post(
            f"/webhook-dead-letters/whdl_{'a' * 32}/archive",
            headers=headers,
        )

    _assert_unavailable_response(listed, "webhook.dead_letter_unavailable")
    _assert_unavailable_response(archived, "webhook.dead_letter_unavailable")


@pytest.mark.anyio
async def test_replay_intent_connection_failure_uses_replay_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """创建 replay intent 的首次数据库连接失败必须返回 replay 专用错误码。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        monkeypatch.setattr(webhook_service, "connect_database", _raise_database_connection_error)
        response = await client.post(
            f"/webhook-deliveries/whd_{'b' * 32}/replay",
            headers={"Authorization": f"Bearer {token}"},
            json={"idempotency_key": "console_prd057_connect_intent"},
        )

    _assert_unavailable_response(response, "webhook.replay_unavailable")


@pytest.mark.anyio
async def test_replay_state_readback_connection_failure_uses_replay_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """intent 已提交但响应回读连接失败时返回 replay 专用错误码，并保留 pending 事实。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            _endpoint_id, source = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 replay 回读连接失败",
            )
            real_connect = webhook_service.connect_database
            connection_calls = 0

            def fail_second_webhook_connection(database: str) -> Any:
                """允许 intent 事务连接成功，只让随后 replay 状态回读连接失败。"""

                nonlocal connection_calls
                connection_calls += 1
                if connection_calls == 2:
                    return _raise_database_connection_error(database)
                return real_connect(database)

            # 跳过即时 worker，确保 service._connect 的第二次调用稳定对应 API 响应回读。
            monkeypatch.setattr(webhook_service, "_attempt_replay_worker", lambda *_args: None)
            monkeypatch.setattr(
                webhook_service,
                "connect_database",
                fail_second_webhook_connection,
            )
            response = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_connect_readback"},
            )

    _assert_unavailable_response(response, "webhook.replay_unavailable")
    assert connection_calls == 2
    connection = connect_database(database_url)
    try:
        replay = connection.execute(
            """
            SELECT status,attempt_count FROM webhook_deliveries
            WHERE tenant_id=? AND replayed_from_delivery_id=?
              AND replay_idempotency_key=?
            """,
            (
                tenant_id,
                source["webhook_delivery_id"],
                "console_prd057_connect_readback",
            ),
        ).fetchone()
    finally:
        connection.close()
    # 回读失败不能回滚已经提交的 intent，也不能在响应失败时偷偷尝试出网。
    assert replay is not None
    assert (replay["status"], replay["attempt_count"]) == ("pending", 0)


def test_replay_delivery_openapi_requires_idempotency_key() -> None:
    """OpenAPI 请求体必须把 idempotency_key 声明为必填，和运行时 DTO 保持一致。"""

    schema = build_control_app().openapi()
    request_schema = schema["paths"][
        "/webhook-deliveries/{webhook_delivery_id}/replay"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "idempotency_key" in request_schema["required"]
    assert request_schema["properties"]["idempotency_key"]["type"] == "string"
@pytest.mark.anyio
async def test_terminal_replay_after_crash_is_reconciled_without_redispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """delivery 已终结但根状态未提交时，到期扫描只能收敛状态，不能再次领取出网。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
        # 任何命中都代表恢复路径违反零重发约束，固定异常不包含 payload 或 Endpoint 信息。
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            endpoint_id, source = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 terminal 崩溃收敛",
            )
            # 公开 API 只提交持久 intent；后续由本测试精确控制 claim 与崩溃窗口。
            monkeypatch.setattr(webhook_service, "_attempt_replay_worker", lambda *_args: None)
            requested = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_terminal_crash"},
            )
            assert requested.status_code == 200, requested.text
            dead_letter_event_id = requested.json()["dead_letter"]["dead_letter_event_id"]
            replay_delivery_id = requested.json()["delivery"]["webhook_delivery_id"]

    claim_time = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    old_claim = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-terminal-old-owner",
        now=claim_time,
        dead_letter_event_id=dead_letter_event_id,
    )
    assert old_claim is not None
    connection = connect_database(database_url)
    try:
        endpoint = connection.execute(
            """
            SELECT * FROM webhook_endpoints
            WHERE tenant_id=? AND webhook_endpoint_id=?
            """,
            (tenant_id, endpoint_id),
        ).fetchone()
        assert endpoint is not None
        success_result: dict[str, object] = {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }
        # 模拟 attempt 与 delivery success 已提交，但进程在根状态 CAS 前退出。
        webhook_service._record_attempt(
            connection,
            endpoint,
            replay_delivery_id,
            1,
            success_result,
        )
        webhook_service._update_delivery_after_attempt(
            connection,
            endpoint,
            replay_delivery_id,
            1,
            success_result,
            final=True,
        )
    finally:
        connection.close()

    outbound_delivery_ids: list[str] = []

    def reject_unexpected_send(
        _config: Any,
        _endpoint: Any,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """记录并拒绝崩溃恢复中的任何重复出网；正确路径不会调用本替身。"""

        outbound_delivery_ids.append(delivery_id)
        # 任何命中都表示恢复路径违反零重发约束，固定异常不得包含正文或 Endpoint 信息。
        raise AssertionError("terminal replay was redispatched")

    monkeypatch.setattr(webhook_service, "_send_once", reject_unexpected_send)
    recovered = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-terminal-recovery-owner",
        now=old_claim.lease_expires_at + timedelta(seconds=1),
        dead_letter_event_id=dead_letter_event_id,
    )
    # terminal delivery 只需本地收敛根状态，因此不会返回可执行的网络 claim。
    assert recovered is None
    assert outbound_delivery_ids == []

    connection = connect_database(database_url)
    try:
        dead_letter = connection.execute(
            """
            SELECT status,replay_claim_token,replay_lease_owner_id,replay_lease_expires_at,
                   replay_dispatch_attempts
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND dead_letter_event_id=?
            """,
            (tenant_id, dead_letter_event_id),
        ).fetchone()
        replay = connection.execute(
            """
            SELECT status,attempt_count FROM webhook_deliveries
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (tenant_id, replay_delivery_id),
        ).fetchone()
        assert dead_letter is not None and replay is not None
        assert tuple(dead_letter) == ("replayed", None, None, None, 1)
        assert tuple(replay) == ("success", 1)
        assert (
            webhook_outbox._finish_claim(
                connection,
                old_claim,
                replay_succeeded=False,
                error_code="webhook.http_error",
                now=old_claim.lease_expires_at + timedelta(seconds=2),
            )
            is False
        )
    finally:
        connection.close()
