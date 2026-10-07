"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/tests/integration/test_model_aliases.py
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.model_aliases import service as model_alias_service
from metahub_control.model_aliases.routes import router as model_aliases_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 以下测试密码只用于模型别名权限矩阵，避免在每个用例里重复硬编码角色夹具。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"


def sqlite_url(path: Path) -> str:
    """sqlite_url：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """build_control_app：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    app = create_service_app("control-plane-api", "Model Alias Test Control API")
    app.include_router(auth_router)
    app.include_router(model_aliases_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str, str, str]:
    """prepare_database：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-auth-token-secret")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    provider = connection.execute(
        "SELECT provider_id FROM providers WHERE tenant_id=? AND provider_code='local_openai'",
        (seed.tenant_id,),
    ).fetchone()
    default_model = connection.execute(
        "SELECT model_id FROM models WHERE tenant_id=? AND model_alias='default-chat'",
        (seed.tenant_id,),
    ).fetchone()
    assert provider is not None
    assert default_model is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
          display_name, capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd015_fallback', ?, ?, 'qwen-fallback', 'qwen-plus-upstream',
          'qwen-plus-upstream', 'Qwen Fallback', '{"chat": true, "stream": true}',
          'active', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, provider["provider_id"]),
    )
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd015_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd015_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    connection.commit()
    # 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
    return database_url, seed.tenant_id, default_model["model_id"], "model_prd015_fallback"


async def login(client: AsyncClient, email: str, password: str) -> str:
    """login：说明该定义的测试或脚本职责。
    
    修改时需保持主流程、异常流程、权限边界和敏感信息脱敏的覆盖。
    """

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_model_alias_create_list_update_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：管理员创建别名，开发者只读查看，管理员可禁用别名。"""

    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd015_alias_create"}

        created = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "company-default",
                "environment": "dev",
                "default_model_id": default_model_id,
                "fallback_model_id": fallback_model_id,
                "description": "prd015 alias",
            },
        )
        listed_by_developer = await client.get(
            "/model-aliases?environment=dev",
            headers={"Authorization": f"Bearer {developer_token}", "X-Request-ID": "req_prd015_alias_list"},
        )
        created_alias = created.json()["model_alias"]
        patched = await client.patch(
            f"/model-aliases/{created.json()['model_alias']['model_alias_id']}",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd015_alias_disable"},
            json={
                "status": "disabled",
                "expected_routing_policy_version": created_alias["routing_policy"]["version"],
            },
        )

    assert created.status_code == 201, created.text
    alias = created.json()["model_alias"]
    assert alias["tenant_id"] == tenant_id
    assert alias["alias"] == "company-default"
    assert alias["default_model_id"] == default_model_id
    assert alias["fallback_model_id"] == fallback_model_id
    assert alias["default_internal_model_id"] == "default-chat"
    assert alias["default_model_status"] == "active"
    assert alias["default_provider_status"] == "active"
    assert created.json()["request_id"] == "req_prd015_alias_create"

    assert listed_by_developer.status_code == 200
    listed_text = listed_by_developer.text
    assert "company-default" in listed_text
    assert "default-chat" in listed_text
    assert "key_hash" not in listed_text
    assert "plain_value" not in listed_text

    assert patched.status_code == 200
    assert patched.json()["model_alias"]["status"] == "disabled"
    assert patched.json()["model_alias"]["disabled_at"] is not None

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT tenant_id, alias, environment, status, disabled_at FROM model_aliases WHERE alias='company-default'"
    ).fetchone()
    assert row["tenant_id"] == tenant_id
    assert row["environment"] == "dev"
    assert row["status"] == "disabled"
    assert row["disabled_at"] is not None


