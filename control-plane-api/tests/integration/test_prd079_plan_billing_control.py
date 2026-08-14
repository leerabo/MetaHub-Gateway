"""PRD-079 套餐与额度 Control API、后端权限、审计原子性和租户隔离专项测试。"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.plan_billing import outbox as quota_outbox
from metahub_control.plan_billing import service as plan_service
from metahub_control.plan_billing.routes import router as plan_billing_router
from metahub_control.webhooks import service as webhook_service
from metahub_control.webhooks.routes import router as webhook_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

# 测试凭据、租户坐标和远期时间只服务于隔离夹具，禁止复用为生产配置。
ADMIN_PASSWORD = "Prd079AdminOnly!2026"
ROLE_PASSWORD = "Prd079RoleOnly!2026"
AUTH_SECRET = "prd079-test-auth-secret-with-sufficient-entropy"
ENCRYPTION_SECRET = "prd079-test-encryption-secret-with-sufficient-entropy"
FINANCE_EMAIL = "finance-prd079@example.com"
DEVELOPER_EMAIL = "developer-prd079@example.com"
OTHER_ADMIN_EMAIL = "admin-prd079@other.example.com"
OTHER_TENANT_ID = "tenant_prd079_other"
OTHER_TENANT_SLUG = "prd079-other"
FUTURE_TRIAL_END = "2099-08-14T12:00:00Z"


@dataclass(frozen=True)
class ControlTemplate:
    """一次真实迁移与种子后可复制的只读测试模板。"""

    database: Path
    tenant_id: str
    admin_email: str


@dataclass(frozen=True)
class ControlFixture:
    """单个测试独占的数据库和稳定租户身份。"""

    database_url: str
    tenant_id: str
    admin_email: str


def _url(path: Path) -> str:
    """把独占测试数据库路径转换为控制面配置使用的 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd079_control_template(tmp_path_factory: pytest.TempPathFactory) -> ControlTemplate:
    """从空库迁移到 0072 一次，后续测试复制数据库以避免重复执行 72 个迁移。"""

    root = tmp_path_factory.mktemp("prd079-control-template")
    database = root / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        result = apply_migrations(database_url)
        assert result.applied[-1] == "0072"
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous

    connection = connect_database(database_url)
    now = "2026-08-14T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,'active',?,?)
        """,
        (
            (
                "user_prd079_finance",
                seed.tenant_id,
                FINANCE_EMAIL,
                "PRD-079 Finance",
                hash_password(ROLE_PASSWORD),
                "finance_admin",
                now,
                now,
            ),
            (
                "user_prd079_developer",
                seed.tenant_id,
                DEVELOPER_EMAIL,
                "PRD-079 Developer",
                hash_password(ROLE_PASSWORD),
                "developer",
                now,
                now,
            ),
        ),
    )
    # 新租户触发 revision-15 RBAC 初始化，证明接口只使用 Bearer Session 的 tenant owner。
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,?,'internal_only','active',?,?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, "PRD-079 Other", now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd079_other_admin',?,?,?,?,'admin','active',?,?)
        """,
        (
            OTHER_TENANT_ID,
            OTHER_ADMIN_EMAIL,
            "PRD-079 Other Admin",
            hash_password(ROLE_PASSWORD),
            now,
            now,
        ),
    )
    connection.commit()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
    # 模板只返回数据库路径和稳定租户坐标，密码与连接对象均不跨夹具泄露。
    return ControlTemplate(database, seed.tenant_id, seed.admin_email)


