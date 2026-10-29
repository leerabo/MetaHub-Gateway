"""PRD-057 Webhook 死信、持久重放与 lease 恢复专项集成测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.webhooks import outbox as webhook_outbox
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import connect_database
from metahub_shared.db.security import hash_password
from test_webhooks import (
    DEVELOPER_PASSWORD,
    VIEWER_PASSWORD,
    RunningWebhookServer,
    build_control_app,
    login,
    prepare_database,
    server_url,
)

# 第二租户密码仅用于专项登录夹具，响应和普通日志都不得回显该测试凭据。
OTHER_PASSWORD = "OtherDeveloper!2026"

# PAGE-083 公开响应不能携带内部所有权、正文、Secret、幂等键或 worker 租约字段。
FORBIDDEN_PUBLIC_KEYS = {
    "archived_by",
    "endpoint_url",
    "error_summary",
    "payload",
    "payload_json",
    "replay_claim_token",
    "replay_idempotency_key",
    "replay_lease_expires_at",
    "replay_lease_owner_id",
    "replay_requested_by",
    "replayed_by",
    "secret_version",
    "signing_secret",
    "tenant_id",
}


def _assert_public_dead_letter_response(value: object) -> None:
    """递归验证死信公开 JSON 白名单，防止嵌套 attempt/delivery 重新引入内部字段。"""

    if isinstance(value, list):
        for item in value:
            _assert_public_dead_letter_response(item)
        return
    if not isinstance(value, dict):
        return
    leaked = FORBIDDEN_PUBLIC_KEYS.intersection(value)
    assert not leaked, f"dead-letter response leaked internal keys: {sorted(leaked)}"
    for item in value.values():
        _assert_public_dead_letter_response(item)


async def _create_failed_source(
    client: AsyncClient,
    token: str,
    receiver: Any,
    *,
    name: str,
) -> tuple[str, dict[str, Any]]:
    """经公开 API 创建 Endpoint 和真实最终失败来源，确保死信由迁移 trigger 同事务生成。"""

    created = await client.post(
        "/webhooks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": name,
            "endpoint_url": server_url(receiver, "/fail"),
            "event_types": ["gateway.request_failed"],
            "timeout_ms": 2000,
        },
    )
    assert created.status_code == 200, created.text
    endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
    receiver.signing_secret = created.json()["signing_secret"]
    tested = await client.post(
        f"/webhooks/{endpoint_id}/test",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["delivery"]["status"] == "failed"
    return endpoint_id, tested.json()["delivery"]


def _insert_other_tenant(database_url: str) -> None:
    """插入第二租户开发者，跨租户断言必须通过真实登录态而不是伪造请求字段。"""

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
            VALUES('tenant_prd057_other','prd057-other','PRD057 其他租户',
                   'internal_only','active',datetime('now'),datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            )
            VALUES('user_prd057_other','tenant_prd057_other','other-prd057@example.com',
                   'Other Developer',?,'developer','active',datetime('now'),datetime('now'))
            """,
            (hash_password(OTHER_PASSWORD),),
        )
        connection.commit()
    finally:
        connection.close()