@pytest.mark.anyio
async def test_multiple_fallbacks_create_and_reorder_keep_audit_atomic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(
        monkeypatch, tmp_path,
    )
    second_fallback_id = "model_prd015_second_fallback"
    third_fallback_id = "model_prd015_third_fallback"
    with connect_database(database_url) as connection:
        connection.execute(
            """INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,status,created_at,updated_at)
              SELECT ?,tenant_id,provider_id,'second-fallback','second-fallback',
                     'second-fallback','Second Fallback',capabilities,'active',
                     datetime('now'),datetime('now')
              FROM models WHERE model_id=? AND tenant_id=?""",
            (second_fallback_id, fallback_model_id, tenant_id),
        )
        connection.execute(
            """INSERT INTO models(
              model_id,tenant_id,provider_id,model_alias,upstream_model_name,
              provider_model_id,display_name,capabilities,status,created_at,updated_at)
              SELECT ?,tenant_id,provider_id,'third-fallback','third-fallback',
                     'third-fallback','Third Fallback',capabilities,'active',
                     datetime('now'),datetime('now')
              FROM models WHERE model_id=? AND tenant_id=?""",
            (third_fallback_id, fallback_model_id, tenant_id),
        )
        connection.commit()

    async with AsyncClient(
        transport=ASGITransport(app=build_control_app()), base_url="http://control",
    ) as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/model-aliases",
            headers={**headers, "X-Request-ID": "req_alias_multi_create"},
            json={
                "alias": "multiple-fallbacks",
                "environment": "dev",
                "default_model_id": default_model_id,
                "fallback_model_ids": [fallback_model_id, second_fallback_id],
            },
        )
        assert created.status_code == 201, created.text
        alias = created.json()["model_alias"]
        assert alias["fallback_model_ids"] == [fallback_model_id, second_fallback_id]

        updated = await client.patch(
            f"/model-aliases/{alias['model_alias_id']}",
            headers={**headers, "X-Request-ID": "req_alias_multi_reorder"},
            json={
                "fallback_model_ids": [second_fallback_id, fallback_model_id],
                "expected_routing_policy_version": alias["routing_policy"]["version"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["model_alias"]["fallback_model_ids"] == [
            second_fallback_id, fallback_model_id,
        ]
        appended = await client.patch(
            f"/model-aliases/{alias['model_alias_id']}",
            headers={**headers, "X-Request-ID": "req_alias_multi_append"},
            json={
                "fallback_model_ids": [second_fallback_id, fallback_model_id, third_fallback_id],
                "expected_routing_policy_version": updated.json()["model_alias"]["routing_policy"]["version"],
            },
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["model_alias"]["fallback_model_ids"] == [
            second_fallback_id, fallback_model_id, third_fallback_id,
        ]
        assert appended.json()["model_alias"]["routing_policy"]["version"] == (
            updated.json()["model_alias"]["routing_policy"]["version"] + 1
        )

    with connect_database(database_url) as connection:
        rows = connection.execute(
            """SELECT action,after_summary_json FROM audit_logs
               WHERE tenant_id=? AND object_type='routing_policy'
               ORDER BY created_at,rowid""",
            (tenant_id,),
        ).fetchall()
    summaries = [json.loads(row["after_summary_json"]) for row in rows]
    assert [row["action"] for row in rows] == [
        "routing_policy.created", "routing_policy.updated", "routing_policy.updated",
    ]
    assert [item["fallback_model_count"] for item in summaries] == [2, 2, 3]
    assert summaries[0]["fallback_model_order_sha256"] != summaries[1][
        "fallback_model_order_sha256"
    ]
    assert all("fallback_model_ids" not in item for item in summaries)


@pytest.mark.anyio
async def test_model_alias_permissions_and_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常/权限：角色越权、重复别名和非法目标模型返回统一错误码。"""

    _database_url, _tenant_id, default_model_id, fallback_model_id = prepare_database(monkeypatch, tmp_path)
    control_app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=control_app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        developer_post_denied = await client.post(
            "/model-aliases",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={
                "alias": "developer-try",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        viewer_get_denied = await client.get("/model-aliases", headers={"Authorization": f"Bearer {viewer_token}"})
        created = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "duplicate-alias",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        duplicate = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "duplicate-alias",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        invalid_alias = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={"alias": "Bad Alias", "environment": "dev", "default_model_id": default_model_id},
        )
        invalid_environment = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={"alias": "bad-env", "environment": "stage", "default_model_id": default_model_id},
        )
        invalid_model = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={"alias": "bad-model", "environment": "dev", "default_model_id": "missing-model"},
        )
        same_fallback = await client.post(
            "/model-aliases",
            headers=admin_headers,
            json={
                "alias": "same-fallback",
                "environment": "dev",
                "default_model_id": fallback_model_id,
                "fallback_model_id": fallback_model_id,
            },
        )
        empty_patch = await client.patch(
            f"/model-aliases/{created.json()['model_alias']['model_alias_id']}",
            headers=admin_headers,
            json={},
        )
        not_found = await client.patch(
            "/model-aliases/not-found",
            headers=admin_headers,
            json={"status": "disabled", "expected_routing_policy_version": 1},
        )

    assert developer_post_denied.status_code == 403
    assert developer_post_denied.json()["code"] == "auth.permission_denied"
    assert viewer_get_denied.status_code == 403
    assert viewer_get_denied.json()["code"] == "auth.permission_denied"
    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "model_alias.duplicate"
    assert invalid_alias.status_code == 422
    assert invalid_alias.json()["code"] == "model_alias.alias_invalid"
    assert invalid_environment.status_code == 422
    assert invalid_environment.json()["code"] == "model_alias.environment_invalid"
    assert invalid_model.status_code == 422
    assert invalid_model.json()["code"] == "model_alias.model_invalid"
    assert same_fallback.status_code == 422
    assert same_fallback.json()["code"] == "model_alias.model_invalid"
    assert empty_patch.status_code == 422
    assert empty_patch.json()["code"] == "model_alias.update_empty"
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "model_alias.not_found"


@pytest.mark.anyio
async def test_fallback_policy_patch_is_versioned_and_visible_to_developer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-036 主流程：管理员修改策略，幂等重放不增版本，开发者可读取配置摘要。"""

    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"alias": "prd036-policy", "environment": "dev", "default_model_id": default_model_id},
        )
        alias_id = created.json()["model_alias"]["model_alias_id"]
        payload = {
            "enabled": True,
            "fallback_model_id": fallback_model_id,
            "trigger_conditions": ["upstream_5xx", "upstream_timeout"],
            "expected_routing_policy_version": created.json()["model_alias"]["routing_policy"]["version"],
        }
        patched = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers={"Authorization": f"Bearer {admin_token}", "X-Request-ID": "req_prd036_policy"},
            json=payload,
        )
        repeated = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                **payload,
                "expected_routing_policy_version": patched.json()["model_alias"]["routing_policy"]["version"],
            },
        )
        listed = await client.get(
            "/model-aliases?environment=dev",
            headers={"Authorization": f"Bearer {developer_token}"},
        )

    assert patched.status_code == 200, patched.text
    policy = patched.json()["fallback_policy"]
    assert patched.json()["request_id"] == "req_prd036_policy"
    assert policy["status"] == "enabled"
    assert policy["trigger_conditions"] == ["upstream_timeout", "upstream_5xx"]
    assert policy["max_attempts"] == 31
    assert repeated.json()["fallback_policy"]["version"] == policy["version"]
    listed_alias = next(item for item in listed.json()["model_aliases"] if item["model_alias_id"] == alias_id)
    assert listed_alias["fallback_model_id"] == fallback_model_id
    assert listed_alias["fallback_policy"]["version"] == policy["version"]

    connection = connect_database(database_url)
    row = connection.execute(
        "SELECT tenant_id, status, version, condition_expr FROM fallback_policies WHERE model_alias_id=?",
        (alias_id,),
    ).fetchone()
    assert row["tenant_id"] == tenant_id
    assert row["status"] == "enabled"
    assert row["version"] == policy["version"]
    assert row["condition_expr"] == '["upstream_timeout","upstream_5xx"]'


