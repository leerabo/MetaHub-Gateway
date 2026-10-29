"""PRD-051 FinOps 正式账本 Control API 专项集成测试。

本文件只覆盖账本查询、财务调整、后端权限、租户隔离、统一错误、审计事务和
Webhook 配置兼容性。所有数据库都创建在 pytest 的工作区临时目录中，测试不会读取或
修改本地开发库，也不会把 Token、幂等键、调整原因或 Webhook 密钥写入业务日志。
"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response

from metahub_control.auth.routes import router as auth_router
from metahub_control.finops import routes as finops_routes
from metahub_control.finops import service as finops_service
from metahub_control.finops.routes import router as finops_router
from metahub_control.webhooks.routes import router as webhooks_router
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError, ERROR_SPECS
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.i18n import error_label

# 固定密码只用于隔离测试库中的角色矩阵；访问令牌始终只进入 Authorization 请求头。
ADMIN_PASSWORD = "LocalAdmin!2026"
DEVELOPER_PASSWORD = "DeveloperLocal!2026"
VIEWER_PASSWORD = "ViewerLocal!2026"
OTHER_ADMIN_PASSWORD = "OtherAdminLocal!2026"
OTHER_TENANT_ID = "tenant_prd051_other"
OTHER_TENANT_SLUG = "prd051-other"

FINOPS_ERROR_CODES = (
    "finops.query_invalid",
    "finops.adjustment_invalid",
    "finops.amount_invalid",
    "finops.entry_not_found",
    "finops.request_not_found",
    "finops.reversal_not_allowed",
    "finops.reversal_exceeds_remaining",
    "finops.idempotency_conflict",
    "finops.ledger_unavailable",
    "finops.usage_record_unavailable",
    "finops.adjustment_failed",
)


def sqlite_url(path: Path) -> str:
    """把 pytest 临时数据库路径转换为项目统一 SQLite URL。"""

    return f"sqlite:///{path.as_posix()}"


def build_control_app():
    """构造只包含认证、FinOps 和 Webhook 的最小控制面应用。

    `Idempotency-Key` 显式加入 CORS 合同，保持本测试应用与生产 Control API 一致；
    其余中间件、错误响应和 request_id 均复用共享运行时，避免测试替身掩盖真实行为。
    """

    app = create_service_app(
        "control-plane-api",
        "FinOps Ledger Test Control API",
        extra_cors_allow_headers=("Idempotency-Key",),
    )
    app.include_router(auth_router)
    app.include_router(finops_router)
    app.include_router(webhooks_router)
    return app


def prepare_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """迁移隔离数据库并准备默认租户角色和第二租户管理员。

    参数：pytest 环境变量替换器和工作区临时目录。返回：数据库 URL 与默认租户 ID。
    第二租户用于证明游标签名和冲正目标都不能跨 tenant 边界恢复。
    """

    database_url = sqlite_url(tmp_path / "metahub-finops.db")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd051-test-auth-token-secret")
    monkeypatch.setenv(
        "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
        "prd051-test-webhook-encryption-secret",
    )
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    now = "2026-07-27T00:00:00Z"
    connection.executemany(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        [
            (
                "user_prd051_developer",
                seed.tenant_id,
                "developer@example.com",
                "Developer User",
                hash_password(DEVELOPER_PASSWORD),
                "developer",
                now,
                now,
            ),
            (
                "user_prd051_viewer",
                seed.tenant_id,
                "viewer@example.com",
                "Viewer User",
                hash_password(VIEWER_PASSWORD),
                "viewer",
                now,
                now,
            ),
        ],
    )
    connection.execute(
        """
        INSERT INTO tenants(
          tenant_id, slug, name, service_mode, status, created_at, updated_at
        ) VALUES(?, ?, 'PRD-051 第二租户', 'internal_only', 'active', ?, ?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id, tenant_id, email, display_name, password_hash,
          role, status, created_at, updated_at
        ) VALUES(
          'user_prd051_other_admin', ?, 'admin@other.example.com', 'Other Admin', ?,
          'admin', 'active', ?, ?
        )
        """,
        (OTHER_TENANT_ID, hash_password(OTHER_ADMIN_PASSWORD), now, now),
    )
    connection.commit()
    connection.close()
    # 只返回后续断言所需的非敏感定位字段，不暴露密码哈希或加密配置。
    return {"database_url": database_url, "tenant_id": seed.tenant_id}


async def login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    """登录指定测试租户用户并返回仅供后续请求头使用的访问令牌。"""

    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    # 令牌仅返回给调用用例并写入 Authorization 头，不能进入业务断言或日志。
    return response.json()["access_token"]


async def post_adjustment(
    client: AsyncClient,
    token: str,
    *,
    adjustment_type: str,
    amount: object,
    idempotency_key: str,
    reason: str,
    request_id: str,
    reversal_of_entry_id: str | None = None,
    related_request_id: str | None = None,
) -> Response:
    """按真实 wire 合同提交调整，tenant、actor、方向和币种均不允许客户端自报。"""

    payload: dict[str, object] = {
        "adjustment_type": adjustment_type,
        "amount": amount,
        "reason": reason,
    }
    if reversal_of_entry_id is not None:
        payload["reversal_of_entry_id"] = reversal_of_entry_id
    # 关联请求是可选维度；未提供时必须省略字段，不能用空串绕过后端标识校验。
    if related_request_id is not None:
        payload["related_request_id"] = related_request_id
    return await client.post(
        "/finops/adjustments",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "X-Request-ID": request_id,
        },
        json=payload,
    )


@pytest.mark.anyio
async def test_finops_adjustment_precision_idempotency_reversal_and_totals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主流程：五类调整精确入账，幂等不重复审计/事件，部分和全部冲正受余额保护。"""

    setup = prepare_database(monkeypatch, tmp_path)
    ledger_events: list[Any] = []
    audit_dispatches: list[Any] = []
    monkeypatch.setattr(
        finops_service,
        "_dispatch_ledger_event",
        lambda _config, event, _request_id: ledger_events.append(event),
    )
    monkeypatch.setattr(
        finops_service,
        "dispatch_audit_recorded",
        lambda _config, _context, record, **_kwargs: audit_dispatches.append(record),
    )
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        debit = await post_adjustment(
            client,
            token,
            adjustment_type="manual_debit",
            amount="10.123456",
            idempotency_key="idem-prd051-main",
            reason="六位金额借记",
            request_id="req_prd051_debit",
        )
        replay = await post_adjustment(
            client,
            token,
            adjustment_type="manual_debit",
            amount="10.123456",
            idempotency_key="idem-prd051-main",
            reason="六位金额借记",
            request_id="req_prd051_debit_replay",
        )
        conflict = await post_adjustment(
            client,
            token,
            adjustment_type="manual_debit",
            amount="10.123456",
            idempotency_key="idem-prd051-main",
            reason="同键不同原因",
            request_id="req_prd051_debit_conflict",
        )

        assert debit.status_code == 201, debit.text
        assert debit.headers["cache-control"] == "no-store, private"
        debit_body = debit.json()
        debit_entry_id = debit_body["ledger_entry"]["finops_ledger_entry_id"]
        assert debit_body["ledger_entry"]["amount"] == "10.123456"
        assert debit_body["ledger_entry"]["direction"] == "debit"
        assert debit_body["adjustment"]["amount"] == "10.123456"
        assert debit_body["request_id"] == "req_prd051_debit"
        assert debit_body["trace_id"]
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["ledger_entry"]["finops_ledger_entry_id"] == debit_entry_id
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "finops.idempotency_conflict"
        # 完全相同的重放和同键冲突都不能产生第二条财务事件或第二条审计。
        assert len(ledger_events) == 1
        assert len(audit_dispatches) == 1

        created: list[Response] = []
        for adjustment_type, amount, key in (
            ("manual_credit", "2.000001", "idem-prd051-credit"),
            ("gift_credit", "1", "idem-prd051-gift"),
            ("trial_credit", "0.5", "idem-prd051-trial"),
        ):
            response = await post_adjustment(
                client,
                token,
                adjustment_type=adjustment_type,
                amount=amount,
                idempotency_key=key,
                reason=f"{adjustment_type} 专项验证",
                request_id=f"req_prd051_{adjustment_type}",
            )
            assert response.status_code == 201, response.text
            created.append(response)

        partial = await post_adjustment(
            client,
            token,
            adjustment_type="reversal",
            amount="4.123456",
            idempotency_key="idem-prd051-reversal-partial",
            reason="部分冲正",
            request_id="req_prd051_reversal_partial",
            reversal_of_entry_id=debit_entry_id,
        )
        full = await post_adjustment(
            client,
            token,
            adjustment_type="reversal",
            amount="6.000000",
            idempotency_key="idem-prd051-reversal-full",
            reason="冲正剩余余额",
            request_id="req_prd051_reversal_full",
            reversal_of_entry_id=debit_entry_id,
        )
        exceeds = await post_adjustment(
            client,
            token,
            adjustment_type="reversal",
            amount="0.000001",
            idempotency_key="idem-prd051-reversal-exceeds",
            reason="超过可冲正余额",
            request_id="req_prd051_reversal_exceeds",
            reversal_of_entry_id=debit_entry_id,
        )
        reversal_of_reversal = await post_adjustment(
            client,
            token,
            adjustment_type="reversal",
            amount="1",
            idempotency_key="idem-prd051-reversal-chain",
            reason="禁止冲正另一条冲正",
            request_id="req_prd051_reversal_chain",
            reversal_of_entry_id=partial.json()["ledger_entry"]["finops_ledger_entry_id"],
        )
        listed = await client.get(
            "/finops/ledger?limit=100",
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "req_prd051_list"},
        )

    assert all(item.status_code == 201 for item in created)
    assert partial.status_code == 201, partial.text
    assert partial.json()["ledger_entry"]["direction"] == "credit"
    assert full.status_code == 201, full.text
    assert exceeds.status_code == 409
    assert exceeds.json()["code"] == "finops.reversal_exceeds_remaining"
    assert reversal_of_reversal.status_code == 409
    assert reversal_of_reversal.json()["code"] == "finops.reversal_not_allowed"
    assert listed.status_code == 200, listed.text
    totals = listed.json()["totals"]
    assert totals["entry_count"] == 6
    assert totals["unnormalized_entry_count"] == 0
    assert totals["by_currency"]["CNY"] == {
        "debit": "10.123456",
        "credit": "13.623457",
        "net": "-3.500001",
    }
    assert totals["net_cost_cny"] == "-3.500001"
    assert len(ledger_events) == 6
    assert len(audit_dispatches) == 6
    assert all(event.payload.get("reason") is None for event in ledger_events)
    assert all(event.payload["currency"] == "CNY" for event in ledger_events)

    connection = connect_database(setup["database_url"])
    audit_rows = connection.execute(
        """
        SELECT object_type, before_summary_json, after_summary_json
        FROM audit_logs
        WHERE tenant_id=? AND action='finops.adjustment_created'
        ORDER BY created_at, audit_log_id
        """,
        (setup["tenant_id"],),
    ).fetchall()
    adjustment_count = connection.execute(
        "SELECT COUNT(*) FROM finops_adjustments WHERE tenant_id=?",
        (setup["tenant_id"],),
    ).fetchone()[0]
    ledger_count = connection.execute(
        "SELECT COUNT(*) FROM finops_ledger_entries WHERE tenant_id=?",
        (setup["tenant_id"],),
    ).fetchone()[0]
    ledger_row = connection.execute(
        """
        SELECT le.*, a.reason, u.display_name AS actor_display_name, u.email AS actor_email
        FROM finops_ledger_entries le
        LEFT JOIN finops_adjustments a
          ON a.tenant_id=le.tenant_id AND a.finops_adjustment_id=le.finops_adjustment_id
        LEFT JOIN users u
          ON u.tenant_id=le.tenant_id AND u.user_id=le.actor_user_id
        WHERE le.tenant_id=? AND le.finops_ledger_entry_id=?
        """,
        (setup["tenant_id"], debit_entry_id),
    ).fetchone()
    adjustment_row = connection.execute(
        "SELECT * FROM finops_adjustments WHERE tenant_id=? AND finops_adjustment_id=?",
        (setup["tenant_id"], debit_body["adjustment"]["finops_adjustment_id"]),
    ).fetchone()
    connection.close()
    assert adjustment_count == 6
    assert ledger_count == 6
    assert len(audit_rows) == 6
    summaries = [json.loads(row["after_summary_json"]) for row in audit_rows]
    expected_reason_hash = hashlib.sha256("六位金额借记".encode()).hexdigest()
    assert all(row["object_type"] == "finops_adjustment" for row in audit_rows)
    assert all(json.loads(row["before_summary_json"]) == {} for row in audit_rows)
    assert expected_reason_hash in {summary["reason_sha256"] for summary in summaries}
    assert all("六位金额借记" not in row["after_summary_json"] for row in audit_rows)
    assert all("idempotency" not in row["after_summary_json"].lower() for row in audit_rows)
    # DTO 恢复必须显式校验持久枚举，不能依赖 Pydantic 在未知值上偶然抛出不同异常。
    corrupt_ledger = dict(ledger_row)
    corrupt_ledger["entry_type"] = "unknown_entry"
    corrupt_adjustment = dict(adjustment_row)
    corrupt_adjustment["adjustment_type"] = "unknown_adjustment"
    with pytest.raises(ValueError, match="entry type invalid"):
        finops_service._ledger_entry_info(corrupt_ledger)
    with pytest.raises(ValueError, match="adjustment type invalid"):
        finops_service._adjustment_info(corrupt_adjustment)


