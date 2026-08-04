"""PRD-061 Provider 状态页主流程、租户、权限、异常和敏感信息集成测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.provider_status.routes import router as provider_status_router
from metahub_control.providers.routes import router as providers_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.fastapi_app import create_service_app

# 三种测试身份覆盖双权限允许、普通只读拒绝和管理员维护备注写入边界。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def _db_time(value: datetime) -> str:
    """把受控测试时间转换为运行时使用的 UTC 毫秒格式。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str, str]:
    """创建最新迁移数据库，并返回 URL、租户、Provider 与模型 ID。"""

    database_url = f"sqlite:///{(tmp_path / 'provider-status.db').as_posix()}"
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-provider-status-secret")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "status_dev",
                seed.tenant_id,
                "status-dev@example.com",
                "Status Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                _db_time(datetime.now(UTC)),
                _db_time(datetime.now(UTC)),
            ),
            (
                "status_viewer",
                seed.tenant_id,
                "status-viewer@example.com",
                "Status Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                _db_time(datetime.now(UTC)),
                _db_time(datetime.now(UTC)),
            ),
        ],
    )
    provider = connection.execute(
        "SELECT provider_id, provider_code FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND provider_id=? ORDER BY model_id LIMIT 1",
        (seed.tenant_id, provider["provider_id"]),
    ).fetchone()
    assert provider is not None and model is not None
    now = datetime.now(UTC)
    connection.execute(
        """
        UPDATE providers
        SET health_status='healthy', consecutive_probe_failures=0,
            health_updated_at=?, last_tested_at=?, last_probe_latency_ms=42
        WHERE tenant_id=? AND provider_id=?
        """,
        (_db_time(now), _db_time(now), seed.tenant_id, provider["provider_id"]),
    )
    connection.commit()
    # 返回值只含本租户固定标识，测试绝不从客户端构造或覆盖 tenant_id。
    return database_url, seed.tenant_id, provider["provider_id"], model["model_id"]


