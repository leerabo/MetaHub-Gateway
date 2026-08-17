"""验证产品激活接口的租户授权、聚合口径、事件快照、租约恢复和隐私边界。"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.product_analytics.outbox import (
    claim_next_product_event,
    dispatch_claimed_product_event,
    recover_product_event_source_request_id,
)
from metahub_control.product_analytics.routes import router as product_analytics_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import (
    encrypt_secret,
    hash_password,
    secret_fingerprint,
    secret_prefix,
)
from metahub_shared.rbac import permission_matrix_sha256
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 三个测试身份和签名密钥只服务于隔离数据库，覆盖 admin、ops 与第二租户边界。
ADMIN_PASSWORD = "Prd085AdminTestOnly!2026"
OPS_PASSWORD = "Prd085OpsTestOnly!2026"
SECOND_PASSWORD = "Prd085SecondTestOnly!2026"
AUTH_SECRET = "prd085-auth-secret-" + "a" * 48


def _url(path: Path) -> str:
    """把测试临时 SQLite 文件转换为服务配置使用的数据库地址。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def empty_0078(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """构建一次空库 0078 模板，后续用例复制后再写各自租户事实。"""

    root = tmp_path_factory.mktemp("prd085-control-template")
    database = root / "empty.sqlite3"
    assert apply_migrations(_url(database)).applied[-1] == "0078"
    return database


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_0078: Path,
) -> str:
    """复制隔离数据库并建立默认租户 ops 用户与第二租户管理员边界。"""

    database_url = _url(tmp_path / "prd085-control.sqlite3")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", AUTH_SECRET + "-enc")
    shutil.copy2(empty_0078, tmp_path / "prd085-control.sqlite3")
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd085_ops',?,'ops@prd085.local','PRD085 Ops',?,
                 'ops_admin','active','2026-08-17T00:00:00Z','2026-08-17T00:00:00Z')
        """,
        (seed.tenant_id, hash_password(OPS_PASSWORD)),
    )
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id,slug,name,service_mode,status,created_at,updated_at
        ) VALUES('tenant_prd085_second','prd085-second','PRD085 Second','public_service',
                 'active','2026-08-17T00:00:00Z','2026-08-17T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd085_second','tenant_prd085_second','admin@prd085-second.local',
                 'PRD085 Second Admin',?,'admin','active',
                 '2026-08-17T00:00:00Z','2026-08-17T00:00:00Z')
        """,
        (hash_password(SECOND_PASSWORD),),
    )
    connection.execute(
        """
        INSERT INTO seed_runs(
          seed_run_id,tenant_id,seed_name,checksum,status,created_at,updated_at
        ) VALUES('seed_prd085_second','tenant_prd085_second','prd085-second',
                 'prd085-second-checksum','applied',
                 '2026-08-17T00:00:01Z','2026-08-17T00:00:01Z')
        """
    )
    connection.commit()
    connection.close()
    return database_url


def _app():
    """创建只挂载认证和产品分析路由的测试应用，并强制私有缓存响应头。"""

    app = create_service_app("control-plane-api", "PRD085 Control Test")

    @app.middleware("http")
    async def private_boundary(request, call_next):
        """为漏斗成功与错误响应统一添加私有不缓存边界。"""

        response = await call_next(request)
        if request.url.path == "/product-analytics/funnel":
            response.headers["Cache-Control"] = "no-store, private"
        return response

    app.include_router(auth_router)
    app.include_router(product_analytics_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    tenant_slug: str,
    email: str,
    password: str,
) -> str:
    """通过真实认证路由登录指定租户，并返回后续权限测试使用的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌仅在进程内传给测试客户端，不写日志或验收输出。
    return response.json()["access_token"]


def _identity(connection):
    """读取默认租户管理员与 Provider 身份，保证测试来源具有一致 owner。"""

    row = connection.execute(
        """
        SELECT tenant.tenant_id,user.user_id,user.email,user.role,provider.provider_id
        FROM tenants tenant
        JOIN users user ON user.tenant_id=tenant.tenant_id AND user.role='admin'
        JOIN providers provider ON provider.tenant_id=tenant.tenant_id
        WHERE tenant.slug='default' ORDER BY provider.provider_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return row


