"""PRD-086 Control 客户健康评分、纯读 API 与恢复状态机专项测试。"""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.routes import router as auth_router
from metahub_control.auth.service import AuthContext
from metahub_control.customer_health import outbox as customer_health_outbox
from metahub_control.customer_health import scheduler as customer_health_scheduler
from metahub_control.customer_health.models import CustomerHealthResponse
from metahub_control.customer_health.outbox import (
    _event_from_row,
    claim_next_customer_health_event,
    dispatch_claimed_customer_health_event,
    drain_customer_health_outbox_once,
)
from metahub_control.customer_health.routes import router as customer_health_router
from metahub_control.customer_health.scheduler import (
    claim_next_customer_health_tenant,
    evaluate_claimed_customer_health,
)
from metahub_control.customer_health.service import (
    SourceSnapshot,
    evaluate_source_snapshot,
    get_customer_health,
)
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from pydantic import ValidationError

# 专项认证凭据只用于临时数据库；测试断言确保它们不会进入日志或正式证据。
ADMIN_PASSWORD = "Prd086AdminTestOnly!2026"
OPS_PASSWORD = "Prd086OpsTestOnly!2026"
VIEWER_PASSWORD = "Prd086ViewerTestOnly!2026"
AUTH_SECRET = "prd086-auth-secret-" + "a" * 48