@pytest.mark.anyio
async def test_finops_rbac_strict_wire_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """异常与权限流程：先鉴权再解析，管理员请求仍严格拒绝未知字段、重复键和控制字符。"""

    setup = prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await login(client, "developer@example.com", DEVELOPER_PASSWORD)
        viewer = await login(client, "viewer@example.com", VIEWER_PASSWORD)
        anonymous = await client.get("/finops/ledger")
        developer_denied = await client.get(
            "/finops/ledger?tenant_id=other",
            headers={"Authorization": f"Bearer {developer}"},
        )
        viewer_denied = await client.post(
            "/finops/adjustments",
            headers={
                "Authorization": f"Bearer {viewer}",
                "Idempotency-Key": "idem-prd051-viewer",
                "Content-Type": "application/json",
            },
            content=b"{",
        )
        unknown_query = await client.get(
            "/finops/ledger?tenant_id=other",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd051_unknown_query",
            },
        )
        duplicate_query = await client.get(
            "/finops/ledger?limit=1&limit=2",
            headers={"Authorization": f"Bearer {admin}"},
        )
        invalid_range = await client.get(
            "/finops/ledger?start_date=2025-01-01&end_date=2026-07-27",
            headers={"Authorization": f"Bearer {admin}"},
        )
        duplicate_json = await client.post(
            "/finops/adjustments",
            headers={
                "Authorization": f"Bearer {admin}",
                "Idempotency-Key": "idem-prd051-duplicate-json",
                "Content-Type": "application/json",
            },
            content=(
                b'{"adjustment_type":"manual_debit","amount":"1",'
                b'"amount":"2","reason":"duplicate"}'
            ),
        )
        unknown_field = await client.post(
            "/finops/adjustments",
            headers={
                "Authorization": f"Bearer {admin}",
                "Idempotency-Key": "idem-prd051-unknown-field",
            },
            json={
                "adjustment_type": "manual_debit",
                "amount": "1",
                "reason": "unknown field",
                "tenant_id": "other",
            },
        )
        numeric_amount = await post_adjustment(
            client,
            admin,
            adjustment_type="manual_debit",
            amount=1.1,
            idempotency_key="idem-prd051-float",
            reason="禁止浮点金额",
            request_id="req_prd051_float",
        )
        c1_identifier = await post_adjustment(
            client,
            admin,
            adjustment_type="manual_credit",
            amount="1",
            idempotency_key="idem-prd051-c1",
            reason="拒绝 C1 标识字符",
            request_id="req_prd051_c1",
            related_request_id="req_bad\u0085identifier",
        )

    assert anonymous.status_code == 401
    assert developer_denied.status_code == 403
    assert developer_denied.json()["code"] == "auth.permission_denied"
    assert viewer_denied.status_code == 403
    assert viewer_denied.json()["code"] == "auth.permission_denied"
    assert unknown_query.status_code == 422
    assert unknown_query.json()["code"] == "finops.query_invalid"
    assert unknown_query.json()["request_id"] == "req_prd051_unknown_query"
    assert unknown_query.headers["cache-control"] == "no-store, private"
    assert duplicate_query.status_code == 422
    assert invalid_range.status_code == 422
    assert duplicate_json.status_code == 422
    assert duplicate_json.json()["code"] == "finops.adjustment_invalid"
    assert unknown_field.status_code == 422
    assert unknown_field.json()["code"] == "finops.adjustment_invalid"
    assert numeric_amount.status_code == 422
    assert numeric_amount.json()["code"] == "finops.amount_invalid"
    assert c1_identifier.status_code == 422
    assert c1_identifier.json()["code"] == "finops.adjustment_invalid"
    connection = connect_database(setup["database_url"])
    assert connection.execute("SELECT COUNT(*) FROM finops_adjustments").fetchone()[0] == 0
    connection.close()


