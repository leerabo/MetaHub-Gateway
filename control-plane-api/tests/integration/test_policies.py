"""PRD-044 策略 DSL 控制面主流程、权限、租户、事件与异常集成测试。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.policies.routes import router as policy_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.webhooks import WEBHOOK_SUBSCRIBABLE_EVENT_TYPES

# 四类本地测试口令只用于隔离认证夹具，绝不写入日志、审计、事件或生产配置。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"


class _ConnectionProbe:
    """代理真实 SQLite 连接，记录事务顺序并可在 commit 前稳定注入故障。

    代理不改写 SQL 或参数，未覆盖的方法全部委托真实连接；因此测试仍会执行生产迁移、
    复合外键和触发器，只在指定提交边界模拟磁盘/锁异常。
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        index: int,
        timeline: list[str],
        *,
        fail_commit: bool = False,
    ) -> None:
        """保存真实连接、调用序号和共享时间线，提交故障默认关闭。"""

        self._connection = connection
        self._index = index
        self._timeline = timeline
        self._fail_commit = fail_commit

    def execute(self, sql: str, parameters=()):
        """原样执行 SQL；仅把 BEGIN IMMEDIATE 记入时间线供解锁顺序断言。"""

        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._timeline.append(f"begin:{self._index}")
        return self._connection.execute(sql, parameters)

    def commit(self) -> None:
        """记录提交；启用故障时在真实 commit 前抛 SQLite 异常，确保随后可 rollback。"""

        self._timeline.append(f"commit:{self._index}")
        if self._fail_commit:
            # 故障发生在真实提交前，测试才能确定性验证事件行未落库而不是未知提交结果。
            raise sqlite3.OperationalError("synthetic policy commit failure")
        self._connection.commit()

    def rollback(self) -> None:
        """回滚真实事务并记录顺序，用于证明失败事件在写锁释放之后才开始。"""

        self._timeline.append(f"rollback:{self._index}")
        self._connection.rollback()

    def __getattr__(self, name: str):
        """把 row_factory 等未覆写能力委托给真实 SQLite 连接。"""

        return getattr(self._connection, name)


