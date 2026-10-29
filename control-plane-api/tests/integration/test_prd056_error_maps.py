"""PRD-056 错误归一规则 Control API、内部快照与 Source Outbox 专项测试。

测试只使用 pytest 独占 SQLite 数据库和内存 ASGI 应用。Webhook 边界替换为固定测试桩，
既验证真实租户、Session、RBAC、迁移触发器和事务，也避免任何外部网络或敏感正文落盘。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.auth.routes import router as auth_router
from metahub_control.error_maps import outbox, scheduler
from metahub_control.error_maps import service as error_map_service
from metahub_control.error_maps.routes import router as error_map_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 固定测试身份只存在临时库；密码和内部 Token 不进入规则、Audit、事件或断言快照。
ADMIN_PASSWORD = "Prd056AdminLocal!2026"
DEVELOPER_PASSWORD = "Prd056DeveloperLocal!2026"
VIEWER_PASSWORD = "Prd056ViewerLocal!2026"
OTHER_ADMIN_PASSWORD = "Prd056OtherAdminLocal!2026"
INTERNAL_TOKEN = "prd056-internal-token-0123456789abcdef"
OTHER_TENANT_ID = "tenant_prd056_control_other"
OTHER_TENANT_SLUG = "prd056-control-other"


@dataclass(frozen=True, slots=True)
class ErrorMapFixture:
    """保存专项测试可公开的数据库和 owner ID，不携带任何 Token 或密钥。"""

    database_url: str
    tenant_id: str
    provider_id: str


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> ErrorMapFixture:
    """应用真实迁移和 seed，并创建 developer、viewer 与第二租户管理员。"""

    database_url = _sqlite_url(tmp_path / name)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd056-control-auth-secret")
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        timestamp = "2026-07-29T10:00:00Z"
        connection.executemany(
            """
            INSERT INTO users(
              user_id, tenant_id, email, display_name, password_hash,
              role, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            [
                (
                    "user_prd056_control_developer",
                    seed.tenant_id,
                    "developer-prd056@example.com",
                    "PRD056 Developer",
                    hash_password(DEVELOPER_PASSWORD),
                    "developer",
                    timestamp,
                    timestamp,
                ),
                (
                    "user_prd056_control_viewer",
                    seed.tenant_id,
                    "viewer-prd056@example.com",
                    "PRD056 Viewer",
                    hash_password(VIEWER_PASSWORD),
                    "viewer",
                    timestamp,
                    timestamp,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id, slug, name, service_mode, status, created_at, updated_at
            ) VALUES(?, ?, 'PRD056 其他租户', 'internal_only', 'active', ?, ?)
            """,
            (OTHER_TENANT_ID, OTHER_TENANT_SLUG, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id, tenant_id, email, display_name, password_hash,
              role, status, created_at, updated_at
            ) VALUES(
              'user_prd056_control_other_admin', ?, 'admin-prd056@other.example.com',
              'PRD056 Other Admin', ?, 'admin', 'active', ?, ?
            )
            """,
            (
                OTHER_TENANT_ID,
                hash_password(OTHER_ADMIN_PASSWORD),
                timestamp,
                timestamp,
            ),
        )
        provider = connection.execute(
            """
            SELECT provider_id FROM providers
            WHERE tenant_id=? AND status='active'
            ORDER BY created_at, provider_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert provider is not None
        connection.commit()
        return ErrorMapFixture(
            database_url=database_url,
            tenant_id=seed.tenant_id,
            provider_id=str(provider["provider_id"]),
        )
    finally:
        connection.close()


def _build_app() -> FastAPI:
    """构造带统一中间件、真实认证和错误规则路由的最小 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-056 Error Map Control Test")
    app.include_router(auth_router)
    app.include_router(error_map_router)
    return app


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录指定临时角色，并把 Token 只返回给当前测试调用栈。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只留在当前测试调用栈，不写日志、证据文件或失败快照。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str, trace_id: str | None = None) -> dict[str, str]:
    """构造安全认证与链路头；测试输出不得打印 Authorization 值。"""

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
    }
    # trace_id 仅在用例显式指定时发送，便于验证服务端缺省生成与透传两条路径。
    if trace_id is not None:
        headers["X-Trace-ID"] = trace_id
    return headers


