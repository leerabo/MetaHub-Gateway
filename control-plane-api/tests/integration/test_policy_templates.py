"""PRD-068 策略模板目录、原子应用和幂等恢复的 Control API 集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.policies.routes import router as policy_router
from metahub_control.policy_snapshots.routes import router as snapshot_router
from metahub_control.policy_templates.routes import router as template_router
from metahub_control.projects.routes import router as project_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 这些固定密码只用于每个用例新建的隔离数据库，不能复用生产凭据或写入运行日志。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
READ_ONLY_PASSWORD = "ReadOnlyLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"
FINANCE_PASSWORD = "FinanceLocal!2026"
SECURITY_PASSWORD = "SecurityLocal!2026"
OPS_PASSWORD = "OpsLocal!2026"


def _sqlite_url(path: Path) -> str:
    """把 pytest 沙箱临时文件转换为共享数据库 URL。"""

    return f"sqlite:///{path.as_posix()}"


def _build_app():
    """构造只包含认证、Project、Snapshot 与 Template 路由的隔离应用。"""

    app = create_service_app(
        "control-plane-api",
        "Policy Template Test API",
        extra_cors_allow_headers=("If-Match", "If-Policy-Candidate-Match"),
        extra_cors_expose_headers=("ETag", "X-Policy-Candidate-ETag"),
    )
    app.include_router(auth_router)
    app.include_router(policy_router)
    app.include_router(project_router)
    app.include_router(snapshot_router)
    app.include_router(template_router)
    return app


def _prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """迁移并 seed 一个启用 mask 部署门禁的独立 0060 数据库。"""

    database_url = _sqlite_url(tmp_path / "metahub-prd068.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "test-policy-template-auth-secret")
    monkeypatch.setenv("METAHUB_POLICY_MASK_RUNTIME_ENABLED", "true")
    assert apply_migrations(database_url)
    assert apply_seed_data(database_url)
    return database_url


async def _login(
    client: AsyncClient,
    *,
    email: str = "admin@metahub.local",
    password: str = ADMIN_PASSWORD,
    tenant_slug: str = "default",
) -> str:
    """登录指定 tenant 用户并仅在当前测试内存保存短期 Token。"""

    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": tenant_slug,
            "email": email,
            "password": password,
        },
    )
    assert response.status_code == 200, response.text
    # 登录辅助函数只向调用方传递短期令牌，不把令牌写入测试证据或持久化数据库。
    return response.json()["access_token"]


def _discard_webhook_dispatch(_config, _tenant_id, _event, **_kwargs):
    """隔离集成测试的提交后网络副作用，仅让本地持久事务接受验收。"""

    return []


async def _create_template_project(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    code: str,
) -> str:
    """通过真实 Project API 创建模板绑定对象并返回服务端生成的租户内 ID。"""

    response = await client.post(
        "/projects",
        headers={**auth, "X-Request-ID": f"req_prd068_project_{code}"},
        json={"project_code": f"prd068-{code}", "name": f"PRD068 {code} 项目"},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]["project_id"]


async def _publish_template_baseline(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    request_suffix: str,
) -> dict[str, object]:
    """读取同一时点双 CAS 后发布基线，确保每次模板应用都有真实回滚目标。"""

    current = await client.get("/policy-snapshots/current", headers=auth)
    assert current.status_code == 200, current.text
    response = await client.post(
        "/policy-snapshots/publish",
        headers={
            **auth,
            "If-Match": current.headers["ETag"],
            "If-Policy-Candidate-Match": current.headers[
                "X-Policy-Candidate-ETag"
            ],
            "X-Request-ID": f"req_prd068_baseline_{request_suffix}",
        },
        json={"reason": "建立策略模板应用基线"},
    )
    assert response.status_code == 201, response.text
    return response.json()["policy_snapshot"]


async def _preview_template(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    template_id: str,
    bindings: dict[str, str],
) -> tuple[object, dict[str, object]]:
    """请求一次精确预览，并返回响应与指定模板的安全计划投影。"""

    response = await client.get(
        "/policy-templates",
        headers=auth,
        params={"template_id": template_id, **bindings},
    )
    assert response.status_code == 200, response.text
    selected = next(
        item
        for item in response.json()["policy_templates"]
        if item["policy_template_id"] == template_id
    )
    return response, selected


async def _apply_template_preview(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    template_id: str,
    bindings: dict[str, str],
    preview_response,
    preview: dict[str, object],
    request_id: str,
    reason: str = "应用已复核的内置策略模板",
):
    """把服务端签发的精确预览原样带回 apply，客户端不重算任何治理事实。"""

    payload = {
        "template_version": preview["version"],
        "preview_nonce": preview["preview_nonce"],
        "preview_as_of": preview["preview_as_of"],
        "impact_hash": preview["impact_hash"],
        "bindings": bindings,
        "reason": reason,
    }
    response = await client.post(
        f"/policy-templates/{template_id}/apply",
        headers={
            **auth,
            "If-Match": preview_response.headers["ETag"],
            "If-Policy-Candidate-Match": preview_response.headers[
                "X-Policy-Candidate-ETag"
            ],
            "X-Request-ID": request_id,
        },
        json=payload,
    )
    return response, payload


def _seed_template_planner_facts(
    database_url: str,
    *,
    budget_project_id: str,
) -> dict[str, str]:
    """写入五类 planner 所需的非敏感租户事实。

    低成本候选刻意同时包含 PricePlan 惩罚项、不完整能力项、同价项和迁移平台项，
    用一个真实应用流程证明筛选、价格优先级和稳定 tie-break；国产模板另外包含一个
    本地兼容 Alias，证明 Provider 分类而非名称决定国产资格。
    """

    connection = connect_database(database_url)
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()
    environment = connection.execute(
        "SELECT environment_id FROM environments WHERE tenant_id=? AND code='dev'",
        (tenant["tenant_id"],),
    ).fetchone()
    tenant_id = str(tenant["tenant_id"])
    environment_id = str(environment["environment_id"])
    now = "2026-08-03T00:00:00Z"
    # Provider 健康事实使用固定安全元数据；测试不创建明文 Provider Key。
    connection.executemany(
        """
        INSERT INTO providers(
          provider_id,tenant_id,provider_code,name,base_url,status,provider_type,
          auth_type,health_status,health_generation,health_updated_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,'active',?,?,'healthy',1,?,?,?)
        """,
        [
            (
                "provider_prd068_local_a",
                tenant_id,
                "prd068_local_a",
                "PRD068 Local A",
                "http://127.0.0.1:19068/v1",
                "openai_compatible",
                "none",
                now,
                now,
                now,
            ),
            (
                "provider_prd068_local_b",
                tenant_id,
                "prd068_local_b",
                "PRD068 Local B",
                "http://127.0.0.1:19069/v1",
                "openai_compatible",
                "none",
                now,
                now,
                now,
            ),
            (
                "provider_prd068_domestic",
                tenant_id,
                "prd068_domestic",
                "PRD068 智谱",
                "https://open.bigmodel.cn/api/paas/v4",
                "zhipu",
                "bearer",
                now,
                now,
                now,
            ),
            (
                "provider_prd068_migration",
                tenant_id,
                "prd068_migration",
                "PRD068 Migration",
                "https://migration.example.invalid/v1",
                "migration_compatible",
                "bearer",
                now,
                now,
                now,
            ),
        ],
    )
    complete_capabilities = json.dumps(
        {"chat": True, "embeddings": True, "stream": True},
        separators=(",", ":"),
        sort_keys=True,
    )
    incomplete_capabilities = json.dumps(
        {"chat": True, "embeddings": True, "stream": False},
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.executemany(
        """
        INSERT INTO models(
          model_id,tenant_id,provider_id,model_alias,upstream_model_name,
          provider_model_id,capabilities,status,input_price_per_1k,
          output_price_per_1k,currency,price_schema_version,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,'active',?,?, 'CNY','catalog-v1',?,?)
        """,
        [
            (
                "model_prd068_price_plan_penalty",
                tenant_id,
                "provider_prd068_local_a",
                "prd068-price-plan-penalty",
                "price-plan-penalty",
                "price-plan-penalty",
                complete_capabilities,
                "0.1",
                "0.1",
                now,
                now,
            ),
            (
                "model_prd068_catalog_winner",
                tenant_id,
                "provider_prd068_local_b",
                "prd068-catalog-winner",
                "catalog-winner",
                "catalog-winner",
                complete_capabilities,
                "1",
                "1",
                now,
                now,
            ),
            (
                "model_prd068_catalog_z_tie",
                tenant_id,
                "provider_prd068_local_b",
                "prd068-catalog-z-tie",
                "catalog-z-tie",
                "catalog-z-tie",
                complete_capabilities,
                "1",
                "1",
                now,
                now,
            ),
            (
                "model_prd068_incomplete",
                tenant_id,
                "provider_prd068_local_a",
                "prd068-incomplete",
                "incomplete",
                "incomplete",
                incomplete_capabilities,
                "0.001",
                "0.001",
                now,
                now,
            ),
            (
                "model_prd068_domestic",
                tenant_id,
                "provider_prd068_domestic",
                "prd068-domestic",
                "glm-4-plus",
                "glm-4-plus",
                complete_capabilities,
                "0.5",
                "0.5",
                now,
                now,
            ),
            (
                "model_prd068_migration_cheapest",
                tenant_id,
                "provider_prd068_migration",
                "prd068-migration-cheapest",
                "migration-cheapest",
                "migration-cheapest",
                complete_capabilities,
                "0.0001",
                "0.0001",
                now,
                now,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO model_price_plans(
          price_plan_id,tenant_id,model_id,input_price_per_1k,output_price_per_1k,
          currency,price_schema_version,effective_from,source,created_at
        ) VALUES('price_prd068_penalty',?,?,10,10,'CNY','price-plan-v1',
                 '2026-01-01T00:00:00Z','manual',?)
        """,
        (tenant_id, "model_prd068_price_plan_penalty", now),
    )
    connection.executemany(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,environment_id,
          default_model_id,status,created_at,updated_at
        ) VALUES(?,?,?,'dev',?,?,'active',?,?)
        """,
        [
            (
                "alias_prd068_domestic",
                tenant_id,
                "secure-domestic",
                environment_id,
                "model_prd068_domestic",
                now,
                now,
            ),
            (
                "alias_prd068_local",
                tenant_id,
                "foreign-local",
                environment_id,
                "model_prd068_catalog_winner",
                now,
                now,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,environment,
          environment_id,project_id,created_at,updated_at
        ) VALUES('vk_prd068_budget',?,'PRD068 Budget Key','mh_prd068',?,'active',
                 'dev',?,?,?,?)
        """,
        (tenant_id, "a" * 64, environment_id, budget_project_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO budget_rules(
          budget_rule_id,tenant_id,environment_id,scope_type,scope_id,name,
          daily_budget_cny,warning_threshold_percent,exhausted_threshold_percent,
          exhausted_action,status,created_at,updated_at
        ) VALUES('budget_prd068_key',?,?,'virtual_key','vk_prd068_budget',
                 'PRD068 Key Budget',100,80,100,'block','active',?,?)
        """,
        (tenant_id, environment_id, now, now),
    )
    connection.commit()
    connection.close()
    return {
        "tenant_id": tenant_id,
        "environment_id": environment_id,
        "low_cost_model_id": "model_prd068_catalog_winner",
        "domestic_alias_id": "alias_prd068_domestic",
    }


def _seed_template_role_users(database_url: str) -> None:
    """创建三类职责管理员，权限仍完全来自迁移后的数据库角色矩阵。"""

    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    now = "2026-08-03T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        [
            (
                "user_prd068_finance",
                tenant_id,
                "finance-prd068@example.com",
                "PRD068 Finance",
                hash_password(FINANCE_PASSWORD),
                "finance_admin",
                now,
                now,
            ),
            (
                "user_prd068_security",
                tenant_id,
                "security-prd068@example.com",
                "PRD068 Security",
                hash_password(SECURITY_PASSWORD),
                "security_admin",
                now,
                now,
            ),
            (
                "user_prd068_ops",
                tenant_id,
                "ops-prd068@example.com",
                "PRD068 Ops",
                hash_password(OPS_PASSWORD),
                "ops_admin",
                now,
                now,
            ),
        ],
    )
    connection.commit()
    connection.close()


def _set_template_test_role_permission(
    database_url: str,
    *,
    role_code: str,
    permission_code: str,
    granted: bool,
) -> tuple[str, ...]:
    """在隔离测试库内增删一项角色权限，并同步 revision 与矩阵摘要。

    参数：数据库 URL、可编辑角色码、权限码和期望授权状态。返回：变更后的规范权限集合。
    关键约束：这里只模拟管理面已经完成的一次合法矩阵变更，不按角色名推导其他权限；
    每次关系变化都把 revision 单调加一并重算 SHA-256，确保认证层读取的是完整实时事实。
    """

    connection = connect_database(database_url)
    try:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()["tenant_id"]
        role = connection.execute(
            """
            SELECT role_id,permission_revision FROM roles
            WHERE tenant_id=? AND code=? AND status='active'
            """,
            (tenant_id, role_code),
        ).fetchone()
        permission = connection.execute(
            """
            SELECT permission_id FROM permissions
            WHERE tenant_id=? AND code=? AND status='active'
            """,
            (tenant_id, permission_code),
        ).fetchone()
        assert role is not None
        assert permission is not None
        relation = connection.execute(
            """
            SELECT role_permission_id FROM role_permissions
            WHERE tenant_id=? AND role_id=? AND permission_id=?
            """,
            (tenant_id, role["role_id"], permission["permission_id"]),
        ).fetchone()
        now = "2026-08-03T00:00:00Z"
        if granted:
            assert relation is None
            # 恢复的仍是目录允许的同 tenant 关系；测试 ID 不携带用户、Token 或业务正文。
            connection.execute(
                """
                INSERT INTO role_permissions(
                  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"rperm_prd068_replay_{role_code}_{permission_code}".replace(
                        ".", "_"
                    ),
                    tenant_id,
                    role["role_id"],
                    permission["permission_id"],
                    now,
                    now,
                ),
            )
        else:
            assert relation is not None
            connection.execute(
                """
                DELETE FROM role_permissions
                WHERE tenant_id=? AND role_id=? AND permission_id=?
                """,
                (tenant_id, role["role_id"], permission["permission_id"]),
            )
        codes = tuple(
            row["code"]
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                ORDER BY permission.code
                """,
                (tenant_id, role["role_id"]),
            ).fetchall()
        )
        assert (permission_code in codes) is granted
        connection.execute(
            """
            UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(codes),
                now,
                tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
        return codes
    finally:
        connection.close()


def _template_apply_fact_counts(database_url: str) -> dict[str, int]:
    """读取默认 tenant 的 application、Snapshot、审计和三类 source event 计数。"""

    connection = connect_database(database_url)
    try:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()["tenant_id"]
        row = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM policy_template_applications WHERE tenant_id=?)
                AS applications,
              (SELECT COUNT(*) FROM policy_snapshots WHERE tenant_id=?) AS snapshots,
              (SELECT COUNT(*) FROM audit_logs WHERE tenant_id=?) AS audits,
              (SELECT COUNT(*) FROM policy_events WHERE tenant_id=?) AS policy_events,
              (SELECT COUNT(*) FROM policy_snapshot_events WHERE tenant_id=?)
                AS snapshot_events,
              (SELECT COUNT(*) FROM policy_template_events WHERE tenant_id=?)
                AS template_events
            """,
            (tenant_id,) * 6,
        ).fetchone()
        assert row is not None
        # 计数只用于证明拒绝路径没有创建或补写任何治理事实，不读取事件 payload。
        return {name: int(row[name]) for name in row.keys()}
    finally:
        connection.close()


@pytest.mark.anyio
async def test_all_five_template_planners_apply_real_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """五类主流程：真实预览并依次应用，核对派生 DSL、动作、价格选择与单调快照。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        projects = {
            code: await _create_template_project(client, auth, code=code)
            for code in (
                "low-cost",
                "high-security",
                "domestic",
                "budget",
                "redaction",
            )
        }
        facts = _seed_template_planner_facts(
            database_url,
            budget_project_id=projects["budget"],
        )
        # 基础种子模型在 SQLite 中也有合法小数价格；此用例只比较自建候选的
        # PricePlan、能力和同价稳定排序，因此先停用无关种子模型。
        connection = connect_database(database_url)
        connection.execute(
            "UPDATE models SET status='disabled' WHERE model_id NOT LIKE 'model_prd068_%'"
        )
        connection.commit()
        connection.close()
        baseline = await _publish_template_baseline(
            client,
            auth,
            request_suffix="five_planners",
        )
        assert baseline["snapshot_version"] == 1

        cases = [
            (
                "ptpl_low_cost_mode",
                "low_cost_mode",
                {"project_id": projects["low-cost"]},
                "route_to",
            ),
            (
                "ptpl_high_security_mode",
                "high_security_mode",
                {
                    "project_id": projects["high-security"],
                    "model_alias_id": facts["domestic_alias_id"],
                },
                "deny",
            ),
            (
                "ptpl_domestic_models_only",
                "domestic_models_only",
                {"project_id": projects["domestic"]},
                "deny",
            ),
            (
                "ptpl_budget_burst_guard",
                "budget_burst_guard",
                {"project_id": projects["budget"], "threshold_cny": "25.500000"},
                "deny",
            ),
            (
                "ptpl_customer_service_redaction",
                "customer_service_redaction",
                {"project_id": projects["redaction"]},
                "mask",
            ),
        ]
        applied_by_code: dict[str, dict[str, object]] = {}
        for index, (template_id, code, bindings, action_type) in enumerate(cases, start=2):
            preview_response, preview = await _preview_template(
                client,
                auth,
                template_id=template_id,
                bindings=bindings,
            )
            assert preview["compatibility"]["status"] in {
                "ready",
                "ready_with_warnings",
            }
            assert preview["compatibility"]["can_apply"] is True
            assert preview["impact"]["before_policy_count"] == index - 2
            assert preview["impact"]["after_policy_count"] == index - 1
            # 成本和国产模板的派生事实来自服务端快照，断言必须证明筛选约束而非只看应用成功。
            if code == "low_cost_mode":
                # PricePlan 覆盖更低的目录价，不完整能力和迁移平台也不能进入最终候选。
                assert (
                    preview["impact"]["derived_target_model_id"]
                    == facts["low_cost_model_id"]
                )
            elif code == "domestic_models_only":
                assert preview["impact"]["allowed_model_alias_names"] == [
                    "secure-domestic"
                ]
                assert preview["impact"]["frozen_environment_codes"] == ["dev"]

            applied, _payload = await _apply_template_preview(
                client,
                auth,
                template_id=template_id,
                bindings=bindings,
                preview_response=preview_response,
                preview=preview,
                request_id=f"req_prd068_apply_{code}",
            )
            assert applied.status_code == 201, applied.text
            body = applied.json()
            assert body["generated_policy"]["action"]["type"] == action_type
            assert body["policy_snapshot"]["snapshot_version"] == index
            assert body["application"]["previous_snapshot_version"] == index - 1
            assert body["application"]["result_snapshot_version"] == index
            assert body["idempotent_replay"] is False
            applied_by_code[code] = body

    connection = connect_database(database_url)
    rows = connection.execute(
        """
        SELECT template.template_code,policy.priority,rule.condition_expr,
               action.action_type,action.target_model_id
        FROM policy_template_applications application
        JOIN policy_templates template
          ON template.tenant_id=application.tenant_id
         AND template.policy_template_id=application.policy_template_id
        JOIN policies policy
          ON policy.tenant_id=application.tenant_id
         AND policy.policy_id=application.generated_policy_id
        JOIN policy_rules rule
          ON rule.tenant_id=policy.tenant_id AND rule.policy_id=policy.policy_id
        JOIN policy_actions action
          ON action.tenant_id=policy.tenant_id AND action.policy_id=policy.policy_id
        ORDER BY template.template_code
        """
    ).fetchall()
    facts_by_code = {row["template_code"]: row for row in rows}
    assert len(facts_by_code) == 5
    assert facts_by_code["low_cost_mode"]["priority"] == 1000
    assert facts_by_code["low_cost_mode"]["target_model_id"] == facts[
        "low_cost_model_id"
    ]
    assert facts_by_code["high_security_mode"]["priority"] == 10
    assert "env" in facts_by_code["high_security_mode"]["condition_expr"]
    assert "secure-domestic" in facts_by_code["high_security_mode"][
        "condition_expr"
    ]
    assert "env not in" in facts_by_code["domestic_models_only"]["condition_expr"]
    assert "foreign-local" not in facts_by_code["domestic_models_only"][
        "condition_expr"
    ]
    assert "budget_remaining < 25.5" in facts_by_code["budget_burst_guard"][
        "condition_expr"
    ]
    assert facts_by_code["customer_service_redaction"]["action_type"] == "mask"
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_application_items"
    ).fetchone()["count"] == 10
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_events"
    ).fetchone()["count"] == 5
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_snapshots"
    ).fetchone()["count"] == 6
    connection.close()
    assert set(applied_by_code) == {
        "low_cost_mode",
        "high_security_mode",
        "domestic_models_only",
        "budget_burst_guard",
        "customer_service_redaction",
    }


