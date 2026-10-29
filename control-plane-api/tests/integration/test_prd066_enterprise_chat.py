"""PRD-066 企业 Chat 的 Control 主流程、异常、权限、租户和隐私集成测试。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path
from uuid import uuid4

import metahub_control.enterprise_chat.routes as chat_routes
import metahub_control.enterprise_chat.service as chat_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import authenticate_bearer
from metahub_control.console.routes import router as console_router
from metahub_control.enterprise_chat.models import CreateChatSessionRequest
from metahub_control.enterprise_chat.routes import router as chat_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试凭据仅服务隔离数据库；正文金丝雀用于证明 Prompt/Response 不进入持久证据。
ADMIN_PASSWORD = "MetaHubLocal!2026"
VIEWER_PASSWORD = "PRD066Viewer!2026"
PROMPT_CANARY = "PRD066_PROMPT_MUST_STAY_IN_MEMORY"
ASSISTANT_CANARY = "PRD066_ASSISTANT_MUST_STAY_IN_MEMORY"


@pytest.mark.parametrize(
    ("cache_tokens", "final_selection_exists", "expected"),
    [
        (2, False, "hit"),
        (2, True, "hit"),
        (0, True, "miss"),
        (0, False, "not_configured"),
    ],
)
def test_enterprise_chat_cache_summary_uses_formal_final_facts(
    cache_tokens: int,
    final_selection_exists: bool,
    expected: str,
) -> None:
    """Cache 摘要只采信正式 Token 和最终 attempt，覆盖全部可达三态及命中优先级。"""

    assert chat_service._cache_status(
        cache_tokens=cache_tokens,
        final_selection_exists=final_selection_exists,
    ) == expected


def _sqlite_url(path: Path) -> str:
    """把 pytest 独占数据库路径转换为项目支持的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def _prepare_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[str, str, str, str, str, str]:
    """创建 0058 数据库、唯一 Project Key 绑定和第二租户 Viewer。

    返回数据库 URL、默认租户、Project、Environment、Alias 和第二租户 slug。所有测试
    资源均为稳定 ID；明文 Virtual Key、Provider Key 和模型正文不写入测试证据。
    """

    database_url = _sqlite_url(tmp_path / "metahub-prd066.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd066-control-auth-secret-32-bytes-minimum")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
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
    admin_user = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? AND email='admin@metahub.local'",
        (seed.tenant_id,),
    ).fetchone()
    virtual_key = connection.execute(
        """
        SELECT virtual_key_id FROM virtual_keys
        WHERE tenant_id=? AND status='active'
          AND (environment_id=? OR (environment_id IS NULL AND environment=?))
        ORDER BY virtual_key_id LIMIT 1
        """,
        (seed.tenant_id, environment["environment_id"], environment["code"]),
    ).fetchone()
    assert all(
        item is not None for item in (environment, model, admin_user, virtual_key)
    )
    admin_user_id = str(admin_user["user_id"])
    now = "2026-08-02T01:00:00Z"
    project_id = "prj_prd066_control"
    alias_id = "mal_prd066_control"
    # 默认 seed 只创建底层模型，不创建租户别名；测试必须显式建立环境内 active 别名，
    # 不能依赖开发数据库存量，否则独立 pytest 数据库会在业务请求前产生假失败。
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
            "prd066-chat",
            environment["code"],
            environment["environment_id"],
            model["model_id"],
            "PRD-066 企业 Chat 测试别名",
            admin_user_id,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?, 'active',?,?,?,?)
        """,
        (
            project_id,
            seed.tenant_id,
            "prd066-control",
            "PRD066 Control",
            "",
            admin_user_id,
            admin_user_id,
            admin_user_id,
            now,
            now,
        ),
    )
    # 只有一把 active Key 绑定本 Project；其他种子 Key 保持未绑定，不能被目录误计数。
    connection.execute(
        """
        UPDATE virtual_keys SET project_id=?,environment_id=?,updated_at=?
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (
            project_id,
            environment["environment_id"],
            now,
            seed.tenant_id,
            virtual_key["virtual_key_id"],
        ),
    )

    second_tenant_id = "tenant_prd066_second"
    second_slug = "prd066-second"
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (second_tenant_id, second_slug, "PRD066 Second", now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'viewer','active',?,?)
        """,
        (
            "user_prd066_second_viewer",
            second_tenant_id,
            "viewer-prd066@example.com",
            "PRD066 Viewer",
            hash_password(VIEWER_PASSWORD),
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()
    return (
        database_url,
        seed.tenant_id,
        project_id,
        str(environment["environment_id"]),
        alias_id,
        second_slug,
    )


def _build_app():
    """构造只含认证与企业 Chat 路由的隔离 Control 应用。"""

    app = create_service_app("control-plane-api", "PRD-066 Enterprise Chat Test API")
    app.include_router(auth_router)
    app.include_router(console_router)
    app.include_router(chat_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    request_id: str,
) -> str:
    """登录测试用户并返回仅在当前协程内使用的 Session Token。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200, response.text
    # 登录 Token 只返回当前协程调用方，不写入断言、日志或任何验收证据文件。
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str) -> dict[str, str]:
    """构造认证和统一链路请求头，不把 Token 写入任何断言。"""

    # 明文 Token 仅在内存请求头存活，测试结果只能检查状态和固定链路标识。
    return {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}


def _cache_fact_id(prefix: str, request_id: str, purpose: str) -> str:
    """为隔离数据库中的缓存路由事实生成规范且不可关联正文的稳定 ID。

    参数：表要求的公开前缀、请求 ID 和固定事实用途。返回：前缀加 32 位十六进制标识。
    关键约束：摘要只消费测试 request_id 和固定用途，不接触 Prompt、Response 或凭据。
    """

    digest = hashlib.sha256(f"{request_id}:{purpose}".encode("ascii")).hexdigest()
    return f"{prefix}{digest[:32]}"


