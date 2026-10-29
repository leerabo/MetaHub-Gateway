"""PRD-065 角色首页后端主流程、权限、租户隔离和异常合同测试。

测试只构造可公开的聚合事实；敏感 canary 仅写入底层日志/Usage，用于证明 role-view 响应和事件不会回传正文、Endpoint 或密钥。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.console.routes import router as console_router
from metahub_control.dashboard.routes import router as dashboard_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import ROLE_GRANTABLE_PERMISSIONS, permission_matrix_sha256
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试身份、敏感 canary 和六角色期望共同冻结权限/租户合同；这些值只用于隔离测试数据库。
ADMIN_PASSWORD = "LocalAdmin!2026"
ROLE_PASSWORD = "RoleDashboard!2026"
SENSITIVE_CANARY = "sk-prd065-prompt-response-endpoint-canary"
ROLE_EMAILS = {
    "developer": "developer-prd065@example.com",
    "viewer": "viewer-prd065@example.com",
    "finance_admin": "finance-prd065@example.com",
    "security_admin": "security-prd065@example.com",
    "ops_admin": "ops-prd065@example.com",
}
EXPECTED_DEFAULTS = {
    "admin": "executive",
    "developer": "developer",
    "viewer": "executive",
    "finance_admin": "finance",
    "security_admin": "security",
    "ops_admin": "operations",
}
EXPECTED_AVAILABLE = {
    "admin": ["executive", "finance", "security", "developer", "operations"],
    "developer": ["developer", "operations", "executive"],
    "viewer": ["executive"],
    "finance_admin": ["finance", "executive"],
    "security_admin": ["security", "executive"],
    "ops_admin": ["operations", "executive"],
}
EXPECTED_FIRST_CARD = {
    "admin": "cost",
    "developer": "adoption",
    "viewer": "cost",
    "finance_admin": "cost",
    "security_admin": "security",
    "ops_admin": "provider_health",
}


def _sqlite_url(path: Path) -> str:
    """把 pytest 隔离数据库转换成项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只包含认证、Dashboard 和 Console 事件路由的测试应用。"""

    app = create_service_app("control-plane-api", "PRD-065 Role Dashboard Test API")
    app.include_router(auth_router)
    app.include_router(dashboard_router)
    app.include_router(console_router)
    return app


def _db_time(offset_minutes: int = 0) -> str:
    """生成当前上海业务日内的秒级 UTC 字符串。"""

    shanghai_noon = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    return (
        (shanghai_noon.astimezone(UTC) + timedelta(minutes=offset_minutes))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    with_facts: bool = True,
) -> tuple[str, str]:
    """创建 0057 数据库、六角色用户和可验证的跨租户聚合事实。"""

    database_url = _sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd065-auth-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    now = _db_time()
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        [
            (
                f"user_prd065_{role}",
                seed.tenant_id,
                email,
                f"PRD065 {role}",
                hash_password(ROLE_PASSWORD),
                role,
                now,
                now,
            )
            for role, email in ROLE_EMAILS.items()
        ],
    )
    if with_facts:
        _insert_current_tenant_facts(connection, seed.tenant_id)
        _insert_other_tenant_canary(connection)
    connection.commit()
    connection.close()
    # 仅返回隔离数据库 URL 和公开 tenant ID，敏感 canary 始终留在底层事实中用于泄漏断言。
    return database_url, seed.tenant_id


def _insert_current_tenant_facts(connection, tenant_id: str) -> None:
    """插入一个安全失败和一条 unpriced Usage，覆盖稳定性、安全和成本异常语义。"""

    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? ORDER BY provider_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? ORDER BY model_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        "SELECT virtual_key_id FROM virtual_keys WHERE tenant_id=? ORDER BY virtual_key_id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    assert provider is not None and model is not None and virtual_key is not None
    request_id = "req_prd065_security"
    trace_id = "trc_prd065_security"
    created_at = _db_time(-5)
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,virtual_key_id,
          snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES(?,?,?,?,NULL,'disabled',?)
        """,
        (tenant_id, request_id, trace_id, virtual_key["virtual_key_id"], created_at),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,virtual_key_id,provider_id,model_id,
          endpoint,request_model,resolved_model,provider_code,status,error_code,
          latency_ms,total_tokens,estimated_cost_amount,currency,price_schema_version,
          dlp_status,dlp_action,dlp_match_count,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?, 'company-default','default-chat','local_openai',
          'failed','security.dlp_blocked',250,100,0,'CNY','unpriced-v1',
          'matched','block',2,?,?)
        """,
        (
            request_id,
            tenant_id,
            trace_id,
            virtual_key["virtual_key_id"],
            provider["provider_id"],
            model["model_id"],
            f"https://{SENSITIVE_CANARY}.invalid/v1",
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO usage_ledger(
          usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,total_tokens,
          raw_usage,normalized_usage,cost_amount,currency,price_schema_version,
          estimated,usage_source,model_id,provider_id,virtual_key_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?, ?,0,'CNY','unpriced-v1',0,'upstream',?,?,?,?,?)
        """,
        (
            "usg_prd065_security",
            tenant_id,
            request_id,
            100,
            0,
            100,
            '{"prompt":"' + SENSITIVE_CANARY + '"}',
            '{"total_tokens":100}',
            model["model_id"],
            provider["provider_id"],
            virtual_key["virtual_key_id"],
            created_at,
            created_at,
        ),
    )