@pytest.mark.anyio
async def test_low_cost_preview_blocks_post_route_mask_but_warns_log_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全冲突：低成本路由后的重叠 mask 必须阻断，log_only 仍只产生顺序告警。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_id = await _create_template_project(client, auth, code="low-cost-shadow")
        _seed_template_planner_facts(
            database_url,
            budget_project_id=project_id,
        )
        await _publish_template_baseline(
            client,
            auth,
            request_suffix="low_cost_shadow",
        )

        policy_ids: dict[str, str] = {}
        for priority, action_type in ((1100, "mask"), (1200, "log_only")):
            created = await client.post(
                "/policies",
                headers={
                    **auth,
                    "X-Request-ID": f"req_prd068_low_cost_shadow_{action_type}",
                },
                json={
                    "name": f"低成本路由后置 {action_type} 策略",
                    "status": "active",
                    "priority": priority,
                    "rule": {"condition_expr": f'project == "{project_id}"'},
                    "action": {"type": action_type},
                },
            )
            assert created.status_code == 201, created.text
            policy_ids[action_type] = created.json()["policy"]["policy_id"]

        _response, preview = await _preview_template(
            client,
            auth,
            template_id="ptpl_low_cost_mode",
            bindings={"project_id": project_id},
        )

    compatibility = preview["compatibility"]
    assert compatibility["status"] == "incompatible"
    assert compatibility["can_apply"] is False
    assert compatibility["reason_codes"] == ["policy_shadowing_conflict"]
    # Provider 健康新鲜度是独立时间告警；这里只要求顺序分析必须保留 log_only 告警。
    assert "policy_shadowing_warning" in compatibility["warning_codes"]
    # 不兼容预览不能签发可被误当成 apply 凭据的 nonce/hash，但仍返回安全排序摘要供管理员定位。
    assert preview["preview_nonce"] is None
    assert preview["impact_hash"] is None
    impact = preview["impact"]
    assert impact is not None
    order_by_id = {item["policy_id"]: item for item in impact["evaluation_order"]}
    generated = next(item for item in impact["evaluation_order"] if item["generated"])
    assert generated["priority"] == 1000
    assert generated["action_type"] == "route_to"
    assert order_by_id[policy_ids["mask"]]["shadowing"] == "blocking"
    assert order_by_id[policy_ids["log_only"]]["shadowing"] == "warning"
    assert set(impact["shadowing_policy_ids"]) == set(policy_ids.values())


