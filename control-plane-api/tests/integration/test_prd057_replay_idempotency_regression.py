"""PRD-057 replay 幂等纯读取与权限先行专项回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import connect_database
from test_prd057_webhook_dead_letters import _create_failed_source
from test_webhooks import (
    DEVELOPER_PASSWORD,
    VIEWER_PASSWORD,
    RunningWebhookServer,
    build_control_app,
    login,
    prepare_database,
    server_url,
)


@pytest.mark.anyio
async def test_old_idempotency_key_does_not_claim_new_pending_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """幂等隔离：回读旧 replay 时不得领取同一根死信当前的新 pending intent。"""

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
                name="PRD057 幂等领取隔离",
            )
            first = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_old_key"},
            )
            assert first.status_code == 200, first.text
            assert first.json()["delivery"]["status"] == "failed"

            original_worker = webhook_service._attempt_replay_worker
            # 暂停即时 drain，稳定构造 key B 已提交、但尚未被 scheduler 领取的窗口。
            monkeypatch.setattr(
                webhook_service,
                "_attempt_replay_worker",
                lambda *_args: None,
            )
            second = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_new_key"},
            )
            assert second.status_code == 200, second.text
            assert second.json()["delivery"]["status"] == "pending"
            new_replay_delivery_id = second.json()["delivery"]["webhook_delivery_id"]
            received_before_old_read = len(receiver.received)

            monkeypatch.setattr(
                webhook_service,
                "_attempt_replay_worker",
                original_worker,
            )
            old_read = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_old_key"},
            )
            assert old_read.status_code == 200, old_read.text
            assert old_read.json()["replayed"] is False
            assert old_read.json()["delivery"]["status"] == "failed"
            assert len(receiver.received) == received_before_old_read

    connection = connect_database(database_url)
    try:
        dead = connection.execute(
            """
            SELECT status,current_replay_delivery_id,replay_dispatch_attempts,
                   replay_claim_token
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (tenant_id, source["webhook_delivery_id"]),
        ).fetchone()
        pending = connection.execute(
            """
            SELECT status,attempt_count FROM webhook_deliveries
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (tenant_id, new_replay_delivery_id),
        ).fetchone()
    finally:
        connection.close()
    assert dead is not None
    assert pending is not None
    assert dead["status"] == "replay_pending"
    assert dead["current_replay_delivery_id"] == new_replay_delivery_id
    assert dead["replay_dispatch_attempts"] == 0
    assert dead["replay_claim_token"] is None
    assert (pending["status"], pending["attempt_count"]) == ("pending", 0)


@pytest.mark.anyio
async def test_replay_rbac_precedes_malformed_sensitive_body_parsing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限先行：viewer/匿名的畸形敏感正文必须先返回统一 403/401，且不得落库。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            developer = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            viewer = await login(client, "viewer@example.com", VIEWER_PASSWORD)
            _endpoint_id, source = await _create_failed_source(
                client,
                developer,
                receiver,
                name="PRD057 权限先行",
            )
            path = f"/webhook-deliveries/{source['webhook_delivery_id']}/replay"
            sensitive_malformed = b'{"provider_key":"must-not-read"'
            viewer_response = await client.post(
                path,
                content=sensitive_malformed,
                headers={
                    "Authorization": f"Bearer {viewer}",
                    "Content-Type": "application/json",
                },
            )
            anonymous_response = await client.post(
                path,
                content=sensitive_malformed,
                headers={"Content-Type": "application/json"},
            )

    assert viewer_response.status_code == 403
    assert viewer_response.json()["code"] == "auth.permission_denied"
    assert anonymous_response.status_code == 401
    assert anonymous_response.json()["code"] == "auth.token_missing"
    for response in (viewer_response, anonymous_response):
        assert response.json()["request_id"]
        assert "must-not-read" not in response.text

    connection = connect_database(database_url)
    try:
        replay_count = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND replayed_from_delivery_id=?
            """,
            (tenant_id, source["webhook_delivery_id"]),
        ).fetchone()[0]
    finally:
        connection.close()
    assert replay_count == 0