def _build_app():
    """构造只包含认证、Provider 管理和状态路由的轻量测试应用。"""

    app = create_service_app("control-plane-api", "Provider Status Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(providers_router)
    app.include_router(provider_status_router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    """登录固定测试角色并返回仅在内存使用的 Bearer Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只保留在当前测试进程内存，不能写入日志、事件或测试证据文件。
    return response.json()["access_token"]


def _insert_call_and_probe_facts(
    database_url: str,
    tenant_id: str,
    provider_id: str,
    model_id: str,
    now: datetime,
) -> None:
    """写入受控调用与探测事实，覆盖资格矩阵、P95 和完整探测分组。"""

    connection = connect_database(database_url)
    provider_code = connection.execute(
        "SELECT provider_code FROM providers WHERE tenant_id=? AND provider_id=?",
        (tenant_id, provider_id),
    ).fetchone()["provider_code"]
    attempts = [
        (
            "ra_status_success",
            "req_status_success",
            "trace_status_success",
            "succeeded",
            None,
            None,
            100,
            "recorded",
            now - timedelta(minutes=50),
        ),
        (
            "ra_status_timeout",
            "req_status_timeout",
            "trace_status_timeout",
            "failed",
            "gateway.upstream_transport_failed",
            "timeout",
            300,
            "unavailable",
            now - timedelta(minutes=40),
        ),
        (
            "ra_status_auth",
            "req_status_auth",
            "trace_status_auth",
            "failed",
            "gateway.upstream_auth_failed",
            "other",
            50,
            "not_applicable",
            now - timedelta(minutes=30),
        ),
    ]
    for sequence, values in enumerate(attempts, start=1):
        attempt_id, request_id, trace_id, status, error_code, category, latency, usage, completed = values
        connection.execute(
            """
            INSERT INTO routing_attempts(
              routing_attempt_id, tenant_id, request_id, trace_id, attempt_sequence,
              attempt_role, trigger_type, model_id, internal_model_id, provider_id,
              provider_code, status, error_code, error_category, retryable, latency_ms,
              usage_status, created_at, completed_at, updated_at
            ) VALUES(?, ?, ?, ?, 1, 'primary', 'initial', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                tenant_id,
                request_id,
                trace_id,
                model_id,
                model_id,
                provider_id,
                provider_code,
                status,
                error_code,
                category,
                latency,
                usage,
                _db_time(completed - timedelta(milliseconds=latency)),
                _db_time(completed),
                _db_time(completed),
            ),
        )

    stream_rows = [
        (
            "ss_status_completed",
            "req_stream_completed",
            "trace_stream_completed",
            "completed",
            None,
            now - timedelta(minutes=20),
            now - timedelta(minutes=20) + timedelta(milliseconds=150),
            now - timedelta(minutes=19),
        ),
        (
            "ss_status_timeout",
            "req_stream_timeout",
            "trace_stream_timeout",
            "timed_out",
            "gateway.upstream_stream_timeout",
            now - timedelta(minutes=10),
            None,
            now - timedelta(minutes=9),
        ),
    ]
    for session_id, request_id, trace_id, status, error_code, started, first_chunk, completed in stream_rows:
        connection.execute(
            """
            INSERT INTO stream_sessions(
              stream_session_id, tenant_id, request_id, trace_id, model_id, provider_id,
              provider_code, status, inter_token_timeout_ms, chunk_count, bytes_sent,
              first_chunk_at, last_chunk_at, error_code, started_at, completed_at, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 30000, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                tenant_id,
                request_id,
                trace_id,
                model_id,
                provider_id,
                provider_code,
                status,
                1 if first_chunk else 0,
                32 if first_chunk else 0,
                _db_time(first_chunk) if first_chunk else None,
                _db_time(first_chunk) if first_chunk else None,
                error_code,
                _db_time(started),
                _db_time(completed),
                _db_time(started),
                _db_time(completed),
            ),
        )

    probe_rows = [
        (
            "probe_status_bad_key",
            "probe_group_success",
            "trace_probe_success",
            1,
            "failed",
            "authentication_failed",
            20,
            now - timedelta(minutes=8),
            now - timedelta(minutes=6),
        ),
        (
            "probe_status_success",
            "probe_group_success",
            "trace_probe_success",
            2,
            "success",
            None,
            80,
            now - timedelta(minutes=7),
            now - timedelta(minutes=6),
        ),
        (
            "probe_status_failure",
            "probe_group_failure",
            "trace_probe_failure",
            1,
            "failed",
            "upstream_5xx",
            400,
            now - timedelta(minutes=6),
            now - timedelta(minutes=5),
        ),
    ]
    # 正式探测服务在单个事务中为同组所有 Key attempt 写入相同 created_at；
    # checked_at 仍保留各次上游请求的独立时间，避免测试把持久化时间误当探测耗时。
    for (
        log_id,
        request_id,
        trace_id,
        sequence,
        result,
        error_code,
        latency,
        checked_at,
        created_at,
    ) in probe_rows:
        connection.execute(
            """
            INSERT INTO provider_health_probe_logs(
              probe_log_id, tenant_id, provider_id, health_generation, attempt_sequence,
              trigger_type, result_status, auth_result, http_status, latency_ms,
              error_code, result_summary, request_id, trace_id, checked_at, created_at
            ) VALUES(?, ?, ?, 0, ?, 'manual', ?, ?, ?, ?, ?, 'safe summary', ?, ?, ?, ?)
            """,
            (
                log_id,
                tenant_id,
                provider_id,
                sequence,
                result,
                "accepted" if result == "success" else "rejected",
                200 if result == "success" else 500,
                latency,
                error_code,
                request_id,
                trace_id,
                _db_time(checked_at),
                _db_time(created_at),
            ),
        )
    connection.commit()


@pytest.mark.anyio
async def test_provider_status_metrics_probe_grouping_and_maintenance_note(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：调用、流和完整探测组独立统计，维护备注只以授权字段返回。"""

    database_url, tenant_id, provider_id, model_id = _prepare_database(monkeypatch, tmp_path)
    fixed_now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("metahub_control.provider_status.service._utcnow", lambda: fixed_now)
    _insert_call_and_probe_facts(database_url, tenant_id, provider_id, model_id, fixed_now)
    app = _build_app()
    note = "计划维护 <script>alert('x')</script>"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}", "X-Request-ID": "req_status_page"}
        updated = await client.patch(
            f"/providers/{provider_id}",
            headers=headers,
            json={"maintenance_note": note},
        )
        response = await client.get(
            f"/status/providers?provider_id={provider_id}",
            headers=headers,
        )

    assert updated.status_code == 200, updated.text
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == "req_status_page"
    assert body["summary"]["total"] == 1
    item = body["provider_statuses"][0]
    assert item["maintenance_note"] == note
    assert item["terminal_call_count"] == 5
    assert item["eligible_call_count"] == 4
    assert item["success_count"] == 2
    assert item["provider_failure_count"] == 2
    assert item["credential_failure_count"] == 1
    assert item["excluded_call_count"] == 1
    assert item["success_rate_bps"] == 5000
    assert item["call_latency_sample_count"] == 2
    assert item["call_latency_p95_ms"] == 300
    assert item["stream_first_chunk_sample_count"] == 1
    assert item["stream_first_chunk_p95_ms"] == 150
    assert item["probe_total"] == 2
    assert item["probe_success_count"] == 1
    assert item["probe_failure_count"] == 1
    assert item["probe_success_rate_bps"] == 5000
    assert item["probe_latency_p95_ms"] == 400
    assert any(failure["failure_kind"] == "credential" for failure in item["recent_failures"])
    assert all("safe summary" not in str(failure) for failure in item["recent_failures"])

    audit = connect_database(database_url).execute(
        "SELECT after_summary_json FROM audit_logs WHERE object_id=? ORDER BY created_at DESC LIMIT 1",
        (provider_id,),
    ).fetchone()["after_summary_json"]
    assert note not in audit
    assert "maintenance_note_hash" in audit


@pytest.mark.anyio
async def test_provider_status_separates_reused_request_id_by_health_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归：相同 request_id 的不同健康代次各自从序号 1 开始，必须作为独立探测统计。"""

    database_url, tenant_id, provider_id, _ = _prepare_database(monkeypatch, tmp_path)
    fixed_now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("metahub_control.provider_status.service._utcnow", lambda: fixed_now)
    connection = connect_database(database_url)
    for generation, result, error_code, checked_at in (
        (4, "failed", "endpoint_blocked", fixed_now - timedelta(minutes=2)),
        (7, "success", None, fixed_now - timedelta(minutes=1)),
    ):
        connection.execute(
            """
            INSERT INTO provider_health_probe_logs(
              probe_log_id, tenant_id, provider_id, health_generation, attempt_sequence,
              trigger_type, result_status, auth_result, http_status, latency_ms,
              error_code, result_summary, request_id, trace_id, checked_at, created_at
            ) VALUES(?, ?, ?, ?, 1, 'manual', ?, ?, ?, 25, ?, 'safe summary', ?, ?, ?, ?)
            """,
            (
                f"probe_reused_generation_{generation}",
                tenant_id,
                provider_id,
                generation,
                result,
                "accepted" if result == "success" else "rejected",
                200 if result == "success" else None,
                error_code,
                "req_reused_across_generations",
                "trace_reused_across_generations",
                _db_time(checked_at),
                _db_time(checked_at),
            ),
        )
    connection.commit()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/status/providers?provider_id={provider_id}",
            headers={"Authorization": f"Bearer {admin}"},
        )

    assert response.status_code == 200, response.text
    item = response.json()["provider_statuses"][0]
    assert item["probe_total"] == 2
    assert item["probe_success_count"] == 1
    assert item["probe_failure_count"] == 1
    assert item["probe_success_rate_bps"] == 5000
    assert item["last_error"]["error_code"] == "endpoint_blocked"


@pytest.mark.anyio
async def test_provider_status_separates_reused_request_id_within_same_health_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归：状态未变化时可复用链路 ID，两次探测仍须按各自事务独立统计。"""

    database_url, tenant_id, provider_id, _ = _prepare_database(monkeypatch, tmp_path)
    fixed_now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("metahub_control.provider_status.service._utcnow", lambda: fixed_now)
    connection = connect_database(database_url)
    for probe_number, result, error_code, created_at in (
        (1, "failed", "endpoint_blocked", fixed_now - timedelta(minutes=2)),
        (2, "success", None, fixed_now - timedelta(minutes=1)),
    ):
        connection.execute(
            """
            INSERT INTO provider_health_probe_logs(
              probe_log_id, tenant_id, provider_id, health_generation, attempt_sequence,
              trigger_type, result_status, auth_result, http_status, latency_ms,
              error_code, result_summary, request_id, trace_id, checked_at, created_at
            ) VALUES(?, ?, ?, 7, 1, 'manual', ?, ?, ?, 25, ?, 'safe summary', ?, ?, ?, ?)
            """,
            (
                f"probe_reused_same_generation_{probe_number}",
                tenant_id,
                provider_id,
                result,
                "accepted" if result == "success" else "rejected",
                200 if result == "success" else None,
                error_code,
                "req_reused_within_generation",
                "trace_reused_within_generation",
                _db_time(created_at),
                _db_time(created_at),
            ),
        )
    connection.commit()

    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.get(
            f"/status/providers?provider_id={provider_id}",
            headers={"Authorization": f"Bearer {admin}"},
        )

    assert response.status_code == 200, response.text
    item = response.json()["provider_statuses"][0]
    assert item["probe_total"] == 2
    assert item["probe_success_count"] == 1
    assert item["probe_failure_count"] == 1
    assert item["probe_success_rate_bps"] == 5000
    assert item["last_error"]["error_code"] == "endpoint_blocked"


@pytest.mark.anyio
async def test_provider_status_permissions_filters_and_query_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/异常：developer 双权限可读，viewer/匿名拒绝，非法筛选和缺表返回统一码。"""

    database_url, tenant_id, _, _ = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_status_other','status-other','Status Other','internal_only','active',?,?)
        """,
        (_db_time(datetime.now(UTC)), _db_time(datetime.now(UTC))),
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,maintenance_note,created_at,updated_at
        ) VALUES(
          'provider_status_other','tenant_status_other','local_openai','Other Tenant Provider',
          'https://other-tenant-secret.example/v1','active','other-tenant-note',?,?
        )
        """,
        (_db_time(datetime.now(UTC)), _db_time(datetime.now(UTC))),
    )
    connection.commit()
    current_tenant_count = connection.execute(
        "SELECT COUNT(*) FROM providers WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()[0]
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(client, "status-dev@example.com", DEVELOPER_PASSWORD)
        viewer = await _login(client, "status-viewer@example.com", VIEWER_PASSWORD)
        developer_headers = {"Authorization": f"Bearer {developer}"}
        allowed = await client.get("/status/providers", headers=developer_headers)
        cross_tenant = await client.get(
            "/status/providers?provider_id=provider_status_other", headers=developer_headers
        )
        denied = await client.get(
            "/status/providers", headers={"Authorization": f"Bearer {viewer}"}
        )
        anonymous = await client.get("/status/providers")
        invalid = await client.get(
            "/status/providers?health_status=maybe", headers=developer_headers
        )
        invalid_pagination = [
            await client.get(path, headers=developer_headers)
            for path in (
                "/status/providers?limit=%2B1",
                "/status/providers?limit=1.0",
                "/status/providers?limit=%201",
                "/status/providers?limit=%EF%BC%91",
                "/status/providers?limit=1&limit=2",
                "/status/providers?offset=10001",
            )
        ]
        injected_tenant = await client.get(
            "/status/providers?tenant_id=other", headers=developer_headers
        )
        duplicate_filter = await client.get(
            "/status/providers?provider_type=zhipu&provider_type=deepseek",
            headers=developer_headers,
        )
        connect_database(database_url).execute("DROP TABLE routing_attempts").connection.commit()
        unavailable = await client.get("/status/providers", headers=developer_headers)

    assert allowed.status_code == 200, allowed.text
    assert allowed.headers["cache-control"] == "no-store"
    assert allowed.json()["summary"]["total"] == current_tenant_count
    assert "provider_status_other" not in allowed.text
    assert "other-tenant-secret" not in allowed.text
    assert "other-tenant-note" not in allowed.text
    assert cross_tenant.status_code == 200
    assert cross_tenant.json()["summary"]["total"] == 0
    assert cross_tenant.json()["provider_statuses"] == []
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert anonymous.status_code == 401
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "provider_status.filter_invalid"
    assert invalid.json()["request_id"]
    assert all(response.status_code == 422 for response in invalid_pagination)
    assert all(
        response.json()["code"] == "provider_status.pagination_invalid"
        and response.json()["request_id"]
        and response.json()["trace_id"]
        for response in invalid_pagination
    )
    assert injected_tenant.status_code == 422
    assert injected_tenant.json()["code"] == "provider_status.filter_invalid"
    assert duplicate_filter.status_code == 422
    assert duplicate_filter.json()["code"] == "provider_status.filter_invalid"
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "provider_status.query_unavailable"
    assert "routing_attempts" not in unavailable.text


@pytest.mark.anyio
@pytest.mark.parametrize("revoked_permission", ["provider.health.read", "request_log.read"])
async def test_provider_status_revoked_permission_takes_effect_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    revoked_permission: str,
) -> None:
    """权限流程：双权限任一被撤销后，旧 Session 下一请求立即由后端拒绝。"""

    database_url, tenant_id, _, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(client, "status-dev@example.com", DEVELOPER_PASSWORD)
        headers = {"Authorization": f"Bearer {developer}"}
        assert (await client.get("/status/providers", headers=headers)).status_code == 200

        connection = connect_database(database_url)
        role = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='developer'",
            (tenant_id,),
        ).fetchone()
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE code=?",
            (revoked_permission,),
        ).fetchone()
        assert role is not None and permission is not None
        connection.execute(
            "DELETE FROM role_permissions WHERE role_id=? AND permission_id=?",
            (role["role_id"], permission["permission_id"]),
        )
        remaining_codes = {
            row["code"]
            for row in connection.execute(
                """
                SELECT permission.code
                FROM role_permissions role_permission
                JOIN permissions permission ON permission.permission_id=role_permission.permission_id
                WHERE role_permission.role_id=?
                """,
                (role["role_id"],),
            ).fetchall()
        }
        connection.execute(
            """
            UPDATE roles
            SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(remaining_codes),
                _db_time(datetime.now(UTC)),
                tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
        denied = await client.get("/status/providers", headers=headers)

    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert denied.json()["request_id"] and denied.json()["trace_id"]