@pytest.fixture
def control_fixture(
    prd079_control_template: ControlTemplate,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ControlFixture:
    """复制模板并把所有运行时配置绑定到当前测试的仓库内临时数据库。"""

    database = tmp_path / "control.sqlite3"
    shutil.copy2(prd079_control_template.database, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", ENCRYPTION_SECRET)
    return ControlFixture(
        database_url,
        prd079_control_template.tenant_id,
        prd079_control_template.admin_email,
    )


def _build_app():
    """装配仅包含认证、套餐额度与 Webhook 路由的隔离测试应用。"""

    app = create_service_app("control-plane-api", "PRD-079 Plan Billing Control Test")
    app.include_router(auth_router)
    app.include_router(plan_billing_router)
    app.include_router(webhook_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
    tenant_slug: str = "default",
) -> str:
    """通过真实认证路由登录指定租户，并只返回当前测试内存中的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 访问令牌仅在当前测试调用栈传递，不写入断言输出或持久证据。
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    """构造当前请求使用的单值认证 Header，令牌不进入其他测试字段。"""

    # Authorization 只传给本地 ASGI 请求，不记录到数据库或测试日志。
    return {"Authorization": f"Bearer {token}"}


def _plan_payload(*, revision: int, trial_ends_at: str | None = None) -> dict[str, object]:
    """构造不含租户和服务端 owner 的精确套餐版本化请求正文。"""

    return {
        "expected_revision": revision,
        "display_name": "Enterprise Plan",
        "base_tier": "paid",
        "status": "active",
        "trial_ends_at": trial_ends_at,
    }


async def _create_plan(
    client: AsyncClient,
    token: str,
    *,
    trial: bool = False,
) -> dict[str, object]:
    """通过公开套餐接口创建同租户套餐，并返回已校验的响应对象。"""

    response = await client.patch(
        "/plans",
        headers=_auth(token),
        json=_plan_payload(revision=0, trial_ends_at=FUTURE_TRIAL_END if trial else None),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _force_trial_due(database_url: str, tenant_id: str) -> tuple[int, int]:
    """把 active 试用改为数据库时钟之前到期，并返回 Plan/Free Bucket revision。"""

    connection = connect_database(database_url)
    try:
        plan = connection.execute(
            "SELECT * FROM plans WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        trial = connection.execute(
            "SELECT * FROM quota_buckets WHERE tenant_id=? AND quota_type='trial'",
            (tenant_id,),
        ).fetchone()
        assert plan is not None and trial is not None
        connection.execute(
            """
            UPDATE plans SET trial_started_at='2026-08-01T00:00:00Z',
              trial_ends_at='2026-08-02T00:00:00Z',state_revision=state_revision+1,
              updated_at='2026-08-02T00:00:00Z'
            WHERE tenant_id=? AND plan_id=? AND state_revision=?
            """,
            (tenant_id, plan["plan_id"], plan["state_revision"]),
        )
        connection.execute(
            """
            UPDATE quota_buckets SET expires_at='2026-08-02T00:00:00Z',
              state_revision=state_revision+1,updated_at='2026-08-02T00:00:00Z'
            WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
            """,
            (tenant_id, trial["quota_bucket_id"], trial["state_revision"]),
        )
        connection.commit()
        plan_revision = connection.execute(
            "SELECT state_revision FROM plans WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
        free_revision = connection.execute(
            "SELECT state_revision FROM quota_buckets WHERE tenant_id=? AND quota_type='free'",
            (tenant_id,),
        ).fetchone()[0]
        return int(plan_revision), int(free_revision)
    finally:
        connection.close()


async def _create_trial_expiry_event(client: AsyncClient, token: str) -> dict[str, object]:
    """通过公开套餐接口形成不可变试用到期事件 source 与 outbox 事实。"""

    created = await _create_plan(client, token, trial=True)
    response = await client.patch(
        "/plans",
        headers=_auth(token),
        json=_plan_payload(revision=int(created["plan"]["state_revision"])),
    )
    assert response.status_code == 200, response.text
    assert response.json()["plan"]["trial_status"] == "expired"
    return response.json()


async def _create_quota_webhook(
    client: AsyncClient,
    token: str,
    *,
    marker: str,
) -> str:
    """创建仅订阅试用到期事件的测试 Endpoint，明文签名材料不进入断言。"""

    response = await client.post(
        "/webhooks",
        headers=_auth(token),
        json={
            "name": f"PRD-079 quota {marker}",
            "endpoint_url": f"http://127.0.0.1:65530/prd079-{marker}",
            "event_types": ["trial.expired"],
            "timeout_ms": 1000,
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["webhook"]["webhook_endpoint_id"])


@pytest.mark.anyio
async def test_plan_create_cas_rbac_tenant_and_audit(control_fixture: ControlFixture) -> None:
    """Plan 创建必须后端授权、原子建四桶、CAS，并只审计当前租户安全摘要。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        finance = await _login(client, email=FINANCE_EMAIL, password=ROLE_PASSWORD)
        developer = await _login(client, email=DEVELOPER_EMAIL, password=ROLE_PASSWORD)
        other = await _login(
            client,
            email=OTHER_ADMIN_EMAIL,
            password=ROLE_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )

        denied = await client.get("/plans", headers=_auth(developer))
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"
        assert denied.json()["request_id"]

        empty = await client.get("/plans", headers=_auth(finance))
        assert empty.status_code == 200
        assert empty.json()["plan"] is None
        assert empty.headers["cache-control"] == "no-store, private"

        # 数组/对象枚举必须进入稳定业务校验，不能让 set membership 抛出原生 TypeError。
        malformed_plan = await client.patch(
            "/plans",
            headers=_auth(admin),
            json={**_plan_payload(revision=0), "base_tier": []},
        )
        assert malformed_plan.status_code == 422
        assert malformed_plan.json()["code"] == "plan.request_invalid"
        assert malformed_plan.json()["request_id"]

        created = await _create_plan(client, admin)
        assert created["plan"]["tenant_id"] == control_fixture.tenant_id
        assert created["plan"]["state_revision"] == 1
        assert [item["quota_type"] for item in created["quota_buckets"]] == [
            "trial",
            "free",
            "gift",
            "paid",
        ]
        assert all(item["available"] == "0.000000" for item in created["quota_buckets"])

        malformed_adjustment = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "malformed-direction-prd079"},
            json={
                "quota_type": "paid",
                "direction": [],
                "amount": "1.000000",
                "reason": "Malformed enum boundary",
                "expected_bucket_revision": 1,
            },
        )
        assert malformed_adjustment.status_code == 422
        assert malformed_adjustment.json()["code"] == "quota.request_invalid"
        assert malformed_adjustment.json()["request_id"]

        conflict = await client.patch(
            "/plans",
            headers=_auth(admin),
            json=_plan_payload(revision=0),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "plan.revision_conflict"

        foreign = await client.get("/plans", headers=_auth(other))
        assert foreign.status_code == 200
        assert foreign.json()["plan"] is None

        adjustment_denied = await client.post(
            "/quota-adjustments",
            headers={**_auth(developer), "Idempotency-Key": "developer-denied-079"},
            json={
                "quota_type": "paid",
                "direction": "credit",
                "amount": "1.000000",
                "reason": "Should not persist",
                "expected_bucket_revision": 1,
            },
        )
        assert adjustment_denied.status_code == 403

    connection = connect_database(control_fixture.database_url)
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM quota_buckets").fetchone()[0] == 4
    audit = connection.execute(
        """
        SELECT action,object_type,tenant_id,before_summary_json,after_summary_json
        FROM audit_logs WHERE action='plan.created'
        """
    ).fetchone()
    assert audit is not None
    assert tuple(audit[:3]) == ("plan.created", "plan", control_fixture.tenant_id)
    assert json.loads(audit["before_summary_json"]) == {}
    after = json.loads(audit["after_summary_json"])
    assert set(after) == {
        "display_name",
        "base_tier",
        "status",
        "trial_status",
        "trial_started_at",
        "trial_ends_at",
        "trial_cycle",
        "state_revision",
        "quota_generation",
    }
    connection.close()


@pytest.mark.anyio
async def test_adjustment_idempotency_order_expiry_and_report(
    control_fixture: ControlFixture,
) -> None:
    """四桶调整保持精确金额、幂等、debit 边界，并在试用结束后生成唯一事件。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        created = await _create_plan(client, admin, trial=True)
        plan_revision = int(created["plan"]["state_revision"])
        latest: dict[str, object] | None = None
        for index, quota_type in enumerate(("trial", "free", "gift", "paid"), start=1):
            latest_response = await client.post(
                "/quota-adjustments",
                headers={**_auth(admin), "Idempotency-Key": f"credit-{quota_type}-prd079"},
                json={
                    "quota_type": quota_type,
                    "direction": "credit",
                    "amount": f"{index}.000000",
                    "reason": f"Test credit {quota_type}",
                    "expected_bucket_revision": 1,
                },
            )
            assert latest_response.status_code == 201, latest_response.text
            latest = latest_response.json()
            plan_revision = int(latest["plan"]["state_revision"])
        assert latest is not None

        replay = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "credit-paid-prd079"},
            json={
                "quota_type": "paid",
                "direction": "credit",
                "amount": "4.000000",
                "reason": "Test credit paid",
                "expected_bucket_revision": 1,
            },
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        key_conflict = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "credit-paid-prd079"},
            json={
                "quota_type": "paid",
                "direction": "credit",
                "amount": "4.000001",
                "reason": "Test credit paid",
                "expected_bucket_revision": 1,
            },
        )
        assert key_conflict.status_code == 409
        assert key_conflict.json()["code"] == "quota.adjustment_conflict"

        excessive_debit = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "paid-debit-too-large-prd079"},
            json={
                "quota_type": "paid",
                "direction": "debit",
                "amount": "4.000001",
                "reason": "Boundary debit",
                "expected_bucket_revision": 2,
            },
        )
        assert excessive_debit.status_code == 409

        ended = await client.patch(
            "/plans",
            headers=_auth(admin),
            json=_plan_payload(revision=plan_revision, trial_ends_at=None),
        )
        assert ended.status_code == 200, ended.text
        assert ended.json()["plan"]["trial_status"] == "expired"
        assert ended.json()["plan"]["effective_tier"] == "paid"

        expired_trial_credit = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "expired-trial-credit-prd079"},
            json={
                "quota_type": "trial",
                "direction": "credit",
                "amount": "1.000000",
                "reason": "Expired trial must reject",
                "expected_bucket_revision": 3,
            },
        )
        assert expired_trial_credit.status_code == 409

        report = await client.get("/quota-buckets", headers=_auth(admin))
        assert report.status_code == 200
        assert [item["granted"] for item in report.json()["quota_buckets"]] == [
            "1.000000",
            "2.000000",
            "3.000000",
            "4.000000",
        ]
        assert len(report.json()["recent_adjustments"]) == 4
        assert all("reason" not in item for item in report.json()["recent_adjustments"])
        assert report.json()["overage"] == {"amount": "0.000000", "settlement_count": 0}

    connection = connect_database(control_fixture.database_url)
    assert connection.execute("SELECT COUNT(*) FROM quota_adjustments").fetchone()[0] == 4
    assert connection.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='quota.adjustment_created'"
    ).fetchone()[0] == 4
    assert connection.execute(
        "SELECT COUNT(*) FROM quota_events WHERE event_type='trial.expired'"
    ).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM quota_event_outbox").fetchone()[0] == 1
    audit_json = connection.execute(
        """
        SELECT after_summary_json FROM audit_logs
        WHERE action='quota.adjustment_created' ORDER BY created_at LIMIT 1
        """
    ).fetchone()[0]
    audit = json.loads(audit_json)
    assert "reason" not in audit and "idempotency_key" not in audit
    assert len(audit["reason_sha256"]) == 64
    connection.close()


@pytest.mark.anyio
async def test_audit_failure_rolls_back_plan_and_adjustment(
    control_fixture: ControlFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任何审计写入故障都必须回滚 Plan/Bucket/Adjustment，而不是返回半成功。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(
                plan_service,
                "record_audit_log",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    ApiError("audit_log.record_unavailable")
                ),
            )
            failed_plan = await client.patch(
                "/plans",
                headers=_auth(admin),
                json=_plan_payload(revision=0),
            )
        assert failed_plan.status_code == 503
        assert failed_plan.json()["code"] == "plan.unavailable"

        created = await _create_plan(client, admin)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                plan_service,
                "record_audit_log",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    ApiError("audit_log.record_unavailable")
                ),
            )
            failed_adjustment = await client.post(
                "/quota-adjustments",
                headers={**_auth(admin), "Idempotency-Key": "audit-failure-prd079"},
                json={
                    "quota_type": "paid",
                    "direction": "credit",
                    "amount": "5.000000",
                    "reason": "Must roll back",
                    "expected_bucket_revision": 1,
                },
            )
        assert failed_adjustment.status_code == 503
        assert failed_adjustment.json()["code"] == "quota.unavailable"

    connection = connect_database(control_fixture.database_url)
    assert connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM quota_adjustments").fetchone()[0] == 0
    paid = connection.execute(
        "SELECT granted_micros,state_revision FROM quota_buckets WHERE quota_type='paid'"
    ).fetchone()
    assert tuple(paid) == (0, 1)
    assert created["plan"]["state_revision"] == 1
    connection.close()


@pytest.mark.anyio
async def test_scheduler_expiry_is_idempotent_and_preserves_trial_balance(
    control_fixture: ControlFixture,
) -> None:
    """Scheduler 与 Gateway guard 共用到期 CAS；到期不回收既有余额或伪扣 paid。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        await _create_plan(client, admin, trial=True)
        credit = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "scheduler-trial-credit-prd079"},
            json={
                "quota_type": "trial",
                "direction": "credit",
                "amount": "2.000000",
                "reason": "Scheduler boundary",
                "expected_bucket_revision": 1,
            },
        )
        assert credit.status_code == 201

    connection = connect_database(control_fixture.database_url)
    plan = connection.execute("SELECT * FROM plans").fetchone()
    trial = connection.execute(
        "SELECT * FROM quota_buckets WHERE tenant_id=? AND quota_type='trial'",
        (control_fixture.tenant_id,),
    ).fetchone()
    connection.execute(
        """
        UPDATE plans SET trial_started_at='2026-08-01T00:00:00Z',
          trial_ends_at='2026-08-02T00:00:00Z',state_revision=state_revision+1,
          updated_at='2026-08-02T00:00:00Z'
        WHERE tenant_id=? AND plan_id=? AND state_revision=?
        """,
        (control_fixture.tenant_id, plan["plan_id"], plan["state_revision"]),
    )
    connection.execute(
        """
        UPDATE quota_buckets SET expires_at='2026-08-02T00:00:00Z',
          state_revision=state_revision+1,updated_at='2026-08-02T00:00:00Z'
        WHERE tenant_id=? AND quota_bucket_id=? AND state_revision=?
        """,
        (control_fixture.tenant_id, trial["quota_bucket_id"], trial["state_revision"]),
    )
    connection.commit()
    connection.close()

    assert plan_service.expire_due_trials(_build_app().state.config, limit=10) == 1
    assert plan_service.expire_due_trials(_build_app().state.config, limit=10) == 0

    connection = connect_database(control_fixture.database_url)
    plan_after = connection.execute("SELECT trial_status FROM plans").fetchone()[0]
    trial_after = connection.execute(
        """
        SELECT granted_micros,consumed_micros,reserved_micros,status
        FROM quota_buckets WHERE quota_type='trial'
        """
    ).fetchone()
    paid_after = connection.execute(
        """
        SELECT granted_micros,consumed_micros,reserved_micros
        FROM quota_buckets WHERE quota_type='paid'
        """
    ).fetchone()
    assert plan_after == "expired"
    assert tuple(trial_after) == (2_000_000, 0, 0, "expired")
    assert tuple(paid_after) == (0, 0, 0)
    assert connection.execute(
        "SELECT COUNT(*) FROM quota_events WHERE event_type='trial.expired'"
    ).fetchone()[0] == 1
    connection.close()