@pytest.mark.anyio
async def test_template_wire_validation_and_all_cas_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Wire/冲突：严格校验 query、media/JSON、双 CAS、impact、nonce 与 request 并固定错误码。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_id = await _create_template_project(client, auth, code="wire")
        await _publish_template_baseline(client, auth, request_suffix="wire")
        template_id = "ptpl_customer_service_redaction"
        bindings = {"project_id": project_id}

        unknown_query = await client.get(
            "/policy-templates?tenant_id=other",
            headers={**auth, "X-Request-ID": "req_prd068_wire_unknown_query"},
        )
        duplicate_query = await client.get(
            "/policy-templates?template_id=ptpl_customer_service_redaction"
            "&template_id=ptpl_customer_service_redaction&project_id=" + project_id,
            headers={**auth, "X-Request-ID": "req_prd068_wire_duplicate_query"},
        )
        get_body = await client.request(
            "GET",
            "/policy-templates",
            headers={**auth, "X-Request-ID": "req_prd068_wire_get_body"},
            content=b"{}",
        )
        for response in (unknown_query, duplicate_query, get_body):
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "policy_template.query_invalid"

        preview_response, preview = await _preview_template(
            client,
            auth,
            template_id=template_id,
            bindings=bindings,
        )
        valid_payload = {
            "template_version": preview["version"],
            "preview_nonce": preview["preview_nonce"],
            "preview_as_of": preview["preview_as_of"],
            "impact_hash": preview["impact_hash"],
            "bindings": bindings,
            "reason": "验证严格 wire 与冲突优先级",
        }
        path = f"/policy-templates/{template_id}/apply"
        wrong_media = await client.post(
            path,
            headers={
                **auth,
                "Content-Type": "text/plain",
                "X-Request-ID": "req_prd068_wire_media",
            },
            content=b"{}",
        )
        duplicate_json = await client.post(
            path,
            headers={
                **auth,
                "Content-Type": "application/json",
                "X-Request-ID": "req_prd068_wire_duplicate_json",
            },
            content=(
                b'{"bindings":{"project_id":"first",'
                b'"project_id":"second"}}'
            ),
        )
        # 上面的正文故意不是合法业务 DTO；无论重复键位于哪一层都只能返回安全 payload 错误。
        missing_cas = await client.post(
            path,
            headers={**auth, "X-Request-ID": "req_prd068_wire_missing_cas"},
            json=valid_payload,
        )
        bad_header = await client.post(
            path,
            headers={
                **auth,
                "If-Match": '"01"',
                "If-Policy-Candidate-Match": preview_response.headers[
                    "X-Policy-Candidate-ETag"
                ],
                "X-Request-ID": "req_prd068_wire_bad_header",
            },
            json=valid_payload,
        )
        assert wrong_media.status_code == 415
        assert wrong_media.json()["code"] == "policy_template.media_type_unsupported"
        assert duplicate_json.status_code == 422
        assert duplicate_json.json()["code"] == "policy_template.payload_invalid"
        assert missing_cas.status_code == 428
        assert missing_cas.json()["code"] == "policy_template.precondition_required"
        assert bad_header.status_code == 422
        assert bad_header.json()["code"] == "policy_template.payload_invalid"

        common_headers = {
            **auth,
            "If-Match": preview_response.headers["ETag"],
            "If-Policy-Candidate-Match": preview_response.headers[
                "X-Policy-Candidate-ETag"
            ],
        }
        version_payload = {**valid_payload, "template_version": 2}
        version_conflict = await client.post(
            path,
            headers={**common_headers, "X-Request-ID": "req_prd068_wire_version"},
            json=version_payload,
        )
        head_conflict = await client.post(
            path,
            headers={
                **common_headers,
                "If-Match": '"999"',
                "X-Request-ID": "req_prd068_wire_head",
            },
            json=valid_payload,
        )
        candidate_conflict = await client.post(
            path,
            headers={
                **common_headers,
                "If-Policy-Candidate-Match": '"' + "0" * 64 + '"',
                "X-Request-ID": "req_prd068_wire_candidate",
            },
            json=valid_payload,
        )
        impact_payload = {**valid_payload, "impact_hash": "0" * 64}
        impact_conflict = await client.post(
            path,
            headers={**common_headers, "X-Request-ID": "req_prd068_wire_impact"},
            json=impact_payload,
        )
        assert version_conflict.status_code == 409
        assert version_conflict.json()["code"] == "policy_template.version_conflict"
        for response in (head_conflict, candidate_conflict, impact_conflict):
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "policy_template.impact_conflict"

        applied = await client.post(
            path,
            headers={**common_headers, "X-Request-ID": "req_prd068_wire_apply"},
            json=valid_payload,
        )
        assert applied.status_code == 201, applied.text
        request_conflict = await client.post(
            path,
            headers={**common_headers, "X-Request-ID": "req_prd068_wire_apply"},
            json={**valid_payload, "reason": "同一 request 不同意图"},
        )
        nonce_conflict = await client.post(
            path,
            headers={**common_headers, "X-Request-ID": "req_prd068_wire_nonce"},
            json=valid_payload,
        )
        assert request_conflict.status_code == 409
        assert request_conflict.json()["code"] == "policy_template.request_conflict"
        assert nonce_conflict.status_code == 409
        assert nonce_conflict.json()["code"] == "policy_template.nonce_conflict"

        error_responses = (
            unknown_query,
            duplicate_query,
            get_body,
            wrong_media,
            duplicate_json,
            missing_cas,
            bad_header,
            version_conflict,
            head_conflict,
            candidate_conflict,
            impact_conflict,
            request_conflict,
            nonce_conflict,
        )
        for response in error_responses:
            body_text = json.dumps(response.json(), ensure_ascii=False, sort_keys=True)
            assert response.json()["request_id"]
            assert project_id not in body_text
            assert valid_payload["preview_nonce"] not in body_text

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_applications"
    ).fetchone()["count"] == 1
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM audit_logs WHERE request_id='req_prd068_wire_apply'"
    ).fetchone()["count"] == 3
    connection.close()