def _rule(
    provider_id: str,
    *,
    priority: int = 500,
    pattern: str = "quota_demo",
) -> dict[str, object]:
    """构造完整 exact-schema 租户规则，显式保留所有可空字段。"""

    return {
        "provider_id": provider_id,
        "priority": priority,
        "http_statuses": [429],
        "http_status_class": None,
        "error_field": "error.code",
        "field_operator": "equals",
        "field_pattern": pattern,
        "message_operator": None,
        "message_pattern": None,
        "output_error_code": "gateway.upstream_quota_exceeded",
        "status": "active",
    }


async def _create_rule(
    client: AsyncClient,
    token: str,
    provider_id: str,
    *,
    request_id: str,
    priority: int,
    pattern: str,
) -> Response:
    """通过真实公共路由创建一条不冲突规则，并返回 HTTP 结果供场景断言。"""

    return await client.post(
        "/error-maps",
        headers=_headers(token, request_id),
        json=_rule(provider_id, priority=priority, pattern=pattern),
    )


def _count(database_url: str, table: str, tenant_id: str) -> int:
    """按可信固定表名统计当前租户行数，避免测试把跨租户数据混入断言。"""

    allowed = {
        "audit_logs",
        "provider_error_map_events",
        "provider_error_map_head_versions",
        "provider_error_map_operations",
        "provider_error_map_versions",
        "provider_error_maps",
    }
    assert table in allowed
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE tenant_id=?",  # noqa: S608
            (tenant_id,),
        ).fetchone()
        return int(row["count"])
    finally:
        connection.close()