@pytest.mark.anyio
async def test_plan_patch_commits_due_trial_before_rejecting_extension(
    control_fixture: ControlFixture,
) -> None:
    """已过 DB 时钟的 active 试用先到期，再以 revision conflict 拒绝续期。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        await _create_plan(client, admin, trial=True)
        expired_revision, _ = _force_trial_due(
            control_fixture.database_url,
            control_fixture.tenant_id,
        )
        rejected = await client.patch(
            "/plans",
            headers=_auth(admin),
            json=_plan_payload(
                revision=expired_revision,
                trial_ends_at=FUTURE_TRIAL_END,
            ),
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "plan.revision_conflict"
        assert rejected.json()["request_id"]

        connection = connect_database(control_fixture.database_url)
        plan = connection.execute("SELECT * FROM plans").fetchone()
        trial = connection.execute(
            "SELECT status FROM quota_buckets WHERE quota_type='trial'"
        ).fetchone()
        assert plan["trial_status"] == "expired"
        assert plan["trial_ends_at"] == "2026-08-02T00:00:00Z"
        assert plan["state_revision"] == expired_revision + 1
        assert trial["status"] == "expired"
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_events WHERE event_type='trial.expired'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM quota_event_outbox").fetchone()[0] == 1
        current_revision = int(plan["state_revision"])
        connection.close()

        impossible = await client.patch(
            "/plans",
            headers=_auth(admin),
            json=_plan_payload(
                revision=current_revision,
                trial_ends_at=FUTURE_TRIAL_END,
            ),
        )
        assert impossible.status_code == 422
        assert impossible.json()["code"] == "plan.request_invalid"


@pytest.mark.anyio
async def test_quota_adjustment_keeps_committed_expiry_across_second_transaction(
    control_fixture: ControlFixture,
) -> None:
    """非 Trial 调整可在到期提交后继续，后续事务不会回滚 source/outbox。"""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        await _create_plan(client, admin, trial=True)
        _plan_revision, free_revision = _force_trial_due(
            control_fixture.database_url,
            control_fixture.tenant_id,
        )
        adjusted = await client.post(
            "/quota-adjustments",
            headers={**_auth(admin), "Idempotency-Key": "expiry-free-credit-prd079"},
            json={
                "quota_type": "free",
                "direction": "credit",
                "amount": "1.000000",
                "reason": "Post-expiry free quota",
                "expected_bucket_revision": free_revision,
            },
        )
        assert adjusted.status_code == 201, adjusted.text

    connection = connect_database(control_fixture.database_url)
    try:
        assert connection.execute("SELECT trial_status FROM plans").fetchone()[0] == "expired"
        assert connection.execute(
            "SELECT status FROM quota_buckets WHERE quota_type='trial'"
        ).fetchone()[0] == "expired"
        assert connection.execute(
            "SELECT granted_micros FROM quota_buckets WHERE quota_type='free'"
        ).fetchone()[0] == 1_000_000
        assert connection.execute(
            "SELECT COUNT(*) FROM quota_events WHERE event_type='trial.expired'"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM quota_event_outbox").fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_quota_outbox_freezes_snapshot_takeover_and_dispatches_once(
    control_fixture: ControlFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次领取冻结订阅集合；过期接管后旧 owner 零出网，新 owner 只投递一次。"""

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        first_endpoint = await _create_quota_webhook(
            client,
            admin,
            marker="snapshot-first",
        )
        await _create_trial_expiry_event(client, admin)
        claimed_at = datetime.now(UTC) + timedelta(seconds=2)
        first = quota_outbox.claim_next_quota_event(
            app.state.config,
            worker_id="quota-prd079-first",
            now=claimed_at,
        )
        assert first is not None
        assert first.attempt_count == 0
        assert first.lease_generation == 1
        frozen_delivery_ids = json.loads(first.snapshot_json)
        assert len(frozen_delivery_ids) == 1

        # 首次领取后新增的 Endpoint 不属于历史事件快照，恢复时不得追溯补投。
        second_endpoint = await _create_quota_webhook(
            client,
            admin,
            marker="snapshot-late",
        )

    assert quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-too-early",
        now=first.lease_expires_at - timedelta(seconds=1),
    ) is None
    takeover = quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-takeover",
        now=first.lease_expires_at,
    )
    assert takeover is not None
    assert takeover.attempt_count == 0
    assert takeover.lease_generation == 2
    assert takeover.lease_owner_id != first.lease_owner_id
    assert takeover.lease_token != first.lease_token
    assert takeover.snapshot_json == first.snapshot_json

    outbound_delivery_ids: list[str] = []

    def send_success(
        _config: object,
        _endpoint: object,
        _body: bytes,
        delivery_id: str,
    ) -> dict[str, object]:
        """替换最终 HTTP 原语，只记录非敏感 delivery ID。"""

        outbound_delivery_ids.append(delivery_id)
        return {
            "status": "success",
            "http_status": 204,
            "duration_ms": 1,
            "error_code": None,
            "error_summary": None,
        }

    monkeypatch.setattr(webhook_service, "_send_once", send_success)
    # 旧 claim 在 dispatcher 的出网前 lease guard 即被拒绝，不能产生网络副作用或终态回写。
    assert quota_outbox.dispatch_claimed_quota_event(
        app.state.config,
        first,
        now=first.lease_expires_at + timedelta(seconds=1),
    ) is False
    assert outbound_delivery_ids == []
    assert quota_outbox.dispatch_claimed_quota_event(
        app.state.config,
        takeover,
        now=takeover.lease_expires_at - timedelta(seconds=5),
    ) is True
    assert outbound_delivery_ids == frozen_delivery_ids

    # 已完成 claim 的迟到重放只能回读 delivery 终态，不能再次出网或改写 source。
    assert quota_outbox.dispatch_claimed_quota_event(
        app.state.config,
        takeover,
        now=takeover.lease_expires_at - timedelta(seconds=1),
    ) is False
    assert outbound_delivery_ids == frozen_delivery_ids

    connection = connect_database(control_fixture.database_url)
    source = connection.execute("SELECT * FROM quota_event_outbox").fetchone()
    deliveries = connection.execute(
        """
        SELECT webhook_endpoint_id,webhook_delivery_id,status,attempt_count
        FROM webhook_deliveries WHERE event_type='trial.expired'
        ORDER BY webhook_endpoint_id
        """
    ).fetchall()
    assert source["status"] == "dispatched"
    assert source["attempt_count"] == 1
    assert source["next_attempt_at"] is None
    assert source["delivery_snapshot_json"] == first.snapshot_json
    assert [(row["webhook_endpoint_id"], row["status"]) for row in deliveries] == [
        (first_endpoint, "success")
    ]
    assert all(row["webhook_endpoint_id"] != second_endpoint for row in deliveries)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-after-success",
        now=datetime.now(UTC) + timedelta(days=1),
    ) is None