@pytest.mark.anyio
async def test_provider_status_navigation_and_view_event_are_backend_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事件流程：PAGE-088 导航和访问事件都由后端固定合同约束，且不派生 Webhook。"""

    database_url, _, _, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        navigation = await client.get(
            "/console/navigation?active_item_id=provider-health", headers=headers
        )
        accepted = await client.post(
            "/console/events",
            headers=headers,
            json={
                "event_type": "status.viewed",
                "navigation_item_id": "provider-health",
                "target_section": "provider-health",
                "path": "/#provider-health",
                "payload": {
                    "source": "sidebar",
                    "hash": "#provider-health",
                    "viewport_class": "desktop",
                },
            },
        )
        forged = await client.post(
            "/console/events",
            headers=headers,
            json={
                "event_type": "status.viewed",
                "navigation_item_id": "provider-health",
                "target_section": "provider-health",
                "path": "/#provider-health",
                "payload": {
                    "source": "sidebar",
                    "hash": "#provider-health",
                    "viewport_class": "desktop",
                    "provider_id": "must-not-enter-event",
                },
            },
        )

    assert navigation.status_code == 200, navigation.text
    provider_navigation = next(
        item
        for item in navigation.json()["navigation_items"]
        if item["id"] == "provider-health"
    )
    assert provider_navigation["label"] == "Provider 状态"
    assert provider_navigation["required_permissions"] == [
        "provider.health.read",
        "request_log.read",
    ]
    assert accepted.status_code == 200, accepted.text
    assert forged.status_code == 422
    assert forged.json()["code"] == "console.event_invalid"
    connection = connect_database(database_url)
    event = connection.execute(
        """
        SELECT event_type,navigation_item_id,target_section,path,payload_json
        FROM console_page_events WHERE event_id=?
        """,
        (accepted.json()["event_id"],),
    ).fetchone()
    assert event["event_type"] == "status.viewed"
    assert event["navigation_item_id"] == event["target_section"] == "provider-health"
    assert event["path"] == "/#provider-health"
    assert "provider_id" not in event["payload_json"]
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='status.viewed'"
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_provider_maintenance_note_validation_clear_and_audit_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """维护备注：覆盖 240/241、控制字符、显式 null 清空、未知字段和审计哈希。"""

    database_url, _, provider_id, _ = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    accepted_note = "维" * 240
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        accepted = await client.patch(
            f"/providers/{provider_id}", headers=headers, json={"maintenance_note": accepted_note}
        )
        too_long = await client.patch(
            f"/providers/{provider_id}", headers=headers, json={"maintenance_note": "维" * 241}
        )
        control = await client.patch(
            f"/providers/{provider_id}", headers=headers, json={"maintenance_note": "维护\n中"}
        )
        unknown = await client.patch(
            f"/providers/{provider_id}",
            headers=headers,
            json={"maintenance_note": "ok", "tenant_id": "other"},
        )
        cleared = await client.patch(
            f"/providers/{provider_id}", headers=headers, json={"maintenance_note": None}
        )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["provider"]["maintenance_note"] == accepted_note
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "provider.maintenance_note_invalid"
    assert control.status_code == 422
    assert control.json()["code"] == "provider.maintenance_note_invalid"
    assert unknown.status_code == 422
    assert unknown.json()["request_id"]
    assert cleared.status_code == 200
    assert cleared.json()["provider"]["maintenance_note"] is None
    connection = connect_database(database_url)
    summaries = "\n".join(
        row["after_summary_json"]
        for row in connection.execute(
            "SELECT after_summary_json FROM audit_logs WHERE object_id=? ORDER BY created_at",
            (provider_id,),
        ).fetchall()
    )
    assert accepted_note not in summaries
    assert "maintenance_note_hash" in summaries