def sqlite_url(path: Path) -> str:
    """把临时数据库路径转换为项目支持的 sqlite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证与 Policy API 的隔离测试应用。"""

    app = create_service_app(
        "control-plane-api",
        "Policy Test Control API",
        extra_cors_allow_headers=("If-Match",),
        extra_cors_expose_headers=("ETag",),
    )
    app.include_router(auth_router)
    app.include_router(policy_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """准备两租户、三基础角色以及 active/disabled/跨租户模型。"""

    database_url = sqlite_url(tmp_path / "metahub.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-policy-auth-secret")
    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", "5")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        [
            (
                "user_prd044_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
            ),
            (
                "user_prd044_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
            ),
        ],
    )
    active_model = connection.execute(
        "SELECT model_id, provider_id FROM models WHERE tenant_id=? AND status='active' LIMIT 1",
        (seed.tenant_id,),
    ).fetchone()
    assert active_model is not None
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd044_disabled', ?, ?, 'prd044-disabled', 'prd044-disabled',
          'prd044-disabled', '{}', 'disabled', datetime('now'), datetime('now')
        )
        """,
        (seed.tenant_id, active_model["provider_id"]),
    )

    other_tenant_id = "tenant_prd044_other"
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        )
        VALUES(
          ?, 'prd044-other', 'PRD044 Other', 'internal_only', 'active',
          datetime('now'), datetime('now')
        )
        """,
        (other_tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash, role, status,
          created_at, updated_at
        )
        VALUES(
          'user_prd044_other_admin', ?, 'admin@other.example.com', 'Other Admin', ?,
          'admin', 'active', datetime('now'), datetime('now')
        )
        """,
        (other_tenant_id, hash_password(OTHER_ADMIN_PASSWORD)),
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, created_at, updated_at
        )
        VALUES(
          'provider_prd044_other', ?, 'prd044-other', 'Other Provider',
          'https://example.invalid/v1', 'active', datetime('now'), datetime('now')
        )
        """,
        (other_tenant_id,),
    )
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, capabilities, status, created_at, updated_at
        )
        VALUES(
          'model_prd044_other', ?, 'provider_prd044_other', 'prd044-other', 'prd044-other',
          'prd044-other', '{}', 'active', datetime('now'), datetime('now')
        )
        """,
        (other_tenant_id,),
    )
    connection.commit()
    return {
        "database_url": database_url,
        "tenant_id": seed.tenant_id,
        "active_model_id": active_model["model_id"],
        "disabled_model_id": "model_prd044_disabled",
        "other_tenant_id": other_tenant_id,
        "other_model_id": "model_prd044_other",
    }


async def login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录指定租户用户并返回只用于 Authorization 头的 Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # Token 只在测试请求 Authorization 头中短暂使用，不能进入断言文本、数据库或错误输出。
    return response.json()["access_token"]


def auth_headers(token: str, request_id: str) -> dict[str, str]:
    """构造带稳定 request_id 的 Bearer 请求头，并限制令牌只停留在测试传输边界。"""

    # 认证令牌与链路 ID 仅交给 HTTP 客户端，不得拼入日志、事件 payload 或数据库摘要。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


@pytest.mark.anyio
async def test_policy_admin_validate_create_list_patch_and_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：admin 校验、创建、查询和 If-Match 更新，三对象/审计/事件保持一致。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        validated = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_validate_valid"),
            json={
                "rule": {
                    "condition_expr": 'env == "prod" and budget_remaining < 100.00'
                },
                "action": {"type": "deny"},
                "context": {"env": "prod", "budget_remaining": "50.0"},
            },
        )
        created = await client.post(
            "/policies",
            headers={
                **auth_headers(token, "req_prd044_create"),
                "Origin": "http://localhost:5173",
            },
            json={
                "name": "生产低预算阻断",
                "description": "预算保护说明",
                "status": "active",
                "priority": 20,
                "rule": {"condition_expr": 'env == "prod" and budget_remaining < 100'},
                "action": {"type": "deny"},
            },
        )
        policy_id = created.json()["policy"]["policy_id"]
        listed = await client.get(
            "/policies?status=all&action=deny",
            headers=auth_headers(token, "req_prd044_list"),
        )
        updated = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_patch"),
                "If-Match": '"1"',
            },
            json={"priority": 10, "status": "disabled", "description": None},
        )
        stale = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_patch_stale"),
                "If-Match": '"1"',
            },
            json={"priority": 9},
        )
        unknown_query = await client.get(
            "/policies?tenant_id=other",
            headers=auth_headers(token, "req_prd044_unknown_query"),
        )
        cors_preflight = await client.options(
            f"/policies/{policy_id}",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "authorization,if-match,x-request-id",
            },
        )

    assert validated.status_code == 200, validated.text
    assert validated.json()["valid"] is True
    assert validated.json()["canonical_expression"].endswith("budget_remaining < 100")
    assert validated.json()["evaluation"]["decision"] == "deny"
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    assert created.headers["cache-control"] == "no-store"
    exposed_headers = {
        item.strip().lower()
        for item in created.headers["access-control-expose-headers"].split(",")
    }
    assert "etag" in exposed_headers
    policy = created.json()["policy"]
    assert policy["tenant_id"] == setup["tenant_id"]
    assert policy["version"] == 1
    assert listed.status_code == 200
    assert [item["policy_id"] for item in listed.json()["policies"]] == [policy_id]
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] == '"2"'
    assert updated.json()["policy"]["version"] == 2
    assert updated.json()["policy"]["priority"] == 10
    assert updated.json()["policy"]["description"] is None
    assert stale.status_code == 409
    assert stale.json()["code"] == "policy.version_conflict"
    assert unknown_query.status_code == 422
    assert unknown_query.json()["code"] == "policy.query_invalid"
    assert cors_preflight.status_code == 200
    allowed_headers = {
        item.strip().lower()
        for item in cors_preflight.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "if-match", "x-request-id"}.issubset(allowed_headers)

    connection = connect_database(setup["database_url"])
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_rules WHERE tenant_id=? AND policy_id=?",
        (setup["tenant_id"], policy_id),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_actions WHERE tenant_id=? AND policy_id=?",
        (setup["tenant_id"], policy_id),
    ).fetchone()[0] == 1
    audit_rows = connection.execute(
        """
        SELECT action, before_summary_json, after_summary_json
        FROM audit_logs WHERE object_id=? ORDER BY created_at
        """,
        (policy_id,),
    ).fetchall()
    assert [row["action"] for row in audit_rows] == ["policy.created", "policy.updated"]
    audit_text = "".join(
        row["before_summary_json"] + row["after_summary_json"] for row in audit_rows
    )
    assert "condition_expr" not in audit_text
    assert "budget_remaining" in audit_text  # 仅 variables 摘要，表达式正文仍不在审计。
    event = connection.execute(
        "SELECT event_type, reason_code, payload_json FROM policy_events WHERE policy_id=?",
        (policy_id,),
    ).fetchone()
    assert event["event_type"] == "policy.created"
    assert event["reason_code"] == "policy.created"
    assert 'env == "prod"' not in event["payload_json"]


@pytest.mark.anyio
async def test_policy_validation_rejects_injection_and_separates_structure_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全异常：注入与领域值形成幂等事件，额外字段保持统一结构 422 且无副作用。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched_validation_events: list[object] = []

    def capture_validation_event(_config, _tenant_id, event, **_kwargs):
        """捕获失败事件信封，验证新草稿的 policy_id 在 DB 与 Webhook 中都明确为 null。"""

        if event.event_type == "policy.validation_failed":
            dispatched_validation_events.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_validation_event)
    app = build_control_app()
    sentinel = "PRD044_SENTINEL___import__"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        injection_body = {
            "rule": {"condition_expr": f'{sentinel}("os")'},
            "action": {"type": "deny"},
        }
        first = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_invalid_same"),
            json=injection_body,
        )
        second = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_invalid_same"),
            json=injection_body,
        )
        invalid_action = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_invalid_action"),
            json={"rule": {"condition_expr": 'env == "prod"'}, "action": {"type": sentinel}},
        )
        invalid_budget = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_invalid_budget"),
            json={
                "rule": {"condition_expr": "budget_remaining < 1"},
                "action": {"type": "deny"},
                "context": {"budget_remaining": 0.5},
            },
        )
        structural = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_structural"),
            json={
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
                "context": {"tenant": "forged"},
            },
        )
        save_invalid = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_invalid_save"),
            json={"name": "非法脚本", **injection_body},
        )

    assert first.status_code == 200 and first.json()["valid"] is False
    assert second.status_code == 200 and second.json()["valid"] is False
    assert sentinel not in first.text
    assert invalid_action.status_code == 200 and invalid_action.json()["valid"] is False
    assert invalid_budget.status_code == 200 and invalid_budget.json()["valid"] is False
    assert structural.status_code == 422
    assert structural.json()["code"] == "infra.validation_failed"
    assert save_invalid.status_code == 422
    assert save_invalid.json()["code"] == "policy.expression_invalid"
    assert sentinel not in save_invalid.text
    assert save_invalid.headers["cache-control"] == "no-store"

    connection = connect_database(setup["database_url"])
    same_count = connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_invalid_same'"
    ).fetchone()[0]
    assert same_count == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_structural'"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
    stored_events = connection.execute(
        "SELECT reason_code, payload_json FROM policy_events ORDER BY request_id"
    ).fetchall()
    assert {row["reason_code"] for row in stored_events} >= {
        "policy.expression_invalid",
        "policy.action_invalid",
        "policy.context_invalid",
    }
    joined_payload = "".join(row["payload_json"] for row in stored_events)
    assert sentinel not in joined_payload
    assert all(json.loads(row["payload_json"])["policy_id"] is None for row in stored_events)
    assert dispatched_validation_events
    assert all(event.payload["policy_id"] is None for event in dispatched_validation_events)
    invalid_action_payload = json.loads(
        next(
            row["payload_json"]
            for row in stored_events
            if row["reason_code"] == "policy.action_invalid"
        )
    )
    assert invalid_action_payload["action_type"] == "invalid"


@pytest.mark.anyio
async def test_policy_backend_permissions_have_no_business_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限：developer/viewer/匿名均被后端拒绝，策略、事件、审计和投递保持为空。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched_events: list[str] = []

    def capture_forbidden_dispatch(_config, _tenant_id, event, **_kwargs):
        """记录任何越权请求意外触发的 Webhook；正确权限链路下列表必须保持为空。"""

        dispatched_events.append(event.event_type)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_forbidden_dispatch)
    app = build_control_app()
    forbidden_policy_id = "pol_" + "0" * 32
    valid_draft = {
        "rule": {"condition_expr": 'env == "prod"'},
        "action": {"type": "deny"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        developer = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        role_responses = []
        for role_name, token in (("developer", developer), ("viewer", viewer)):
            role_responses.extend(
                [
                    await client.get(
                        "/policies",
                        headers=auth_headers(token, f"req_prd044_{role_name}_get"),
                    ),
                    await client.post(
                        "/policies/validate",
                        headers=auth_headers(token, f"req_prd044_{role_name}_validate"),
                        json=valid_draft,
                    ),
                    await client.post(
                        "/policies",
                        headers=auth_headers(token, f"req_prd044_{role_name}_create"),
                        json={"name": f"{role_name}-forbidden", **valid_draft},
                    ),
                    await client.patch(
                        f"/policies/{forbidden_policy_id}",
                        headers={
                            **auth_headers(token, f"req_prd044_{role_name}_patch"),
                            "If-Match": '"1"',
                        },
                        json={"priority": 1},
                    ),
                ]
            )
        anonymous_responses = [
            await client.get(
                "/policies",
                headers={"X-Request-ID": "req_prd044_anonymous_get"},
            ),
            await client.post(
                "/policies/validate",
                headers={"X-Request-ID": "req_prd044_anonymous_validate"},
                json=valid_draft,
            ),
            await client.post(
                "/policies",
                headers={"X-Request-ID": "req_prd044_anonymous_create"},
                json={"name": "anonymous-forbidden", **valid_draft},
            ),
            await client.patch(
                f"/policies/{forbidden_policy_id}",
                headers={
                    "X-Request-ID": "req_prd044_anonymous_patch",
                    "If-Match": '"1"',
                },
                json={"priority": 1},
            ),
        ]

    for response in role_responses:
        assert response.status_code == 403
        assert response.json()["code"] == "auth.permission_denied"
    for response in anonymous_responses:
        assert response.status_code == 401
        assert response.json()["code"] == "auth.token_missing"
    connection = connect_database(setup["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policy_events").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='policy'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE event_type LIKE 'policy.%'"
    ).fetchone()[0] == 0
    assert dispatched_events == []


@pytest.mark.anyio
async def test_policy_route_target_and_cross_tenant_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """租户：route_to 只接受当前租户 active 模型，跨租户 Policy 不可查询或更新。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched_validation_events: list[object] = []

    def capture_route_validation_event(_config, _tenant_id, event, **_kwargs):
        """捕获 route 失败信封，核对 PATCH 的既有 policy_id 与新草稿 null 语义。"""

        if event.event_type == "policy.validation_failed":
            dispatched_validation_events.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_route_validation_event)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        other_token = await login(
            client,
            "admin@other.example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug="prd044-other",
        )
        valid_route = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_route_create"),
            json={
                "name": "路由到当前租户模型",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "route_to", "target_model_id": setup["active_model_id"]},
            },
        )
        policy_id = valid_route.json()["policy"]["policy_id"]
        disabled_target = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_route_disabled"),
                "If-Match": '"1"',
            },
            json={
                "action": {
                    "type": "route_to",
                    "target_model_id": setup["disabled_model_id"],
                }
            },
        )
        cross_target = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_route_cross"),
            json={
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "route_to", "target_model_id": setup["other_model_id"]},
            },
        )
        other_created = await client.post(
            "/policies",
            headers=auth_headers(other_token, "req_prd044_other_create"),
            json={
                "name": "其他租户策略",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        other_policy_id = other_created.json()["policy"]["policy_id"]
        cross_patch = await client.patch(
            f"/policies/{other_policy_id}",
            headers={
                **auth_headers(token, "req_prd044_cross_patch"),
                "If-Match": '"1"',
            },
            json={"priority": 1},
        )
        listed = await client.get(
            "/policies?status=all",
            headers=auth_headers(token, "req_prd044_tenant_list"),
        )

    assert valid_route.status_code == 201, valid_route.text
    assert disabled_target.status_code == 422
    assert disabled_target.json()["code"] == "policy.route_target_invalid"
    assert cross_target.status_code == 200
    assert cross_target.json()["valid"] is False
    assert cross_target.json()["diagnostics"][0]["code"] == "policy.route_target_invalid"
    assert other_created.status_code == 201, other_created.text
    assert cross_patch.status_code == 404
    assert cross_patch.json()["code"] == "policy.not_found"
    assert [item["policy_id"] for item in listed.json()["policies"]] == [policy_id]

    connection = connect_database(setup["database_url"])
    stored = connection.execute(
        """
        SELECT p.version, a.target_model_id
        FROM policies p JOIN policy_actions a
          ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.policy_id=?
        """,
        (setup["tenant_id"], policy_id),
    ).fetchone()
    assert dict(stored) == {"version": 1, "target_model_id": setup["active_model_id"]}
    failed = connection.execute(
        """
        SELECT reason_code, policy_id, payload_json FROM policy_events
        WHERE request_id='req_prd044_route_disabled'
        """
    ).fetchone()
    assert failed["reason_code"] == "policy.route_target_invalid"
    assert failed["policy_id"] == policy_id
    assert json.loads(failed["payload_json"])["policy_id"] == policy_id
    webhook_policy_ids = [event.payload["policy_id"] for event in dispatched_validation_events]
    assert policy_id in webhook_policy_ids
    assert None in webhook_policy_ids


@pytest.mark.anyio
async def test_policy_route_target_failure_releases_lock_before_independent_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务：锁内 route target 失败必须先 rollback，再用新连接独立提交失败事件。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_lock_create"),
            json={
                "name": "锁释放测试策略",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["policy"]["policy_id"]

        from metahub_control.policies import service as policy_service

        timeline: list[str] = []
        probes: list[_ConnectionProbe] = []

        def tracked_connect(_config):
            """为业务事务和独立事件事务分别返回真实连接代理，并记录调用先后。"""

            probe = _ConnectionProbe(
                connect_database(setup["database_url"]),
                len(probes),
                timeline,
            )
            probes.append(probe)
            return probe

        monkeypatch.setattr(policy_service, "_connect", tracked_connect)
        failed = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_lock_target_failed"),
                "If-Match": '"1"',
            },
            json={
                "action": {
                    "type": "route_to",
                    "target_model_id": setup["disabled_model_id"],
                }
            },
        )

    assert failed.status_code == 422
    assert failed.json()["code"] == "policy.route_target_invalid"
    assert len(probes) == 2
    assert timeline == ["begin:0", "rollback:0", "begin:1", "commit:1"]
    connection = connect_database(setup["database_url"])
    stored = connection.execute(
        """
        SELECT p.version, a.action_type, a.target_model_id
        FROM policies p
        JOIN policy_actions a ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.policy_id=?
        """,
        (setup["tenant_id"], policy_id),
    ).fetchone()
    assert dict(stored) == {"version": 1, "action_type": "deny", "target_model_id": None}
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_lock_target_failed'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd044_lock_target_failed'"
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_policy_patch_preliminary_read_failure_returns_503_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """故障：PATCH 锁外 tenant 回读失败必须返回统一 503，且不改版本、不写内部或外部事实。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_preliminary_create"),
            json={
                "name": "锁外回读故障基线",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["policy"]["policy_id"]

        from metahub_control.policies import service as policy_service

        dispatched_events: list[str] = []

        def fail_preliminary_read(*_args, **_kwargs):
            """在写事务开始之前模拟租户限定查询的 SQLite I/O 故障并验证统一映射。"""

            # 初读失败不能被误判为 not_found，也不能进入写事务后产生任何副作用。
            raise sqlite3.OperationalError("synthetic preliminary policy read failure")

        def capture_unexpected_dispatch(*_args, **_kwargs):
            """捕获锁外读取失败后不应出现的 audit 或 policy Webhook 调用。"""

            dispatched_events.append("unexpected")

        monkeypatch.setattr(policy_service, "_load_policy_row", fail_preliminary_read)
        monkeypatch.setattr(policy_service, "dispatch_audit_recorded", capture_unexpected_dispatch)
        monkeypatch.setattr(policy_service, "_dispatch_policy_event", capture_unexpected_dispatch)
        response = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_preliminary_failed"),
                "If-Match": '"1"',
            },
            json={"priority": 1},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "policy.unavailable"
    assert response.json()["request_id"] == "req_prd044_preliminary_failed"
    connection = connect_database(setup["database_url"])
    stored = connection.execute(
        "SELECT version, priority FROM policies WHERE tenant_id=? AND policy_id=?",
        (setup["tenant_id"], policy_id),
    ).fetchone()
    assert dict(stored) == {"version": 1, "priority": 100}
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd044_preliminary_failed'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_preliminary_failed'"
    ).fetchone()[0] == 0
    assert dispatched_events == []


@pytest.mark.anyio
async def test_policy_connect_oserror_returns_503_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """故障：数据库目录或文件系统 OSError 必须映射为 policy 503，不能泄露本机路径。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.policies import service as policy_service

    def fail_database_connect(_database_url):
        """模拟 connect_database 在创建 SQLite 父目录时遇到文件系统拒绝。"""

        # OSError 文本包含本机路径也只能留在异常链内，HTTP 响应必须使用固定安全文案。
        raise OSError("synthetic path failure D:/sensitive/database")

    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        monkeypatch.setattr(policy_service, "connect_database", fail_database_connect)
        response = await client.get(
            "/policies",
            headers=auth_headers(token, "req_prd044_connect_oserror"),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "policy.unavailable"
    assert response.json()["request_id"] == "req_prd044_connect_oserror"
    assert "D:/sensitive/database" not in response.text
    connection = connect_database(setup["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policy_events").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='policy'"
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_policy_update_audit_failure_translates_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """故障：PATCH 审计写入失败翻译为 policy 503，并回滚版本、Rule 与 Action 更新。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_update_audit_create"),
            json={
                "name": "更新审计故障基线",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["policy"]["policy_id"]

        from metahub_control.policies import service as policy_service

        dispatched_events: list[str] = []

        def fail_audit_record(*_args, **_kwargs):
            """模拟审计表写入不可用，沿用审计服务原始错误供 Policy 边界翻译。"""

            # 原始 audit_log 错误不得穿透 Policy HTTP 合同，且当前事务必须整体回滚。
            raise ApiError("audit_log.record_unavailable")

        def capture_unexpected_dispatch(*_args, **_kwargs):
            """审计失败发生在提交前，任何提交后投递调用都属于错误副作用。"""

            dispatched_events.append("unexpected")

        monkeypatch.setattr(policy_service, "record_audit_log", fail_audit_record)
        monkeypatch.setattr(policy_service, "dispatch_audit_recorded", capture_unexpected_dispatch)
        response = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_update_audit_failed"),
                "If-Match": '"1"',
            },
            json={
                "priority": 1,
                "rule": {"condition_expr": 'env == "test"'},
                "action": {"type": "mask"},
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "policy.unavailable"
    connection = connect_database(setup["database_url"])
    stored = connection.execute(
        """
        SELECT p.version, p.priority, r.condition_expr, a.action_type
        FROM policies p
        JOIN policy_rules r ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
        JOIN policy_actions a ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.policy_id=?
        """,
        (setup["tenant_id"], policy_id),
    ).fetchone()
    assert dict(stored) == {
        "version": 1,
        "priority": 100,
        "condition_expr": 'env == "prod"',
        "action_type": "deny",
    }
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd044_update_audit_failed'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_update_audit_failed'"
    ).fetchone()[0] == 0
    assert dispatched_events == []


@pytest.mark.anyio
async def test_policy_corrupt_rows_return_503_and_update_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """腐损：非法持久日期与 variables 类型均返回 policy 503，PATCH 不留下版本半更新。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_corrupt_create"),
            json={
                "name": "持久行腐损基线",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["policy"]["policy_id"]
        connection = connect_database(setup["database_url"])
        connection.execute(
            "UPDATE policy_rules SET updated_at='invalid-datetime' WHERE policy_id=?",
            (policy_id,),
        )
        connection.commit()
        corrupt_list = await client.get(
            "/policies",
            headers=auth_headers(token, "req_prd044_corrupt_datetime"),
        )
        connection.execute(
            "UPDATE policy_rules SET updated_at='2026-07-17T00:00:00Z', variables_json='[1]' "
            "WHERE policy_id=?",
            (policy_id,),
        )
        connection.commit()
        corrupt_audit = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth_headers(token, "req_prd044_corrupt_variables"),
                "If-Match": '"1"',
            },
            json={"priority": 1},
        )

    assert corrupt_list.status_code == 503
    assert corrupt_list.json()["code"] == "policy.unavailable"
    assert corrupt_audit.status_code == 503
    assert corrupt_audit.json()["code"] == "policy.unavailable"
    stored = connection.execute(
        """
        SELECT p.version, p.priority, r.variables_json
        FROM policies p JOIN policy_rules r
          ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.policy_id=?
        """,
        (setup["tenant_id"], policy_id),
    ).fetchone()
    assert dict(stored) == {"version": 1, "priority": 100, "variables_json": "[1]"}
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE request_id='req_prd044_corrupt_variables'"
    ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_policy_validation_event_commit_failure_returns_503_without_partial_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """故障：validation_failed 提交失败必须 rollback、返回统一 503 且不尝试 Webhook。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.policies import service as policy_service
    from metahub_control.webhooks import service as webhook_service

    timeline: list[str] = []
    dispatched_events: list[str] = []

    def failing_connect(_config):
        """返回在真实提交前抛错的连接，确保失败事件事务可以被确定性回滚。"""

        return _ConnectionProbe(
            connect_database(setup["database_url"]),
            0,
            timeline,
            fail_commit=True,
        )

    def capture_unexpected_dispatch(_config, _tenant_id, event, **_kwargs):
        """捕获提交失败后不应发生的外部投递。"""

        dispatched_events.append(event.event_type)
        return []

    monkeypatch.setattr(policy_service, "_connect", failing_connect)
    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_unexpected_dispatch)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_event_commit_failed"),
            json={
                "rule": {"condition_expr": '__import__("os")'},
                "action": {"type": "deny"},
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == "policy.unavailable"
    assert response.json()["request_id"] == "req_prd044_event_commit_failed"
    assert timeline == ["begin:0", "commit:0", "rollback:0"]
    connection = connect_database(setup["database_url"])
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE request_id='req_prd044_event_commit_failed'"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
    assert dispatched_events == []


@pytest.mark.parametrize(
    ("failure_point", "expected_code"),
    [
        ("audit_insert", "policy.unavailable"),
        ("created_event_insert", "policy.unavailable"),
        ("transaction_commit", "policy.unavailable"),
    ],
)
@pytest.mark.anyio
async def test_policy_create_transaction_failures_rollback_all_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_point: str,
    expected_code: str,
) -> None:
    """故障：审计、created 事件或最终 commit 任一点失败都原子回滚三对象和内部事实。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.policies import service as policy_service

    dispatched_events: list[str] = []

    def inject_transaction_failure(*_args, **_kwargs):
        """在参数指定的事务阶段抛固定异常，不把测试正文或底层连接写入日志。"""

        if failure_point == "created_event_insert":
            # created 事件属于同一事务，普通 SQLite I/O 故障也应收敛为可重试 policy 错误。
            raise sqlite3.OperationalError("synthetic created event insert failure")
        # 审计写入或最终提交故障沿用审计子系统的可重试统一错误码。
        raise ApiError("audit_log.record_unavailable")

    def capture_unexpected_dispatch(*_args, **_kwargs):
        """提交前故障不允许进入任何提交后 Webhook 分支。"""

        dispatched_events.append("unexpected")

    target_name = {
        "audit_insert": "record_audit_log",
        "created_event_insert": "_insert_created_event",
        "transaction_commit": "commit_audited_transaction",
    }[failure_point]
    monkeypatch.setattr(policy_service, target_name, inject_transaction_failure)
    monkeypatch.setattr(policy_service, "dispatch_audit_recorded", capture_unexpected_dispatch)
    monkeypatch.setattr(policy_service, "_dispatch_policy_event", capture_unexpected_dispatch)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies",
            headers=auth_headers(token, f"req_prd044_{failure_point}"),
            json={
                "name": f"事务故障-{failure_point}",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )

    assert response.status_code == 503
    assert response.json()["code"] == expected_code
    connection = connect_database(setup["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policy_actions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM policy_events").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='policy'"
    ).fetchone()[0] == 0
    assert dispatched_events == []


@pytest.mark.parametrize("failed_event_type", ["audit.recorded", "policy.created"])
@pytest.mark.anyio
async def test_policy_post_commit_webhooks_fail_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failed_event_type: str,
) -> None:
    """Webhook：audit 与 policy 任一路投递失败都不阻断另一路，也不改变已提交 201。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    attempted_event_types: list[str] = []
    sensitive_sentinel = (
        'PRD044_WEBHOOK_SENTINEL SQL=SELECT_secret DSL=env=="prod" Authorization=Bearer-secret'
    )
    caplog.set_level("ERROR")

    def inject_one_dispatch_failure(_config, _tenant_id, event, **_kwargs):
        """只让参数指定的事件类型失败，并记录另一事件仍被调用。"""

        attempted_event_types.append(event.event_type)
        if event.event_type == failed_event_type:
            # 外部 Endpoint/投递存储异常属于提交后副作用，不能反向回滚策略事务。
            raise RuntimeError(sensitive_sentinel)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", inject_one_dispatch_failure)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies",
            headers=auth_headers(token, f"req_prd044_webhook_{failed_event_type}"),
            json={
                "name": f"Webhook 隔离-{failed_event_type}",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )

    assert response.status_code == 201, response.text
    assert attempted_event_types == ["audit.recorded", "policy.created"]
    assert sensitive_sentinel not in caplog.text
    assert 'env == "prod"' not in caplog.text
    assert token not in caplog.text
    assert "Authorization=Bearer-secret" not in caplog.text
    policy_id = response.json()["policy"]["policy_id"]
    connection = connect_database(setup["database_url"])
    assert connection.execute(
        "SELECT COUNT(*) FROM policies WHERE tenant_id=? AND policy_id=?",
        (setup["tenant_id"], policy_id),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='policy' AND object_id=?",
        (policy_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE event_type='policy.created' AND policy_id=?",
        (policy_id,),
    ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_policy_audit_event_build_failure_keeps_policy_webhook_and_201(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Webhook：audit 信封安全构造失败也属于提交后故障，policy.created 仍须继续分发。"""

    setup = prepare_database(monkeypatch, tmp_path)
    from metahub_control.audit import service as audit_service
    from metahub_control.webhooks import service as webhook_service

    sensitive_sentinel = "PRD044_AUDIT_BUILD_SENTINEL_SQL_DSL_AUTHORIZATION"
    attempted_event_types: list[str] = []
    caplog.set_level("ERROR")

    def fail_audit_event_build(*_args, **_kwargs):
        """模拟 audit payload 安全构造异常，异常正文不得进入固定摘要日志。"""

        # 构造异常正文含唯一 sentinel，用于证明固定失败日志不会记录原始异常链。
        raise ValueError(sensitive_sentinel)

    def capture_policy_dispatch(_config, _tenant_id, event, **_kwargs):
        """记录 audit 构造失败后仍应独立调用的 policy.created 分发。"""

        attempted_event_types.append(event.event_type)
        return []

    monkeypatch.setattr(audit_service, "build_webhook_event", fail_audit_event_build)
    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", capture_policy_dispatch)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        response = await client.post(
            "/policies",
            headers=auth_headers(token, "req_prd044_audit_build_failed"),
            json={
                "name": "审计信封构造隔离",
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )

    assert response.status_code == 201, response.text
    assert attempted_event_types == ["policy.created"]
    assert sensitive_sentinel not in caplog.text
    assert token not in caplog.text
    policy_id = response.json()["policy"]["policy_id"]
    connection = connect_database(setup["database_url"])
    assert connection.execute(
        "SELECT COUNT(*) FROM policies WHERE tenant_id=? AND policy_id=?",
        (setup["tenant_id"], policy_id),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE object_type='policy' AND object_id=?",
        (policy_id,),
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM policy_events WHERE event_type='policy.created' AND policy_id=?",
        (policy_id,),
    ).fetchone()[0] == 1


@dataclass
class _ForcedTimeoutResult:
    """提供 TestClient 故障注入使用的固定 fail-closed 结果，验证超时始终拒绝。"""

    reason_code: str = "policy.evaluation_timeout"
    missing_variable: str | None = None
    timed_out: bool = True

    def as_dict(self) -> dict[str, object]:
        """返回与 shared evaluator 一致的最小安全响应。"""

        return {
            "matched": False,
            "decision": "deny",
            "reason_code": self.reason_code,
            "target_model_id": None,
            "policy_reason": None,
            "policy_checks": [],
            "log_only_matches": [],
            "checked_policy_count": 0,
            "explanation_truncated": False,
            "timed_out": True,
            "alert_required": True,
            "elapsed_us": 5000,
            "missing_variable": None,
        }


@pytest.mark.anyio
async def test_policy_validate_timeout_is_fail_closed_and_event_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """超时：HTTP 仍返回有效 AST + deny，重复 request 只追加一条告警事件。"""

    setup = prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "metahub_control.policies.service.evaluate_policy_set",
        lambda *args, **kwargs: _ForcedTimeoutResult(),
    )
    app = build_control_app()
    body = {
        "rule": {"condition_expr": 'env == "prod"'},
        "action": {"type": "allow"},
        "context": {"env": "prod"},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        first = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_timeout"),
            json=body,
        )
        second = await client.post(
            "/policies/validate",
            headers=auth_headers(token, "req_prd044_timeout"),
            json=body,
        )

    for response in (first, second):
        assert response.status_code == 200
        assert response.json()["valid"] is True
        assert response.json()["evaluation"]["decision"] == "deny"
        assert response.json()["evaluation"]["reason_code"] == "policy.evaluation_timeout"
    connection = connect_database(setup["database_url"])
    events = connection.execute(
        "SELECT reason_code, payload_json FROM policy_events WHERE request_id='req_prd044_timeout'"
    ).fetchall()
    assert len(events) == 1
    assert events[0]["reason_code"] == "policy.evaluation_timeout"
    assert "policy.evaluation_timeout" in events[0]["payload_json"]
    assert {"policy.created", "policy.validation_failed"}.issubset(
        WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    )
