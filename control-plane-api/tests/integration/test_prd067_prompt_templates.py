"""PRD-067 企业 Prompt 模板 Control 主流程、异常、权限、租户与隐私集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import metahub_control.prompt_templates.service as template_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.prompt_templates.routes import router as prompt_template_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试凭据和金丝雀只服务隔离库与内存请求；断言禁止把这些值写入日志或普通数据库列。
_ADMIN_PASSWORD = "MetaHubLocal!2026"
_VIEWER_PASSWORD = "PRD067Viewer!2026"
_SECOND_PASSWORD = "PRD067Second!2026"
_AUTH_SECRET = "prd067-auth-token-secret-only-0123456789abcdef"
_TEMPLATE_SECRET = "prd067-template-encryption-only-0123456789abcdef"
_PROMPT_CANARY = "PRD067_PROMPT_MUST_ONLY_EXIST_IN_CIPHERTEXT"


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str, str]:
    """迁移/seed 隔离库，并建立 Project、ModelAlias、Viewer 与第二租户 Admin。

    返回数据库 URL、默认 tenant ID、Project ID 与 ModelAlias ID。所有密码进入数据库前
    只保存 PBKDF2 hash；Prompt 金丝雀只进入后续 API 请求和认证密文。
    """

    database_url = _sqlite_url(tmp_path / "prd067-prompt-templates.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", _AUTH_SECRET)
    monkeypatch.setenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET", _TEMPLATE_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    admin = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (seed.tenant_id,),
    ).fetchone()
    environment = connection.execute(
        """
        SELECT environment_id,code FROM environments
        WHERE tenant_id=? AND status='active' ORDER BY environment_id LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    model = connection.execute(
        """
        SELECT model_id FROM models
        WHERE tenant_id=? AND status='active' ORDER BY model_id LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert admin is not None and environment is not None and model is not None
    now = "2026-08-02T08:00:00Z"
    project_id = "prj_prd067_primary"
    alias_id = "mal_prd067_primary"
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?,?,?)
        """,
        (
            project_id,
            seed.tenant_id,
            "prd067-primary",
            "PRD067 Primary",
            "",
            admin["user_id"],
            admin["user_id"],
            admin["user_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,default_model_id,
          fallback_model_id,status,description,created_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,NULL,'active',?,?,?,?)
        """,
        (
            alias_id,
            seed.tenant_id,
            "prd067-primary",
            environment["code"],
            environment["environment_id"],
            model["model_id"],
            "PRD067 推荐模型",
            admin["user_id"],
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'viewer','active',?,?)
        """,
        (
            "user_prd067_viewer",
            seed.tenant_id,
            "viewer-prd067@example.com",
            "PRD067 Viewer",
            hash_password(_VIEWER_PASSWORD),
            now,
            now,
        ),
    )
    second_tenant_id = "tenant_prd067_second"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,'prd067-second','PRD067 Second','internal_only','active',?,?)
        """,
        (second_tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'admin','active',?,?)
        """,
        (
            "user_prd067_second_admin",
            second_tenant_id,
            "admin-prd067-second@example.com",
            "PRD067 Second Admin",
            hash_password(_SECOND_PASSWORD),
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()
    # 返回当前隔离库 owner 资源，调用者不得从请求正文自行提供 tenant 或 actor。
    return database_url, seed.tenant_id, project_id, alias_id


def _build_app():
    """构造只装配认证与 Prompt 模板路由的隔离 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-067 Prompt Template Test API")
    app.include_router(auth_router)
    app.include_router(prompt_template_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    request_id: str,
) -> str:
    """登录隔离测试用户，并仅向当前调用栈返回 Session Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200, response.text
    # Session Token 仅在测试调用栈传递，不打印到断言、日志或落盘证据。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造认证与稳定 request ID Header；Token 不进入断言输出。"""

    # tenant/actor 始终由 Bearer Session 恢复，测试 Header 不能注入这些 owner 字段。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _create_payload(project_id: str, alias_id: str) -> dict[str, object]:
    """构造包含一个严格变量和 selected Project 的 v1 请求。"""

    return {
        "name": "客户回复模板",
        "description": "用于客户成功团队",
        "content": f"{_PROMPT_CANARY}: ${{customer_name}}",
        "variables": [
            {
                "name": "customer_name",
                "label": "客户名称",
                "required": True,
                "max_chars": 80,
            }
        ],
        "project_scope": "selected",
        "project_ids": [project_id],
        "recommended_model_alias_ids": [alias_id],
        "change_summary": "创建首个受控版本",
    }


@pytest.mark.anyio
async def test_prompt_template_main_flow_is_immutable_idempotent_and_encrypted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """覆盖创建、读取、重放冲突、发布、禁用及明文不落库主流程。"""

    database_url, tenant_id, project_id, alias_id = _prepare_database(monkeypatch, tmp_path)
    # 隔离库没有 Webhook Endpoint；真实派发器只验证信封并把 source event 终结为 dispatched，
    # 不发生外部网络访问，从而同时覆盖提交后状态跟踪合同。
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=_ADMIN_PASSWORD,
            request_id="prd067-login-admin",
        )
        options = await client.get(
            "/prompt-templates/options",
            headers=_headers(token, "prd067-options"),
        )
        assert options.status_code == 200
        assert options.headers["cache-control"] == "no-store"
        assert {item["project_id"] for item in options.json()["projects"]} == {project_id}
        assert {item["model_alias_id"] for item in options.json()["model_aliases"]} == {alias_id}

        payload = _create_payload(project_id, alias_id)
        created = await client.post(
            "/prompt-templates",
            json=payload,
            headers=_headers(token, "prd067-create-stable"),
        )
        assert created.status_code == 201, created.text
        created_body = created.json()
        template_id = created_body["template"]["prompt_template_id"]
        version_id = created_body["version"]["prompt_template_version_id"]
        assert created_body["replayed"] is False
        assert created_body["template"]["metadata_revision"] == 1
        assert created_body["version"]["version_number"] == 1
        assert created_body["event_id"].startswith("ptev_")
        assert _PROMPT_CANARY not in created.text

        replay = await client.post(
            "/prompt-templates",
            json=payload,
            headers=_headers(token, "prd067-create-stable"),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["version"]["prompt_template_version_id"] == version_id
        conflicting_payload = dict(payload)
        conflicting_payload["name"] = "冲突请求"
        conflict = await client.post(
            "/prompt-templates",
            json=conflicting_payload,
            headers=_headers(token, "prd067-create-stable"),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "prompt_template.idempotency_conflict"

        listing = await client.get(
            "/prompt-templates?status=active&limit=10&offset=0",
            headers=_headers(token, "prd067-list"),
        )
        detail = await client.get(
            f"/prompt-templates/{template_id}",
            headers=_headers(token, "prd067-detail"),
        )
        definition = await client.get(
            f"/prompt-templates/{template_id}/versions/{version_id}",
            headers=_headers(token, "prd067-definition"),
        )
        assert listing.status_code == detail.status_code == definition.status_code == 200
        assert listing.json()["total"] == 1
        assert detail.json()["versions"][0]["project_ids"] == [project_id]
        assert definition.json()["definition"]["content"].startswith(_PROMPT_CANARY)

        v2_payload = {
            "expected_revision": 1,
            "content": "第二版固定回复",
            "variables": [],
            "project_scope": "all",
            "project_ids": [],
            "recommended_model_alias_ids": [],
            "change_summary": "发布第二版",
        }
        published = await client.post(
            f"/prompt-templates/{template_id}/versions",
            json=v2_payload,
            headers=_headers(token, "prd067-publish-v2"),
        )
        assert published.status_code == 201, published.text
        assert published.json()["version"]["version_number"] == 2
        assert published.json()["template"]["metadata_revision"] == 2
        stale = await client.patch(
            f"/prompt-templates/{template_id}",
            json={"expected_revision": 1, "status": "disabled"},
            headers=_headers(token, "prd067-stale-patch"),
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "prompt_template.revision_conflict"
        disabled = await client.patch(
            f"/prompt-templates/{template_id}",
            json={"expected_revision": 2, "status": "disabled"},
            headers=_headers(token, "prd067-disable"),
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["template"]["status"] == "disabled"
        assert disabled.json()["template"]["metadata_revision"] == 3

    connection = connect_database(database_url)
    version_row = connection.execute(
        """
        SELECT definition_ciphertext FROM prompt_template_versions
        WHERE tenant_id=? AND prompt_template_version_id=?
        """,
        (tenant_id, version_id),
    ).fetchone()
    assert version_row is not None
    assert str(version_row["definition_ciphertext"]).startswith("ptpl-aesgcm-v1:v1:")
    dump = "\n".join(connection.iterdump())
    assert _PROMPT_CANARY not in dump
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND request_id='prd067-create-stable'",
        (tenant_id,),
    ).fetchone()[0] == 2
    event_payload = connection.execute(
        """
        SELECT payload_json,delivery_status FROM prompt_template_events
        WHERE tenant_id=? AND prompt_template_version_id=?
        """,
        (tenant_id, version_id),
    ).fetchone()
    assert event_payload is not None
    assert event_payload["delivery_status"] == "dispatched"
    assert set(json.loads(event_payload["payload_json"])) == {
        "template_id",
        "version_id",
        "version_number",
        "template_revision",
        "project_scope",
        "project_count",
        "recommended_model_count",
    }
    connection.close()


@pytest.mark.anyio
async def test_prompt_template_permissions_precede_body_and_cross_tenant_is_404(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Viewer 管理请求在正文解析前 403，跨租户模板与不存在 ID 保持同形 404。"""

    _prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(template_service, "dispatch_prompt_template_event", lambda *_: None)
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        viewer_token = await _login(
            client,
            tenant_slug="default",
            email="viewer-prd067@example.com",
            password=_VIEWER_PASSWORD,
            request_id="prd067-login-viewer",
        )
        denied_read = await client.get(
            "/prompt-templates",
            headers=_headers(viewer_token, "prd067-viewer-read"),
        )
        denied_write = await client.post(
            "/prompt-templates",
            content=b"{not-json",
            headers={
                **_headers(viewer_token, "prd067-viewer-write"),
                "Content-Type": "application/json",
            },
        )
        assert denied_read.status_code == denied_write.status_code == 403
        assert denied_write.json()["code"] == "auth.permission_denied"
        # 权限依赖在模板路由函数之前失败，必须由最外层路径策略给错误响应补 no-store；
        # 仅断言成功响应会漏掉最敏感的授权状态缓存旁路。
        assert denied_read.headers["cache-control"] == "no-store"
        assert denied_write.headers["cache-control"] == "no-store"

        second_token = await _login(
            client,
            tenant_slug="prd067-second",
            email="admin-prd067-second@example.com",
            password=_SECOND_PASSWORD,
            request_id="prd067-login-second",
        )
        second_payload = {
            "name": "第二租户模板",
            "description": "",
            "content": "固定内容",
            "variables": [],
            "project_scope": "all",
            "project_ids": [],
            "recommended_model_alias_ids": [],
            "change_summary": "初始化",
        }
        second_created = await client.post(
            "/prompt-templates",
            json=second_payload,
            headers=_headers(second_token, "prd067-second-create"),
        )
        assert second_created.status_code == 201, second_created.text
        second_template_id = second_created.json()["template"]["prompt_template_id"]

        default_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=_ADMIN_PASSWORD,
            request_id="prd067-login-default-again",
        )
        cross_tenant = await client.get(
            f"/prompt-templates/{second_template_id}",
            headers=_headers(default_token, "prd067-cross-tenant"),
        )
        missing = await client.get(
            "/prompt-templates/ptpl_00000000000000000000000000000000",
            headers=_headers(default_token, "prd067-missing"),
        )
        assert cross_tenant.status_code == missing.status_code == 404
        assert cross_tenant.json()["code"] == missing.json()["code"]
        assert cross_tenant.headers["cache-control"] == "no-store"
        assert missing.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_prompt_template_missing_secret_fails_closed_without_breaking_login(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """独立模板密钥缺失只使模板写/定义读取 503，认证与安全 metadata 列表仍可运行。"""

    _prepare_database(monkeypatch, tmp_path)
    monkeypatch.delenv("METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET")
    app = _build_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=_ADMIN_PASSWORD,
            request_id="prd067-login-no-template-secret",
        )
        listing = await client.get(
            "/prompt-templates",
            headers=_headers(token, "prd067-list-no-secret"),
        )
        create = await client.post(
            "/prompt-templates",
            json={
                "name": "不可创建",
                "description": "",
                "content": "固定内容",
                "variables": [],
                "project_scope": "all",
                "project_ids": [],
                "recommended_model_alias_ids": [],
                "change_summary": "初始化",
            },
            headers=_headers(token, "prd067-create-no-secret"),
        )
        assert listing.status_code == 200
        assert create.status_code == 503
        assert create.json()["code"] == "prompt_template.unavailable"