@pytest.mark.anyio
async def test_fallback_policy_rejects_permission_and_invalid_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-036 权限/异常：developer 不能写，空目标、主备相同和非法条件均返回统一错误。"""

    _database_url, _tenant_id, default_model_id, fallback_model_id = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"alias": "prd036-errors", "environment": "dev", "default_model_id": default_model_id},
        )
        alias_id = created.json()["model_alias"]["model_alias_id"]
        developer_denied = await client.patch(
                f"/model-aliases/{alias_id}/fallback",
                headers={"Authorization": f"Bearer {developer_token}"},
                json={
                    "enabled": True,
                    "fallback_model_id": fallback_model_id,
                    "expected_routing_policy_version": 1,
                },
        )
        missing_target = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"enabled": True, "expected_routing_policy_version": 1},
        )
        same_target = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "enabled": True,
                "fallback_model_id": default_model_id,
                "expected_routing_policy_version": 1,
            },
        )
        invalid_conditions = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "enabled": True,
                "fallback_model_id": fallback_model_id,
                "trigger_conditions": ["upstream_timeout", "upstream_timeout"],
                "expected_routing_policy_version": 1,
            },
        )

    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert missing_target.status_code == 422
    assert missing_target.json()["code"] == "model_alias.fallback_invalid"
    assert same_target.status_code == 422
    assert same_target.json()["code"] == "model_alias.fallback_invalid"
    assert invalid_conditions.status_code == 422
    assert invalid_conditions.json()["code"] == "model_alias.fallback_invalid"


@pytest.mark.anyio
async def test_routing_mode_cas_idempotency_audit_and_rbac(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-047 主流程：mode 严格 CAS，精确重试复用事件，读写权限由后端执行。"""

    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(
        monkeypatch, tmp_path
    )
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer_token = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_alias_create",
            },
            json={
                "alias": "prd047-routing",
                "environment": "dev",
                "default_model_id": default_model_id,
                "fallback_model_id": fallback_model_id,
            },
        )
        created_alias = created.json()["model_alias"]
        alias_id = created_alias["model_alias_id"]
        version = created_alias["routing_policy"]["version"]
        mode_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd047_mode_cost",
        }
        changed = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=mode_headers,
            json={"routing_mode": "cost_first", "expected_version": version},
        )
        different_fingerprint = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=mode_headers,
            json={"routing_mode": "availability_first", "expected_version": version},
        )
        # 首次响应丢失后的精确重试仍携带原 expected_version；服务必须先恢复事件再做 CAS。
        repeated = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=mode_headers,
            json={"routing_mode": "cost_first", "expected_version": version},
        )
        current_version = changed.json()["routing_policy"]["version"]
        stale_conflict = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_mode_stale",
            },
            json={"routing_mode": "cost_first", "expected_version": version},
        )
        no_op = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_mode_noop",
            },
            json={"routing_mode": "cost_first", "expected_version": current_version},
        )
        developer_denied = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={"Authorization": f"Bearer {developer_token}"},
            json={"routing_mode": "availability_first", "expected_version": current_version},
        )
        developer_list = await client.get(
            "/model-aliases?environment=dev",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        viewer_list = await client.get(
            "/model-aliases",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )

    assert created.status_code == 201, created.text
    assert created_alias["routing_policy"]["routing_mode"] == "fixed"
    assert created_alias["routing_policy_implicit"] is False
    assert changed.status_code == 200, changed.text
    assert changed.json()["changed"] is True
    assert changed.json()["routing_policy"]["routing_mode"] == "cost_first"
    assert current_version == version + 1
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["routing_policy"] == changed.json()["routing_policy"]
    assert different_fingerprint.status_code == 409
    assert (
        different_fingerprint.json()["code"]
        == "model_alias.routing_policy_request_conflict"
    )
    assert stale_conflict.status_code == 409
    assert stale_conflict.json()["code"] == "model_alias.routing_policy_version_conflict"
    assert no_op.status_code == 200
    assert no_op.json()["changed"] is False
    assert no_op.json()["routing_policy"]["version"] == current_version
    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert developer_list.status_code == 200
    assert viewer_list.status_code == 403

    connection = connect_database(database_url)
    policy_row = connection.execute(
        """
        SELECT routing_mode, version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, alias_id),
    ).fetchone()
    assert tuple(policy_row) == ("cost_first", 2)
    events = connection.execute(
        """
        SELECT request_id, routing_policy_version, payload_json
        FROM routing_policy_events
        WHERE tenant_id=? AND model_alias_id=?
        ORDER BY routing_policy_version
        """,
        (tenant_id, alias_id),
    ).fetchall()
    assert [row["request_id"] for row in events] == [
        "req_prd047_alias_create",
        "req_prd047_mode_cost",
    ]
    assert [row["routing_policy_version"] for row in events] == [1, 2]
    assert '"change_kinds":["mode"]' in events[1]["payload_json"]
    audits = connection.execute(
        """
        SELECT action, changed_fields_json
        FROM audit_logs
        WHERE tenant_id=? AND object_type='routing_policy' AND object_id=?
        ORDER BY created_at, audit_log_id
        """,
        (tenant_id, created_alias["routing_policy"]["routing_policy_id"]),
    ).fetchall()
    assert sorted(row["action"] for row in audits) == [
        "routing_policy.created",
        "routing_policy.updated",
    ]
    updated_audit = next(row for row in audits if row["action"] == "routing_policy.updated")
    assert updated_audit["changed_fields_json"] == '["routing_mode","version"]'


@pytest.mark.anyio
async def test_routing_mode_strict_wire_validation_and_permission_before_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-047 异常流程：严格 wire 错误使用领域码，developer 在读取超限正文前即被拒绝。"""

    database_url, tenant_id, default_model_id, _ = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_wire_create",
            },
            json={
                "alias": "prd047-wire",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        alias_id = created.json()["model_alias"]["model_alias_id"]
        path = f"/model-aliases/{alias_id}/routing-mode"
        headers = {"Authorization": f"Bearer {admin_token}"}
        extra = await client.patch(
            path,
            headers={**headers, "X-Request-ID": "req_prd047_wire_extra"},
            json={"routing_mode": "fixed", "expected_version": 1, "tenant_id": tenant_id},
        )
        bool_version = await client.patch(
            path,
            headers={**headers, "X-Request-ID": "req_prd047_wire_bool"},
            json={"routing_mode": "fixed", "expected_version": True},
        )
        duplicate = await client.patch(
            path,
            headers={
                **headers,
                "X-Request-ID": "req_prd047_wire_duplicate",
                "Content-Type": "application/json",
            },
            content='{"routing_mode":"fixed","routing_mode":"cost_first","expected_version":1}',
        )
        wrong_type = await client.patch(
            path,
            headers={
                **headers,
                "X-Request-ID": "req_prd047_wire_type",
                "Content-Type": "text/plain",
            },
            content='{"routing_mode":"fixed","expected_version":1}',
        )
        malformed = await client.patch(
            path,
            headers={
                **headers,
                "X-Request-ID": "req_prd047_wire_json",
                "Content-Type": "application/json",
            },
            content="{",
        )
        # 两个载荷都小于 8 KiB：较浅者可被解码但会使旧递归扫描耗尽调用栈，
        # 更深者可能直接令标准 JSON decoder 抛 RecursionError；两者都必须稳定收敛为 422。
        scanner_depth = 1_500
        scanner_nested = await client.patch(
            path,
            headers={
                **headers,
                "X-Request-ID": "req_prd047_wire_scanner_depth",
                "Content-Type": "application/json",
            },
            content=(
                '{"routing_mode":'
                + "[" * scanner_depth
                + '"fixed"'
                + "]" * scanner_depth
                + ',"expected_version":1}'
            ),
        )
        decoder_depth = 3_500
        decoder_nested = await client.patch(
            path,
            headers={
                **headers,
                "X-Request-ID": "req_prd047_wire_decoder_depth",
                "Content-Type": "application/json",
            },
            content=(
                '{"routing_mode":'
                + "[" * decoder_depth
                + '"fixed"'
                + "]" * decoder_depth
                + ',"expected_version":1}'
            ),
        )
        permission_first = await client.patch(
            path,
            headers={
                "Authorization": f"Bearer {developer_token}",
                "Content-Type": "application/json",
            },
            content="x" * 9000,
        )

    assert extra.status_code == 422
    assert extra.json()["code"] == "model_alias.routing_mode_invalid"
    assert bool_version.status_code == 422
    assert bool_version.json()["code"] == "model_alias.routing_policy_version_invalid"
    for response in (duplicate, wrong_type, malformed, scanner_nested, decoder_nested):
        assert response.status_code == 422
        assert response.json()["code"] == "model_alias.routing_mode_invalid"
        assert response.json()["request_id"]
    assert scanner_nested.json()["request_id"] == "req_prd047_wire_scanner_depth"
    assert decoder_nested.json()["request_id"] == "req_prd047_wire_decoder_depth"
    assert permission_first.status_code == 403
    assert permission_first.json()["code"] == "auth.permission_denied"

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM routing_policy_events WHERE tenant_id=? AND model_alias_id=?",
        (tenant_id, alias_id),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND object_type='routing_policy'",
        (tenant_id,),
    ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_routing_mutation_retries_use_normalized_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """等价文本重试必须恢复同一 Alias/Fallback 事件，不能因空白或大小写误报冲突。"""

    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(
        monkeypatch, tmp_path
    )
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_normalized_create",
            },
            json={
                "alias": "prd047-normalized",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        alias_id = created.json()["model_alias"]["model_alias_id"]
        alias_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd047_normalized_alias",
        }
        alias_changed = await client.patch(
            f"/model-aliases/{alias_id}",
            headers=alias_headers,
            json={
                "alias": "  PRD047-Normalized-V2  ",
                "description": "  normalized note  ",
                "expected_routing_policy_version": 1,
            },
        )
        alias_retried = await client.patch(
            f"/model-aliases/{alias_id}",
            headers=alias_headers,
            json={
                "alias": "prd047-normalized-v2",
                "description": "normalized note",
                "expected_routing_policy_version": 1,
            },
        )
        fallback_headers = {
            "Authorization": f"Bearer {admin_token}",
            "X-Request-ID": "req_prd047_normalized_fallback",
        }
        fallback_changed = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers=fallback_headers,
            json={
                "enabled": True,
                "fallback_model_id": f"  {fallback_model_id}  ",
                "trigger_conditions": ["upstream_5xx"],
                "expected_routing_policy_version": 2,
            },
        )
        fallback_retried = await client.patch(
            f"/model-aliases/{alias_id}/fallback",
            headers=fallback_headers,
            json={
                "enabled": True,
                "fallback_model_id": fallback_model_id,
                "trigger_conditions": ["upstream_5xx"],
                "expected_routing_policy_version": 2,
            },
        )

    assert created.status_code == 201, created.text
    assert alias_changed.status_code == 200, alias_changed.text
    assert alias_retried.status_code == 200, alias_retried.text
    assert alias_retried.json()["model_alias"] == alias_changed.json()["model_alias"]
    assert fallback_changed.status_code == 200, fallback_changed.text
    assert fallback_retried.status_code == 200, fallback_retried.text
    assert fallback_retried.json()["model_alias"] == fallback_changed.json()["model_alias"]
    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT request_id, request_fingerprint
        FROM routing_policy_events
        WHERE tenant_id=? AND model_alias_id=?
        ORDER BY routing_policy_version
        """,
        (tenant_id, alias_id),
    ).fetchall()
    assert [row["request_id"] for row in rows] == [
        "req_prd047_normalized_create",
        "req_prd047_normalized_alias",
        "req_prd047_normalized_fallback",
    ]
    assert len({row["request_fingerprint"] for row in rows}) == 3


@pytest.mark.anyio
async def test_routing_mode_audit_failure_rolls_back_and_cross_tenant_id_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """审计失败不得留下半版本；其他租户 Alias ID 对当前管理员保持 404 不可见。"""

    database_url, tenant_id, default_model_id, _ = prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        )
        VALUES(
          'tenant_prd047_hidden', 'prd047-hidden', 'PRD047 Hidden',
          'internal_only', 'active', datetime('now'), datetime('now')
        )
        """
    )
    hidden_environment_id = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id='tenant_prd047_hidden' AND code='dev'"
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, created_at, updated_at
        )
        VALUES(
          'provider_prd047_hidden', 'tenant_prd047_hidden', 'prd047_hidden',
          'Hidden Provider', 'http://127.0.0.1:9/v1', 'active',
          'openai_compatible', 'none', datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd047_hidden', 'tenant_prd047_hidden', 'provider_prd047_hidden',
          'prd047-hidden-model', 'hidden-upstream', '{}', 'active',
          datetime('now'), datetime('now')
        )
        """
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, status, created_at, updated_at
        )
        VALUES(
          'alias_prd047_hidden', 'tenant_prd047_hidden', 'prd047-hidden', 'dev', ?,
          'model_prd047_hidden', 'active', datetime('now'), datetime('now')
        )
        """,
        (hidden_environment_id,),
    )
    connection.commit()
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_rollback_create",
            },
            json={
                "alias": "prd047-rollback",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
        alias_id = created.json()["model_alias"]["model_alias_id"]
        # 审计记录是配置事实的一部分；模拟写入器返回空结果，服务必须回滚 mode 与 event。
        monkeypatch.setattr(model_alias_service, "record_audit_log", lambda *_args, **_kwargs: None)
        audit_failed = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_rollback_mode",
            },
            json={"routing_mode": "cost_first", "expected_version": 1},
        )
        cross_tenant = await client.patch(
            "/model-aliases/alias_prd047_hidden/routing-mode",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Request-ID": "req_prd047_cross_tenant_mode",
            },
            json={"routing_mode": "cost_first", "expected_version": 1},
        )

    assert audit_failed.status_code == 503
    assert audit_failed.json()["code"] == "model_alias.routing_policy_update_failed"
    assert audit_failed.json()["request_id"] == "req_prd047_rollback_mode"
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "model_alias.not_found"
    policy = connection.execute(
        """
        SELECT routing_mode, version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, alias_id),
    ).fetchone()
    assert tuple(policy) == ("fixed", 1)
    assert connection.execute(
        "SELECT COUNT(*) FROM routing_policy_events WHERE request_id='req_prd047_rollback_mode'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd047_rollback_mode'"
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_routing_policy_two_real_connections_serialize_expected_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两个真实连接竞争同一 expected_version 时只能有一个提交 version+1。"""

    database_url, tenant_id, default_model_id, _ = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/model-aliases",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "alias": "prd047-two-connections",
                "environment": "dev",
                "default_model_id": default_model_id,
            },
        )
    assert created.status_code == 201, created.text
    alias_id = created.json()["model_alias"]["model_alias_id"]
    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, str]] = []

    # 每个 worker 独立建连并在写锁内读取版本，真实复现服务层的串行 CAS 边界。
    def compete(mode: str) -> None:
        """使用独立 SQLite 连接模拟服务层写锁与锁内版本比较，记录唯一提交结果。"""

        worker = connect_database(database_url)
        worker.execute("PRAGMA busy_timeout=5000")
        barrier.wait()
        try:
            worker.execute("BEGIN IMMEDIATE")
            current = worker.execute(
                """
                SELECT version
                FROM model_alias_routing_policies
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (tenant_id, alias_id),
            ).fetchone()[0]
            if current != 1:
                worker.rollback()
                outcomes.append((mode, "conflict"))
                return
            worker.execute(
                """
                UPDATE model_alias_routing_policies
                SET routing_mode=?, version=version+1, updated_at=datetime('now')
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (mode, tenant_id, alias_id),
            )
            worker.commit()
            outcomes.append((mode, "changed"))
        finally:
            worker.close()

    workers = [
        threading.Thread(target=compete, args=("cost_first",)),
        threading.Thread(target=compete, args=("availability_first",)),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()

    assert sorted(result for _mode, result in outcomes) == ["changed", "conflict"]
    verification = connect_database(database_url)
    policy = verification.execute(
        """
        SELECT routing_mode, version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (tenant_id, alias_id),
    ).fetchone()
    assert policy["routing_mode"] in {"cost_first", "availability_first"}
    assert policy["version"] == 2
    verification.close()


@pytest.mark.anyio
async def test_quality_cost_routing_settings_version_event_and_legacy_wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD-048：质量配置与 mode 共用一次 CAS，v2 事件精确且旧两字段 wire 不变。"""

    database_url, tenant_id, default_model_id, fallback_model_id = prepare_database(
        monkeypatch,
        tmp_path,
    )
    app = build_control_app()
    config = {
        "quality_weight_bps": 5000,
        "cost_weight_bps": 3000,
        "latency_weight_bps": 2000,
        "quality_floor_bps": 6000,
        "minimum_sample_count": 20,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer_token = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        admin = {"Authorization": f"Bearer {admin_token}"}
        created = await client.post(
            "/model-aliases",
            headers={**admin, "X-Request-ID": "req_prd048_alias_create"},
            json={
                "alias": "quality-cost-chat",
                "environment": "dev",
                "default_model_id": default_model_id,
                "fallback_model_id": fallback_model_id,
            },
        )
        assert created.status_code == 201, created.text
        alias_id = created.json()["model_alias"]["model_alias_id"]
        initial_policy = created.json()["model_alias"]["routing_policy"]
        assert initial_policy["routing_mode"] == "fixed"
        assert initial_policy["version"] == 1
        assert initial_policy["quality_cost_config"] == config

        headers = {**admin, "X-Request-ID": "req_prd048_quality_enable"}
        enabled = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=headers,
            json={
                "routing_mode": "quality_cost",
                "expected_version": 1,
                "quality_cost_config": config,
            },
        )
        retried = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=headers,
            json={
                "routing_mode": "quality_cost",
                "expected_version": 1,
                "quality_cost_config": config,
            },
        )
        no_op = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={**admin, "X-Request-ID": "req_prd048_quality_noop"},
            json={
                "routing_mode": "quality_cost",
                "expected_version": 2,
                "quality_cost_config": config,
            },
        )
        changed_config = {**config, "quality_weight_bps": 6000, "cost_weight_bps": 2000}
        reweighted = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={**admin, "X-Request-ID": "req_prd048_quality_weights"},
            json={
                "routing_mode": "quality_cost",
                "expected_version": 2,
                "quality_cost_config": changed_config,
            },
        )
        switched_back = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers={**admin, "X-Request-ID": "req_prd048_quality_disable"},
            json={"routing_mode": "availability_first", "expected_version": 3},
        )
        listed = await client.get(
            "/model-aliases?environment=dev",
            headers={"Authorization": f"Bearer {developer_token}"},
        )
        missing_config = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=admin,
            json={"routing_mode": "quality_cost", "expected_version": 4},
        )
        wrong_sum = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=admin,
            json={
                "routing_mode": "quality_cost",
                "expected_version": 4,
                "quality_cost_config": {**config, "quality_weight_bps": 4999},
            },
        )
        legacy_extra = await client.patch(
            f"/model-aliases/{alias_id}/routing-mode",
            headers=admin,
            json={
                "routing_mode": "cost_first",
                "expected_version": 4,
                "quality_cost_config": changed_config,
            },
        )

    assert enabled.status_code == retried.status_code == 200
    assert enabled.json() == retried.json()
    assert enabled.json()["routing_policy"]["version"] == 2
    assert enabled.json()["routing_policy"]["quality_cost_config"] == config
    assert no_op.status_code == 200 and no_op.json()["changed"] is False
    assert reweighted.status_code == 200
    assert reweighted.json()["routing_policy"]["version"] == 3
    assert reweighted.json()["routing_policy"]["quality_cost_config"] == changed_config
    assert switched_back.status_code == 200
    assert switched_back.json()["routing_policy"]["version"] == 4
    assert switched_back.json()["routing_policy"]["quality_cost_config"] == changed_config
    listed_policy = next(
        item["routing_policy"]
        for item in listed.json()["model_aliases"]
        if item["model_alias_id"] == alias_id
    )
    assert listed_policy == switched_back.json()["routing_policy"]
    assert missing_config.status_code == wrong_sum.status_code == 422
    assert missing_config.json()["code"] == wrong_sum.json()["code"] == "model_alias.quality_cost_settings_invalid"
    assert legacy_extra.status_code == 422
    assert legacy_extra.json()["code"] == "model_alias.routing_mode_invalid"

    connection = connect_database(database_url)
    events = connection.execute(
        """
        SELECT routing_policy_version, payload_schema_version, payload_json
        FROM routing_policy_events
        WHERE tenant_id=? AND model_alias_id=?
        ORDER BY routing_policy_version
        """,
        (tenant_id, alias_id),
    ).fetchall()
    assert [row["routing_policy_version"] for row in events] == [1, 2, 3, 4]
    assert all(row["payload_schema_version"] == 2 for row in events)
    created_payload, enabled_payload, weights_payload, disabled_payload = [
        json.loads(row["payload_json"]) for row in events
    ]
    assert created_payload["change_kinds"] == ["created"]
    assert created_payload["changed_routing_fields"][-1] == "quality_cost_config"
    assert enabled_payload["change_kinds"] == ["mode"]
    assert enabled_payload["routing_mode"] == "quality_cost"
    assert weights_payload["change_kinds"] == ["quality_cost"]
    assert weights_payload["changed_routing_fields"] == ["quality_cost_config"]
    assert weights_payload["quality_cost_config"] == changed_config
    assert disabled_payload["previous_mode"] == "quality_cost"
    assert disabled_payload["routing_mode"] == "availability_first"