def _event_row(database_url: str, event_id: str) -> Any:
    """读取一条 source event 状态；不返回 Webhook Endpoint、密钥或远端正文。"""

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT * FROM provider_error_map_events WHERE provider_error_map_event_id=?",
            (event_id,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _parse_time(value: object) -> datetime:
    """把 SQLite canonical UTC 时间转换为时区感知 datetime，供 lease、退避和终态边界精确断言。"""

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@pytest.mark.anyio
async def test_prd056_rbac_precedes_wire_and_rejected_wire_writes_no_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限流程：developer 只读，viewer/匿名拒绝，畸形 wire 不产生业务事实。"""

    fixture = _prepare_database(monkeypatch, tmp_path, "prd056-rbac.db")
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer-prd056@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "viewer-prd056@example.com", VIEWER_PASSWORD)

        listed = await client.get(
            "/error-maps",
            headers=_headers(developer, "req_prd056_developer_list"),
        )
        developer_denied = await client.post(
            "/error-maps",
            headers={
                **_headers(developer, "req_prd056_developer_denied"),
                "Content-Type": "application/json",
            },
            content=b"{" + b"x" * 20_000,
        )
        viewer_denied = await client.get(
            "/error-maps?unknown=1",
            headers=_headers(viewer, "req_prd056_viewer_denied"),
        )
        anonymous_denied = await client.post(
            "/error-maps",
            headers={"Content-Type": "application/json"},
            content=b"not-json",
        )
        duplicate_request_id = await client.post(
            "/error-maps",
            headers=[
                ("Authorization", f"Bearer {admin}"),
                ("X-Request-ID", "req_prd056_duplicate_a"),
                ("X-Request-ID", "req_prd056_duplicate_b"),
                ("Content-Type", "application/json"),
            ],
            content=json.dumps(_rule(fixture.provider_id)).encode("utf-8"),
        )
        unknown_field = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_unknown_field"),
            json={**_rule(fixture.provider_id), "tenant_id": fixture.tenant_id},
        )

    assert listed.status_code == 200
    assert listed.headers["Cache-Control"] == "no-store"
    assert listed.json()["head_revision"] == 0
    assert listed.json()["request_id"] == "req_prd056_developer_list"
    assert listed.json()["provider_options"]
    assert all("endpoint" not in option for option in listed.json()["provider_options"])
    assert developer_denied.status_code == 403
    assert viewer_denied.status_code == 403
    assert anonymous_denied.status_code == 401
    assert duplicate_request_id.status_code == 422
    assert duplicate_request_id.json()["code"] == "error_map.input_invalid"
    assert unknown_field.status_code == 422
    assert unknown_field.json()["code"] == "error_map.input_invalid"
    for response in (
        developer_denied,
        viewer_denied,
        anonymous_denied,
        duplicate_request_id,
        unknown_field,
    ):
        assert response.json()["request_id"]
        assert response.json()["trace_id"]
    assert _count(fixture.database_url, "provider_error_map_operations", fixture.tenant_id) == 0
    assert _count(fixture.database_url, "provider_error_maps", fixture.tenant_id) == 0


@pytest.mark.anyio
async def test_prd056_mutations_are_atomic_replayable_and_tenant_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主/异常流程：创建、更新、no-op、业务失败和跨租户 404 均保持事务合同。"""

    fixture = _prepare_database(monkeypatch, tmp_path, "prd056-mutations.db")
    # Audit 的外部 Webhook 是提交后 best-effort；本测试只验证已提交 Audit 行，不访问网络。
    monkeypatch.setattr(
        error_map_service,
        "dispatch_audit_recorded",
        lambda *_args, **_kwargs: None,
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        other_admin = await _login(
            client,
            "admin-prd056@other.example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )
        create = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_create", "trc_prd056_create_first"),
            json=_rule(fixture.provider_id),
        )
        replay = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_create", "trc_prd056_create_replay"),
            json=_rule(fixture.provider_id),
        )
        assert create.status_code == replay.status_code == 201
        error_map_id = str(create.json()["error_map"]["error_map_id"])

        changed = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(admin, "req_prd056_update", "trc_prd056_update_first"),
            json={"expected_version": 1, "priority": 510},
        )
        changed_replay = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(admin, "req_prd056_update", "trc_prd056_update_replay"),
            json={"expected_version": 1, "priority": 510},
        )
        noop = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(admin, "req_prd056_noop"),
            json={"expected_version": 2, "priority": 510},
        )
        invalid = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(admin, "req_prd056_invalid_output", "trc_prd056_invalid_first"),
            json={"expected_version": 2, "output_error_code": "gateway.invalid"},
        )
        invalid_replay = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(admin, "req_prd056_invalid_output", "trc_prd056_invalid_replay"),
            json={"expected_version": 2, "output_error_code": "gateway.invalid"},
        )

        # 用运行时常量模拟已到达持久 revision 上限，验证 canonical 意图写 business_error。
        monkeypatch.setattr(error_map_service, "MAX_HEAD_REVISION", 2)
        limited_body = _rule(fixture.provider_id, priority=700, pattern="quota_limit")
        limited = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_head_limit"),
            json=limited_body,
        )
        limited_replay = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_head_limit"),
            json=limited_body,
        )
        request_conflict = await client.post(
            "/error-maps",
            headers=_headers(admin, "req_prd056_head_limit"),
            json={**limited_body, "priority": 701},
        )
        cross_tenant = await client.patch(
            f"/error-maps/{error_map_id}",
            headers=_headers(other_admin, "req_prd056_cross_tenant"),
            json={"expected_version": 2, "priority": 520},
        )

    assert create.json()["changed"] is True
    assert create.json()["idempotent_replay"] is False
    assert create.json()["head_revision"] == 1
    assert replay.json()["error_map"] == create.json()["error_map"]
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["trace_id"] == "trc_prd056_create_replay"
    assert changed.status_code == changed_replay.status_code == 200
    assert changed.json()["error_map"]["version"] == 2
    assert changed.json()["head_revision"] == 2
    assert changed_replay.json()["idempotent_replay"] is True
    assert changed_replay.json()["trace_id"] == "trc_prd056_update_replay"
    assert noop.status_code == 200
    assert noop.json()["changed"] is False
    assert noop.json()["head_revision"] == 2
    assert invalid.status_code == invalid_replay.status_code == 422
    assert invalid.json()["code"] == "error_map.output_code_invalid"
    assert invalid_replay.json()["code"] == "error_map.output_code_invalid"
    assert invalid_replay.json()["trace_id"] == "trc_prd056_invalid_replay"
    assert limited.status_code == limited_replay.status_code == 409
    assert limited.json()["code"] == "error_map.limit_exceeded"
    assert limited_replay.json()["code"] == "error_map.limit_exceeded"
    assert request_conflict.status_code == 409
    assert request_conflict.json()["code"] == "error_map.request_conflict"
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "error_map.not_found"

    assert _count(fixture.database_url, "provider_error_maps", fixture.tenant_id) == 1
    assert _count(fixture.database_url, "provider_error_map_versions", fixture.tenant_id) == 2
    assert _count(fixture.database_url, "provider_error_map_head_versions", fixture.tenant_id) == 2
    assert _count(fixture.database_url, "provider_error_map_events", fixture.tenant_id) == 2
    assert _count(fixture.database_url, "audit_logs", fixture.tenant_id) == 2
    assert _count(fixture.database_url, "provider_error_map_operations", fixture.tenant_id) == 5
    assert _count(fixture.database_url, "provider_error_map_operations", OTHER_TENANT_ID) == 1
    connection = connect_database(fixture.database_url)
    try:
        operations = connection.execute(
            """
            SELECT outcome, error_code, response_snapshot_json
            FROM provider_error_map_operations
            WHERE tenant_id=? ORDER BY created_at, provider_error_map_operation_id
            """,
            (fixture.tenant_id,),
        ).fetchall()
        assert {row["outcome"] for row in operations} == {
            "success",
            "noop",
            "business_error",
        }
        assert {row["error_code"] for row in operations if row["error_code"]} == {
            "error_map.output_code_invalid",
            "error_map.limit_exceeded",
        }
        event_text = "\n".join(
            str(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM provider_error_map_events WHERE tenant_id=?",
                (fixture.tenant_id,),
            )
        )
        audit_text = "\n".join(
            f"{row['before_summary_json']}\n{row['after_summary_json']}"
            for row in connection.execute(
                """
                SELECT before_summary_json, after_summary_json FROM audit_logs
                WHERE tenant_id=? AND object_type='provider_error_map'
                """,
                (fixture.tenant_id,),
            )
        )
        assert "quota_demo" not in event_text
        assert "quota_demo" not in audit_text
    finally:
        connection.close()