async def _login_other(client: AsyncClient) -> str:
    """登录第二租户开发者并返回 Token，验证服务端从认证上下文恢复 tenant_id。"""

    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "prd057-other",
            "email": "other-prd057@example.com",
            "password": OTHER_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    # 只把 Token 留在测试进程内构造认证头，不打印、不持久化，也不写入失败消息。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_dead_letter_replay_root_idempotency_archive_and_tenant_rbac(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主/异常/权限：失败回根、成功恢复、同键回读、归档、跨租户与 viewer 全部闭环。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control"
        ) as client:
            developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
            endpoint_id, source = await _create_failed_source(
                client,
                developer_token,
                receiver,
                name="PRD057 持久重放",
            )
            listed = await client.get(
                "/webhook-dead-letters",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            assert listed.status_code == 200, listed.text
            assert listed.headers["cache-control"] == "no-store"
            _assert_public_dead_letter_response(listed.json())
            initial_dead = listed.json()["dead_letters"][0]
            dead_letter_id = initial_dead["dead_letter_event_id"]
            assert initial_dead["webhook_delivery_id"] == source["webhook_delivery_id"]
            assert initial_dead["replayable"] is True
            assert initial_dead["allowed_actions"] == ["replay", "archive"]
            assert "payload" not in json.dumps(listed.json(), ensure_ascii=False).lower()

            received_before_failed_replay = len(receiver.received)
            failed_replay = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"idempotency_key": "console_prd057_fail"},
            )
            assert failed_replay.status_code == 200, failed_replay.text
            assert failed_replay.headers["cache-control"] == "no-store"
            _assert_public_dead_letter_response(failed_replay.json())
            assert failed_replay.json()["delivery"]["status"] == "failed"
            assert failed_replay.json()["dead_letter"]["status"] == "dead_letter"
            assert failed_replay.json()["delivery"]["event_id"] == source["event_id"]
            assert "payload" not in failed_replay.json()["delivery"]
            assert len(receiver.received) == received_before_failed_replay + 3

            connection = connect_database(database_url)
            connection.execute(
                """
                UPDATE webhook_endpoints SET endpoint_url=?
                WHERE tenant_id=? AND webhook_endpoint_id=?
                """,
                (server_url(receiver, "/ok"), tenant_id, endpoint_id),
            )
            connection.commit()
            connection.close()
            received_before_success = len(receiver.received)
            successful_replay = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"idempotency_key": "console_prd057_success"},
            )
            duplicate_replay = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"idempotency_key": "console_prd057_success"},
            )
            assert successful_replay.status_code == 200, successful_replay.text
            assert successful_replay.headers["cache-control"] == "no-store"
            _assert_public_dead_letter_response(successful_replay.json())
            assert successful_replay.json()["delivery"]["status"] == "success"
            assert successful_replay.json()["dead_letter"]["status"] == "replayed"
            assert duplicate_replay.status_code == 200, duplicate_replay.text
            assert duplicate_replay.json()["replayed"] is False
            assert (
                duplicate_replay.json()["delivery"]["webhook_delivery_id"]
                == (successful_replay.json()["delivery"]["webhook_delivery_id"])
            )
            assert len(receiver.received) == received_before_success + 1
            assert {item["payload"]["event_id"] for item in receiver.received} == {
                source["event_id"]
            }

            archived = await client.post(
                f"/webhook-dead-letters/{dead_letter_id}/archive",
                headers={"Authorization": f"Bearer {developer_token}"},
            )
            same_key_after_archive = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"idempotency_key": "console_prd057_success"},
            )
            new_key_after_archive = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={"idempotency_key": "console_prd057_after_archive"},
            )
            assert archived.status_code == 200, archived.text
            assert archived.headers["cache-control"] == "no-store"
            _assert_public_dead_letter_response(archived.json())
            assert archived.json()["dead_letter"]["status"] == "archived"
            assert same_key_after_archive.status_code == 200
            assert same_key_after_archive.json()["replayed"] is False
            assert new_key_after_archive.status_code == 409
            assert new_key_after_archive.json()["code"] == "webhook.dead_letter_not_replayable"

            injected = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={
                    "idempotency_key": "console_prd057_injected",
                    "tenant_id": "tenant_other",
                    "payload": {"prompt": "must-not-accept"},
                },
            )
            viewer_list = await client.get(
                "/webhook-dead-letters",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            viewer_replay = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {viewer_token}"},
                json={"idempotency_key": "console_prd057_viewer"},
            )
            viewer_archive = await client.post(
                f"/webhook-dead-letters/{dead_letter_id}/archive",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            anonymous_list = await client.get("/webhook-dead-letters")
            assert injected.status_code == 422
            assert injected.json()["code"] == "infra.validation_failed"
            assert viewer_list.status_code == 403
            assert viewer_replay.status_code == 403
            assert viewer_archive.status_code == 403
            assert anonymous_list.status_code == 401

            _insert_other_tenant(database_url)
            other_token = await _login_other(client)
            other_list = await client.get(
                "/webhook-dead-letters",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            other_replay = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {other_token}"},
                json={"idempotency_key": "console_prd057_other"},
            )
            other_archive = await client.post(
                f"/webhook-dead-letters/{dead_letter_id}/archive",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert other_list.status_code == 200
            assert other_list.json()["dead_letters"] == []
            assert other_replay.status_code == 404
            assert other_replay.json()["code"] == "webhook.dead_letter_not_found"
            assert other_archive.status_code == 404
            assert other_archive.json()["code"] == "webhook.dead_letter_not_found"

    connection = connect_database(database_url)
    try:
        dead_count = connection.execute(
            "SELECT COUNT(*) AS count FROM webhook_dead_letter_events WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        replay_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM webhook_deliveries
            WHERE tenant_id=? AND replayed_from_delivery_id=?
            """,
            (tenant_id, source["webhook_delivery_id"]),
        ).fetchone()
        archive_audit = connection.execute(
            """
            SELECT before_summary_json,after_summary_json FROM audit_logs
            WHERE tenant_id=? AND action='webhook_dead_letter.archived' AND object_id=?
            """,
            (tenant_id, dead_letter_id),
        ).fetchone()
    finally:
        connection.close()
    # 两次失败/成功 replay 始终回写同一个根死信，迁移 trigger 不为 failed replay 建子死信。
    assert int(dead_count["count"]) == 1
    assert int(replay_count["count"]) == 2
    assert archive_audit is not None
    audit_text = archive_audit["before_summary_json"] + archive_audit["after_summary_json"]
    assert "payload" not in audit_text.lower()
    assert "console_prd057" not in audit_text


@pytest.mark.anyio
async def test_replay_crash_after_remote_success_is_taken_over_with_same_event_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """崩溃窗口：远端成功但 attempt 未提交时 lease 到期可接管，重复出网保持原 event_id。"""

    database_url, tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control"
        ) as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            _endpoint_id, source = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 崩溃接管",
            )
            # 本用例显式控制 worker 时序，API 只提交 intent，不在请求线程即时领取。
            monkeypatch.setattr(webhook_service, "_attempt_replay_worker", lambda *_args: None)
            requested = await client.post(
                f"/webhook-deliveries/{source['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_crash"},
            )
            assert requested.status_code == 200, requested.text
            dead_letter_id = requested.json()["dead_letter"]["dead_letter_event_id"]
            replay_delivery_id = requested.json()["delivery"]["webhook_delivery_id"]

    claim_time = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    first_claim = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-worker-first",
        now=claim_time,
        dead_letter_event_id=dead_letter_id,
    )
    assert first_claim is not None
    sent: list[dict[str, Any]] = []

    def send_success(
        _config: Any,
        _endpoint: Any,
        body: bytes,
        seen_delivery_id: str,
    ) -> dict[str, object]:
        """模拟接收方已经成功处理，但本地 attempt 事务尚未提交。"""

        sent.append(
            {
                "delivery_id": seen_delivery_id,
                "payload": json.loads(body.decode("utf-8")),
            }
        )
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    original_record_attempt = webhook_service._record_attempt

    def crash_before_attempt_commit(*_args: Any, **_kwargs: Any) -> None:
        """在远端成功之后模拟进程退出，lease 和 pending intent 必须留在数据库。"""

        # 此异常精确制造远端成功、本地事务未提交窗口，正文不包含任何接收端敏感数据。
        raise RuntimeError("simulated_crash_after_remote_success")

    monkeypatch.setattr(webhook_service, "_send_once", send_success)
    monkeypatch.setattr(webhook_service, "_record_attempt", crash_before_attempt_commit)
    with pytest.raises(RuntimeError, match="simulated_crash_after_remote_success"):
        webhook_outbox.process_claimed_replay(app.state.config, first_claim)

    before_expiry = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-worker-too-early",
        now=first_claim.lease_expires_at - timedelta(seconds=1),
        dead_letter_event_id=dead_letter_id,
    )
    assert before_expiry is None
    takeover_time = first_claim.lease_expires_at + timedelta(seconds=1)
    takeover = webhook_outbox.claim_next_replay(
        app.state.config,
        worker_id="prd057-worker-takeover",
        now=takeover_time,
        dead_letter_event_id=dead_letter_id,
    )
    assert takeover is not None
    assert takeover.claim_token != first_claim.claim_token
    monkeypatch.setattr(webhook_service, "_record_attempt", original_record_attempt)
    assert (
        webhook_outbox.process_claimed_replay(
            app.state.config,
            takeover,
            now=takeover_time,
        )
        is True
    )

    connection = connect_database(database_url)
    try:
        dead = connection.execute(
            """
            SELECT status,replay_dispatch_attempts,current_replay_delivery_id
            FROM webhook_dead_letter_events
            WHERE tenant_id=? AND dead_letter_event_id=?
            """,
            (tenant_id, dead_letter_id),
        ).fetchone()
        replay = connection.execute(
            """
            SELECT status,attempt_count,event_id FROM webhook_deliveries
            WHERE tenant_id=? AND webhook_delivery_id=?
            """,
            (tenant_id, replay_delivery_id),
        ).fetchone()
        # 旧 token 在新 owner 完成后提交必然为 no-op，不能覆盖 replayed 终态。
        stale_finish = webhook_outbox._finish_claim(
            connection,
            first_claim,
            replay_succeeded=False,
            error_code="webhook.http_error",
            now=takeover_time + timedelta(seconds=1),
        )
    finally:
        connection.close()

    assert stale_finish is False
    assert dead["status"] == "replayed"
    assert dead["replay_dispatch_attempts"] == 2
    assert dead["current_replay_delivery_id"] == replay_delivery_id
    assert (replay["status"], replay["attempt_count"]) == ("success", 1)
    assert [item["delivery_id"] for item in sent] == [replay_delivery_id, replay_delivery_id]
    assert {item["payload"]["event_id"] for item in sent} == {source["event_id"]}
    assert replay["event_id"] == source["event_id"]


@pytest.mark.anyio
async def test_dead_letter_seek_cursor_filters_and_fixed_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """列表/过期：seek 分页无重复，非法筛选 422，到期后 replay/archive 都被后端拒绝。"""

    _database_url, _tenant_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    with RunningWebhookServer() as receiver:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://control"
        ) as client:
            token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
            endpoint_a, source_a = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 游标 A",
            )
            _endpoint_b, source_b = await _create_failed_source(
                client,
                token,
                receiver,
                name="PRD057 游标 B",
            )
            first_page = await client.get(
                "/webhook-dead-letters?status=dead_letter&limit=1",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert first_page.status_code == 200, first_page.text
            assert len(first_page.json()["dead_letters"]) == 1
            next_cursor = first_page.json()["next_cursor"]
            assert next_cursor
            second_page = await client.get(
                f"/webhook-dead-letters?status=dead_letter&limit=1&cursor={next_cursor}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert second_page.status_code == 200, second_page.text
            first_id = first_page.json()["dead_letters"][0]["dead_letter_event_id"]
            second_id = second_page.json()["dead_letters"][0]["dead_letter_event_id"]
            assert first_id != second_id
            endpoint_filter = await client.get(
                f"/webhook-dead-letters?webhook_endpoint_id={endpoint_a}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert endpoint_filter.status_code == 200
            assert {
                item["webhook_delivery_id"] for item in endpoint_filter.json()["dead_letters"]
            } == {source_a["webhook_delivery_id"]}
            invalid_status = await client.get(
                "/webhook-dead-letters?status=unknown",
                headers={"Authorization": f"Bearer {token}"},
            )
            invalid_cursor = await client.get(
                "/webhook-dead-letters?cursor=not-valid-base64",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert invalid_status.status_code == 422
            assert invalid_status.json()["code"] == "webhook.dead_letter_filter_invalid"
            assert invalid_cursor.status_code == 422
            assert invalid_cursor.json()["code"] == "webhook.dead_letter_filter_invalid"

            future = datetime.now(UTC).replace(microsecond=0) + timedelta(days=31)
            assert webhook_outbox.expire_dead_letters(app.state.config, now=future) == 2
            expired = await client.get(
                "/webhook-dead-letters?status=expired",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert expired.status_code == 200
            assert len(expired.json()["dead_letters"]) == 2
            assert all(not item["allowed_actions"] for item in expired.json()["dead_letters"])
            expired_replay = await client.post(
                f"/webhook-deliveries/{source_b['webhook_delivery_id']}/replay",
                headers={"Authorization": f"Bearer {token}"},
                json={"idempotency_key": "console_prd057_expired"},
            )
            expired_archive = await client.post(
                f"/webhook-dead-letters/{first_id}/archive",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert expired_replay.status_code == 409
            assert expired_replay.json()["code"] == "webhook.dead_letter_not_replayable"
            assert expired_archive.status_code == 409
            assert expired_archive.json()["code"] == "webhook.dead_letter_not_archivable"