def _insert_provider_event(
    database_url: str,
    *,
    audit_id: str,
    created_at: str = "2026-08-17T01:00:00Z",
) -> str:
    """插入 Provider 创建审计并返回 trigger 派生的同租户 ProductEvent 身份。"""

    connection = connect_database(database_url)
    owner = _identity(connection)
    connection.execute(
        """
        INSERT INTO audit_logs(
          audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
          action,object_type,object_id,object_name,before_summary_json,
          after_summary_json,changed_fields_json,client_ip,user_agent,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,'provider.created','provider',?,NULL,
                 '{}','{}','[]',NULL,NULL,?,?,?)
        """,
        (
            audit_id,
            owner["tenant_id"],
            owner["user_id"],
            owner["email"],
            owner["role"],
            owner["provider_id"],
            f"req_{audit_id}",
            f"trc_{audit_id}",
            created_at,
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT product_event_id FROM product_events WHERE source_id=?",
        (audit_id,),
    ).fetchone()
    assert row is not None
    connection.close()
    return str(row[0])


def _insert_webhook_endpoint(
    database_url: str,
    *,
    endpoint_id: str,
    created_at: str,
) -> None:
    """创建订阅产品事件的加密 Endpoint，用于验证首次 claim 冻结目标集合。"""

    connection = connect_database(database_url)
    owner = _identity(connection)
    fixture_value = "fixture-" + "x" * 40
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
          max_attempts,created_by,updated_by,created_at,updated_at,payload_format,
          security_event_rate_limit_per_minute
        ) VALUES(?,?,'PRD085 snapshot','https://example.invalid/prd085',
                 '["product_event.recorded"]',?,?,?,'active',1000,1,?,?,?,?,'json',60)
        """,
        (
            endpoint_id,
            owner["tenant_id"],
            encrypt_secret(fixture_value),
            secret_prefix(fixture_value),
            secret_fingerprint(fixture_value),
            owner["user_id"],
            owner["user_id"],
            created_at,
            created_at,
        ),
    )
    connection.commit()
    connection.close()


def _revoke_ops_product_read(database_url: str) -> None:
    """直接撤销当前租户 ops 读取权限并重算矩阵哈希，验证请求级动态复核。"""

    connection = connect_database(database_url)
    role = connection.execute(
        "SELECT role_id,permission_revision FROM roles WHERE code='ops_admin' LIMIT 1"
    ).fetchone()
    permission = connection.execute(
        "SELECT permission_id FROM permissions WHERE code='product_analytics.read' LIMIT 1"
    ).fetchone()
    assert role is not None and permission is not None
    connection.execute(
        "DELETE FROM role_permissions WHERE role_id=? AND permission_id=?",
        (role["role_id"], permission["permission_id"]),
    )
    remaining = {
        row[0]
        for row in connection.execute(
            """
            SELECT permission.code FROM role_permissions relation
            JOIN permissions permission
              ON permission.tenant_id=relation.tenant_id
             AND permission.permission_id=relation.permission_id
            WHERE relation.role_id=?
            """,
            (role["role_id"],),
        )
    }
    connection.execute(
        """
        UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
        WHERE role_id=?
        """,
        (
            int(role["permission_revision"]) + 1,
            permission_matrix_sha256(remaining),
            "2026-08-17T02:00:00Z",
            role["role_id"],
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.anyio
async def test_funnel_api_exact_query_permissions_isolation_and_dynamic_revoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_0078: Path,
) -> None:
    """验证 exact query、双租户隔离、admin/ops 授权、动态撤权和统一错误链路。"""

    database_url = _prepare(monkeypatch, tmp_path, empty_0078)
    _insert_provider_event(database_url, audit_id="aud_prd085_api")
    _insert_provider_event(
        database_url,
        audit_id="aud_prd085_api_fractional",
        created_at="2026-08-17T01:00:00.500Z",
    )
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await _login(
            client,
            tenant_slug="default",
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        ops = await _login(
            client,
            tenant_slug="default",
            email="ops@prd085.local",
            password=OPS_PASSWORD,
        )
        second = await _login(
            client,
            tenant_slug="prd085-second",
            email="admin@prd085-second.local",
            password=SECOND_PASSWORD,
        )
        admin_response = await client.get(
            "/product-analytics/funnel",
            headers={"Authorization": f"Bearer {admin}", "X-Request-ID": "req_prd085_api"},
        )
        second_response = await client.get(
            "/product-analytics/funnel",
            headers={"Authorization": f"Bearer {second}"},
        )
        invalid_query = await client.get(
            "/product-analytics/funnel?tenant_id=tenant_prd085_second",
            headers={"Authorization": f"Bearer {admin}"},
        )
        anonymous_query = await client.get("/product-analytics/funnel?unknown=1")
        assert (
            await client.get(
                "/product-analytics/funnel",
                headers={"Authorization": f"Bearer {ops}"},
            )
        ).status_code == 200
        _revoke_ops_product_read(database_url)
        revoked = await client.get(
            "/product-analytics/funnel",
            headers={"Authorization": f"Bearer {ops}"},
        )

    assert admin_response.status_code == 200, admin_response.text
    assert admin_response.headers["Cache-Control"] == "no-store, private"
    payload = admin_response.json()
    assert set(payload) == {
        "tenant_id",
        "window",
        "cohort_tenant_count",
        "stages",
        "summary",
        "generated_at",
        "request_id",
        "trace_id",
    }
    assert payload["request_id"] == admin_response.headers["X-Request-ID"]
    assert payload["trace_id"] == admin_response.headers["X-Trace-ID"]
    assert payload["window"] == {"kind": "all_time", "timezone": "UTC"}
    assert [stage["event_type"] for stage in payload["stages"]] == [
        "tenant.registered",
        "provider.created",
        "virtual_key.created",
        "gateway.first_call_succeeded",
        "budget.configured",
        "teammate.invited",
        "request_logs.viewed",
    ]
    assert [stage["event_count"] for stage in payload["stages"]] == [1, 2, 0, 0, 0, 0, 0]
    assert payload["stages"][1]["first_occurred_at"] == "2026-08-17T01:00:00Z"
    assert payload["stages"][1]["last_occurred_at"] == "2026-08-17T01:00:00.500000Z"
    assert payload["summary"] == {
        "completed_stage_count": 2,
        "cumulative_stage_count": 2,
        "total_stage_count": 7,
        "completion_bps": 2857,
        "activated": False,
    }
    second_payload = second_response.json()
    assert second_payload["tenant_id"] == "tenant_prd085_second"
    assert [stage["event_count"] for stage in second_payload["stages"]] == [1, 0, 0, 0, 0, 0, 0]
    assert invalid_query.status_code == 422
    assert invalid_query.json()["code"] == "product_analytics.request_invalid"
    assert anonymous_query.status_code == 401
    assert anonymous_query.json()["code"] == "auth.token_missing"
    assert revoked.status_code == 403
    assert revoked.json()["code"] == "auth.permission_denied"
    for response in (invalid_query, anonymous_query, revoked):
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_outbox_snapshot_retry_expiry_stale_cas_and_source_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_0078: Path,
) -> None:
    """验证订阅快照冻结、退避重试、过期接管、陈旧 CAS 和原请求恢复。"""

    database_url = _prepare(monkeypatch, tmp_path, empty_0078)
    _insert_webhook_endpoint(
        database_url,
        endpoint_id="wh_11111111111111111111111111111111",
        created_at="2026-08-17T00:30:00Z",
    )
    product_event_id = _insert_provider_event(
        database_url,
        audit_id="aud_prd085_outbox",
    )
    config = SimpleNamespace(database_url=database_url)
    start = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
    first = claim_next_product_event(config, worker_id="prd085-worker-a", now=start)
    assert first is not None
    assert first.product_event_id == product_event_id
    assert first.attempt_count == 1
    assert first.event.event_id.startswith("evt_pev_")
    connection = connect_database(database_url)
    first_delivery = connection.execute(
        """
        SELECT * FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        """,
        (first.tenant_id, first.event.event_id),
    ).fetchone()
    assert first_delivery is not None
    assert webhook_service._validated_pending_delivery_body(
        connection,
        first_delivery,
    )
    connection.close()

    def fail_dispatch(*_args, **_kwargs):
        """模拟固定 Webhook 不可用错误，驱动 source 进入可重试状态。"""

        # 测试只抛固定错误码，禁止把 Endpoint 或密钥材料写入失败证据。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    assert dispatch_claimed_product_event(
        config,
        first,
        now=start + timedelta(seconds=1),
    )
    connection = connect_database(database_url)
    retry = connection.execute(
        """
        SELECT status,attempt_count,generation,subscription_snapshot_json,
               snapshot_frozen_at,next_attempt_at,last_error_code
        FROM product_event_outbox WHERE product_event_id=?
        """,
        (product_event_id,),
    ).fetchone()
    snapshot = json.loads(retry["subscription_snapshot_json"])
    assert retry["status"] == "pending"
    assert retry["attempt_count"] == 1
    assert retry["generation"] == 2
    assert snapshot == [first_delivery["webhook_delivery_id"]]
    assert retry["snapshot_frozen_at"] == "2030-01-01T00:00:00Z"
    assert retry["next_attempt_at"] == "2030-01-01T00:00:06Z"
    assert retry["last_error_code"] == "webhook.delivery_unavailable"
    connection.close()
    _insert_webhook_endpoint(
        database_url,
        endpoint_id="wh_22222222222222222222222222222222",
        created_at="2030-01-01T00:00:02Z",
    )
    assert claim_next_product_event(
        config,
        worker_id="prd085-too-early",
        now=start + timedelta(seconds=5),
    ) is None

    second = claim_next_product_event(
        config,
        worker_id="prd085-worker-b",
        now=start + timedelta(seconds=6),
    )
    assert second is not None and second.attempt_count == 2
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_deliveries WHERE event_id=?",
        (second.event.event_id,),
    ).fetchone()[0] == 1
    connection.close()
    takeover = claim_next_product_event(
        config,
        worker_id="prd085-worker-c",
        now=second.lease_expires_at + timedelta(seconds=1),
    )
    assert takeover is not None
    assert takeover.attempt_count == second.attempt_count
    assert takeover.generation == second.generation + 1

    def successful_dispatch(*_args, **kwargs):
        """模拟成功投递并调用 source 租约守卫，验证接管后的完整 CAS。"""

        kwargs["source_lease_guard"]()
        return []

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", successful_dispatch)
    stale_time = second.lease_expires_at + timedelta(seconds=2)
    assert not dispatch_claimed_product_event(config, second, now=stale_time)
    assert dispatch_claimed_product_event(config, takeover, now=stale_time)
    connection = connect_database(database_url)
    final = connection.execute(
        """
        SELECT status,attempt_count,subscription_snapshot_json,last_error_code,delivered_at
        FROM product_event_outbox WHERE product_event_id=?
        """,
        (product_event_id,),
    ).fetchone()
    assert final["status"] == "sent"
    assert final["attempt_count"] == 2
    assert json.loads(final["subscription_snapshot_json"]) == snapshot
    assert final["last_error_code"] is None
    assert final["delivered_at"] == "2030-01-01T00:01:38Z"
    assert recover_product_event_source_request_id(
        connection,
        takeover.event.to_payload(),
    ) == "req_aud_prd085_outbox"
    tampered = takeover.event.to_payload()
    tampered["payload"] = {**tampered["payload"], "event_type": "teammate.invited"}
    with pytest.raises(ApiError):
        recover_product_event_source_request_id(connection, tampered)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_outbox_ten_failures_reach_fixed_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_0078: Path,
) -> None:
    """验证连续十次失败后 source、delivery 和死信进入固定终态且不再领取。"""

    database_url = _prepare(monkeypatch, tmp_path, empty_0078)
    _insert_webhook_endpoint(
        database_url,
        endpoint_id="wh_33333333333333333333333333333333",
        created_at="2026-08-17T00:30:00Z",
    )
    product_event_id = _insert_provider_event(
        database_url,
        audit_id="aud_prd085_terminal",
    )
    config = SimpleNamespace(database_url=database_url)

    def fail_dispatch(*_args, **_kwargs):
        """持续返回固定投递错误，验证十次上限和退避状态机。"""

        # 固定错误码保证终态证据可聚合，且不包含接收端响应正文。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    due = datetime(2031, 1, 1, 0, 0, 0, tzinfo=UTC)
    for expected_attempt in range(1, 11):
        claim = claim_next_product_event(
            config,
            worker_id="prd085-terminal-worker",
            now=due,
        )
        assert claim is not None and claim.attempt_count == expected_attempt
        assert dispatch_claimed_product_event(
            config,
            claim,
            now=due + timedelta(seconds=1),
        )
        connection = connect_database(database_url)
        row = connection.execute(
            """
            SELECT status,attempt_count,next_attempt_at,last_error_code
            FROM product_event_outbox WHERE product_event_id=?
            """,
            (product_event_id,),
        ).fetchone()
        connection.close()
        if expected_attempt < 10:
            assert row["status"] == "pending"
            due = datetime.fromisoformat(row["next_attempt_at"].replace("Z", "+00:00"))
        else:
            assert tuple(row) == (
                "failed",
                10,
                None,
                "webhook.delivery_unavailable",
            )

    assert claim_next_product_event(
        config,
        worker_id="prd085-terminal-worker",
        now=due + timedelta(days=1),
    ) is None
    connection = connect_database(database_url)
    assert connection.execute(
        "SELECT status FROM webhook_deliveries LIMIT 1"
    ).fetchone()[0] == "failed"
    assert connection.execute(
        "SELECT COUNT(*) FROM webhook_dead_letter_events WHERE event_type='product_event.recorded'"
    ).fetchone()[0] == 1
    connection.close()


def test_no_sensitive_fields_enter_product_event_or_outbox_schema() -> None:
    """静态审计迁移与 outbox，确认凭据、正文、地址和敏感日志没有存储入口。"""

    source = Path(__file__).resolve().parents[3]
    migration_path = (
        source / "infra/migrations/0078_product_analytics_activation_funnel.sql"
    )
    migration = migration_path.read_text(encoding="utf-8")
    outbox = (
        source
        / "control-plane-api/src/metahub_control/product_analytics/outbox.py"
    ).read_text(encoding="utf-8")
    forbidden_columns = {
        "prompt_text",
        "response_text",
        "provider_key",
        "virtual_key_plaintext",
        "authorization_header",
        "endpoint_url",
    }
    lowered = migration.lower()
    assert all(column not in lowered for column in forbidden_columns)
    assert "logger." not in outbox
    assert "repr=False" in outbox
    assert json.dumps({"migration": len(migration), "outbox": len(outbox)})