def _write_cache_routing_facts(
    connection,
    grant_row,
    *,
    request_id: str,
    trace_id: str,
    scenario: str,
) -> tuple[str, int, int, int]:
    """写入一次 Gateway 最终成功所需的合法 Attempt 与可选缓存 Selection 图。

    参数：当前独占连接、Chat grant 权威行、链路 ID 和固定场景。返回：最终 attempt ID、
    fallback 标记、attempt 数和真实 cache token 数。``fallback_primary_selection`` 会让
    主 attempt 的 Selection 以失败终态保留，再由无 Selection 的 fallback 成功，专门证明
    Control 只读取 ``final_routing_attempt_id``，不会把旧选择误报为 miss。
    """

    assert scenario in {
        "hit",
        "miss",
        "not_configured",
        "fallback_primary_selection",
    }
    model = connection.execute(
        """
        SELECT model.model_id,model.model_alias AS internal_model_id,
               model.provider_id,provider.provider_code
        FROM model_aliases alias
        JOIN models model
          ON model.tenant_id=alias.tenant_id
         AND model.model_id=alias.default_model_id
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id
         AND provider.provider_id=model.provider_id
        WHERE alias.tenant_id=? AND alias.model_alias_id=?
        """,
        (grant_row["tenant_id"], grant_row["model_alias_id"]),
    ).fetchone()
    assert model is not None
    fallback = scenario == "fallback_primary_selection"
    attempt_count = 2 if fallback else 1
    attempt_ids = {
        sequence: _cache_fact_id("rta_", request_id, f"attempt-{sequence}")
        for sequence in range(1, attempt_count + 1)
    }
    completed_at = str(grant_row["issued_at"])
    # Selection 的插入门禁要求 attempt 仍为 started，因此先建立完整尝试，再提交终态。
    for sequence in range(1, attempt_count + 1):
        is_fallback = sequence == 2
        connection.execute(
            """
            INSERT INTO routing_attempts(
              routing_attempt_id,tenant_id,request_id,trace_id,model_alias_id,
              attempt_sequence,attempt_role,trigger_type,trigger_error_code,
              model_id,internal_model_id,provider_id,provider_code,status,retryable,
              usage_status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'started',0,'pending',?,?)
            """,
            (
                attempt_ids[sequence],
                grant_row["tenant_id"],
                request_id,
                trace_id,
                grant_row["model_alias_id"],
                sequence,
                "fallback" if is_fallback else "primary",
                "fallback" if is_fallback else "initial",
                "gateway.upstream_error" if is_fallback else None,
                model["model_id"],
                model["internal_model_id"],
                model["provider_id"],
                model["provider_code"],
                grant_row["issued_at"],
                grant_row["issued_at"],
            ),
        )

    selection_id: str | None = None
    if scenario in {"hit", "miss", "fallback_primary_selection"}:
        cache_affinity_id = _cache_fact_id("caf_", request_id, "affinity")
        scope_id = _cache_fact_id("cafs_", request_id, "scope")
        binding_id = _cache_fact_id("cab_", request_id, "binding")
        selection_id = _cache_fact_id("cas_", request_id, "selection")
        endpoint_digest = hashlib.sha256(
            f"{request_id}:endpoint".encode("ascii")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO cache_affinities(
              cache_affinity_id,tenant_id,model_alias_id,status,data_class,
              minimum_prefix_chars,ttl_seconds,version,created_by,updated_by,
              created_at,updated_at
            ) VALUES(?,?,?,'enabled','internal',1024,3600,1,?,?,?,?)
            """,
            (
                cache_affinity_id,
                grant_row["tenant_id"],
                grant_row["model_alias_id"],
                grant_row["owner_user_id"],
                grant_row["owner_user_id"],
                grant_row["issued_at"],
                grant_row["issued_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinity_scopes(
              cache_affinity_scope_id,tenant_id,virtual_key_id,digest_schema_version,
              affinity_scope_digest,preferred_binding_id,preferred_lease_generation,
              last_selected_at,created_at,updated_at
            ) VALUES(?,?,?,1,?,NULL,NULL,?,?,?)
            """,
            (
                scope_id,
                grant_row["tenant_id"],
                grant_row["virtual_key_id"],
                hashlib.sha256(f"{request_id}:scope".encode("ascii")).hexdigest(),
                grant_row["issued_at"],
                grant_row["issued_at"],
                grant_row["issued_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinity_bindings(
              cache_affinity_binding_id,tenant_id,cache_affinity_scope_id,
              affinity_key_digest,model_endpoint_id,endpoint_revision_digest,
              provider_id,provider_credential_id,target_kind,target_generation,
              pending_lease_generation,pending_lease_started_at,
              pending_lease_expires_at,active_selection_count,last_success_at,
              selection_count,estimated_hit_count,last_selected_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,NULL,'none',1,1,?,?,1,NULL,1,0,?,?,?)
            """,
            (
                binding_id,
                grant_row["tenant_id"],
                scope_id,
                hashlib.sha256(f"{request_id}:binding".encode("ascii")).hexdigest(),
                model["model_id"],
                endpoint_digest,
                model["provider_id"],
                grant_row["issued_at"],
                grant_row["expires_at"],
                grant_row["issued_at"],
                grant_row["issued_at"],
                grant_row["issued_at"],
            ),
        )
        connection.execute(
            """
            INSERT INTO cache_affinity_selections(
              cache_affinity_selection_id,tenant_id,request_id,trace_id,
              attempt_sequence,routing_attempt_id,stream_session_id,
              cache_affinity_id,cache_affinity_version,
              cache_affinity_scope_id_snapshot,cache_affinity_binding_id_snapshot,
              model_alias_id,model_endpoint_id,endpoint_revision_digest_snapshot,
              provider_id,provider_code_snapshot,provider_credential_id_snapshot,
              target_kind,target_generation,pending_lease_generation_snapshot,
              ttl_seconds_snapshot,warm_evidence_at_snapshot,
              endpoint_preference_applied,endpoint_preference_state,
              binding_state_before,selection_reason,estimated_hit,
              outcome_status,outcome_at,created_at
            ) VALUES(?,?,?,?,1,?,NULL,?,1,?,?,?,?,?,?,?,NULL,'none',1,1,3600,NULL,
              0,'none','missing','binding_created',0,'pending',NULL,?)
            """,
            (
                selection_id,
                grant_row["tenant_id"],
                request_id,
                trace_id,
                attempt_ids[1],
                cache_affinity_id,
                scope_id,
                binding_id,
                grant_row["model_alias_id"],
                model["model_id"],
                endpoint_digest,
                model["provider_id"],
                model["provider_code"],
                grant_row["issued_at"],
            ),
        )

    if fallback:
        # 主 attempt 先失败并把旧 Selection 收口为 failed；它仍保留为可审计历史。
        connection.execute(
            """
            UPDATE routing_attempts
            SET status='failed',error_code='gateway.upstream_error',
                error_category='upstream_5xx',upstream_http_status=502,retryable=1,
                latency_ms=5,usage_status='unavailable',completed_at=?,updated_at=?
            WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
            """,
            (
                completed_at,
                completed_at,
                grant_row["tenant_id"],
                attempt_ids[1],
            ),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
        assert selection_id is not None
        connection.execute(
            """
            UPDATE cache_affinity_selections
            SET outcome_status='failed',outcome_at=?
            WHERE tenant_id=? AND cache_affinity_selection_id=?
              AND outcome_status='pending'
            """,
            (completed_at, grant_row["tenant_id"], selection_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1

    final_sequence = 2 if fallback else 1
    connection.execute(
        """
        UPDATE routing_attempts
        SET status='succeeded',latency_ms=3,usage_status='recorded',
            completed_at=?,updated_at=?
        WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
        """,
        (
            completed_at,
            completed_at,
            grant_row["tenant_id"],
            attempt_ids[final_sequence],
        ),
    )
    assert connection.execute("SELECT changes()").fetchone()[0] == 1
    if selection_id is not None and not fallback:
        connection.execute(
            """
            UPDATE cache_affinity_selections
            SET outcome_status='succeeded',outcome_at=?
            WHERE tenant_id=? AND cache_affinity_selection_id=?
              AND outcome_status='pending'
            """,
            (completed_at, grant_row["tenant_id"], selection_id),
        )
        assert connection.execute("SELECT changes()").fetchone()[0] == 1
    return (
        attempt_ids[final_sequence],
        int(fallback),
        attempt_count,
        2 if scenario == "hit" else 0,
    )


def _successful_gateway(
    database_url: str,
    *,
    write_usage: bool = True,
    cache_scenario: str | None = None,
):
    """返回会消费 grant 并写入安全治理事实的 Gateway 替身。

    ``write_usage=False`` 只用于验证账本缺失时 Control 必须失败关闭；RequestLog 仍保留
    成功快照，从而证明实现不会回退到估算 Token 或成本伪造页面成功。``cache_scenario``
    仅供缓存摘要专项写入真实 Attempt/Selection 图；默认 ``None`` 保持既有夹具行为。
    """

    async def call_gateway(
        config,
        *,
        grant: str,
        history: list[dict[str, str]],
        request_id: str,
        trace_id: str,
    ) -> dict[str, object]:
        """模拟共用治理链成功，不保存传入 history 或 assistant 正文。"""

        assert history[-1] == {"role": "user", "content": PROMPT_CANARY}
        connection = connect_database(database_url)
        row = connection.execute(
            """
            SELECT * FROM chat_execution_grants
            WHERE gateway_request_id=? AND status='issued'
            """,
            (request_id,),
        ).fetchone()
        assert row is not None
        assert hashlib.sha256(grant.encode("ascii")).hexdigest() == row["grant_hash"]
        connection.execute(
            """
            UPDATE chat_execution_grants SET status='consumed',consumed_at=issued_at
            WHERE tenant_id=? AND chat_execution_grant_id=? AND status='issued'
            """,
            (row["tenant_id"], row["chat_execution_grant_id"]),
        )
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at
            ) VALUES(?,?,?,?,NULL,'disabled',?)
            """,
            (row["tenant_id"], request_id, trace_id, row["virtual_key_id"], row["issued_at"]),
        )
        final_attempt_id = None
        fallback_triggered = 0
        routing_attempt_count = 0
        cache_tokens = 0
        if cache_scenario is not None:
            (
                final_attempt_id,
                fallback_triggered,
                routing_attempt_count,
                cache_tokens,
            ) = _write_cache_routing_facts(
                connection,
                row,
                request_id=request_id,
                trace_id=trace_id,
                scenario=cache_scenario,
            )
        connection.execute(
            """
            INSERT INTO request_logs(
              request_id,tenant_id,trace_id,virtual_key_id,status,error_code,latency_ms,
              created_at,endpoint,request_model,input_tokens,output_tokens,total_tokens,
              estimated_cost_amount,currency,updated_at,project_id,environment_id,
              fallback_triggered,routing_attempt_count,final_routing_attempt_id,snapshot_version,
              policy_snapshot_status,policy_decision,policy_reason_code
            ) VALUES(?,?,?,?,'success',NULL,7,?,'chat/completions','prd066-chat',4,3,7,
              0,'CNY',?,?,?,?,?,?,NULL,'disabled','allow','policy.default_allow')
            """,
            (
                request_id,
                row["tenant_id"],
                trace_id,
                row["virtual_key_id"],
                row["issued_at"],
                row["issued_at"],
                row["project_id"],
                row["environment_id"],
                fallback_triggered,
                routing_attempt_count,
                final_attempt_id,
            ),
        )
        # 成功摘要必须由正式 UsageLedger 证明；关闭开关专门验证缺账时失败关闭。
        if write_usage:
            connection.execute(
                """
                INSERT INTO usage_ledger(
                  usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,
                  raw_usage,normalized_usage,cost_amount,currency,price_schema_version,
                  created_at,status,total_tokens,cache_tokens,cost_cny,cost_usd,estimated,
                  usage_source,virtual_key_id,project_id,environment_id,updated_at,
                  routing_attempt_id
                ) VALUES(?,?,?,?,?,'{}',?,0,'CNY','unpriced-v1',?,'recorded',?,?,0,0,0,
                  'upstream',?,?,?,?,?)
                """,
                (
                    f"usg_{uuid4().hex}",
                    row["tenant_id"],
                    request_id,
                    4,
                    3,
                    '{"cost_amount":"0.000000","cost_cny":"0.000000",'
                    '"currency":"CNY","posting_source":"direct"}',
                    row["issued_at"],
                    7,
                    cache_tokens,
                    row["virtual_key_id"],
                    row["project_id"],
                    row["environment_id"],
                    row["issued_at"],
                    final_attempt_id,
                ),
            )
        connection.commit()
        connection.close()
        return {
            "choices": [
                {"message": {"role": "assistant", "content": ASSISTANT_CANARY}}
            ]
        }

    return call_gateway


def _revoke_chat_permission(database_url: str, tenant_id: str) -> None:
    """对目标租户 Viewer 执行一次合法实时撤权并同步矩阵 revision/hash。"""

    connection = connect_database(database_url)
    role = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code='viewer'",
        (tenant_id,),
    ).fetchone()
    permission = connection.execute(
        "SELECT permission_id FROM permissions WHERE tenant_id=? AND code='chat.use'",
        (tenant_id,),
    ).fetchone()
    assert role is not None and permission is not None
    connection.execute(
        "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
        (tenant_id, role["role_id"], permission["permission_id"]),
    )
    remaining = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.tenant_id=? AND relation.role_id=?
            """,
            (tenant_id, role["role_id"]),
        ).fetchall()
    }
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE tenant_id=? AND role_id=?
        """,
        (
            int(role["permission_revision"]) + 1,
            permission_matrix_sha256(remaining),
            "2026-08-02T01:10:00Z",
            tenant_id,
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


def _insert_default_tenant_viewer(database_url: str, tenant_id: str) -> None:
    """在默认租户创建用于事务内撤权竞态的 Viewer，不复用管理员不可变矩阵。

    参数：隔离数据库 URL 与可信默认租户 ID。返回：无。用户只绑定既有 Viewer 角色，
    不创建额外权限关系；测试密码仅在当前进程登录使用，不写入断言或验收输出。
    """

    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'viewer','active',?,?)
        """,
        (
            "user_prd066_default_viewer",
            tenant_id,
            "default-viewer-prd066@example.com",
            "PRD066 Default Viewer",
            hash_password(VIEWER_PASSWORD),
            "2026-08-02T01:00:00Z",
            "2026-08-02T01:00:00Z",
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_enterprise_chat_success_failure_idempotency_and_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主/异常：成功、失败、重复、清空、校验和正文零落库均可验收。"""

    database_url, _, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    captured_events = []

    def capture_event(config, event, *, request_id: str, trace_id: str) -> None:
        """捕获安全事件信封；测试不启动任何外部 Webhook 网络。"""

        captured_events.append(event)

    monkeypatch.setattr(chat_routes, "dispatch_chat_event_safely", capture_event)
    monkeypatch.setattr(
        chat_service,
        "_call_gateway",
        _successful_gateway(database_url),
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="req_prd066_login",
        )
        options = await client.get(
            "/chat/options", headers=_headers(token, "req_prd066_options")
        )
        invalid = await client.post(
            "/chat/sessions",
            json={"tenant_id": "tenant_forged"},
            headers=_headers(token, "req_prd066_invalid"),
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(token, "req_prd066_create"),
        )
        session_id = created.json()["session"]["chat_session_id"]
        first_id = "11111111-1111-4111-8111-111111111111"
        succeeded = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": first_id,
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, "req_prd066_success"),
        )

        async def fail_gateway(*args, **kwargs):
            """模拟 Gateway 可追踪不可用，不记录传入正文或内部 Token。"""

            # 替身只抛固定目录错误，禁止把正文参数或底层网络详情带入失败证据。
            raise ApiError("chat.gateway_unavailable")

        monkeypatch.setattr(chat_service, "_call_gateway", fail_gateway)
        second_id = "22222222-2222-4222-8222-222222222222"
        failed = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": second_id,
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, "req_prd066_failed"),
        )
        duplicate = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": second_id,
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, "req_prd066_duplicate"),
        )
        cleared = await client.delete(
            f"/chat/sessions/{session_id}",
            headers=_headers(token, "req_prd066_clear"),
        )
        after_clear = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "33333333-3333-4333-8333-333333333333",
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, "req_prd066_after_clear"),
        )

    assert options.status_code == 200
    assert any(item["availability"] == "ready" for item in options.json()["projects"])
    assert invalid.status_code == 422 and invalid.json()["code"] == "chat.request_invalid"
    assert created.status_code == 201
    assert succeeded.status_code == 200, succeeded.text
    assert succeeded.json()["assistant"]["content"] == ASSISTANT_CANARY
    assert succeeded.json()["execution"] == {
        "request_id": "req_prd066_success",
        "trace_id": "trc_prd066_success",
        "input_tokens": 4,
        "output_tokens": 3,
        "total_tokens": 7,
        "cache_tokens": 0,
        "cost_cny": "0.000000",
        "policy": {"decision": "allow", "reason_code": "policy.default_allow"},
        "fallback_triggered": False,
        "cache_status": "not_configured",
    }
    assert failed.status_code == 503 and failed.json()["code"] == "chat.gateway_unavailable"
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "chat.message_duplicate"
    assert cleared.status_code == 204
    assert after_clear.status_code == 409 and after_clear.json()["code"] == "chat.session_cleared"
    assert [event.payload["status"] for event in captured_events] == ["succeeded", "failed"]
    assert all(PROMPT_CANARY not in str(event.to_payload()) for event in captured_events)
    assert all(ASSISTANT_CANARY not in str(event.to_payload()) for event in captured_events)

    connection = connect_database(database_url)
    rows = [
        tuple(row)
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
    ]
    states = connection.execute(
        "SELECT status,error_code FROM chat_messages ORDER BY message_sequence"
    ).fetchall()
    connection.close()
    assert PROMPT_CANARY not in str(rows)
    assert ASSISTANT_CANARY not in str(rows)
    assert [tuple(row) for row in states] == [
        ("succeeded", None),
        ("failed", "chat.gateway_unavailable"),
    ]