@pytest.mark.anyio
async def test_quota_outbox_retries_with_bounded_backoff_and_fails_at_ten(
    control_fixture: ControlFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """瞬时 dispatcher 故障按 5/30/300 秒退避，第十次原子终结 source 与 delivery。"""

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        await _create_quota_webhook(client, admin, marker="terminal")
        await _create_trial_expiry_event(client, admin)

    def dispatch_unavailable(*_args: object, **_kwargs: object) -> list[object]:
        """模拟固定领域错误；异常不包含 URL、响应正文或凭据。"""

        # 模拟异常只携带固定错误码，确保重试证据不依赖敏感外部响应。
        raise ApiError("webhook.delivery_unavailable")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", dispatch_unavailable)
    due = datetime.now(UTC) + timedelta(seconds=2)
    frozen_snapshot: str | None = None
    for expected_attempt in range(1, quota_outbox.MAX_ATTEMPTS + 1):
        claim = quota_outbox.claim_next_quota_event(
            app.state.config,
            worker_id=f"quota-prd079-retry-{expected_attempt}",
            now=due,
        )
        assert claim is not None
        assert claim.attempt_count == expected_attempt - 1
        if frozen_snapshot is None:
            frozen_snapshot = claim.snapshot_json
            assert len(json.loads(frozen_snapshot)) == 1
        else:
            assert claim.snapshot_json == frozen_snapshot
        completed_at = claim.lease_expires_at - timedelta(seconds=5)
        assert quota_outbox.dispatch_claimed_quota_event(
            app.state.config,
            claim,
            now=completed_at,
        ) is True

        connection = connect_database(control_fixture.database_url)
        source = connection.execute("SELECT * FROM quota_event_outbox").fetchone()
        connection.close()
        assert source["attempt_count"] == expected_attempt
        assert source["last_error_code"] == "webhook.delivery_unavailable"
        assert source["delivery_snapshot_json"] == frozen_snapshot
        if expected_attempt < quota_outbox.MAX_ATTEMPTS:
            expected_delay = quota_outbox.BACKOFF_SECONDS[
                min(expected_attempt - 1, len(quota_outbox.BACKOFF_SECONDS) - 1)
            ]
            assert source["status"] == "pending"
            due = datetime.fromisoformat(source["next_attempt_at"].replace("Z", "+00:00"))
            assert due == completed_at + timedelta(seconds=expected_delay)
        else:
            assert source["status"] == "failed"
            assert source["next_attempt_at"] is None
            assert source["lease_owner_id"] is None
            assert source["lease_token"] is None

    connection = connect_database(control_fixture.database_url)
    delivery = connection.execute(
        """
        SELECT status,error_code,error_summary,next_retry_at
        FROM webhook_deliveries WHERE event_type='trial.expired'
        """
    ).fetchone()
    assert tuple(delivery) == (
        "failed",
        "webhook.delivery_unavailable",
        "Source delivery attempts are exhausted.",
        None,
    )
    assert connection.execute(
        """
        SELECT COUNT(*) FROM webhook_dead_letter_events
        WHERE event_type='trial.expired' AND status='dead_letter'
        """
    ).fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-after-terminal",
        now=due + timedelta(days=1),
    ) is None