@pytest.mark.anyio
async def test_corrupted_payload_dynamic_prompt_marker_is_never_disclosed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """安全回归：损坏历史信封命中动态 prompt 标记时拒绝出网且不回显标记。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    # 标记每次运行都随临时目录变化，避免测试只对某个硬编码示例形成假阳性。
    sensitive_marker = f"dynamic-prompt-{tmp_path.name}-must-not-leak"
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
                name="PRD057 损坏信封零泄露",
            )
            connection = connect_database(database_url)
            try:
                row = connection.execute(
                    """
                    SELECT payload_json FROM webhook_deliveries
                    WHERE tenant_id=? AND webhook_delivery_id=?
                    """,
                    (tenant_id, source["webhook_delivery_id"]),
                ).fetchone()
                corrupted_envelope = json.loads(row["payload_json"])
                # replay 前必须重新递归检查存量正文；动态敏感值不能进入响应、日志或接收端。
                corrupted_envelope["prompt"] = sensitive_marker
                connection.execute(
                    """
                    UPDATE webhook_deliveries SET payload_json=?
                    WHERE tenant_id=? AND webhook_delivery_id=?
                    """,
                    (
                        json.dumps(corrupted_envelope, ensure_ascii=False),
                        tenant_id,
                        source["webhook_delivery_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE webhook_endpoints SET endpoint_url=?
                    WHERE tenant_id=? AND webhook_endpoint_id=?
                    """,
                    (server_url(receiver, "/ok"), tenant_id, endpoint_id),
                )
                connection.commit()
            finally:
                connection.close()
            received_before = len(receiver.received)
            response = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_corrupted_payload"},
            )

    assert response.status_code == 503
    assert response.json()["code"] == "webhook.replay_unavailable"
    assert response.json()["request_id"]
    assert len(receiver.received) == received_before
    # 普通响应与捕获日志都只允许固定错误码，禁止泄露动态 Prompt 值。
    assert sensitive_marker not in response.text
    assert sensitive_marker not in caplog.text
    connection = connect_database(database_url)
    try:
        replay_count = connection.execute(
            """
            SELECT COUNT(*) FROM webhook_deliveries
            WHERE tenant_id=? AND replayed_from_delivery_id=?
            """,
            (tenant_id, source["webhook_delivery_id"]),
        ).fetchone()[0]
    finally:
        connection.close()
    assert replay_count == 0


@pytest.mark.anyio
async def test_authorized_replay_body_is_bounded_and_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wire 边界：manager 正文也必须拒绝超限、重复键、非标准常量和非法 UTF-8。"""

    database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    dynamic_marker = f"wire-sensitive-{tmp_path.name}-must-not-leak"
    duplicate_body = (
        b'{"idempotency_key":"console_prd057_wire_a",'
        b'"idempotency_key":"console_prd057_wire_b"}'
    )
    oversized_body = (
        b'{"idempotency_key":"console_prd057_wire_limit","padding":"'
        + dynamic_marker.encode("utf-8")
        + (b"x" * 4096)
        + b'"}'
    )
    cases = [
        duplicate_body,
        b'{"idempotency_key":NaN}',
        b'{"idempotency_key":"console_prd057_wire_utf8","note":"\xff"}',
        oversized_body,
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        responses = []
        for body in cases:
            responses.append(
                await client.post(
                    "/webhook-deliveries/whd_0123456789abcdef0123456789abcdef/replay",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    content=body,
                )
            )

    for response in responses:
        assert response.status_code == 422
        assert response.json()["code"] == "infra.validation_failed"
        assert response.json()["request_id"]
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert dynamic_marker not in response.text
    assert dynamic_marker not in caplog.text
    connection = connect_database(database_url)
    try:
        replay_count = connection.execute(
            "SELECT COUNT(*) FROM webhook_deliveries WHERE delivery_kind='replay'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert replay_count == 0


@pytest.mark.anyio
async def test_replay_delivery_id_over_limit_uses_uniform_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """资源边界：97 字符 delivery ID 与不存在、跨租户资源共享 PRD-057 统一 404。"""

    _database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.post(
            f"/webhook-deliveries/{'d' * 97}/replay",
            headers={"Authorization": f"Bearer {token}"},
            json={"idempotency_key": "console_prd057_long_delivery_id"},
        )

    assert response.status_code == 404
    assert response.json()["code"] == "webhook.dead_letter_not_found"
    assert response.json()["request_id"]
    assert response.headers["x-request-id"] == response.json()["request_id"]


@pytest.mark.anyio
async def test_dead_letter_list_header_and_body_request_id_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """链路合同：死信列表响应头和正文必须回显同一个经过中间件规范化的 request_id。"""

    _database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    requested_id = "req_prd057_list_header_body_0001"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        response = await client.get(
            "/webhook-dead-letters",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": requested_id,
            },
        )

    assert response.status_code == 200
    assert response.json()["request_id"] == requested_id
    assert response.headers["x-request-id"] == requested_id