def _insert_other_tenant_canary(connection) -> None:
    """插入第二租户大额失败事实，证明所有角色卡片都保持 tenant-first 隔离。"""

    now = _db_time(-2)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES('tenant_prd065_other','prd065-other','PRD065 Other','internal_only','active',?,?)
        """,
        (now, now),
    )
    connection.execute(
        """
        INSERT INTO gateway_request_claims(
          tenant_id,request_id,trace_id,snapshot_version,policy_snapshot_status,claimed_at
        ) VALUES('tenant_prd065_other','req_prd065_other','trc_prd065_other',NULL,'disabled',?)
        """,
        (now,),
    )
    connection.execute(
        """
        INSERT INTO request_logs(
          request_id,tenant_id,trace_id,status,error_code,latency_ms,total_tokens,
          estimated_cost_amount,currency,dlp_status,dlp_action,dlp_match_count,created_at,updated_at
        ) VALUES(
          'req_prd065_other','tenant_prd065_other','trc_prd065_other','failed',
          'security.other_tenant',9999,999999,999999,'CNY','matched','block',99,?,?
        )
        """,
        (now, now),
    )


async def _login(client: AsyncClient, role: str) -> str:
    """按固定角色登录并返回仅在当前测试内存中使用的 Token。"""

    email = "admin@metahub.local" if role == "admin" else ROLE_EMAILS[role]
    password = ADMIN_PASSWORD if role == "admin" else ROLE_PASSWORD
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只在当前测试进程内传递给认证头，不写入日志、DOM 或验收证据。
    return response.json()["access_token"]


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造带统一链路 ID 的认证请求头。"""

    # 认证值只进入内存请求头；断言和错误输出仅使用非敏感 request_id 串联测试。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _revoke_permission(database_url: str, tenant_id: str, role_code: str, code: str) -> None:
    """模拟租户管理员完成一次合法矩阵撤权并同步 revision/hash。"""

    connection = connect_database(database_url)
    role = connection.execute(
        """
        SELECT role_id,permission_revision
        FROM roles WHERE tenant_id=? AND code=?
        """,
        (tenant_id, role_code),
    ).fetchone()
    permission = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?",
        (tenant_id, code),
    ).fetchone()
    assert role is not None and permission is not None
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
        (tenant_id, role["role_id"], permission["permission_id"]),
    )
    remaining = set(ROLE_GRANTABLE_PERMISSIONS[role_code]) - {code}
    connection.execute(
        """
        UPDATE roles
        SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            role["permission_revision"] + 1,
            permission_matrix_sha256(remaining),
            "2099-01-01T00:00:00Z",
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_role_dashboard_six_defaults_admin_switch_and_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：六角色默认视图、卡片顺序、admin 切换和渲染后事件合同均可验收。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path)
    app = _build_app()
    responses: dict[str, dict[str, object]] = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        tokens = {role: await _login(client, role) for role in EXPECTED_DEFAULTS}
        for role, token in tokens.items():
            response = await client.get(
                "/dashboard/role-view",
                headers=_headers(token, f"req_prd065_{role}"),
            )
            assert response.status_code == 200, response.text
            responses[role] = response.json()
            assert response.json()["request_id"] == f"req_prd065_{role}"
            assert tenant_id not in response.text
            assert "tenant_prd065_other" not in response.text
            assert SENSITIVE_CANARY not in response.text

        switched = await client.get(
            "/dashboard/role-view?view=finance",
            headers=_headers(tokens["admin"], "req_prd065_switch"),
        )
        assert switched.status_code == 200, switched.text
        switched_body = switched.json()
        event = await client.post(
            "/console/events",
            json={
                "event_type": "dashboard.role_viewed",
                "navigation_item_id": "overview",
                "target_section": "overview",
                "path": "/#overview",
                "payload": {
                    "source": "view_switch",
                    "hash": "#overview",
                    "viewport_class": "desktop",
                    "view_code": switched_body["config"]["selected_view_code"],
                    "config_revision": switched_body["config"]["config_revision"],
                },
            },
            headers=_headers(tokens["admin"], "req_prd065_event"),
        )

    for role, body in responses.items():
        config = body["config"]
        assert config["selected_view_code"] == EXPECTED_DEFAULTS[role]
        assert [item["code"] for item in config["available_views"]] == EXPECTED_AVAILABLE[role]
        assert len(body["cards"]) == 6
        assert body["cards"][0]["code"] == EXPECTED_FIRST_CARD[role]
        assert all(len(card["facts"]) <= 5 for card in body["cards"])
    # 当前 tenant 只有一条失败请求；第二租户的 99 次 DLP 命中和高延迟不能进入任一卡片。
    admin_cards = {card["code"]: card for card in responses["admin"]["cards"]}
    stability = {fact["code"]: fact["value"] for fact in admin_cards["stability"]["facts"]}
    security = {fact["code"]: fact["value"] for fact in admin_cards["security"]["facts"]}
    cost = {fact["code"]: fact["value"] for fact in admin_cards["cost"]["facts"]}
    assert stability["request_count"] == 1
    assert stability["p95_latency_ms"] == 250
    assert security["dlp_matches"] == 2
    assert security["dlp_blocked"] == 1
    assert cost["unpriced_requests"] == 1
    assert admin_cards["cost"]["primary_value"] == 0
    assert admin_cards["cost"]["status"] == "warning"

    assert switched_body["config"]["selected_view_code"] == "finance"
    assert switched_body["cards"][0]["code"] == "cost"
    assert event.status_code == 200, event.text
    connection = connect_database(database_url)
    stored = connection.execute(
        """
        SELECT tenant_id,event_type,request_id,payload_json
        FROM console_page_events WHERE event_id=?
        """,
        (event.json()["event_id"],),
    ).fetchone()
    connection.close()
    assert stored is not None
    assert stored["tenant_id"] == tenant_id
    assert stored["event_type"] == "dashboard.role_viewed"
    assert stored["request_id"] == "req_prd065_event"
    assert SENSITIVE_CANARY not in stored["payload_json"]


@pytest.mark.anyio
async def test_role_dashboard_realtime_revocation_query_priority_and_event_forgery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/异常：角色不补权、401/403 优先于 query、畸形输入和伪造事件均失败关闭。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path, with_facts=False)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin")
        finance_token = await _login(client, "finance_admin")
        viewer_token = await _login(client, "viewer")

        _revoke_permission(
            database_url,
            tenant_id,
            "finance_admin",
            "billing.anomaly.read",
        )
        finance_denied = await client.get(
            "/dashboard/role-view?view=finance",
            headers=_headers(finance_token, "req_prd065_finance_denied"),
        )
        finance_fallback = await client.get(
            "/dashboard/role-view",
            headers=_headers(finance_token, "req_prd065_finance_fallback"),
        )

        _revoke_permission(database_url, tenant_id, "viewer", "dashboard.read")
        permission_before_query = await client.get(
            "/dashboard/role-view?view=UNKNOWN&tenant_id=tenant_other",
            headers=_headers(viewer_token, "req_prd065_permission_first"),
        )
        anonymous_before_query = await client.get(
            "/dashboard/role-view?view=UNKNOWN&tenant_id=tenant_other",
            headers={"X-Request-ID": "req_prd065_anonymous_first"},
        )
        invalid_queries = [
            "view=",
            "view=Finance",
            "view=unknown",
            "view=finance&view=finance",
            "tenant_id=tenant_other",
            "view=finance&role=admin",
        ]
        invalid_responses = [
            await client.get(
                f"/dashboard/role-view?{query}",
                headers=_headers(admin_token, f"req_prd065_invalid_{index}"),
            )
            for index, query in enumerate(invalid_queries)
        ]
        current = await client.get(
            "/dashboard/role-view?view=security",
            headers=_headers(admin_token, "req_prd065_event_owner"),
        )
        revision = current.json()["config"]["config_revision"]
        forged_revision = await client.post(
            "/console/events",
            json={
                "event_type": "dashboard.role_viewed",
                "navigation_item_id": "overview",
                "target_section": "overview",
                "path": "/#overview",
                "payload": {
                    "source": "refresh",
                    "hash": "#overview",
                    "viewport_class": "mobile",
                    "view_code": "security",
                    "config_revision": revision + 1,
                },
            },
            headers=_headers(admin_token, "req_prd065_forged_revision"),
        )
        forged_payload = await client.post(
            "/console/events",
            json={
                "event_type": "dashboard.role_viewed",
                "navigation_item_id": "overview",
                "target_section": "overview",
                "path": "/#overview",
                "payload": {
                    "source": "refresh",
                    "hash": "#overview",
                    "viewport_class": "desktop",
                    "view_code": "security",
                    "config_revision": revision,
                    "tenant_id": "tenant_other",
                },
            },
            headers=_headers(admin_token, "req_prd065_forged_payload"),
        )

    assert finance_denied.status_code == 403
    assert finance_denied.json()["code"] == "auth.permission_denied"
    assert finance_denied.json()["request_id"] == "req_prd065_finance_denied"
    assert finance_fallback.status_code == 200, finance_fallback.text
    assert finance_fallback.json()["config"]["selected_view_code"] == "executive"
    assert [
        item["code"] for item in finance_fallback.json()["config"]["available_views"]
    ] == ["executive"]
    assert permission_before_query.status_code == 403
    assert permission_before_query.json()["code"] == "auth.permission_denied"
    assert anonymous_before_query.status_code == 401
    assert anonymous_before_query.json()["code"] == "auth.token_missing"
    for response in invalid_responses:
        assert response.status_code == 422
        assert response.json()["code"] == "dashboard.query_invalid"
        assert response.json()["request_id"]
        assert tenant_id not in response.text
    assert forged_revision.status_code == 422
    assert forged_revision.json()["code"] == "console.event_invalid"
    assert forged_payload.status_code == 422
    assert forged_payload.json()["code"] == "console.event_invalid"


@pytest.mark.anyio
async def test_role_dashboard_config_and_dependency_failures_are_uniform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常：停用配置返回固定 500，依赖表缺失返回固定 503，均保留链路 ID 且不泄密。"""

    database_url, tenant_id = _prepare_database(monkeypatch, tmp_path, with_facts=False)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin")
        connection = connect_database(database_url)
        connection.execute(
            """
            UPDATE role_dashboard_configs
            SET status='disabled',config_revision=config_revision+1,updated_at=?
            WHERE tenant_id=? AND role_code='admin'
            """,
            ("2099-01-01T00:00:00Z", tenant_id),
        )
        connection.commit()
        connection.close()
        invalid_config = await client.get(
            "/dashboard/role-view",
            headers=_headers(admin_token, "req_prd065_config_invalid"),
        )

    assert invalid_config.status_code == 500
    assert invalid_config.json()["code"] == "dashboard.config_invalid"
    assert invalid_config.json()["request_id"] == "req_prd065_config_invalid"
    assert "role_dashboard_configs" not in invalid_config.text
    assert database_url not in invalid_config.text

    # 使用另一隔离数据库验证缺表，避免前一个受控配置变更混淆错误分类。
    second_root = tmp_path / "dependency"
    second_root.mkdir()
    database_url, _tenant_id = _prepare_database(monkeypatch, second_root, with_facts=False)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin")
        connection = connect_database(database_url)
        connection.execute(
            "ALTER TABLE role_dashboard_configs RENAME TO role_dashboard_configs_unavailable"
        )
        connection.commit()
        connection.close()
        unavailable = await client.get(
            "/dashboard/role-view",
            headers=_headers(admin_token, "req_prd065_unavailable"),
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "dashboard.unavailable"
    assert unavailable.json()["request_id"] == "req_prd065_unavailable"
    assert "role_dashboard_configs" not in unavailable.text
    assert database_url not in unavailable.text
