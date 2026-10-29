"""验证 PRD-101 平台 Gateway Cell 与 placement API 合同，边界覆盖权限、驻留、CAS 和签名同步。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_gateway import routes as gateway_routes
from metahub_control.platform_gateway import service as gateway_service
from metahub_control.platform_gateway.routes import router
from metahub_control.platform_gateway.snapshots import refresh_gateway_snapshot_once
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_gateway.gateway_fleet import sync_gateway_placements_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app

# 浏览器同源夹具只用于 Origin 与 CSRF 合同，不代表真实部署地址或凭据。
ORIGIN = "http://localhost:5173"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """构造隔离数据库、平台 owner 与真实应用配置，所有固定凭据仅存在 pytest 进程。"""

    database_url = f"sqlite:///{(tmp_path / 'prd101.sqlite3').as_posix()}"
    settings = {
        "METAHUB_ENV": "test",
        "METAHUB_DATABASE_URL": database_url,
        "METAHUB_SEED_ADMIN_PASSWORD": "LocalAdmin!2026",
        "METAHUB_AUTH_TOKEN_SECRET": "test-prd101-auth-token-secret-2026",
        "METAHUB_IDENTITY_SESSION_SECRET": "test-prd101-global-session-secret-2026",
        "METAHUB_IDENTITY_CHALLENGE_SECRET": "test-prd101-challenge-secret-2026",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": "test-prd101-pii-secret-independent-2026",
        "METAHUB_WORKSPACE_SESSION_SECRET": "test-prd101-workspace-secret-2026-long",
        "METAHUB_PLATFORM_SESSION_SECRET": "test-prd101-platform-secret-2026-long",
        "METAHUB_SUPPORT_SESSION_SECRET": "test-prd101-support-secret-2026-long",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": "test-prd101-mfa-secret-2026-long-value",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": "test-prd101-recovery-secret-2026-long",
        "METAHUB_ALLOWED_ORIGINS": ORIGIN,
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    apply_migrations(database_url)
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        account_id = str(
            connection.execute("SELECT account_id FROM global_accounts LIMIT 1").fetchone()[0]
        )
        operator_id = "pto_" + "1" * 32
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
            VALUES(?,'platform_owner','active',NULL,datetime('now'),NULL)
            """,
            (operator_id,),
        )
        owner = connection.execute(
            """
            SELECT binding.workspace_id,region.region_code,region.residency_policy_version
            FROM workspace_tenant_bindings binding
            JOIN workspace_regions region ON region.workspace_id=binding.workspace_id
            LIMIT 1
            """
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    context = PlatformContext(
        platform_session_id="pts_prd101_test",
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gis_prd101_test",
        factor_id="mfa_prd101_test",
        factor_revision=1,
        operator_revision=1,
        roles=("platform_owner",),
        permissions=("platform.gateway.read", "platform.gateway.manage"),
        expires_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    monkeypatch.setattr(
        gateway_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: context,
    )
    monkeypatch.setattr(
        gateway_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: context,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-101 Gateway Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.include_router(router)
    return (
        database_url,
        app,
        str(owner["workspace_id"]),
        str(owner["region_code"]),
        str(owner["residency_policy_version"]),
    )


def _headers(*, revision: int = 0, key: str = "prd101-placement-request-0001"):
    """生成严格 Origin、CSRF、If-Match 和幂等头，不加入 Bearer 或其他会话域。"""

    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": "c" * 43,
        "If-Match": f'"{revision}"',
        "Idempotency-Key": key,
    }


@pytest.mark.anyio
async def test_cell_projection_and_placement_cas_idempotency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """覆盖 Cell 安全投影、驻留拒绝、CAS/幂等、容量门禁、续签与跨 Cell 撤销。"""

    database_url, app, workspace_id, region, policy_version = _prepare(monkeypatch, tmp_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        client.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        listed = await client.get("/platform/gateway-cells")
        assert listed.status_code == 200, listed.text
        cells = listed.json()["cells"]
        assert len(cells) == 2
        assert all("internal_address" not in cell and "provider" not in cell for cell in cells)
        primary = next(cell for cell in cells if cell["region_code"] == region)
        other_region = next(cell for cell in cells if cell["region_code"] != region)
        payload = {
            "primary_region_code": region,
            "primary_cell_code": primary["cell_code"],
            "failover_policy": "deny",
            "failover_cell_code": None,
            "residency_policy_version": policy_version,
            "reason_code": "initial_placement",
            "confirmed": True,
        }
        cross_region = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json={
                **payload,
                "primary_region_code": other_region["region_code"],
                "primary_cell_code": other_region["cell_code"],
            },
            headers=_headers(key="prd101-placement-request-0099"),
        )
        assert cross_region.status_code == 409
        assert cross_region.json()["code"] == "platform_gateway.residency_conflict"
        created = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json=payload,
            headers=_headers(),
        )
        assert created.status_code == 200, created.text
        assert created.headers["etag"] == '"1"'
        projection = created.json()
        assert projection["placement"]["snapshot_generation"] == 1
        assert "signature" not in created.text and "tenant_id" not in created.text
        replay = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json=payload,
            headers=_headers(),
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        stale = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json={**payload, "reason_code": "capacity_rebalance"},
            headers=_headers(key="prd101-placement-request-0002"),
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "platform_gateway.revision_conflict"

        mixed = await client.get(
            "/platform/gateway-cells", headers={"Authorization": "Bearer forbidden"}
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "platform.credential_conflict"
        for cookie in (WORKSPACE_SESSION_COOKIE, SUPPORT_SESSION_COOKIE):
            client.cookies.set(cookie, "x" * 43)
            conflict = await client.get("/platform/gateway-cells")
            assert conflict.status_code == 400
            client.cookies.delete(cookie)

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE gateway_cells
            SET admitted_leases=capacity_limit,revision=revision+1,updated_at=datetime('now')
            WHERE cell_code=?
            """,
            (primary["cell_code"],),
        )
        connection.commit()
    finally:
        connection.close()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        client.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        full_cell = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json={**payload, "reason_code": "capacity_rebalance"},
            headers=_headers(revision=1, key="prd101-placement-request-0098"),
        )
    assert full_cell.status_code == 503
    assert full_cell.json()["code"] == "platform_gateway.unavailable"
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE gateway_cells
            SET admitted_leases=0,revision=revision+1,updated_at=datetime('now')
            WHERE cell_code=?
            """,
            (primary["cell_code"],),
        )
        connection.commit()
    finally:
        connection.close()

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM gateway_cell_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM gateway_config_outbox").fetchone()[0] == 1
        outbox = connection.execute(
            """
            SELECT snapshot_generation,status,snapshot_id,snapshot_payload,
              snapshot_payload_sha256,signing_key_version,published_at,expires_at,
              attempt_count
            FROM gateway_config_outbox
            """
        ).fetchone()
        assert tuple(outbox)[:3] == (1, "completed", outbox["snapshot_id"])
        assert outbox["snapshot_id"].startswith("gss_")
        assert len(outbox["snapshot_payload_sha256"]) == 64
        assert outbox["signing_key_version"] == 1
        assert outbox["attempt_count"] == 1
        assert app.state.config.gateway_placement_signing_secret not in outbox["snapshot_payload"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    published_at = datetime.fromisoformat(str(outbox["published_at"]).replace("Z", "+00:00"))
    renewal_time = published_at + timedelta(seconds=20)
    assert refresh_gateway_snapshot_once(app.state.config, now=renewal_time) is True
    connection = connect_database(database_url)
    try:
        renewed = connection.execute(
            "SELECT snapshot_id,snapshot_generation,attempt_count FROM gateway_config_outbox"
        ).fetchone()
        assert renewed["snapshot_id"] != outbox["snapshot_id"]
        assert tuple(renewed)[1:] == (1, 2)
    finally:
        connection.close()

    gateway_config = app.state.config.model_copy(
        update={
            "gateway_placement_enabled": True,
            "gateway_cell_code": primary["cell_code"],
        }
    )
    gateway_state = SimpleNamespace()
    assert sync_gateway_placements_once(
        gateway_config,
        gateway_state,
        now=renewal_time + timedelta(seconds=1),
    ) == 1
    installed = gateway_state.gateway_placement_snapshots
    assert len(installed) == 1
    snapshot = next(iter(installed.values()))
    assert snapshot.workspace_id == workspace_id
    assert snapshot.cell_code == primary["cell_code"]

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO gateway_cells(
              gateway_cell_id,cell_code,region_code,capacity_class,status,
              health_status,capacity_limit,admitted_leases,release_watermark,
              config_watermark,revision,created_at,updated_at)
            VALUES('gcl_33333333333333333333333333333333','cn-north-1-b',?,
              'standard','active','healthy',500,0,0,0,1,datetime('now'),datetime('now'))
            """,
            (region,),
        )
        connection.commit()
    finally:
        connection.close()
    moved_payload = {
        **payload,
        "primary_cell_code": "cn-north-1-b",
        "reason_code": "capacity_rebalance",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, "p" * 43)
        client.cookies.set(PLATFORM_CSRF_COOKIE, "c" * 43)
        moved = await client.patch(
            f"/platform/workspaces/{workspace_id}/gateway-placement",
            json=moved_payload,
            headers=_headers(revision=1, key="prd101-placement-request-0003"),
        )
    assert moved.status_code == 200, moved.text
    assert moved.headers["etag"] == '"2"'
    assert sync_gateway_placements_once(
        gateway_config,
        gateway_state,
        now=renewal_time + timedelta(seconds=1),
    ) == 1
    assert gateway_state.gateway_placement_snapshots == {}

    moved_gateway_config = gateway_config.model_copy(
        update={"gateway_cell_code": "cn-north-1-b"}
    )
    moved_gateway_state = SimpleNamespace()
    assert sync_gateway_placements_once(
        moved_gateway_config,
        moved_gateway_state,
        now=renewal_time + timedelta(seconds=1),
    ) == 1
    moved_snapshot = next(iter(moved_gateway_state.gateway_placement_snapshots.values()))
    assert moved_snapshot.generation == 2
    assert moved_snapshot.cell_code == "cn-north-1-b"