@pytest.mark.anyio
async def test_enterprise_chat_failed_event_dispatch_is_non_blocking_and_terminal_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """失败事件：慢 Webhook 在后台线程执行，并发请求和已提交错误终态不被阻塞。"""

    database_url, tenant_id, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    dispatch_started = threading.Event()
    dispatch_release = threading.Event()
    dispatch_completed = threading.Event()
    captured_events = []

    def blocking_dispatch(config, event, *, request_id: str, trace_id: str) -> None:
        """模拟慢 Endpoint；只捕获脱敏事件，并由测试显式释放后台线程。"""

        captured_events.append(event)
        dispatch_started.set()
        # 三秒仅是回归保护上限；正常实现由测试立即释放，不引入固定等待。
        dispatch_release.wait(timeout=3.0)
        dispatch_completed.set()

    async def fail_gateway(*args, **kwargs):
        """模拟目录化 Gateway 失败，不读取或保存正文及内部 grant。"""

        # 只抛稳定目录码，让测试聚焦终态与后台调度，不能构造含 URL 或正文的自由异常。
        raise ApiError("chat.gateway_unavailable")

    monkeypatch.setattr(chat_routes, "dispatch_chat_event_safely", blocking_dispatch)
    monkeypatch.setattr(chat_service, "_call_gateway", fail_gateway)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="req_prd066_background_login",
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(token, "req_prd066_background_create"),
        )
        assert created.status_code == 201
        session_id = created.json()["session"]["chat_session_id"]
        failed_task = asyncio.create_task(
            client.post(
                f"/chat/sessions/{session_id}/messages",
                json={
                    "client_message_id": "99999999-9999-4999-8999-999999999999",
                    "history": [{"role": "user", "content": PROMPT_CANARY}],
                },
                headers=_headers(token, "req_prd066_background_failed"),
            )
        )
        # threading.Event.wait 放入辅助线程，测试自身也不能用同步等待堵塞事件循环。
        assert await asyncio.to_thread(dispatch_started.wait, 2.0)
        assert not dispatch_completed.is_set()
        # 后台派发尚未释放时，同一 ASGI 事件循环仍须处理独立请求；同步派发回归会卡住这里。
        health = await asyncio.wait_for(client.get("/health"), timeout=1.0)
        assert health.status_code == 200

        connection = connect_database(database_url)
        terminal_row = connection.execute(
            """
            SELECT * FROM chat_messages
            WHERE tenant_id=? AND gateway_request_id='req_prd066_background_failed'
            """,
            (tenant_id,),
        ).fetchone()
        connection.close()
        assert terminal_row is not None
        persisted = str(tuple(terminal_row))
        assert (terminal_row["status"], terminal_row["error_code"]) == (
            "failed",
            "chat.gateway_unavailable",
        )
        assert PROMPT_CANARY not in persisted

        dispatch_release.set()
        failed = await asyncio.wait_for(failed_task, timeout=2.0)

    assert failed.status_code == 503
    assert failed.json()["code"] == "chat.gateway_unavailable"
    assert failed.json()["request_id"] == "req_prd066_background_failed"
    assert failed.headers["Cache-Control"] == "no-store"
    assert dispatch_completed.is_set()
    assert len(captured_events) == 1
    assert captured_events[0].payload["status"] == "failed"
    assert PROMPT_CANARY not in str(captured_events[0].to_payload())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cache_scenario", "expected_status", "expected_cache_tokens", "expected_fallback"),
    [
        pytest.param("hit", "hit", 2, False, id="final-attempt-hit"),
        pytest.param("miss", "miss", 0, False, id="final-attempt-miss"),
        pytest.param(
            "not_configured",
            "not_configured",
            0,
            False,
            id="final-attempt-not-configured",
        ),
        pytest.param(
            "fallback_primary_selection",
            "not_configured",
            0,
            True,
            id="failed-primary-selection-does-not-pollute-fallback",
        ),
    ],
)
async def test_enterprise_chat_cache_status_uses_only_final_success_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cache_scenario: str,
    expected_status: str,
    expected_cache_tokens: int,
    expected_fallback: bool,
) -> None:
    """缓存摘要：真实 Token 和最终成功 attempt 决定唯一公开状态。

    ``hit`` 覆盖真实 cache token；``miss`` 覆盖最终成功 attempt 的有效 Selection；
    ``not_configured`` 覆盖最终成功 attempt 无 Selection。Fallback 场景额外保留主 attempt
    的失败 Selection，证明旧选择不能污染无 Selection 的最终备用结果。
    """

    database_url, _, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    captured_events = []

    def capture_event(config, event, *, request_id: str, trace_id: str) -> None:
        """捕获已脱敏终态事件，不执行任何外部 Webhook 调用。"""

        captured_events.append(event)

    monkeypatch.setattr(chat_routes, "dispatch_chat_event_safely", capture_event)
    monkeypatch.setattr(
        chat_service,
        "_call_gateway",
        _successful_gateway(database_url, cache_scenario=cache_scenario),
    )
    request_id = f"req_prd066_cache_{cache_scenario}"
    client_message_ids = {
        "hit": "55555555-5555-4555-8555-555555555555",
        "miss": "66666666-6666-4666-8666-666666666666",
        "not_configured": "77777777-7777-4777-8777-777777777777",
        "fallback_primary_selection": "88888888-8888-4888-8888-888888888888",
    }
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id=f"{request_id}_login",
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(token, f"{request_id}_create"),
        )
        assert created.status_code == 201
        session_id = created.json()["session"]["chat_session_id"]
        response = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": client_message_ids[cache_scenario],
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, request_id),
        )

    assert response.status_code == 200
    execution = response.json()["execution"]
    assert execution["cache_status"] == expected_status
    assert execution["cache_tokens"] == expected_cache_tokens
    assert execution["fallback_triggered"] is expected_fallback
    assert response.headers["Cache-Control"] == "no-store"
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.payload["status"] == "succeeded"
    assert event.payload["cache_status"] == expected_status
    assert event.payload["cache_tokens"] == expected_cache_tokens
    assert event.payload["fallback_triggered"] is expected_fallback

    connection = connect_database(database_url)
    message = connection.execute(
        """
        SELECT status,cache_tokens,cache_status,fallback_triggered,error_code
        FROM chat_messages WHERE gateway_request_id=?
        """,
        (request_id,),
    ).fetchone()
    request_log = connection.execute(
        """
        SELECT fallback_triggered,routing_attempt_count,final_routing_attempt_id
        FROM request_logs WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    attempts = connection.execute(
        """
        SELECT routing_attempt_id,attempt_sequence,attempt_role,status
        FROM routing_attempts WHERE request_id=? ORDER BY attempt_sequence
        """,
        (request_id,),
    ).fetchall()
    selections = connection.execute(
        """
        SELECT routing_attempt_id,attempt_sequence,outcome_status
        FROM cache_affinity_selections WHERE request_id=? ORDER BY attempt_sequence
        """,
        (request_id,),
    ).fetchall()
    usage = connection.execute(
        """
        SELECT cache_tokens,routing_attempt_id FROM usage_ledger WHERE request_id=?
        """,
        (request_id,),
    ).fetchone()
    persisted = "\n".join(
        str(value)
        for table in (
            "routing_attempts",
            "cache_affinity_selections",
            "request_logs",
            "usage_ledger",
            "chat_messages",
        )
        for row in connection.execute(
            f"SELECT * FROM {table} WHERE request_id=?"
            if table not in {"chat_messages"}
            else "SELECT * FROM chat_messages WHERE gateway_request_id=?",
            (request_id,),
        ).fetchall()
        for value in tuple(row)
    )
    connection.close()

    assert tuple(message) == (
        "succeeded",
        expected_cache_tokens,
        expected_status,
        int(expected_fallback),
        None,
    )
    assert request_log["fallback_triggered"] == int(expected_fallback)
    assert request_log["routing_attempt_count"] == (2 if expected_fallback else 1)
    assert request_log["final_routing_attempt_id"] == attempts[-1]["routing_attempt_id"]
    assert tuple(usage) == (expected_cache_tokens, attempts[-1]["routing_attempt_id"])
    assert attempts[-1]["status"] == "succeeded"
    if expected_fallback:
        # 主 attempt 的失败 Selection 仍可审计，但最终 fallback attempt 明确没有 Selection。
        assert [tuple(item) for item in attempts] == [
            (attempts[0]["routing_attempt_id"], 1, "primary", "failed"),
            (attempts[1]["routing_attempt_id"], 2, "fallback", "succeeded"),
        ]
        assert [tuple(item) for item in selections] == [
            (attempts[0]["routing_attempt_id"], 1, "failed")
        ]
        assert all(
            item["routing_attempt_id"] != request_log["final_routing_attempt_id"]
            for item in selections
        )
    elif cache_scenario == "not_configured":
        assert selections == []
    else:
        assert [tuple(item) for item in selections] == [
            (attempts[0]["routing_attempt_id"], 1, "succeeded")
        ]
    assert PROMPT_CANARY not in persisted
    assert ASSISTANT_CANARY not in persisted
    assert PROMPT_CANARY not in str(event.to_payload())
    assert ASSISTANT_CANARY not in str(event.to_payload())


@pytest.mark.anyio
async def test_enterprise_chat_missing_usage_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """账务异常：Gateway 成功快照缺正式 Usage 时不得伪造 Token、成本或成功事件。"""

    database_url, _, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    captured_events = []

    def capture_event(config, event, *, request_id: str, trace_id: str) -> None:
        """捕获失败事件，确认其中只有固定元数据且没有正文。"""

        captured_events.append(event)

    monkeypatch.setattr(chat_routes, "dispatch_chat_event_safely", capture_event)
    monkeypatch.setattr(
        chat_service,
        "_call_gateway",
        _successful_gateway(database_url, write_usage=False),
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="req_prd066_usage_login",
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(token, "req_prd066_usage_create"),
        )
        session_id = created.json()["session"]["chat_session_id"]
        response = await client.post(
            f"/chat/sessions/{session_id}/messages",
            json={
                "client_message_id": "44444444-4444-4444-8444-444444444444",
                "history": [{"role": "user", "content": PROMPT_CANARY}],
            },
            headers=_headers(token, "req_prd066_usage_missing"),
        )

    assert response.status_code == 502
    assert response.json()["code"] == "chat.response_invalid"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(captured_events) == 1
    event = captured_events[0]
    assert event.payload["status"] == "failed"
    assert event.payload["error_code"] == "chat.response_invalid"
    assert event.payload["input_tokens"] is None
    assert PROMPT_CANARY not in str(event.to_payload())

    connection = connect_database(database_url)
    message = connection.execute(
        """
        SELECT status,input_tokens,output_tokens,total_tokens,cost_cny_micros,error_code
        FROM chat_messages WHERE gateway_request_id='req_prd066_usage_missing'
        """
    ).fetchone()
    usage_count = connection.execute(
        "SELECT COUNT(*) FROM usage_ledger WHERE request_id='req_prd066_usage_missing'"
    ).fetchone()[0]
    connection.close()
    assert tuple(message) == ("failed", None, None, None, None, "chat.response_invalid")
    assert usage_count == 0


@pytest.mark.anyio
async def test_enterprise_chat_tenant_owner_and_realtime_permission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限/租户：另一租户不能访问 Session，实时撤权立即阻断全部 Chat API。"""

    database_url, _, project_id, environment_id, alias_id, second_slug = _prepare_database(
        monkeypatch, tmp_path
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="req_prd066_owner_admin_login",
        )
        viewer_token = await _login(
            client,
            tenant_slug=second_slug,
            email="viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="req_prd066_owner_viewer_login",
        )
        navigation_before = await client.get(
            "/console/navigation",
            headers=_headers(viewer_token, "req_prd066_navigation_before"),
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(admin_token, "req_prd066_owner_create"),
        )
        session_id = created.json()["session"]["chat_session_id"]
        cross_tenant = await client.delete(
            f"/chat/sessions/{session_id}",
            headers=_headers(viewer_token, "req_prd066_cross_tenant"),
        )
        anonymous = await client.get(
            "/chat/options?tenant_id=forged",
            headers={"X-Request-ID": "req_prd066_anonymous"},
        )
        connection = connect_database(database_url)
        second_tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug=?", (second_slug,)
        ).fetchone()[0]
        connection.close()
        _revoke_chat_permission(database_url, str(second_tenant_id))
        revoked = await client.get(
            "/chat/options?tenant_id=forged",
            headers=_headers(viewer_token, "req_prd066_revoked"),
        )
        navigation_after = await client.get(
            "/console/navigation",
            headers=_headers(viewer_token, "req_prd066_navigation_after"),
        )

    assert created.status_code == 201
    assert navigation_before.status_code == 200
    before_ids = {item["id"] for item in navigation_before.json()["navigation_items"]}
    assert "enterprise-chat" in before_ids
    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["code"] == "chat.session_not_found"
    assert anonymous.status_code == 401 and anonymous.json()["code"] == "auth.token_missing"
    # 权限依赖先于 query 检查执行，撤权用户不能利用畸形参数探测资源或参数合同。
    assert revoked.status_code == 403 and revoked.json()["code"] == "auth.permission_denied"
    assert navigation_after.status_code == 200
    after_ids = {item["id"] for item in navigation_after.json()["navigation_items"]}
    assert "enterprise-chat" not in after_ids


