"""PRD-063 Adapter 规范 API、权限、租户和跨入口失败关闭专项测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.adapter_specs import service as adapter_spec_service
from metahub_control.adapter_specs.routes import router as adapter_specs_router
from metahub_control.auth.routes import router as auth_router
from metahub_control.config_transfer.service import _ImportPlan, _normalize_provider_rows
from metahub_control.provider_health.routes import router as provider_health_router
from metahub_control.providers.routes import router as providers_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.provider_adapter_spec import (
    ProviderAdapterCatalogIdentity,
    catalog_identity,
)
from metahub_shared.runtime.webhooks import canonical_json_bytes

# 固定凭据只服务角色矩阵；所有值均为测试专用且不得进入生产配置或日志证据。
ADMIN_PASSWORD = "Prd063Admin!2026"
DEVELOPER_PASSWORD = "Prd063Developer!2026"
VIEWER_PASSWORD = "Prd063Viewer!2026"
# 内部 Token 只保存在测试进程环境与当前请求中，不写入断言、日志或数据库证据。
INTERNAL_TOKEN = "prd063-internal-token-value-2026-abcdef"


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库文件转换为项目支持的 SQLite URL 格式。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str]:
    """应用 0055、seed 默认租户，并创建 Developer/Viewer 权限样本。"""

    database_url = _sqlite_url(tmp_path / "prd063-adapter-specs.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd063-auth-secret-not-for-production")
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", INTERNAL_TOKEN)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',datetime('now'),datetime('now'))
        """,
        [
            (
                "usr_prd063_developer",
                seed.tenant_id,
                "prd063-developer@example.com",
                "PRD063 Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "usr_prd063_viewer",
                seed.tenant_id,
                "prd063-viewer@example.com",
                "PRD063 Viewer",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    connection.close()
    # tenant_id 只来自 seed 可信结果，后续权限用例不能用客户端参数替换它。
    return database_url, seed.tenant_id


def _build_app():
    """构造只含本专项依赖路由的 Control API，避免无关 scheduler 干扰证据。"""

    app = create_service_app("control-plane-api", "PRD-063 Adapter Spec Test API")
    app.include_router(auth_router)
    app.include_router(adapter_specs_router)
    app.include_router(providers_router)
    app.include_router(provider_health_router)
    return app


async def _login(client: AsyncClient, email: str, password: str, request_id: str) -> str:
    """登录一个测试角色并返回只在当前协程内使用的 Bearer Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200, response.text
    # Bearer Token 仅在当前测试协程内使用，禁止写入持久断言或测试日志。
    return response.json()["access_token"]


def _assert_error(response: Response, code: str, status: int) -> None:
    """断言统一错误信封、链路 ID 与目录接口 no-store。"""

    assert response.status_code == status
    payload = response.json()
    assert payload["code"] == code
    assert payload["request_id"]
    assert payload["trace_id"]
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.anyio
async def test_adapter_spec_read_permission_filters_and_tenant_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程/权限：Developer 可读九类，Viewer 403，筛选不能覆盖会话租户。"""

    _database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await _login(
            client,
            "prd063-developer@example.com",
            DEVELOPER_PASSWORD,
            "req_prd063_login_developer",
        )
        viewer = await _login(
            client,
            "prd063-viewer@example.com",
            VIEWER_PASSWORD,
            "req_prd063_login_viewer",
        )
        success = await client.get(
            "/adapter-specs",
            headers={"Authorization": f"Bearer {developer}", "X-Request-ID": "req_prd063_list"},
        )
        domestic = await client.get(
            "/adapter-specs?classification=model_vendor_cloud&status=active",
            headers={"Authorization": f"Bearer {developer}"},
        )
        viewer_denied = await client.get(
            "/adapter-specs",
            headers={"Authorization": f"Bearer {viewer}"},
        )
        tenant_injection = await client.get(
            "/adapter-specs?tenant_id=another-tenant",
            headers={"Authorization": f"Bearer {developer}"},
        )
        duplicate = await client.get(
            "/adapter-specs?status=active&status=optional",
            headers={"Authorization": f"Bearer {developer}"},
        )

    identity = catalog_identity()
    assert success.status_code == 200, success.text
    assert success.headers["Cache-Control"] == "no-store"
    payload = success.json()
    assert payload["catalog"]["version"] == identity.version
    assert payload["catalog"]["hash"] == identity.catalog_hash
    assert len(payload["specs"]) == identity.spec_count == 9
    assert {item["tenant_id"] for item in payload["specs"]} == {tenant_id}
    assert {item["provider_type"] for item in domestic.json()["specs"]} == {
        "zhipu",
        "dashscope",
        "wenxin",
        "volcengine",
        "hunyuan",
        "deepseek",
    }
    _assert_error(viewer_denied, "auth.permission_denied", 403)
    _assert_error(tenant_injection, "adapter.spec_filter_invalid", 422)
    _assert_error(duplicate, "adapter.spec_filter_invalid", 422)


@pytest.mark.anyio
async def test_internal_activation_auth_noop_replay_conflict_and_no_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/幂等：内部身份先行，同版本留收据，旧 request 冲突不改 active。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    identity = catalog_identity()
    body = {"catalog_version": identity.version, "catalog_hash": identity.catalog_hash}
    path = f"/internal/adapter-specs/{tenant_id}/activate"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        missing = await client.post(
            path,
            content=b"{",
            headers={"Content-Type": "application/json", "X-Request-ID": "req_prd063_missing"},
        )
        wrong = await client.post(
            path,
            json=body,
            headers={"X-Internal-Token": "invalid-internal-token", "X-Request-ID": "req_prd063_wrong"},
        )
        mixed = await client.post(
            path,
            json=body,
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "Authorization": "Bearer user-session-not-allowed",
                "X-Request-ID": "req_prd063_mixed",
            },
        )
        first = await client.post(
            path,
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "X-Request-ID": "req_prd063_noop"},
        )
        replay = await client.post(
            path,
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "X-Request-ID": "req_prd063_noop"},
        )
        conflict = await client.post(
            path,
            json={"catalog_version": identity.version, "catalog_hash": "0" * 64},
            headers={"X-Internal-Token": INTERNAL_TOKEN, "X-Request-ID": "req_prd063_noop"},
        )
        unknown = await client.post(
            "/internal/adapter-specs/tenant-does-not-exist/activate",
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "X-Request-ID": "req_prd063_unknown"},
        )
        extra_query = await client.post(
            f"{path}?tenant_id=other",
            json=body,
            headers={"X-Internal-Token": INTERNAL_TOKEN, "X-Request-ID": "req_prd063_query"},
        )
        oversized = await client.post(
            path,
            content=b"{" + (b" " * 1024),
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": "req_prd063_oversized",
            },
        )

    _assert_error(missing, "auth.internal_token_required", 401)
    _assert_error(wrong, "auth.internal_token_invalid", 403)
    _assert_error(mixed, "auth.internal_token_invalid", 403)
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["changed"] is False
    assert first.json()["activation"]["status"] == "superseded"
    assert replay.json() == first.json()
    _assert_error(conflict, "adapter.spec_activation_conflict", 409)
    _assert_error(unknown, "adapter.spec_tenant_not_found", 404)
    _assert_error(extra_query, "adapter.spec_activation_invalid", 422)
    _assert_error(oversized, "adapter.spec_activation_invalid", 422)

    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT status,changed,request_id FROM provider_adapter_catalog_activations
        WHERE tenant_id=? ORDER BY created_at,activation_id
        """,
        (tenant_id,),
    ).fetchall()
    connection.close()
    assert sum(row["status"] == "active" for row in rows) == 1
    assert [(row["status"], row["changed"]) for row in rows].count(("superseded", 0)) == 1
    assert {row["request_id"] for row in rows if row["request_id"] == "req_prd063_noop"} == {
        "req_prd063_noop"
    }


@pytest.mark.anyio
@pytest.mark.parametrize("drift_field", ("catalog_hash", "spec_count"))
async def test_activation_rejects_same_version_identity_drift_before_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift_field: str,
) -> None:
    """异常流程：同版本 hash/count 漂移必须在 supersede 和 Webhook 之前失败关闭。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    original = catalog_identity()
    drifted = ProviderAdapterCatalogIdentity(
        version=original.version,
        catalog_hash=("d" * 64 if drift_field == "catalog_hash" else original.catalog_hash),
        spec_count=(original.spec_count + 1 if drift_field == "spec_count" else original.spec_count),
        canonical_byte_length=original.canonical_byte_length,
    )
    monkeypatch.setattr(adapter_spec_service, "catalog_identity", lambda: drifted)
    delivered: list[object] = []

    def _capture_unexpected_event(*args: object, **kwargs: object) -> None:
        """记录任何越过事务前门禁的 Webhook，证明失败分支没有外部副作用。"""

        delivered.append((args, kwargs))

    monkeypatch.setattr(adapter_spec_service, "dispatch_webhook_event", _capture_unexpected_event)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        response = await client.post(
            f"/internal/adapter-specs/{tenant_id}/activate",
            json={"catalog_version": drifted.version, "catalog_hash": drifted.catalog_hash},
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": f"req_prd063_same_version_{drift_field}",
            },
        )

    _assert_error(response, "adapter.spec_catalog_invalid", 503)
    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT catalog_version,catalog_hash,spec_count,status,changed
        FROM provider_adapter_catalog_activations WHERE tenant_id=?
        """,
        (tenant_id,),
    ).fetchall()
    connection.close()
    assert delivered == []
    assert len(rows) == 1
    assert tuple(rows[0]) == (
        original.version,
        original.catalog_hash,
        original.spec_count,
        "active",
        0,
    )


@pytest.mark.anyio
async def test_provider_management_and_probe_fail_closed_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """跨入口：非法 type/auth 创建与历史 custom_header 探测均在联网前拒绝。"""

    database_url, _tenant_id = _prepare_database(monkeypatch, tmp_path)
    network_calls = 0

    def _unexpected_probe(*args: object, **kwargs: object) -> str:
        """若组合校验被绕过则立即让测试失败，并记录调用次数。"""

        nonlocal network_calls
        del args, kwargs
        network_calls += 1
        # 任何到达这里的调用都证明 type/auth 门禁发生在网络校验之后，必须立即失败。
        raise AssertionError("network validation must not run")

    monkeypatch.setattr(
        "metahub_control.provider_health.service._validated_probe_url",
        _unexpected_probe,
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(
            client,
            "admin@metahub.local",
            ADMIN_PASSWORD,
            "req_prd063_login_admin",
        )
        headers = {"Authorization": f"Bearer {admin}"}
        domestic_none = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "prd063_bad_none",
                "name": "Invalid Domestic None",
                "provider_type": "deepseek",
                "base_url": "https://example.invalid/v1",
                "auth_type": "none",
            },
        )
        custom_header = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "prd063_bad_header",
                "name": "Invalid Header",
                "provider_type": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "auth_type": "custom_header",
            },
        )
        valid = await client.post(
            "/providers",
            headers=headers,
            json={
                "provider_code": "prd063_history",
                "name": "Historical Provider",
                "provider_type": "openai_compatible",
                "base_url": "https://example.invalid/v1",
                "auth_type": "bearer",
            },
        )
        assert valid.status_code == 201, valid.text
        provider_id = valid.json()["provider"]["provider_id"]
        connection = connect_database(database_url)
        connection.execute(
            "UPDATE providers SET auth_type='custom_header' WHERE provider_id=?",
            (provider_id,),
        )
        connection.commit()
        connection.close()
        probe = await client.post(
            f"/providers/{provider_id}/probe",
            headers=headers,
            json={},
        )
        legacy_test = await client.post(
            f"/providers/{provider_id}/test",
            headers=headers,
            json={"timeout_seconds": 1},
        )

    assert domestic_none.status_code == 422
    assert domestic_none.json()["code"] == "provider.auth_not_supported"
    assert custom_header.status_code == 422
    assert custom_header.json()["code"] == "provider.auth_not_supported"
    assert probe.status_code == 422
    assert probe.json()["code"] == "provider.auth_not_supported"
    assert legacy_test.status_code == 422
    assert legacy_test.json()["code"] == "provider.auth_not_supported"
    assert network_calls == 0
    connection = connect_database(database_url)
    lease = connection.execute(
        "SELECT probe_lease_token,probe_lease_until FROM providers WHERE provider_id=?",
        (provider_id,),
    ).fetchone()
    connection.close()
    assert tuple(lease) == (None, None)


def test_config_import_reuses_provider_auth_spec_gate() -> None:
    """配置迁移：create-only 导入不能绕过 CRUD 的 custom_header 失败关闭。"""

    plan = _ImportPlan()
    _normalize_provider_rows(
        {
            "providers": [
                {
                    "provider_code": "prd063_import_header",
                    "name": "PRD063 Import Header",
                    "provider_type": "openai_compatible",
                    "base_url": "https://example.invalid/v1",
                    "auth_type": "custom_header",
                    "description": None,
                    "maintenance_note": None,
                    "status": "active",
                }
            ]
        },
        plan,
    )
    assert plan.rows["providers"] == []
    assert [(error.field, error.code) for error in plan.errors] == [
        ("auth_type", "provider.auth_not_supported")
    ]


@pytest.mark.anyio
async def test_real_catalog_switch_emits_exact_event_and_webhook_failure_does_not_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """事件流程：受控 v1->v2 切换发 exact 事件，v2->v3 投递失败仍保留已提交 active。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    original = catalog_identity()
    v2 = ProviderAdapterCatalogIdentity(
        version="adapter-spec-v2",
        catalog_hash="b" * 64,
        spec_count=original.spec_count,
        canonical_byte_length=original.canonical_byte_length,
    )
    monkeypatch.setattr(adapter_spec_service, "catalog_identity", lambda: v2)
    delivered: list[object] = []

    def _capture_event(*args: object, **kwargs: object) -> None:
        """捕获已经通过 Shared exact validator 的事件，不执行任何 Endpoint 网络投递。"""

        event = kwargs.get("event") if "event" in kwargs else args[2]
        delivered.append(event)

    monkeypatch.setattr(adapter_spec_service, "dispatch_webhook_event", _capture_event)
    app = _build_app()
    path = f"/internal/adapter-specs/{tenant_id}/activate"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        switched = await client.post(
            path,
            json={"catalog_version": v2.version, "catalog_hash": v2.catalog_hash},
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": "req_prd063_switch_v2",
            },
        )

    assert switched.status_code == 200, switched.text
    assert switched.json()["changed"] is True
    assert switched.json()["activation"]["catalog_version"] == "adapter-spec-v2"
    assert len(delivered) == 1
    event = delivered[0]
    assert event is not None
    assert event.event_type == "adapter.spec_updated"
    assert event.tenant_id == tenant_id
    assert event.actor_id == "adapter-spec-publisher"
    assert event.payload == {
        "catalog_version": "adapter-spec-v2",
        "catalog_hash": "b" * 64,
        "previous_catalog_version": "adapter-spec-v1",
        "spec_count": 9,
        "activated_at": event.payload["activated_at"],
    }

    v3 = ProviderAdapterCatalogIdentity(
        version="adapter-spec-v3",
        catalog_hash="c" * 64,
        spec_count=original.spec_count,
        canonical_byte_length=original.canonical_byte_length,
    )
    monkeypatch.setattr(adapter_spec_service, "catalog_identity", lambda: v3)

    def _fail_delivery(*args: object, **kwargs: object) -> None:
        """模拟含敏感 URL 的投递异常，服务日志不得记录异常字符串或 traceback。"""

        del args, kwargs
        # 敏感 canary 故意放在异常文本中，用于证明生产日志不会记录自由异常内容。
        raise RuntimeError("https://private.example/webhook secret-response-canary")

    monkeypatch.setattr(adapter_spec_service, "dispatch_webhook_event", _fail_delivery)
    caplog.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        failed_delivery = await client.post(
            path,
            json={"catalog_version": v3.version, "catalog_hash": v3.catalog_hash},
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Request-ID": "req_prd063_switch_v3",
            },
        )

    assert failed_delivery.status_code == 200, failed_delivery.text
    assert failed_delivery.json()["changed"] is True
    connection = connect_database(database_url)
    active = connection.execute(
        """
        SELECT catalog_version,catalog_hash,spec_count,status
        FROM provider_adapter_catalog_activations
        WHERE tenant_id=? AND status='active'
        """,
        (tenant_id,),
    ).fetchone()
    # v2 已被 v3 supersede，模拟它遗留的 pending/dead-letter delivery；恢复必须从历史
    # activation 读取原始 source_request_id，而不能用当前 worker 的请求 ID 或占位值。
    pending_delivery = connection.execute(
        """
        SELECT ? AS payload_json,? AS tenant_id,? AS event_id,
               ? AS event_type,? AS event_timestamp
        """,
        (
            json.dumps(event.to_payload(), ensure_ascii=False, sort_keys=True),
            tenant_id,
            event.event_id,
            event.event_type,
            event.timestamp,
        ),
    ).fetchone()
    resumed_body = webhook_service._validated_pending_delivery_body(connection, pending_delivery)
    connection.close()
    assert tuple(active) == ("adapter-spec-v3", "c" * 64, 9, "active")
    assert resumed_body == canonical_json_bytes(event.to_payload())
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "adapter spec webhook delivery unavailable" in logs
    assert "private.example" not in logs
    assert "secret-response-canary" not in logs