@pytest.mark.anyio
async def test_template_compound_role_permissions_and_dependency_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """职责分离：三类管理员只应用授权类别，预算依赖预览后漂移必须原子冲突。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    _seed_template_role_users(database_url)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        admin = await _login(client)
        admin_auth = {"Authorization": f"Bearer {admin}"}
        projects = {
            code: await _create_template_project(client, admin_auth, code=f"role-{code}")
            for code in ("low", "high", "domestic", "budget", "redaction")
        }
        facts = _seed_template_planner_facts(
            database_url,
            budget_project_id=projects["budget"],
        )
        await _publish_template_baseline(client, admin_auth, request_suffix="roles")
        role_tokens = {
            "finance": await _login(
                client,
                email="finance-prd068@example.com",
                password=FINANCE_PASSWORD,
            ),
            "security": await _login(
                client,
                email="security-prd068@example.com",
                password=SECURITY_PASSWORD,
            ),
            "ops": await _login(
                client,
                email="ops-prd068@example.com",
                password=OPS_PASSWORD,
            ),
        }
        bindings_by_code = {
            "low_cost_mode": {"project_id": projects["low"]},
            "high_security_mode": {
                "project_id": projects["high"],
                "model_alias_id": facts["domestic_alias_id"],
            },
            "domestic_models_only": {"project_id": projects["domestic"]},
            "budget_burst_guard": {
                "project_id": projects["budget"],
                "threshold_cny": "20",
            },
            "customer_service_redaction": {"project_id": projects["redaction"]},
        }
        template_id_by_code = {
            code: f"ptpl_{code}" for code in bindings_by_code
        }
        expected_can_apply = {
            "finance": {
                "low_cost_mode": True,
                "high_security_mode": False,
                "domestic_models_only": False,
                "budget_burst_guard": True,
                "customer_service_redaction": False,
            },
            "security": {
                "low_cost_mode": False,
                "high_security_mode": True,
                "domestic_models_only": True,
                "budget_burst_guard": False,
                "customer_service_redaction": True,
            },
            "ops": {
                "low_cost_mode": True,
                "high_security_mode": False,
                "domestic_models_only": False,
                "budget_burst_guard": False,
                "customer_service_redaction": False,
            },
        }
        previews: dict[tuple[str, str], tuple[object, dict[str, object]]] = {}
        for role_name, token in role_tokens.items():
            auth = {"Authorization": f"Bearer {token}"}
            for code, bindings in bindings_by_code.items():
                preview_response, preview = await _preview_template(
                    client,
                    auth,
                    template_id=template_id_by_code[code],
                    bindings=bindings,
                )
                assert preview["compatibility"]["status"] in {
                    "ready",
                    "ready_with_warnings",
                }
                assert (
                    preview["compatibility"]["can_apply"]
                    is expected_can_apply[role_name][code]
                )
                previews[(role_name, code)] = (preview_response, preview)

        denied_cases = (
            ("finance", "high_security_mode"),
            ("security", "budget_burst_guard"),
            ("ops", "customer_service_redaction"),
        )
        for role_name, code in denied_cases:
            preview_response, preview = previews[(role_name, code)]
            denied, _payload = await _apply_template_preview(
                client,
                {"Authorization": f"Bearer {role_tokens[role_name]}"},
                template_id=template_id_by_code[code],
                bindings=bindings_by_code[code],
                preview_response=preview_response,
                preview=preview,
                request_id=f"req_prd068_role_denied_{role_name}",
            )
            assert denied.status_code == 403, denied.text
            assert denied.json()["code"] == "auth.permission_denied"

        finance_auth = {"Authorization": f"Bearer {role_tokens['finance']}"}
        budget_response, budget_preview = await _preview_template(
            client,
            finance_auth,
            template_id=template_id_by_code["budget_burst_guard"],
            bindings=bindings_by_code["budget_burst_guard"],
        )
        connection = connect_database(database_url)
        connection.execute(
            """
            INSERT INTO virtual_keys(
              virtual_key_id,tenant_id,name,key_prefix,key_hash,status,environment,
              environment_id,project_id,created_at,updated_at
            ) VALUES('vk_prd068_budget_uncovered',?,'Uncovered','mh_uncovered',?,'active',
                     'dev',?,?,datetime('now'),datetime('now'))
            """,
            (
                facts["tenant_id"],
                "b" * 64,
                facts["environment_id"],
                projects["budget"],
            ),
        )
        connection.commit()
        connection.close()
        drifted, _payload = await _apply_template_preview(
            client,
            finance_auth,
            template_id=template_id_by_code["budget_burst_guard"],
            bindings=bindings_by_code["budget_burst_guard"],
            preview_response=budget_response,
            preview=budget_preview,
            request_id="req_prd068_budget_dependency_drift",
        )
        assert drifted.status_code == 409, drifted.text
        assert drifted.json()["code"] == "policy_template.impact_conflict"
        connection = connect_database(database_url)
        connection.execute(
            "DELETE FROM virtual_keys WHERE virtual_key_id='vk_prd068_budget_uncovered'"
        )
        connection.commit()
        connection.close()

        allowed_cases = (
            ("finance", "budget_burst_guard"),
            ("security", "customer_service_redaction"),
            ("ops", "low_cost_mode"),
        )
        for role_name, code in allowed_cases:
            auth = {"Authorization": f"Bearer {role_tokens[role_name]}"}
            preview_response, preview = await _preview_template(
                client,
                auth,
                template_id=template_id_by_code[code],
                bindings=bindings_by_code[code],
            )
            allowed, _payload = await _apply_template_preview(
                client,
                auth,
                template_id=template_id_by_code[code],
                bindings=bindings_by_code[code],
                preview_response=preview_response,
                preview=preview,
                request_id=f"req_prd068_role_allowed_{role_name}",
            )
            assert allowed.status_code == 201, allowed.text

    connection = connect_database(database_url)
    actors = connection.execute(
        """
        SELECT actor_user_id,COUNT(*) AS count
        FROM policy_template_applications GROUP BY actor_user_id ORDER BY actor_user_id
        """
    ).fetchall()
    connection.close()
    assert [(row["actor_user_id"], row["count"]) for row in actors] == [
        ("user_prd068_finance", 1),
        ("user_prd068_ops", 1),
        ("user_prd068_security", 1),
    ]


@pytest.mark.anyio
async def test_template_apply_dispatches_six_facts_independently_after_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """提交后隔离：六类派发全部抛错仍逐项执行，已提交应用保持 201 和完整事实。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.policy_templates import service as template_service
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_id = await _create_template_project(client, auth, code="dispatch")
        await _publish_template_baseline(client, auth, request_suffix="dispatch")
        preview_response, preview = await _preview_template(
            client,
            auth,
            template_id="ptpl_customer_service_redaction",
            bindings={"project_id": project_id},
        )
        calls: list[str] = []

        def _fail_audit_dispatch(_config, _context, audit, **_kwargs):
            """记录已尝试的 Audit 类型后失败，验证下一项不会被前一项短路。"""

            calls.append(f"audit:{audit.action}")
            # 此异常发生在数据库提交之后，用来证明派发失败彼此隔离且不会反向撤销事务。
            raise RuntimeError("injected audit dispatch failure")

        def _fail_event_dispatch(label: str):
            """构造不保留 event payload 的失败替身，仅记录稳定派发类别。"""

            def _dispatch(_config, _event):
                """记录派发类别并注入提交后异常。"""

                calls.append(label)
                # 事件替身刻意在记录类别后失败，验证后续派发仍继续且敏感载荷不会进入证据。
                raise RuntimeError("injected event dispatch failure")

            return _dispatch

        monkeypatch.setattr(
            template_service,
            "dispatch_audit_recorded",
            _fail_audit_dispatch,
        )
        monkeypatch.setattr(
            template_service,
            "_dispatch_policy_event",
            _fail_event_dispatch("event:policy.created"),
        )
        monkeypatch.setattr(
            template_service,
            "_dispatch_snapshot_event",
            _fail_event_dispatch("event:policy_snapshot.published"),
        )
        monkeypatch.setattr(
            template_service,
            "_dispatch_template_event",
            _fail_event_dispatch("event:policy_template.applied"),
        )
        applied, _payload = await _apply_template_preview(
            client,
            auth,
            template_id="ptpl_customer_service_redaction",
            bindings={"project_id": project_id},
            preview_response=preview_response,
            preview=preview,
            request_id="req_prd068_dispatch_isolation",
        )
        assert applied.status_code == 201, applied.text

    assert calls == [
        "audit:policy.created",
        "event:policy.created",
        "audit:policy_snapshot.published",
        "event:policy_snapshot.published",
        "audit:policy_template.applied",
        "event:policy_template.applied",
    ]
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_applications"
    ).fetchone()["count"] == 1
    assert connection.execute(
        """
        SELECT COUNT(*) AS count FROM audit_logs
        WHERE request_id='req_prd068_dispatch_isolation'
        """
    ).fetchone()["count"] == 3
    connection.close()