@pytest.mark.anyio
async def test_enterprise_chat_transactions_recheck_realtime_permission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限竞态：路由快照通过后撤权，建会话和消息准备事务仍统一 403 且零写入。"""

    database_url, tenant_id, project_id, environment_id, alias_id, _ = _prepare_database(
        monkeypatch, tmp_path
    )
    _insert_default_tenant_viewer(database_url, tenant_id)
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        token = await _login(
            client,
            tenant_slug="default",
            email="default-viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="req_prd066_transaction_login",
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(token, "req_prd066_transaction_create_before_revoke"),
        )
        assert created.status_code == 201
        chat_session_id = created.json()["session"]["chat_session_id"]

    # 显式保留撤权前的 AuthContext，模拟路由依赖已经通过、业务事务尚未开始的窄竞态。
    # Token 只在内存传给认证 helper，任何断言和数据库证据均不得包含其明文。
    stale_context = authenticate_bearer(app.state.config, f"Bearer {token}")
    connection = connect_database(database_url)
    counts_before = tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
    )
    owner_before = connection.execute(
        """
        SELECT tenant_id,owner_user_id,owner_auth_session_id,status
        FROM chat_sessions WHERE tenant_id=? AND chat_session_id=?
        """,
        (tenant_id, chat_session_id),
    ).fetchone()
    connection.close()
    _revoke_chat_permission(database_url, tenant_id)

    session_payload = CreateChatSessionRequest(
        project_id=project_id,
        environment_id=environment_id,
        model_alias_id=alias_id,
    )
    with pytest.raises(ApiError) as create_error:
        chat_service.create_chat_session(
            app.state.config,
            stale_context,
            session_payload,
            request_id="req_prd066_transaction_create_after_revoke",
            trace_id="trc_prd066_transaction_create_after_revoke",
        )
    with pytest.raises(ApiError) as message_error:
        chat_service._prepare_message(
            app.state.config,
            stale_context,
            chat_session_id,
            client_message_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            history=[{"role": "user", "content": PROMPT_CANARY}],
            prompt_template_version_id=None,
            template_variables=None,
            request_id="req_prd066_transaction_send_after_revoke",
            trace_id="trc_prd066_transaction_send_after_revoke",
        )

    connection = connect_database(database_url)
    counts_after = tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("chat_sessions", "chat_messages", "chat_execution_grants")
    )
    owner_after = connection.execute(
        """
        SELECT tenant_id,owner_user_id,owner_auth_session_id,status
        FROM chat_sessions WHERE tenant_id=? AND chat_session_id=?
        """,
        (tenant_id, chat_session_id),
    ).fetchone()
    connection.close()

    assert (create_error.value.http_status, create_error.value.code) == (
        403,
        "auth.permission_denied",
    )
    assert (message_error.value.http_status, message_error.value.code) == (
        403,
        "auth.permission_denied",
    )
    assert counts_after == counts_before
    assert tuple(owner_before) == tuple(owner_after) == (
        tenant_id,
        stale_context.user.user_id,
        stale_context.session.session_id,
        "active",
    )


@pytest.mark.anyio
async def test_enterprise_chat_auth_precedes_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """权限顺序：畸形、重复键和超大 JSON 不得先于匿名或实时撤权结论返回。"""

    database_url, _, project_id, environment_id, alias_id, second_slug = _prepare_database(
        monkeypatch, tmp_path
    )
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
            request_id="req_prd066_auth_order_admin_login",
        )
        viewer_token = await _login(
            client,
            tenant_slug=second_slug,
            email="viewer-prd066@example.com",
            password=VIEWER_PASSWORD,
            request_id="req_prd066_auth_order_viewer_login",
        )
        created = await client.post(
            "/chat/sessions",
            json={
                "project_id": project_id,
                "environment_id": environment_id,
                "model_alias_id": alias_id,
            },
            headers=_headers(admin_token, "req_prd066_auth_order_create"),
        )
        assert created.status_code == 201
        session_id = created.json()["session"]["chat_session_id"]
        malformed_body = b"{"
        duplicate_body = (
            "{"
            f'"project_id":"{project_id}",'
            f'"environment_id":"{environment_id}",'
            f'"model_alias_id":"{alias_id}",'
            f'"project_id":"{project_id}"'
            "}"
        ).encode()
        # 比 wire 上限多一个字节即可证明声明长度会在 JSON 分配和解析前失败关闭。
        oversized_body = b"{" + (b"x" * chat_routes.MAX_CHAT_WIRE_BODY_BYTES)

        # 有权用户已经通过后端 RBAC，随后才应得到不含字段详情的严格 422。
        authorized_create = await client.post(
            "/chat/sessions",
            content=malformed_body,
            headers={
                **_headers(admin_token, "req_prd066_auth_order_authorized_create"),
                "Content-Type": "application/json",
            },
        )
        authorized_send = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=malformed_body,
            headers={
                **_headers(admin_token, "req_prd066_auth_order_authorized_send"),
                "Content-Type": "application/json",
            },
        )
        authorized_duplicate = await client.post(
            "/chat/sessions",
            content=duplicate_body,
            headers={
                **_headers(admin_token, "req_prd066_auth_order_duplicate"),
                "Content-Type": "application/json",
            },
        )
        authorized_oversized = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=oversized_body,
            headers={
                **_headers(admin_token, "req_prd066_auth_order_oversized"),
                "Content-Type": "application/json",
            },
        )
        # 匿名调用必须在解析同一畸形正文前得到 401，不能借 422 探测请求结构。
        anonymous_create = await client.post(
            "/chat/sessions",
            content=malformed_body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "req_prd066_auth_order_anonymous_create",
            },
        )
        anonymous_send = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=malformed_body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "req_prd066_auth_order_anonymous_send",
            },
        )
        anonymous_oversized = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=oversized_body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "req_prd066_auth_order_anonymous_oversized",
            },
        )
        connection = connect_database(database_url)
        second_tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug=?", (second_slug,)
        ).fetchone()[0]
        connection.close()
        _revoke_chat_permission(database_url, str(second_tenant_id))
        # 登录 Token 仍有效但权限已撤销时，create/send 均须实时 403 后停止处理正文。
        revoked_create = await client.post(
            "/chat/sessions",
            content=malformed_body,
            headers={
                **_headers(viewer_token, "req_prd066_auth_order_revoked_create"),
                "Content-Type": "application/json",
            },
        )
        revoked_send = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=malformed_body,
            headers={
                **_headers(viewer_token, "req_prd066_auth_order_revoked_send"),
                "Content-Type": "application/json",
            },
        )
        revoked_oversized = await client.post(
            f"/chat/sessions/{session_id}/messages",
            content=oversized_body,
            headers={
                **_headers(viewer_token, "req_prd066_auth_order_revoked_oversized"),
                "Content-Type": "application/json",
            },
        )

    assert {
        (authorized_create.status_code, authorized_create.json()["code"]),
        (authorized_send.status_code, authorized_send.json()["code"]),
        (authorized_duplicate.status_code, authorized_duplicate.json()["code"]),
        (authorized_oversized.status_code, authorized_oversized.json()["code"]),
    } == {(422, "chat.request_invalid")}
    assert {
        (anonymous_create.status_code, anonymous_create.json()["code"]),
        (anonymous_send.status_code, anonymous_send.json()["code"]),
        (anonymous_oversized.status_code, anonymous_oversized.json()["code"]),
    } == {(401, "auth.token_missing")}
    assert {
        (revoked_create.status_code, revoked_create.json()["code"]),
        (revoked_send.status_code, revoked_send.json()["code"]),
        (revoked_oversized.status_code, revoked_oversized.json()["code"]),
    } == {(403, "auth.permission_denied")}