@pytest.mark.anyio
async def test_finops_cursor_binding_and_cross_tenant_reversal_404(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """租户流程：HMAC 游标绑定筛选和 tenant，其他租户的分录 ID 对冲正端点表现为 404。"""

    setup = prepare_database(monkeypatch, tmp_path)
    monkeypatch.setattr(finops_service, "_dispatch_ledger_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(finops_service, "dispatch_audit_recorded", lambda *_args, **_kwargs: None)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        other_admin = await login(
            client,
            "admin@other.example.com",
            OTHER_ADMIN_PASSWORD,
            tenant_slug=OTHER_TENANT_SLUG,
        )
        for index in range(3):
            created = await post_adjustment(
                client,
                admin,
                adjustment_type="manual_debit",
                amount=str(index + 1),
                idempotency_key=f"idem-prd051-page-{index}",
                reason=f"游标分录 {index}",
                request_id=f"req_prd051_page_{index}",
            )
            assert created.status_code == 201, created.text
        other = await post_adjustment(
            client,
            other_admin,
            adjustment_type="manual_debit",
            amount="9",
            idempotency_key="idem-prd051-other-entry",
            reason="第二租户分录",
            request_id="req_prd051_other_entry",
        )
        assert other.status_code == 201, other.text
        other_entry_id = other.json()["ledger_entry"]["finops_ledger_entry_id"]
        first_page = await client.get(
            "/finops/ledger?limit=1",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert first_page.status_code == 200, first_page.text
        cursor = first_page.json()["next_cursor"]
        replacement = "A" if cursor[-1] != "A" else "B"
        tampered = await client.get(
            f"/finops/ledger?limit=1&cursor={cursor[:-1]}{replacement}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        changed_filter = await client.get(
            f"/finops/ledger?limit=1&direction=debit&cursor={cursor}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        changed_tenant = await client.get(
            f"/finops/ledger?limit=1&cursor={cursor}",
            headers={"Authorization": f"Bearer {other_admin}"},
        )
        second_page = await client.get(
            f"/finops/ledger?limit=1&cursor={cursor}",
            headers={"Authorization": f"Bearer {admin}"},
        )
        cross_reversal = await post_adjustment(
            client,
            admin,
            adjustment_type="reversal",
            amount="1",
            idempotency_key="idem-prd051-cross-reversal",
            reason="跨租户目标必须隐藏",
            request_id="req_prd051_cross_reversal",
            reversal_of_entry_id=other_entry_id,
        )
        other_list = await client.get(
            "/finops/ledger?limit=100",
            headers={"Authorization": f"Bearer {other_admin}"},
        )

    first_body = first_page.json()
    assert first_body["has_more"] is True
    assert first_body["total"] == 3
    assert first_body["totals"]["entry_count"] == 3
    assert first_body["ledger_entries"][0]["tenant_id"] == setup["tenant_id"]
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["ledger_entries"][0]["tenant_id"] == setup["tenant_id"]
    for rejected in (tampered, changed_filter, changed_tenant):
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "finops.query_invalid"
    assert cross_reversal.status_code == 404
    assert cross_reversal.json()["code"] == "finops.entry_not_found"
    assert other_list.status_code == 200
    assert other_list.json()["total"] == 1
    assert {entry["tenant_id"] for entry in other_list.json()["ledger_entries"]} == {
        OTHER_TENANT_ID
    }


@pytest.mark.anyio
async def test_finops_audit_failure_rolls_back_adjustment_ledger_and_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """事务异常：审计已经插入后再失败时，Adjustment、Ledger 和 Audit 必须整体回滚。"""

    setup = prepare_database(monkeypatch, tmp_path)
    original_record_audit = finops_service.record_audit_log

    def record_then_fail(*args: Any, **kwargs: Any) -> Any:
        """先调用真实审计写入，再模拟审计边界失败以验证同事务回滚。"""

        original_record_audit(*args, **kwargs)
        # 故障必须发生在审计 INSERT 之后，才能证明三张表共享同一回滚边界。
        raise ApiError("audit_log.record_unavailable")

    monkeypatch.setattr(finops_service, "record_audit_log", record_then_fail)
    monkeypatch.setattr(finops_service, "_dispatch_ledger_event", lambda *_args, **_kwargs: None)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        failed = await post_adjustment(
            client,
            token,
            adjustment_type="manual_debit",
            amount="1.000001",
            idempotency_key="idem-prd051-audit-failure",
            reason="审计失败必须整体回滚",
            request_id="req_prd051_audit_failure",
        )

    assert failed.status_code == 500
    assert failed.json()["code"] == "finops.adjustment_failed"
    assert failed.headers["cache-control"] == "no-store, private"
    connection = connect_database(setup["database_url"])
    counts = {
        "adjustments": connection.execute("SELECT COUNT(*) FROM finops_adjustments").fetchone()[0],
        "ledger": connection.execute("SELECT COUNT(*) FROM finops_ledger_entries").fetchone()[0],
        "audit": connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='finops.adjustment_created'"
        ).fetchone()[0],
    }
    connection.close()
    assert counts == {"adjustments": 0, "ledger": 0, "audit": 0}


@pytest.mark.anyio
async def test_finops_webhook_openapi_cache_cors_and_error_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """兼容合同：旧事件名仅在订阅时归一，OpenAPI/CORS/缓存头和双语错误保持稳定。"""

    prepare_database(monkeypatch, tmp_path)
    app = build_control_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created_webhook = await client.post(
            "/webhooks",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": "FinOps 账本事件",
                "endpoint_url": "https://hooks.example.com/ledger",
                "event_types": ["billing.ledger_created", "ledger.entry_created"],
            },
        )
        assert created_webhook.status_code == 200, created_webhook.text
        endpoint_id = created_webhook.json()["webhook"]["webhook_endpoint_id"]
        canonical_filter = await client.get(
            f"/webhooks/{endpoint_id}/deliveries?event_type=ledger.entry_created",
            headers={"Authorization": f"Bearer {token}"},
        )
        legacy_filter = await client.get(
            f"/webhooks/{endpoint_id}/deliveries?event_type=billing.ledger_created",
            headers={"Authorization": f"Bearer {token}"},
        )
        success = await client.get(
            "/finops/ledger",
            headers={"Authorization": f"Bearer {token}"},
        )
        invalid = await client.get(
            "/finops/ledger?unknown=value",
            headers={"Authorization": f"Bearer {token}"},
        )
        openapi = await client.get("/openapi.json")

    assert created_webhook.json()["webhook"]["event_types"] == ["ledger.entry_created"]
    assert canonical_filter.status_code == 200, canonical_filter.text
    assert legacy_filter.status_code == 422
    assert legacy_filter.json()["code"] == "webhook.delivery_filter_invalid"
    assert success.status_code == 200
    assert success.headers["cache-control"] == "no-store, private"
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store, private"
    operation = openapi.json()["paths"]["/finops/adjustments"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {"adjustment_type", "amount", "reason"}
    assert {"adjustment_type", "amount", "reversal_of_entry_id", "related_request_id", "reason"} == set(
        request_schema["properties"]
    )

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        """模拟服务层未捕获异常，验证最外层 500 仍应用 FinOps 私有缓存边界。"""

        # 使用不含密钥或请求正文的固定哨兵，确保 500 响应不会泄露内部异常文本。
        raise RuntimeError("finops test sentinel")

    monkeypatch.setattr(finops_routes, "list_finops_ledger", explode)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        token = await login(client, "admin@metahub.local", ADMIN_PASSWORD)
        unhandled = await client.get(
            "/finops/ledger",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-ID": "req_prd051_unhandled",
            },
        )
    assert unhandled.status_code == 500
    assert unhandled.json()["code"] == "infra.unhandled"
    assert unhandled.json()["request_id"] == "req_prd051_unhandled"
    assert unhandled.headers["cache-control"] == "no-store, private"
    assert "sentinel" not in unhandled.text

    # 这里导入真实生产应用而不是测试工厂，防止 app.py 漏配 Idempotency-Key CORS 头。
    production_app = importlib.import_module("metahub_control.app").app
    async with AsyncClient(
        transport=ASGITransport(app=production_app),
        base_url="http://control",
    ) as client:
        preflight = await client.options(
            "/finops/adjustments",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,idempotency-key,x-request-id"
                ),
            },
        )
    assert preflight.status_code == 200, preflight.text
    allowed_headers = {
        item.strip().lower()
        for item in preflight.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type", "idempotency-key", "x-request-id"}.issubset(
        allowed_headers
    )

    assert all(code in ERROR_SPECS for code in FINOPS_ERROR_CODES)
    assert all(error_label(code, "zh-CN") != code for code in FINOPS_ERROR_CODES)
    assert all(error_label(code, "en-US") != code for code in FINOPS_ERROR_CODES)
    unavailable = ERROR_SPECS["finops.usage_record_unavailable"]
    assert unavailable.http_status == 503
    assert unavailable.retryable is True