@pytest.mark.anyio
async def test_template_planners_fail_closed_without_required_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """适配异常：缺模型、国产覆盖、预算事实或部署 gate 时只返回稳定原因且不签发 CAS。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        projects = {
            code: await _create_template_project(client, auth, code=f"missing-{code}")
            for code in ("low", "domestic", "budget", "redaction", "high")
        }
        await _publish_template_baseline(client, auth, request_suffix="missing")
        cases = (
            (
                "ptpl_domestic_models_only",
                {"project_id": projects["domestic"]},
                "domestic_alias_coverage_required",
            ),
            (
                "ptpl_budget_burst_guard",
                {"project_id": projects["budget"], "threshold_cny": "10"},
                "active_virtual_key_required",
            ),
        )
        for template_id, bindings, expected_reason in cases:
            response, preview = await _preview_template(
                client,
                auth,
                template_id=template_id,
                bindings=bindings,
            )
            assert preview["compatibility"]["status"] == "incompatible"
            assert expected_reason in preview["compatibility"]["reason_codes"]
            assert preview["preview_nonce"] is None
            assert preview["impact_hash"] is None
            assert "ETag" not in response.headers
            assert "X-Policy-Candidate-ETag" not in response.headers

        # 缺少向量能力的对话模型仍可参与低成本模板筛选。
        _seed_template_planner_facts(database_url, budget_project_id=projects["low"])
        connection = connect_database(database_url)
        connection.execute(
            "UPDATE models SET capabilities=?",
            (json.dumps({"chat": True, "stream": True}),),
        )
        connection.commit()
        connection.close()
        _response, low_cost_preview = await _preview_template(
            client,
            auth,
            template_id="ptpl_low_cost_mode",
            bindings={"project_id": projects["low"]},
        )
        assert "eligible_low_cost_model_required" not in (
            low_cost_preview["compatibility"]["reason_codes"]
        )
        assert low_cost_preview["compatibility"]["can_apply"] is True
        assert low_cost_preview["preview_nonce"] is not None

        invalid_alias = await client.get(
            "/policy-templates",
            headers={**auth, "X-Request-ID": "req_prd068_missing_alias"},
            params={
                "template_id": "ptpl_high_security_mode",
                "project_id": projects["high"],
                "model_alias_id": "alias_not_visible",
            },
        )
        assert invalid_alias.status_code == 422
        assert invalid_alias.json()["code"] == "policy_template.binding_invalid"

    # 配置在应用启动时冻结；关闭 operator gate 后使用新应用实例验证默认失败关闭。
    monkeypatch.setenv("METAHUB_POLICY_MASK_RUNTIME_ENABLED", "false")
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        response, preview = await _preview_template(
            client,
            {"Authorization": f"Bearer {token}"},
            template_id="ptpl_customer_service_redaction",
            bindings={"project_id": projects["redaction"]},
        )
        assert preview["compatibility"]["status"] == "incompatible"
        assert preview["compatibility"]["reason_codes"] == [
            "policy_mask_runtime_required"
        ]
        assert "ETag" not in response.headers


async def _apply_customer_redaction(
    client: AsyncClient,
    auth: dict[str, str],
    *,
    request_suffix: str,
) -> dict[str, object]:
    """创建基线和绑定 Project，再应用客服脱敏模板并返回后续断言所需事实。

    参数 ``request_suffix`` 用于构造本测试数据库内唯一的 request_id；返回值只包含
    Project、application、生成 Policy 与结果 Snapshot 的非敏感 ID，不返回 Bearer Token。
    """

    project_response = await client.post(
        "/projects",
        headers={**auth, "X-Request-ID": f"req_prd068_{request_suffix}_project"},
        json={
            "project_code": f"prd068-{request_suffix}",
            "name": f"PRD068 {request_suffix} 项目",
        },
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project"]["project_id"]

    current = await client.get("/policy-snapshots/current", headers=auth)
    assert current.status_code == 200, current.text
    baseline = await client.post(
        "/policy-snapshots/publish",
        headers={
            **auth,
            "If-Match": current.headers["ETag"],
            "If-Policy-Candidate-Match": current.headers["X-Policy-Candidate-ETag"],
            "X-Request-ID": f"req_prd068_{request_suffix}_baseline",
        },
        json={"reason": "建立模板不变量测试基线"},
    )
    assert baseline.status_code == 201, baseline.text

    preview = await client.get(
        "/policy-templates",
        headers=auth,
        params={
            "template_id": "ptpl_customer_service_redaction",
            "project_id": project_id,
        },
    )
    assert preview.status_code == 200, preview.text
    selected = next(
        item
        for item in preview.json()["policy_templates"]
        if item["policy_template_id"] == "ptpl_customer_service_redaction"
    )
    apply_request_id = f"req_prd068_{request_suffix}_apply"
    apply_if_match = preview.headers["ETag"]
    apply_candidate_match = preview.headers["X-Policy-Candidate-ETag"]
    apply_payload = {
        "template_version": selected["version"],
        "preview_nonce": selected["preview_nonce"],
        "preview_as_of": selected["preview_as_of"],
        "impact_hash": selected["impact_hash"],
        "bindings": {"project_id": project_id},
        "reason": "应用模板不变量测试模板",
    }
    applied = await client.post(
        "/policy-templates/ptpl_customer_service_redaction/apply",
        headers={
            **auth,
            "If-Match": apply_if_match,
            "If-Policy-Candidate-Match": apply_candidate_match,
            "X-Request-ID": apply_request_id,
        },
        json=apply_payload,
    )
    assert applied.status_code == 201, applied.text
    return {
        "project_id": project_id,
        "application_id": applied.json()["application"]["application_id"],
        "generated_policy_id": applied.json()["generated_policy"]["policy_id"],
        "previous_snapshot_id": applied.json()["application"]["previous_snapshot_id"],
        "result_snapshot_id": applied.json()["application"]["result_snapshot_id"],
        "snapshot_version": applied.json()["policy_snapshot"]["snapshot_version"],
        "apply_request_id": apply_request_id,
        "apply_if_match": apply_if_match,
        "apply_candidate_match": apply_candidate_match,
        "apply_payload": apply_payload,
    }


@pytest.mark.anyio
async def test_customer_redaction_apply_and_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：发布空基线、精确预览、原子应用，再从六类持久事实回放。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched: list[object] = []

    def _capture_dispatch(_config, _tenant_id, event, **_kwargs):
        """捕获本地 Webhook，避免测试访问网络并核对稳定 event ID。"""

        dispatched.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_dispatch)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_response = await client.post(
            "/projects",
            headers={**auth, "X-Request-ID": "req_prd068_project"},
            json={"project_code": "prd068", "name": "PRD068 客服项目"},
        )
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["project"]["project_id"]

        current = await client.get("/policy-snapshots/current", headers=auth)
        assert current.status_code == 200, current.text
        baseline = await client.post(
            "/policy-snapshots/publish",
            headers={
                **auth,
                "If-Match": current.headers["ETag"],
                "If-Policy-Candidate-Match": current.headers[
                    "X-Policy-Candidate-ETag"
                ],
                "X-Request-ID": "req_prd068_baseline",
            },
            json={"reason": "建立模板应用前基线"},
        )
        assert baseline.status_code == 201, baseline.text
        assert baseline.json()["policy_snapshot"]["snapshot_version"] == 1

        preview = await client.get(
            "/policy-templates",
            headers=auth,
            params={
                "template_id": "ptpl_customer_service_redaction",
                "project_id": project_id,
            },
        )
        assert preview.status_code == 200, preview.text
        selected = next(
            item
            for item in preview.json()["policy_templates"]
            if item["policy_template_id"] == "ptpl_customer_service_redaction"
        )
        assert selected["compatibility"]["status"] == "ready"
        assert selected["compatibility"]["can_apply"] is True
        payload = {
            "template_version": selected["version"],
            "preview_nonce": selected["preview_nonce"],
            "preview_as_of": selected["preview_as_of"],
            "impact_hash": selected["impact_hash"],
            "bindings": {"project_id": project_id},
            "reason": "启用客服输入脱敏模板",
        }
        apply_headers = {
            **auth,
            "If-Match": preview.headers["ETag"],
            "If-Policy-Candidate-Match": preview.headers[
                "X-Policy-Candidate-ETag"
            ],
            "X-Request-ID": "req_prd068_apply",
        }
        applied = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers=apply_headers,
            json=payload,
        )
        assert applied.status_code == 201, applied.text
        body = applied.json()
        assert body["application"]["status"] == "applied"
        assert body["generated_policy"]["action"]["type"] == "mask"
        assert body["policy_snapshot"]["snapshot_version"] == 2
        assert body["idempotent_replay"] is False

        replay = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers=apply_headers,
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["application"]["application_id"] == body["application"][
            "application_id"
        ]

    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_applications"
    ).fetchone()["count"] == 1
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_application_items"
    ).fetchone()["count"] == 2
    assert connection.execute(
        "SELECT COUNT(*) AS count FROM policy_template_events"
    ).fetchone()["count"] == 1
    event_payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM policy_template_events"
        ).fetchone()["payload_json"]
    )
    connection.close()
    assert set(event_payload) == {
        "template_code",
        "template_version",
        "application_id",
        "generated_policy_id",
        "previous_snapshot_version",
        "result_snapshot_version",
        "impact_hash",
        "bindings_hash",
        "changed_resource_types",
    }
    # 首次提交与回放各补派三类 source event；event ID 必须稳定，外部 delivery 可据此去重。
    template_events = [
        event for event in dispatched if event.event_type == "policy_template.applied"
    ]
    assert len(template_events) == 2
    assert template_events[0].event_id == template_events[1].event_id


@pytest.mark.anyio
async def test_apply_replay_rechecks_compound_permission_before_fact_redispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限回归：复合权限撤销后拒绝回放且不补派，恢复后同一命令才可回放。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    _seed_template_role_users(database_url)
    from metahub_control.webhooks import service as webhook_service

    dispatched: list[object] = []

    def _capture_dispatch(_config, _tenant_id, event, **_kwargs):
        """只捕获本地 source event，用于证明 403 分支没有触发外部补派。"""

        dispatched.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_dispatch)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        admin_token = await _login(client)
        security_token = await _login(
            client,
            email="security-prd068@example.com",
            password=SECURITY_PASSWORD,
        )
        admin_auth = {"Authorization": f"Bearer {admin_token}"}
        security_auth = {"Authorization": f"Bearer {security_token}"}
        project_id = await _create_template_project(
            client,
            admin_auth,
            code="replay-permission",
        )
        await _publish_template_baseline(
            client,
            admin_auth,
            request_suffix="replay_permission",
        )
        preview_response, preview = await _preview_template(
            client,
            security_auth,
            template_id="ptpl_customer_service_redaction",
            bindings={"project_id": project_id},
        )
        request_id = "req_prd068_replay_permission_apply"
        applied, payload = await _apply_template_preview(
            client,
            security_auth,
            template_id="ptpl_customer_service_redaction",
            bindings={"project_id": project_id},
            preview_response=preview_response,
            preview=preview,
            request_id=request_id,
            reason="验证幂等回放实时复核复合权限",
        )
        assert applied.status_code == 201, applied.text
        application_id = applied.json()["application"]["application_id"]
        apply_headers = {
            **security_auth,
            "If-Match": preview_response.headers["ETag"],
            "If-Policy-Candidate-Match": preview_response.headers[
                "X-Policy-Candidate-ETag"
            ],
            "X-Request-ID": request_id,
        }
        fact_counts = _template_apply_fact_counts(database_url)
        dispatch_count = len(dispatched)

        revoked_codes = _set_template_test_role_permission(
            database_url,
            role_code="security_admin",
            permission_code="policy.write",
            granted=False,
        )
        # 保留粗粒度 apply 权限，确保 403 来自 service 锁内的模板复合权限复核。
        assert "policy_template.apply" in revoked_codes
        assert "policy.write" not in revoked_codes
        denied = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers=apply_headers,
            json=payload,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "auth.permission_denied"
        assert denied.json()["request_id"] == request_id
        assert _template_apply_fact_counts(database_url) == fact_counts
        assert len(dispatched) == dispatch_count

        restored_codes = _set_template_test_role_permission(
            database_url,
            role_code="security_admin",
            permission_code="policy.write",
            granted=True,
        )
        assert {"policy_template.apply", "policy.write"}.issubset(restored_codes)
        replay = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers=apply_headers,
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["application"]["application_id"] == application_id
        # 成功回放只补派既有 source event，不新增 application、Snapshot、审计或事件行。
        assert _template_apply_fact_counts(database_url) == fact_counts
        assert len(dispatched) > dispatch_count


@pytest.mark.anyio
async def test_low_cost_preview_blocks_overlapping_later_mask(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全预览：低成本 route_to 不得把同 Project 的后置输入脱敏降为 warning。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_id = await _create_template_project(client, auth, code="low-mask-preview")
        _seed_template_planner_facts(database_url, budget_project_id=project_id)
        mask_policy = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_low_mask_preview_policy"},
            json={
                "name": "低成本后置输入脱敏",
                "status": "active",
                "priority": 1500,
                "rule": {"condition_expr": f'project == "{project_id}"'},
                "action": {"type": "mask"},
            },
        )
        assert mask_policy.status_code == 201, mask_policy.text
        mask_policy_id = mask_policy.json()["policy"]["policy_id"]
        await _publish_template_baseline(
            client,
            auth,
            request_suffix="low_mask_preview",
        )

        _preview_response, preview = await _preview_template(
            client,
            auth,
            template_id="ptpl_low_cost_mode",
            bindings={"project_id": project_id},
        )
        assert preview["compatibility"]["status"] == "incompatible"
        assert preview["compatibility"]["can_apply"] is False
        assert "policy_shadowing_conflict" in preview["compatibility"]["reason_codes"]
        assert preview["impact"]["shadowing_policy_ids"] == [mask_policy_id]
        mask_order = next(
            item
            for item in preview["impact"]["evaluation_order"]
            if item["policy_id"] == mask_policy_id
        )
        assert mask_order["shadowing"] == "blocking"
        # incompatible 预览不签发 apply 所需 nonce/hash，前端不能把 warning 当成可确认命令。
        assert preview["preview_nonce"] is None
        assert preview["impact_hash"] is None


@pytest.mark.anyio
async def test_low_cost_active_invariant_blocks_later_mask_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """持续不变量：低成本应用后新增的同 Project 后置 mask 必须在发布前阻断。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        project_id = await _create_template_project(client, auth, code="low-mask-publish")
        _seed_template_planner_facts(database_url, budget_project_id=project_id)
        await _publish_template_baseline(
            client,
            auth,
            request_suffix="low_mask_publish",
        )
        preview_response, preview = await _preview_template(
            client,
            auth,
            template_id="ptpl_low_cost_mode",
            bindings={"project_id": project_id},
        )
        applied, _payload = await _apply_template_preview(
            client,
            auth,
            template_id="ptpl_low_cost_mode",
            bindings={"project_id": project_id},
            preview_response=preview_response,
            preview=preview,
            request_id="req_prd068_low_mask_apply",
        )
        assert applied.status_code == 201, applied.text
        assert applied.json()["policy_snapshot"]["snapshot_version"] == 2

        later_mask = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_low_mask_later_policy"},
            json={
                "name": "低成本应用后的输入脱敏",
                "status": "active",
                "priority": 1500,
                "rule": {"condition_expr": f'project == "{project_id}"'},
                "action": {"type": "mask"},
            },
        )
        assert later_mask.status_code == 201, later_mask.text
        current = await client.get("/policy-snapshots/current", headers=auth)
        assert current.status_code == 200, current.text
        blocked = await client.post(
            "/policy-snapshots/publish",
            headers={
                **auth,
                "If-Match": current.headers["ETag"],
                "If-Policy-Candidate-Match": current.headers[
                    "X-Policy-Candidate-ETag"
                ],
                "X-Request-ID": "req_prd068_low_mask_publish_blocked",
            },
            json={"reason": "不得发布绕过后置脱敏的低成本路由"},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "policy_snapshot.template_invariant_conflict"
        after = await client.get("/policy-snapshots/current", headers=auth)
        assert after.status_code == 200, after.text
        assert after.json()["current_snapshot"]["snapshot_version"] == 2


@pytest.mark.anyio
async def test_active_template_publish_invariant_project_scope_and_graph_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """持续发布：互斥 Project 可发布，同 Project 遮蔽和生成策略图漂移必须阻断。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    def _discard_dispatch(_config, _tenant_id, _event, **_kwargs):
        """专项测试只核验事务事实，隔离提交后的外部 Webhook 副作用。"""

        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _discard_dispatch)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        applied = await _apply_customer_redaction(
            client,
            auth,
            request_suffix="invariant",
        )
        assert applied["snapshot_version"] == 2

        other_project = await client.post(
            "/projects",
            headers={**auth, "X-Request-ID": "req_prd068_invariant_other_project"},
            json={"project_code": "prd068-other", "name": "PRD068 互斥项目"},
        )
        assert other_project.status_code == 201, other_project.text
        other_project_id = other_project.json()["project"]["project_id"]
        disjoint_policy = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_invariant_disjoint"},
            json={
                "name": "互斥项目提前放行",
                "status": "active",
                "priority": 1,
                "rule": {"condition_expr": f'project == "{other_project_id}"'},
                "action": {"type": "allow"},
            },
        )
        assert disjoint_policy.status_code == 201, disjoint_policy.text

        async def _publish_current(reason: str, request_id: str):
            """读取同一时点的双 CAS 后发布，避免测试自身制造陈旧 candidate。"""

            current = await client.get("/policy-snapshots/current", headers=auth)
            assert current.status_code == 200, current.text
            response = await client.post(
                "/policy-snapshots/publish",
                headers={
                    **auth,
                    "If-Match": current.headers["ETag"],
                    "If-Policy-Candidate-Match": current.headers[
                        "X-Policy-Candidate-ETag"
                    ],
                    "X-Request-ID": request_id,
                },
                json={"reason": reason},
            )
            return response

        disjoint_published = await _publish_current(
            "发布互斥 Project 策略",
            "req_prd068_invariant_disjoint_publish",
        )
        assert disjoint_published.status_code == 201, disjoint_published.text
        assert disjoint_published.json()["policy_snapshot"]["snapshot_version"] == 3

        shadowing_policy = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_invariant_shadow"},
            json={
                "name": "同项目提前放行",
                "status": "active",
                "priority": 1,
                "rule": {
                    "condition_expr": f'project == "{applied["project_id"]}"'
                },
                "action": {"type": "allow"},
            },
        )
        assert shadowing_policy.status_code == 201, shadowing_policy.text
        shadowing_policy_id = shadowing_policy.json()["policy"]["policy_id"]
        blocked = await _publish_current(
            "不得发布同 Project 遮蔽策略",
            "req_prd068_invariant_shadow_publish",
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "policy_snapshot.template_invariant_conflict"

        disabled = await client.patch(
            f"/policies/{shadowing_policy_id}",
            headers={
                **auth,
                "If-Match": '"1"',
                "X-Request-ID": "req_prd068_invariant_shadow_disable",
            },
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text

        # 模拟存储事实被越权篡改；运行时必须在推进 head 前交叉核验并失败关闭。
        connection = connect_database(database_url)
        connection.execute(
            "DROP TRIGGER trg_policy_template_application_items_update_prd068"
        )
        connection.execute(
            """
            UPDATE policy_template_application_items SET after_hash=?
            WHERE application_id=? AND resource_type='policy'
            """,
            ("0" * 64, applied["application_id"]),
        )
        connection.commit()
        connection.close()

        drifted = await _publish_current(
            "不得发布生成策略图漂移",
            "req_prd068_invariant_graph_drift",
        )
        assert drifted.status_code == 409, drifted.text
        assert drifted.json()["code"] == "policy_snapshot.template_invariant_conflict"
        current_after_failures = await client.get(
            "/policy-snapshots/current",
            headers=auth,
        )
        assert current_after_failures.status_code == 200
        assert (
            current_after_failures.json()["current_snapshot"]["snapshot_version"] == 3
        )


@pytest.mark.anyio
async def test_template_aware_rollback_compensates_and_replays_persisted_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回滚主流程：补偿发布、停用生成策略，并从四类持久事实幂等补派。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    dispatched: list[object] = []

    def _capture_dispatch(_config, _tenant_id, event, **_kwargs):
        """只在内存捕获提交后事件，验证稳定 ID 且不访问测试沙箱外网络。"""

        dispatched.append(event)
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", _capture_dispatch)
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        applied = await _apply_customer_redaction(
            client,
            auth,
            request_suffix="rollback",
        )
        rollback_headers = {
            **auth,
            "If-Match": '"2"',
            "X-Request-ID": "req_prd068_template_rollback",
        }
        rolled_back = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers=rollback_headers,
            json={"reason": "撤销客服脱敏模板"},
        )
        assert rolled_back.status_code == 201, rolled_back.text
        rollback_snapshot = rolled_back.json()["policy_snapshot"]
        assert rollback_snapshot["snapshot_version"] == 3
        assert rollback_snapshot["operation"] == "publish"
        assert rollback_snapshot["rollback_source_snapshot_version"] is None
        assert rollback_snapshot["policy_count"] == 0
        assert rolled_back.json()["idempotent_replay"] is False

        replay = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers=rollback_headers,
            json={"reason": "撤销客服脱敏模板"},
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["policy_snapshot"] == rollback_snapshot

        request_conflict = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers=rollback_headers,
            json={"reason": "同一请求号不得改变撤销原因"},
        )
        assert request_conflict.status_code == 409, request_conflict.text
        assert request_conflict.json()["code"] == "policy_template.request_conflict"

        forbidden_reactivation = await client.post(
            f"/policy-snapshots/{applied['result_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"3"',
                "X-Request-ID": "req_prd068_template_reactivation_blocked",
            },
            json={"reason": "普通回滚不得复活已撤销模板"},
        )
        assert forbidden_reactivation.status_code == 409, forbidden_reactivation.text
        assert (
            forbidden_reactivation.json()["code"]
            == "policy_snapshot.rollback_target_invalid"
        )

        apply_replay = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers={
                **auth,
                "If-Match": applied["apply_if_match"],
                "If-Policy-Candidate-Match": applied["apply_candidate_match"],
                "X-Request-ID": applied["apply_request_id"],
            },
            json=applied["apply_payload"],
        )
        assert apply_replay.status_code == 200, apply_replay.text
        assert apply_replay.json()["idempotent_replay"] is True
        assert apply_replay.json()["application"]["status"] == "applied"
        assert apply_replay.json()["application"]["rollback_snapshot_id"] is None
        assert apply_replay.json()["generated_policy"]["status"] == "active"
        assert apply_replay.json()["generated_policy"]["version"] == 1

        current = await client.get("/policy-snapshots/current", headers=auth)
        assert current.status_code == 200, current.text
        assert current.json()["candidate_matches_current"] is True
        assert current.json()["candidate_policy_count"] == 0

    connection = connect_database(database_url)
    application = connection.execute(
        """
        SELECT * FROM policy_template_applications WHERE application_id=?
        """,
        (applied["application_id"],),
    ).fetchone()
    policy = connection.execute(
        "SELECT status,version FROM policies WHERE policy_id=?",
        (applied["generated_policy_id"],),
    ).fetchone()
    rollback_facts = connection.execute(
        """
        SELECT action,object_id FROM audit_logs
        WHERE request_id='req_prd068_template_rollback' ORDER BY action
        """
    ).fetchall()
    snapshot_events = connection.execute(
        """
        SELECT policy_snapshot_event_id,operation FROM policy_snapshot_events
        WHERE request_id='req_prd068_template_rollback'
        """
    ).fetchall()
    connection.close()
    assert application["status"] == "rolled_back"
    assert application["rollback_snapshot_id"] == rollback_snapshot[
        "policy_snapshot_id"
    ]
    assert application["rollback_snapshot_version"] == 3
    assert policy["status"] == "disabled"
    assert policy["version"] == 2
    assert [(row["action"], row["object_id"]) for row in rollback_facts] == [
        ("policy.updated", applied["generated_policy_id"]),
        ("policy_snapshot.published", rollback_snapshot["policy_snapshot_id"]),
        ("policy_template.rolled_back", applied["application_id"]),
    ]
    assert len(snapshot_events) == 1
    assert snapshot_events[0]["operation"] == "publish"

    rollback_webhooks = [
        event
        for event in dispatched
        if event.object_id == rollback_snapshot["policy_snapshot_id"]
    ]
    # 首次提交和幂等回放都补派相同 Snapshot Audit 与 source event ID。
    assert len(rollback_webhooks) == 4
    assert len({event.event_id for event in rollback_webhooks}) == 2


@pytest.mark.anyio
async def test_mask_snapshot_can_use_ordinary_rollback_when_runtime_gate_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """兼容：能力门禁开启时，合法 mask 历史制品可走普通上一版回滚。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_manual_mask_create"},
            json={
                "name": "人工输入脱敏策略",
                "status": "active",
                "priority": 20,
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "mask"},
            },
        )
        assert created.status_code == 201, created.text
        policy_id = created.json()["policy"]["policy_id"]
        first = await _publish_template_baseline(
            client,
            auth,
            request_suffix="manual_mask_v1",
        )
        assert first["snapshot_version"] == 1

        disabled = await client.patch(
            f"/policies/{policy_id}",
            headers={
                **auth,
                "If-Match": '"1"',
                "X-Request-ID": "req_prd068_manual_mask_disable",
            },
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text
        current = await client.get("/policy-snapshots/current", headers=auth)
        assert current.status_code == 200, current.text
        second = await client.post(
            "/policy-snapshots/publish",
            headers={
                **auth,
                "If-Match": current.headers["ETag"],
                "If-Policy-Candidate-Match": current.headers[
                    "X-Policy-Candidate-ETag"
                ],
                "X-Request-ID": "req_prd068_manual_mask_v2",
            },
            json={"reason": "临时停用人工脱敏策略"},
        )
        assert second.status_code == 201, second.text
        assert second.json()["policy_snapshot"]["snapshot_version"] == 2

        restored = await client.post(
            f"/policy-snapshots/{first['policy_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"2"',
                "X-Request-ID": "req_prd068_manual_mask_rollback",
            },
            json={"reason": "恢复已验证的人工脱敏制品"},
        )
        assert restored.status_code == 201, restored.text
        restored_snapshot = restored.json()["policy_snapshot"]
        assert restored_snapshot["operation"] == "rollback"
        assert restored_snapshot["rollback_source_snapshot_version"] == 1

    connection = connect_database(database_url)
    restored_actions = connection.execute(
        """
        SELECT action_type FROM policy_snapshot_items
        WHERE tenant_id=? AND policy_snapshot_id=?
        """,
        (created.json()["policy"]["tenant_id"], restored_snapshot["policy_snapshot_id"]),
    ).fetchall()
    connection.close()
    assert [row["action_type"] for row in restored_actions] == ["mask"]