@pytest.mark.anyio
async def test_quota_outbox_payload_invalid_is_immediately_terminal(
    control_fixture: ControlFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确定性 payload 错误首次即失败关闭，不能进入瞬时错误重试队列。"""

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        await _create_quota_webhook(client, admin, marker="payload-invalid")
        await _create_trial_expiry_event(client, admin)

    def reject_payload(*_args: object, **_kwargs: object) -> list[object]:
        """模拟 dispatcher 的确定性信封校验失败，不携带原始正文。"""

        # 确定性 payload 错误必须立即终结，异常文本不包含真实信封内容。
        raise ValueError("invalid webhook envelope")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", reject_payload)
    claim = quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-payload-invalid",
        now=datetime.now(UTC) + timedelta(seconds=2),
    )
    assert claim is not None
    assert quota_outbox.dispatch_claimed_quota_event(
        app.state.config,
        claim,
        now=claim.lease_expires_at - timedelta(seconds=5),
    ) is True

    connection = connect_database(control_fixture.database_url)
    source = connection.execute("SELECT * FROM quota_event_outbox").fetchone()
    delivery = connection.execute(
        """
        SELECT status,error_code,error_summary,next_retry_at
        FROM webhook_deliveries WHERE event_type='trial.expired'
        """
    ).fetchone()
    assert source["status"] == "failed"
    assert source["attempt_count"] == 1
    assert source["last_error_code"] == "webhook.payload_invalid"
    assert source["next_attempt_at"] is None
    assert tuple(delivery) == (
        "failed",
        "webhook.payload_invalid",
        "Source payload validation failed.",
        None,
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert quota_outbox.claim_next_quota_event(
        app.state.config,
        worker_id="quota-prd079-after-invalid",
        now=datetime.now(UTC) + timedelta(days=1),
    ) is None


@pytest.mark.anyio
async def test_invalid_source_is_quarantined_without_starving_other_tenant(
    control_fixture: ControlFixture,
) -> None:
    """坏 source 首次终结后，同一次 drain 仍投递后续租户的合法事件。"""

    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        admin = await _login(
            client,
            email=control_fixture.admin_email,
            password=ADMIN_PASSWORD,
        )
        main_plan = await _create_plan(client, admin)
        other_admin = await _login(
            client,
            email=OTHER_ADMIN_EMAIL,
            password=ROLE_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )
        await _create_trial_expiry_event(client, other_admin)

    invalid_event_id = "qevt_" + "a" * 32
    invalid_outbox_id = "qobx_" + "a" * 32
    connection = connect_database(control_fixture.database_url)
    connection.execute(
        """
        INSERT INTO quota_events(
          quota_event_id,tenant_id,plan_id,quota_bucket_id,event_type,
          quota_generation,trial_cycle,source_request_id,source_trace_id,
          actor_id,payload_json,occurred_at
        ) VALUES(?,?,?,NULL,'quota.exhausted',1,NULL,
                 'req_prd079_invalid_source','trc_prd079_invalid_source',
                 'quota-admission',?,'2026-08-01T00:00:00Z')
        """,
        (
            invalid_event_id,
            control_fixture.tenant_id,
            main_plan["plan"]["plan_id"],
            json.dumps(
                {
                    "schema_version": "quota.exhausted.v1",
                    "plan_id": "pln_" + "f" * 32,
                },
                separators=(",", ":"),
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO quota_event_outbox(
          quota_event_outbox_id,tenant_id,quota_event_id,status,attempt_count,
          lease_generation,lease_owner_id,lease_token,lease_expires_at,
          next_attempt_at,delivery_snapshot_json,snapshot_frozen_at,
          last_error_code,dispatched_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,
                 '2026-08-01T00:00:00Z',NULL,NULL,NULL,NULL,
                 '2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')
        """,
        (invalid_outbox_id, control_fixture.tenant_id, invalid_event_id),
    )
    connection.commit()
    connection.close()

    due = datetime.now(UTC) + timedelta(seconds=2)
    assert quota_outbox.drain_quota_event_once(
        app.state.config,
        worker_id="quota-prd079-source-quarantine",
        now=due,
    ) is True

    connection = connect_database(control_fixture.database_url)
    invalid = connection.execute(
        """
        SELECT status,attempt_count,last_error_code,delivery_snapshot_json
        FROM quota_event_outbox
        WHERE tenant_id=? AND quota_event_outbox_id=?
        """,
        (control_fixture.tenant_id, invalid_outbox_id),
    ).fetchone()
    valid = connection.execute(
        """
        SELECT status,attempt_count,last_error_code,delivery_snapshot_json
        FROM quota_event_outbox
        WHERE tenant_id=?
        """,
        (OTHER_TENANT_ID,),
    ).fetchone()
    assert tuple(invalid) == ("failed", 1, "webhook.payload_invalid", None)
    assert tuple(valid) == ("dispatched", 1, None, "[]")
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert quota_outbox.drain_quota_event_once(
        app.state.config,
        worker_id="quota-prd079-source-empty",
        now=due + timedelta(days=1),
    ) is False