@pytest.mark.anyio
async def test_prd056_internal_snapshot_and_source_outbox_lease_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """内部/恢复流程：强 ETag 200/304 与 Outbox lease、退避、十次终态可运行。"""

    fixture = _prepare_database(monkeypatch, tmp_path, "prd056-outbox.db")
    monkeypatch.setattr(
        error_map_service,
        "dispatch_audit_recorded",
        lambda *_args, **_kwargs: None,
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        first = await _create_rule(
            client,
            admin,
            fixture.provider_id,
            request_id="req_prd056_outbox_first",
            priority=501,
            pattern="quota_first",
        )
        assert first.status_code == 201, first.text
        snapshot = await client.get(
            f"/internal/error-maps/{fixture.tenant_id}/current",
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": "req_prd056_snapshot_200",
            },
        )
        etag = snapshot.headers["ETag"]
        not_modified = await client.get(
            f"/internal/error-maps/{fixture.tenant_id}/current",
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "If-None-Match": etag,
                "X-Request-ID": "req_prd056_snapshot_304",
            },
        )
        mixed_identity = await client.get(
            f"/internal/error-maps/{fixture.tenant_id}/current",
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd056_snapshot_mixed",
            },
        )
        unknown_tenant = await client.get(
            "/internal/error-maps/tenant_prd056_missing/current",
            headers={"X-Internal-Token": INTERNAL_TOKEN},
        )

        second = await _create_rule(
            client,
            admin,
            fixture.provider_id,
            request_id="req_prd056_outbox_retry",
            priority=502,
            pattern="quota_retry",
        )
        assert second.status_code == 201

    payload = snapshot.json()
    assert snapshot.status_code == 200
    assert snapshot.headers["Cache-Control"] == "no-store"
    assert etag.startswith('"') and etag.endswith('"') and len(etag) == 66
    assert payload["tenant_id"] == fixture.tenant_id
    assert payload["revision"] == 1
    assert len(payload["rules"]) == 1
    assert payload["request_id"] == "req_prd056_snapshot_200"
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["ETag"] == etag
    assert mixed_identity.status_code == 503
    assert mixed_identity.json()["code"] == "error_map.snapshot_unavailable"
    assert unknown_tenant.status_code == 404
    assert unknown_tenant.json()["code"] == "error_map.tenant_unavailable"

    config = load_app_config("control-plane-api")
    connection = connect_database(fixture.database_url)
    try:
        rows = connection.execute(
            """
            SELECT provider_error_map_event_id, request_id, created_at
            FROM provider_error_map_events WHERE tenant_id=? ORDER BY head_revision
            """,
            (fixture.tenant_id,),
        ).fetchall()
        assert len(rows) == 2
    finally:
        connection.close()
    first_event, retry_event = rows

    delivered: list[Any] = []

    def successful_dispatch(
        _config: Any,
        actual_tenant: str,
        event: Any,
        **kwargs: Any,
    ) -> list[object]:
        """捕获固定安全信封，不访问网络，并验证恢复投递使用稳定内部 request ID。"""

        assert actual_tenant == fixture.tenant_id
        assert kwargs["resume_existing_pending"] is True
        assert kwargs["request_id"] == f"req_{event.event_id}"
        delivered.append(event)
        return []

    monkeypatch.setattr(outbox, "dispatch_webhook_event", successful_dispatch)
    first_due = _parse_time(first_event["created_at"])
    stale = outbox.claim_next_provider_error_map_event(
        config, worker_id="worker-prd056-stale", now=first_due
    )
    assert stale is not None
    recovered = outbox.claim_next_provider_error_map_event(
        config,
        worker_id="worker-prd056-recovered",
        now=stale.event_lease_expires_at,
    )
    assert recovered is not None
    assert recovered.provider_error_map_event_id == first_event["provider_error_map_event_id"]
    assert recovered.event_dispatch_attempts == 2
    assert (
        outbox.dispatch_claimed_provider_error_map_event(
            config, stale, now=stale.event_lease_expires_at + timedelta(seconds=1)
        )
        is False
    )
    assert (
        outbox.dispatch_claimed_provider_error_map_event(
            config,
            recovered,
            now=recovered.event_lease_expires_at - timedelta(seconds=29),
        )
        is True
    )
    assert len(delivered) == 2
    assert {item.event_id for item in delivered} == {first_event["provider_error_map_event_id"]}
    assert "quota_first" not in json.dumps(delivered[-1].payload)
    attempts: list[int] = []

    def failed_dispatch(*_args: Any, **_kwargs: Any) -> list[object]:
        """模拟固定 Webhook 失败，只抛共享错误码而不携带 URL 或响应正文。"""

        attempts.append(1)
        # 固定 ApiError 让退避测试只观察状态机，不把接收端 URL 或响应正文写入失败证据。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(outbox, "dispatch_webhook_event", failed_dispatch)
    due = _parse_time(retry_event["created_at"])
    for expected_attempt in range(1, 11):
        claim = outbox.claim_next_provider_error_map_event(
            config,
            worker_id=f"worker-prd056-retry-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.provider_error_map_event_id == retry_event["provider_error_map_event_id"]
        assert claim.event_dispatch_attempts == expected_attempt
        completed_at = claim.event_lease_expires_at - timedelta(seconds=29)
        assert outbox.dispatch_claimed_provider_error_map_event(config, claim, now=completed_at)
        state = _event_row(fixture.database_url, retry_event["provider_error_map_event_id"])
        if expected_attempt < 10:
            expected_delay = (5, 30, 300)[min(expected_attempt - 1, 2)]
            assert state["dispatch_status"] == "pending"
            due = _parse_time(state["next_attempt_at"])
            assert due - completed_at == timedelta(seconds=expected_delay)
        else:
            assert state["dispatch_status"] == "failed"
            assert state["dispatch_attempts"] == 10
    assert len(attempts) == 10

    # 先让前一个事件完成十次失败，再创建孤儿场景，避免测试夹具扩张生产排序语义。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        third = await _create_rule(
            client,
            admin,
            fixture.provider_id,
            request_id="req_prd056_outbox_orphan",
            priority=503,
            pattern="quota_orphan",
        )
        assert third.status_code == 201
    connection = connect_database(fixture.database_url)
    try:
        orphan_event = connection.execute(
            """
            SELECT provider_error_map_event_id, request_id, created_at
            FROM provider_error_map_events
            WHERE tenant_id=? AND request_id=?
            """,
            (fixture.tenant_id, "req_prd056_outbox_orphan"),
        ).fetchone()
        assert orphan_event is not None
    finally:
        connection.close()

    # 较早 revision 终态后，模拟后续事件每次 claim 即崩溃；第十次 lease 到期只能终结。
    due = _parse_time(orphan_event["created_at"])
    for expected_attempt in range(1, 11):
        claim = outbox.claim_next_provider_error_map_event(
            config,
            worker_id=f"worker-prd056-orphan-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.provider_error_map_event_id == orphan_event["provider_error_map_event_id"]
        assert claim.event_dispatch_attempts == expected_attempt
        due = claim.event_lease_expires_at
    assert (
        outbox.claim_next_provider_error_map_event(
            config, worker_id="worker-prd056-orphan-reaper", now=due
        )
        is None
    )
    orphan = _event_row(fixture.database_url, orphan_event["provider_error_map_event_id"])
    assert orphan["dispatch_status"] == "failed"
    assert orphan["dispatch_attempts"] == 10
    assert orphan["last_error_code"] == "error_map.event_unavailable"
    assert orphan["claim_token"] is None
    assert len(attempts) == 10


@pytest.mark.anyio
async def test_prd056_scheduler_composes_lifespan_and_waits_for_active_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭流程：调度器保留既有 lifespan，并等待已进入的同步投递 tick。"""

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
        """模拟已进入 SQLite/网络边界且不能由 task.cancel 立即中断的工作。"""

        events.append("worker.start")
        started.set()
        assert release.wait(timeout=3)
        events.append("worker.finish")
        return False

    app = FastAPI(lifespan=original_lifespan)
    app.state.config = SimpleNamespace(
        provider_error_map_event_enabled=True,
        provider_error_map_event_poll_interval_seconds=60.0,
    )
    monkeypatch.setattr(scheduler, "run_provider_error_map_event_once", blocking_tick)
    scheduler.install_provider_error_map_scheduler(app)

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


def test_prd056_scheduler_logs_only_fixed_error_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """隐私流程：未知调度异常的路径、URL 和任意正文不得进入日志。"""

    def unsafe_failure(*_args: Any, **_kwargs: Any) -> bool:
        """模拟可能携带数据库路径、Endpoint 与敏感正文的未知异常。"""

        # 私密哨兵只用于证明 scheduler 日志会过滤异常正文，不能传播到断言之外。
        raise RuntimeError("token=secret https://receiver.invalid D:/private.db")

    monkeypatch.setattr(outbox, "drain_provider_error_map_event_once", unsafe_failure)
    with caplog.at_level("WARNING"):
        assert scheduler.run_provider_error_map_event_once(SimpleNamespace()) is False
    text = caplog.text
    assert "provider error map event iteration failed" in text
    assert "token=secret" not in text
    assert "receiver.invalid" not in text
    assert "private.db" not in text