@pytest.mark.anyio
async def test_template_rollback_preserves_later_published_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """后续发布：补偿撤销只删除 generated item，并重新编号保留其余策略。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        applied = await _apply_customer_redaction(
            client,
            auth,
            request_suffix="preserve",
        )
        other_project = await client.post(
            "/projects",
            headers={**auth, "X-Request-ID": "req_prd068_preserve_project_other"},
            json={"project_code": "prd068-preserve-other", "name": "后续发布项目"},
        )
        assert other_project.status_code == 201, other_project.text
        other_project_id = other_project.json()["project"]["project_id"]
        later_policy = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_preserve_policy"},
            json={
                "name": "后续互斥项目策略",
                "status": "active",
                "priority": 1,
                "rule": {"condition_expr": f'project == "{other_project_id}"'},
                "action": {"type": "allow"},
            },
        )
        assert later_policy.status_code == 201, later_policy.text
        later_policy_id = later_policy.json()["policy"]["policy_id"]
        current = await client.get("/policy-snapshots/current", headers=auth)
        published_later = await client.post(
            "/policy-snapshots/publish",
            headers={
                **auth,
                "If-Match": current.headers["ETag"],
                "If-Policy-Candidate-Match": current.headers[
                    "X-Policy-Candidate-ETag"
                ],
                "X-Request-ID": "req_prd068_preserve_publish",
            },
            json={"reason": "发布模板后的互斥项目策略"},
        )
        assert published_later.status_code == 201, published_later.text
        assert published_later.json()["policy_snapshot"]["snapshot_version"] == 3

        compensated = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"3"',
                "X-Request-ID": "req_prd068_preserve_rollback",
            },
            json={"reason": "仅撤销模板并保留后续策略"},
        )
        assert compensated.status_code == 201, compensated.text
        compensated_snapshot = compensated.json()["policy_snapshot"]
        assert compensated_snapshot["snapshot_version"] == 4
        assert compensated_snapshot["operation"] == "publish"
        assert compensated_snapshot["policy_count"] == 1

    connection = connect_database(database_url)
    items = connection.execute(
        """
        SELECT evaluation_order,source_policy_id FROM policy_snapshot_items
        WHERE policy_snapshot_id=? ORDER BY evaluation_order
        """,
        (compensated_snapshot["policy_snapshot_id"],),
    ).fetchall()
    connection.close()
    assert [(row["evaluation_order"], row["source_policy_id"]) for row in items] == [
        (0, later_policy_id)
    ]


@pytest.mark.anyio
async def test_template_rollback_rejects_unpublished_candidate_drift_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常流程：未发布 candidate 漂移必须 409，三类核心状态保持原值。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        applied = await _apply_customer_redaction(
            client,
            auth,
            request_suffix="drift",
        )
        drift_policy = await client.post(
            "/policies",
            headers={**auth, "X-Request-ID": "req_prd068_rollback_drift_policy"},
            json={
                "name": "尚未发布的候选策略",
                "status": "active",
                "priority": 50,
                "rule": {"condition_expr": 'env == "prod"'},
                "action": {"type": "deny"},
            },
        )
        assert drift_policy.status_code == 201, drift_policy.text
        rejected = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"2"',
                "X-Request-ID": "req_prd068_rollback_candidate_drift",
            },
            json={"reason": "不得用回滚覆盖未发布候选"},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["code"] == "policy_template.rollback_conflict"

    connection = connect_database(database_url)
    application = connection.execute(
        """
        SELECT status,rollback_snapshot_id FROM policy_template_applications
        WHERE application_id=?
        """,
        (applied["application_id"],),
    ).fetchone()
    generated_policy = connection.execute(
        "SELECT status,version FROM policies WHERE policy_id=?",
        (applied["generated_policy_id"],),
    ).fetchone()
    head = connection.execute(
        "SELECT current_snapshot_version FROM policy_snapshot_heads"
    ).fetchone()
    failed_facts = connection.execute(
        """
        SELECT COUNT(*) AS count FROM audit_logs
        WHERE request_id='req_prd068_rollback_candidate_drift'
        """
    ).fetchone()
    connection.close()
    assert application["status"] == "applied"
    assert application["rollback_snapshot_id"] is None
    assert generated_policy["status"] == "active"
    assert generated_policy["version"] == 1
    assert head["current_snapshot_version"] == 2
    assert failed_facts["count"] == 0


@pytest.mark.anyio
async def test_template_permissions_and_tenant_bound_bindings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/租户：读写权限分离，跨租户与 disabled Project 同形拒绝。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()["tenant_id"]
    now = "2026-08-03T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        [
            (
                "user_prd068_developer",
                tenant_id,
                "developer-prd068@example.com",
                "PRD068 Developer",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                now,
                now,
            ),
            (
                "user_prd068_read_only",
                tenant_id,
                "readonly-prd068@example.com",
                "PRD068 Read Only",
                hash_password(READ_ONLY_PASSWORD),
                "finance_admin",
                now,
                now,
            ),
        ],
    )
    finance_role = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='finance_admin'",
        (tenant_id,),
    ).fetchone()
    connection.execute(
        """
        DELETE FROM role_permissions
        WHERE tenant_id=? AND role_id=? AND permission_id=(
          SELECT permission_id FROM permissions
          WHERE tenant_id=? AND code='policy_template.apply'
        )
        """,
        (tenant_id, finance_role["role_id"], tenant_id),
    )
    finance_codes = [
        row["code"]
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
            """,
            (tenant_id, finance_role["role_id"]),
        ).fetchall()
    ]
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            int(finance_role["permission_revision"]) + 1,
            permission_matrix_sha256(finance_codes),
            now,
            tenant_id,
            finance_role["role_id"],
        ),
    )

    other_tenant_id = "tenant_prd068_other"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?, 'prd068-other', 'PRD068 Other', 'internal_only', 'active', ?, ?)
        """,
        (other_tenant_id, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd068_other_admin',?,'admin@prd068-other.example.com',
                 'Other Admin',?,'admin','active',?,?)
        """,
        (other_tenant_id, hash_password(OTHER_ADMIN_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()

    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        admin = await _login(client)
        developer = await _login(
            client,
            email="developer-prd068@example.com",
            password=DEVELOPER_PASSWORD,
        )
        read_only = await _login(
            client,
            email="readonly-prd068@example.com",
            password=READ_ONLY_PASSWORD,
        )
        other_admin = await _login(
            client,
            email="admin@prd068-other.example.com",
            password=OTHER_ADMIN_PASSWORD,
            tenant_slug="prd068-other",
        )
        admin_auth = {"Authorization": f"Bearer {admin}"}
        other_auth = {"Authorization": f"Bearer {other_admin}"}

        default_catalog = await client.get(
            "/policy-templates",
            headers={**admin_auth, "X-Request-ID": "req_prd068_default_catalog"},
        )
        other_catalog = await client.get(
            "/policy-templates",
            headers={**other_auth, "X-Request-ID": "req_prd068_other_catalog"},
        )
        denied_read = await client.get(
            "/policy-templates",
            headers={
                "Authorization": f"Bearer {developer}",
                "X-Request-ID": "req_prd068_denied_read",
            },
        )
        allowed_read = await client.get(
            "/policy-templates",
            headers={
                "Authorization": f"Bearer {read_only}",
                "X-Request-ID": "req_prd068_allowed_read",
            },
        )
        denied_apply = await client.post(
            "/policy-templates/ptpl_customer_service_redaction/apply",
            headers={
                "Authorization": f"Bearer {read_only}",
                "X-Request-ID": "req_prd068_denied_apply",
            },
            content=b"not-json-and-must-not-be-parsed",
        )

        other_project = await client.post(
            "/projects",
            headers={**other_auth, "X-Request-ID": "req_prd068_other_project"},
            json={"project_code": "other-binding", "name": "其他租户项目"},
        )
        assert other_project.status_code == 201, other_project.text
        other_project_id = other_project.json()["project"]["project_id"]
        cross_tenant = await client.get(
            "/policy-templates",
            headers={**admin_auth, "X-Request-ID": "req_prd068_cross_binding"},
            params={
                "template_id": "ptpl_customer_service_redaction",
                "project_id": other_project_id,
            },
        )

        local_project = await client.post(
            "/projects",
            headers={**admin_auth, "X-Request-ID": "req_prd068_disabled_project"},
            json={"project_code": "disabled-binding", "name": "已停用绑定项目"},
        )
        assert local_project.status_code == 201, local_project.text
        local_project_id = local_project.json()["project"]["project_id"]
        disabled = await client.patch(
            f"/projects/{local_project_id}",
            headers={
                **admin_auth,
                "If-Match": '"1"',
                "X-Request-ID": "req_prd068_disable_project",
            },
            json={"status": "disabled"},
        )
        assert disabled.status_code == 200, disabled.text
        disabled_binding = await client.get(
            "/policy-templates",
            headers={**admin_auth, "X-Request-ID": "req_prd068_disabled_binding"},
            params={
                "template_id": "ptpl_customer_service_redaction",
                "project_id": local_project_id,
            },
        )

    assert default_catalog.status_code == 200
    assert other_catalog.status_code == 200
    assert default_catalog.json()["total"] == 5
    assert other_catalog.json()["total"] == 5
    assert {
        item["tenant_id"] for item in default_catalog.json()["policy_templates"]
    } == {tenant_id}
    assert {
        item["tenant_id"] for item in other_catalog.json()["policy_templates"]
    } == {other_tenant_id}
    assert denied_read.status_code == 403
    assert denied_read.json()["code"] == "auth.permission_denied"
    assert denied_read.json()["request_id"] == "req_prd068_denied_read"
    assert allowed_read.status_code == 200
    assert denied_apply.status_code == 403
    assert denied_apply.json()["code"] == "auth.permission_denied"
    assert denied_apply.json()["request_id"] == "req_prd068_denied_apply"
    assert cross_tenant.status_code == 422
    assert cross_tenant.json()["code"] == "policy_template.binding_invalid"
    assert disabled_binding.status_code == 422
    assert disabled_binding.json()["code"] == "policy_template.binding_invalid"


@pytest.mark.anyio
async def test_template_rollback_lock_permission_and_audit_failure_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """写锁权限与审计故障：任一失败都不得留下 rolled_back 或补偿快照。"""

    database_url = _prepare_database(monkeypatch, tmp_path)
    from metahub_control.auth import resource_transactions
    from metahub_control.policy_templates import service as template_service
    from metahub_control.webhooks import service as webhook_service

    monkeypatch.setattr(
        webhook_service,
        "dispatch_webhook_event",
        _discard_webhook_dispatch,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_build_app()),
        base_url="http://control",
    ) as client:
        token = await _login(client)
        auth = {"Authorization": f"Bearer {token}"}
        applied = await _apply_customer_redaction(
            client,
            auth,
            request_suffix="atomic",
        )
        original_permission_check = resource_transactions.require_transaction_permission

        def _deny_locked_publish(
            connection,
            context,
            permission_code,
            *,
            unavailable_error_code,
        ):
            """模拟 dependency 通过后、写锁内权限被撤销的真实竞态。"""

            # 权限在路由预检后仍可能撤销，事务锁内复核必须拒绝发布而不能沿用旧授权。
            if permission_code == "policy.publish":
                raise ApiError("auth.permission_denied")
            return original_permission_check(
                connection,
                context,
                permission_code,
                unavailable_error_code=unavailable_error_code,
            )

        monkeypatch.setattr(
            resource_transactions,
            "require_transaction_permission",
            _deny_locked_publish,
        )
        permission_denied = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"2"',
                "X-Request-ID": "req_prd068_rollback_permission_revoked",
            },
            json={"reason": "写锁内撤权必须失败"},
        )
        assert permission_denied.status_code == 403, permission_denied.text
        assert permission_denied.json()["code"] == "auth.permission_denied"

        monkeypatch.setattr(
            resource_transactions,
            "require_transaction_permission",
            original_permission_check,
        )
        original_audit = template_service.record_audit_log

        def _fail_snapshot_audit(*args, **kwargs):
            """在 Policy Audit 已写入后注入 Snapshot Audit 故障，验证整笔回滚。"""

            # 第二条关键审计失败时必须让数据库事务整体回滚，不能留下半套发布证据。
            if kwargs.get("action") == "policy_snapshot.published":
                raise ApiError("audit_log.record_unavailable")
            return original_audit(*args, **kwargs)

        monkeypatch.setattr(template_service, "record_audit_log", _fail_snapshot_audit)
        audit_failed = await client.post(
            f"/policy-snapshots/{applied['previous_snapshot_id']}/rollback",
            headers={
                **auth,
                "If-Match": '"2"',
                "X-Request-ID": "req_prd068_rollback_audit_failed",
            },
            json={"reason": "审计故障必须整体回滚"},
        )
        assert audit_failed.status_code == 503, audit_failed.text
        assert audit_failed.json()["code"] == "policy_template.unavailable"

    connection = connect_database(database_url)
    application = connection.execute(
        """
        SELECT status,rollback_snapshot_id FROM policy_template_applications
        WHERE application_id=?
        """,
        (applied["application_id"],),
    ).fetchone()
    policy = connection.execute(
        "SELECT status,version FROM policies WHERE policy_id=?",
        (applied["generated_policy_id"],),
    ).fetchone()
    head = connection.execute(
        "SELECT current_snapshot_version FROM policy_snapshot_heads"
    ).fetchone()
    failed_snapshots = connection.execute(
        """
        SELECT COUNT(*) AS count FROM policy_snapshots
        WHERE request_id IN (
          'req_prd068_rollback_permission_revoked',
          'req_prd068_rollback_audit_failed'
        )
        """
    ).fetchone()
    failed_audits = connection.execute(
        """
        SELECT COUNT(*) AS count FROM audit_logs
        WHERE request_id IN (
          'req_prd068_rollback_permission_revoked',
          'req_prd068_rollback_audit_failed'
        )
        """
    ).fetchone()
    connection.close()
    assert application["status"] == "applied"
    assert application["rollback_snapshot_id"] is None
    assert policy["status"] == "active"
    assert policy["version"] == 1
    assert head["current_snapshot_version"] == 2
    assert failed_snapshots["count"] == 0
    assert failed_audits["count"] == 0