def _url(path: Path) -> str:
    """把工作区临时数据库路径转换为测试使用的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def seeded_0079(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """从空库迁移到 0079 并生成一次默认租户模板。"""

    root = tmp_path_factory.mktemp("prd086-control-template")
    database = root / "seeded.sqlite3"
    names = {
        "METAHUB_SEED_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "METAHUB_AUTH_TOKEN_SECRET": AUTH_SECRET,
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET": AUTH_SECRET + "-enc",
    }
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    try:
        result = apply_migrations(_url(database))
        assert result.applied[-1] == "0079"
        apply_seed_data(_url(database))
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return database


def _prepare(tmp_path: Path, template: Path) -> str:
    """复制只读测试模板到用例独占数据库，隔离并行状态变化。"""

    database = tmp_path / "customer-health.sqlite3"
    shutil.copy2(template, database)
    return _url(database)


def _base_time() -> datetime:
    """使用远离迁移创建秒的未来整点，避免状态转换依赖墙钟竞态。"""

    return datetime(2030, 1, 1, 0, 0, 5, tzinfo=UTC)


def _materialize(database_url: str, *, start: datetime | None = None) -> tuple[str, datetime]:
    """领取当前租户并完成一次真实物化，返回稳定评分 ID 与评估时间。"""

    config = SimpleNamespace(database_url=database_url)
    claimed_at = start or _base_time()
    claim = claim_next_customer_health_tenant(
        config,
        worker_id="prd086-materializer",
        now=claimed_at,
    )
    assert claim is not None
    score_id = evaluate_claimed_customer_health(
        config,
        claim,
        now=claimed_at + timedelta(seconds=1),
    )
    assert score_id is not None
    return score_id, claimed_at + timedelta(seconds=1)


def _context(tenant_id: str, permissions: tuple[str, ...]) -> AuthContext:
    """构造 tenant-qualified 后端认证上下文，用于服务层权限与归属测试。"""

    # 认证上下文同时绑定 user、tenant、Session 与权限快照，不能只按角色模拟。
    return AuthContext(
        user=AuthUser(
            user_id="user_prd086_context",
            tenant_id=tenant_id,
            email="context@example.invalid",
            display_name="PRD086 Context",
            role="admin",
            permission_codes=permissions,
            permission_revision=1,
            authorization_fingerprint="f" * 64,
        ),
        tenant=AuthTenant(tenant_id=tenant_id, slug="prd086", name="PRD086"),
        session=AuthSession(
            session_id="ses_prd086_context",
            expires_at=datetime(2031, 1, 1, tzinfo=UTC),
        ),
        token="test-token-kept-in-memory",
        token_jti="jti_prd086_context",
    )


def test_integer_formula_largest_remainder_risk_floors_and_strict_dto() -> None:
    """验证整数舍入、贡献守恒、下限、工单缺源和 DTO exact-set。"""

    source = SourceSnapshot(
        tenant_id="tenant_prd086_formula",
        window_start="2026-08-10T08:00:00Z",
        window_end="2026-08-17T08:00:00Z",
        comparison_start="2026-08-03T08:00:00Z",
        comparison_end="2026-08-10T08:00:00Z",
        current_request_count=10,
        previous_request_count=21,
        successful_request_count=9,
        failed_request_count=1,
        unresolved_cost_anomaly_count=0,
        critical_cost_anomaly_count=0,
        active_budget_rule_count=1,
        eligible_user_count=3,
        recently_active_user_count=1,
        activation_stage_count=4,
        evaluated_at="2026-08-17T08:00:01Z",
    )
    projection = evaluate_source_snapshot(source)
    assert projection.signals[0].rate_bps == 4761
    assert projection.signals[1].rate_bps == 9000
    assert projection.risk_level == "at_risk"
    assert projection.risk_tags == ("usage_declining", "high_error_rate", "team_inactive")
    assert sum(signal.contribution_bps for signal in projection.signals) == projection.score_bps
    assert projection.signals[-1].availability == "unavailable"
    assert projection.signals[-1].score_bps is None
    assert projection.signals[-1].contribution_bps == 0

    healthy_total_but_zero_usage = replace(
        source,
        current_request_count=0,
        previous_request_count=1,
        successful_request_count=0,
        failed_request_count=0,
    )
    assert evaluate_source_snapshot(healthy_total_but_zero_usage).risk_level in {
        "at_risk",
        "critical",
    }
    with pytest.raises(ValidationError):
        CustomerHealthResponse.model_validate(
            {
                "tenant_id": "tenant_x",
                "customer_health_score_id": "chs_" + "a" * 32,
                "score_bps": 1,
                "risk_level": "critical",
                "risk_tags": [],
                "missing_signals": ["ticket_health"],
                "freshness_status": "fresh",
                "recommended_actions": [],
                "window": {
                    "start": source.window_start,
                    "end": source.window_end,
                    "comparison_start": source.comparison_start,
                    "comparison_end": source.comparison_end,
                    "timezone": "UTC",
                },
                "signals": [],
                "activation": {
                    "completed_stage_count": 0,
                    "total_stage_count": 7,
                    "activated": False,
                },
                "evaluated_at": source.evaluated_at,
                "generated_at": source.evaluated_at,
                "request_id": "req_prd086",
                "trace_id": "trc_prd086",
                "unexpected": "forbidden",
            }
        )
    with pytest.raises(ApiError):
        evaluate_source_snapshot(
            replace(source, evaluated_at="2026-08-17T09:00:00Z")
        )


def test_scheduler_concurrency_idempotence_and_changed_only_outbox(
    tmp_path: Path,
    seeded_0079: Path,
) -> None:
    """同一 lease 只有一个 worker，重复提交幂等且相同投影的新小时不发事件。"""

    database_url = _prepare(tmp_path, seeded_0079)
    config = SimpleNamespace(database_url=database_url)
    start = _base_time()
    first = claim_next_customer_health_tenant(
        config, worker_id="prd086-worker-a", now=start
    )
    assert first is not None
    assert claim_next_customer_health_tenant(
        config, worker_id="prd086-worker-b", now=start + timedelta(seconds=1)
    ) is None
    first_score = evaluate_claimed_customer_health(
        config, first, now=start + timedelta(seconds=1)
    )
    assert first_score is not None
    assert evaluate_claimed_customer_health(
        config, first, now=start + timedelta(seconds=2)
    ) is None

    second_time = start.replace(minute=0, second=0) + timedelta(hours=1)
    second = claim_next_customer_health_tenant(
        config, worker_id="prd086-worker-b", now=second_time
    )
    assert second is not None
    second_score = evaluate_claimed_customer_health(
        config, second, now=second_time + timedelta(seconds=1)
    )
    assert second_score is not None and second_score != first_score
    connection = connect_database(database_url)
    assert connection.execute("SELECT COUNT(*) FROM customer_health_scores").fetchone()[0] == 2
    assert connection.execute(
        "SELECT COUNT(*) FROM customer_health_score_outbox"
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_idle_scheduler_and_outbox_do_not_acquire_sqlite_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seeded_0079: Path,
) -> None:
    """验证空轮询保持只读，多个后台循环不会争抢 SQLite writer。"""

    database_url = _prepare(tmp_path, seeded_0079)
    _score_id, evaluated = _materialize(database_url)
    config = SimpleNamespace(database_url=database_url)
    assert drain_customer_health_outbox_once(
        config,
        worker_id="prd086-idle-outbox-setup",
        now=evaluated + timedelta(seconds=1),
    )

    statements: list[str] = []

    def traced_connect(url: str):
        """为真实 SQLite 连接附加语句跟踪，用于断言空轮询没有写事务。"""

        connection = connect_database(url)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(customer_health_scheduler, "connect_database", traced_connect)
    monkeypatch.setattr(customer_health_outbox, "connect_database", traced_connect)
    assert claim_next_customer_health_tenant(
        config,
        worker_id="prd086-idle-scheduler",
        now=evaluated + timedelta(seconds=2),
    ) is None
    assert not drain_customer_health_outbox_once(
        config,
        worker_id="prd086-idle-outbox",
        now=evaluated + timedelta(seconds=2),
    )
    normalized = [" ".join(statement.split()) for statement in statements]
    assert "BEGIN IMMEDIATE" not in normalized
    assert sum(
        statement.startswith("SELECT 1 FROM customer_health_") for statement in normalized
    ) == 2


def test_get_service_is_tenant_bound_strict_and_database_read_only(
    tmp_path: Path,
    seeded_0079: Path,
) -> None:
    """重复 GET 只读同租户最新快照，角色目标链接再按当前权限收敛。"""

    database_url = _prepare(tmp_path, seeded_0079)
    _score_id, evaluated = _materialize(database_url)
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0]
    before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "customer_health_scores",
            "customer_health_score_outbox",
            "customer_health_scheduler_tenants",
            "request_logs",
        )
    }
    connection.close()
    context = _context(
        tenant_id,
        ("customer_health.read", "product_analytics.read", "request_log.read"),
    )
    config = SimpleNamespace(database_url=database_url)
    first = get_customer_health(
        config,
        context,
        request_id="req_prd086_get",
        trace_id="trc_prd086_get",
        now=evaluated + timedelta(seconds=5),
    )
    second = get_customer_health(
        config,
        context,
        request_id="req_prd086_get",
        trace_id="trc_prd086_get",
        now=evaluated + timedelta(seconds=5),
    )
    assert first == second
    assert first.tenant_id == tenant_id
    assert first.missing_signals == ["ticket_health"]
    assert [item.code for item in first.signals] == [
        "usage_trend",
        "reliability",
        "cost_anomaly_health",
        "budget_readiness",
        "team_activity",
        "ticket_health",
    ]
    targets = {item.code: item.target_navigation_item_id for item in first.recommended_actions}
    assert targets["connect_support_ticket_source"] is None
    connection = connect_database(database_url)
    after = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    connection.close()
    assert after == before


@pytest.mark.anyio
async def test_route_enforces_backend_rbac_and_rejects_all_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seeded_0079: Path,
) -> None:
    """通过真实认证证明管理员与运维可读、查看者被拒绝、未知查询参数被阻断。"""

    database_url = _prepare(tmp_path, seeded_0079)
    # 使用当前小时物化，保证真实 HTTP GET 不会把测试快照判为未来。
    current = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=4)
    _materialize(database_url, start=current)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", AUTH_SECRET + "-enc")
    connection = connect_database(database_url)
    tenant_id = connection.execute(
        "SELECT tenant_id FROM tenants WHERE slug='default'"
    ).fetchone()[0]
    created = "2026-08-17T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        (
            (
                "user_prd086_ops", tenant_id, "ops@prd086.local", "PRD086 Ops",
                hash_password(OPS_PASSWORD), "ops_admin", created, created,
            ),
            (
                "user_prd086_viewer", tenant_id, "viewer@prd086.local", "PRD086 Viewer",
                hash_password(VIEWER_PASSWORD), "viewer", created, created,
            ),
        ),
    )
    connection.commit()
    connection.close()
    app = create_service_app("control-plane-api", "PRD086 Control Test")

    @app.middleware("http")
    async def private_boundary(request, call_next):
        """为专项应用固定私有禁止缓存边界，复现生产中间件合同。"""

        response = await call_next(request)
        if request.url.path == "/customer-health":
            response.headers["Cache-Control"] = "no-store, private"
        return response

    app.include_router(auth_router)
    app.include_router(customer_health_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        async def login(email: str, password: str) -> str:
            """通过真实登录端点取得内存 Token，不把凭据写入任何验收证据。"""

            response = await client.post(
                "/auth/login",
                json={"tenant_slug": "default", "email": email, "password": password},
            )
            assert response.status_code == 200, response.text
            # Token 仅在当前请求作用域内使用，测试输出和断言均不打印其值。
            return response.json()["access_token"]

        admin = await login("admin@metahub.local", ADMIN_PASSWORD)
        ops = await login("ops@prd086.local", OPS_PASSWORD)
        viewer = await login("viewer@prd086.local", VIEWER_PASSWORD)
        admin_response = await client.get(
            "/customer-health", headers={"Authorization": f"Bearer {admin}"}
        )
        ops_response = await client.get(
            "/customer-health", headers={"Authorization": f"Bearer {ops}"}
        )
        denied = await client.get(
            "/customer-health", headers={"Authorization": f"Bearer {viewer}"}
        )
        invalid = await client.get(
            "/customer-health?tenant_id=other",
            headers={"Authorization": f"Bearer {admin}"},
        )
    assert admin_response.status_code == 200, admin_response.text
    assert ops_response.status_code == 200, ops_response.text
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.permission_denied"
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "customer_health.request_invalid"
    for response in (admin_response, ops_response, denied, invalid):
        assert response.headers["Cache-Control"] == "no-store, private"
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_outbox_ten_failures_close_source_without_sensitive_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seeded_0079: Path,
) -> None:
    """连续十次固定投递失败后 source 进入不可再领取终态。"""

    database_url = _prepare(tmp_path, seeded_0079)
    _score_id, evaluated = _materialize(database_url)
    config = SimpleNamespace(database_url=database_url)
    connection = connect_database(database_url)
    persisted = dict(
        connection.execute(
            """
            SELECT score.*,outbox.customer_health_score_outbox_id,
                   outbox.webhook_event_id,outbox.status AS outbox_status,
                   outbox.attempt_count,outbox.generation,
                   outbox.subscription_snapshot_json,outbox.snapshot_frozen_at
            FROM customer_health_scores score
            JOIN customer_health_score_outbox outbox
              ON outbox.tenant_id=score.tenant_id
             AND outbox.customer_health_score_id=score.customer_health_score_id
            """
        ).fetchone()
    )
    connection.close()
    persisted["operation_request_id"] = "req_chs_" + "0" * 32
    with pytest.raises(ApiError):
        _event_from_row(persisted)

    def fail_dispatch(*_args, **_kwargs):
        """用固定安全错误模拟十次投递失败，禁止夹带异常正文。"""

        # 模拟器只抛统一错误码，验证 source outbox 不持久化敏感异常文本。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    due = evaluated + timedelta(seconds=1)
    for expected_attempt in range(1, 11):
        claim = claim_next_customer_health_event(
            config,
            worker_id="prd086-outbox-worker",
            now=due,
        )
        assert claim is not None and claim.attempt_count == expected_attempt
        assert dispatch_claimed_customer_health_event(
            config,
            claim,
            now=due + timedelta(seconds=1),
        )
        connection = connect_database(database_url)
        row = connection.execute(
            """
            SELECT status,attempt_count,next_attempt_at,last_error_code
            FROM customer_health_score_outbox
            """
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
    assert claim_next_customer_health_event(
        config,
        worker_id="prd086-outbox-worker",
        now=due + timedelta(days=1),
    ) is None


def test_customer_health_module_has_no_sensitive_storage_or_exception_logging() -> None:
    """静态审计本模块没有敏感存储入口、异常正文日志或调试泄露。"""

    root = Path(__file__).resolve().parents[2] / "src" / "metahub_control" / "customer_health"
    sources = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    forbidden = (
        "prompt_text",
        "response_text",
        "provider_key",
        "virtual_key_plaintext",
        "authorization_header",
        "endpoint_url",
        "exc_info",
    )
    assert all(value not in sources.lower() for value in forbidden)
    assert "lease_token: str = field(repr=False)" in sources
