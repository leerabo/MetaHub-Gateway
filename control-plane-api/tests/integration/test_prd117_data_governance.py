"""PRD-117 policy, authorization, hold, scheduler, and evidence acceptance."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from metahub_control.config_distribution.crypto import open_config_value
from metahub_control.console.service import get_console_navigation
from metahub_control.data_governance import routes as governance_routes
from metahub_control.data_governance import runtime as governance_runtime
from metahub_control.data_governance import service as governance_service
from metahub_control.data_governance.routes import router
from metahub_control.data_governance.runtime import DataRetentionDeleteInput  # noqa: F401
from metahub_control.data_governance.scheduler import enqueue_due_deletion_jobs
from metahub_control.data_governance.service import (
    claim_deletion_fence_in_transaction,
    require_region_allowed_in_transaction,
)
from metahub_control.identity.service import SESSION_COOKIE, login_global_account
from metahub_control.platform_access.constants import PLATFORM_SESSION_COOKIE
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_gateway import service as gateway_service
from metahub_control.platform_gateway.models import GatewayPlacementUpdateRequest
from metahub_control.platform_infrastructure import service as infrastructure_service
from metahub_control.platform_infrastructure.models import BackupCreateRequest
from metahub_control.platform_jobs.runtime import run_platform_job_once
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
    get_workspace_session,
    switch_workspace,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
GLOBAL_TOKEN = "g" * 43


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd117-governance-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0109"
    apply_seed_data(database_url)
    with connect_database(database_url) as connection:
        owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,region.region_code,
                      region.residency_policy_version,
                      (SELECT account_id FROM global_accounts ORDER BY account_id LIMIT 1)
                        account_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_regions region
                   ON region.workspace_id=binding.workspace_id
                  AND region.tenant_id=binding.tenant_id
                WHERE binding.status='active' AND region.status='active'
                ORDER BY binding.workspace_id LIMIT 1"""
        ).fetchone()
        cell = connection.execute(
            "SELECT gateway_cell_id FROM gateway_cells WHERE region_code=? "
            "AND status='active' AND health_status='healthy' ORDER BY gateway_cell_id LIMIT 1",
            (owner["region_code"],),
        ).fetchone()
        assert owner is not None and cell is not None
        operator_id = "pto_" + "7" * 32
        connection.execute(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,
                 created_at,updated_at)
               VALUES(?,?,'active',1,NULL,
                 strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                 strftime('%Y-%m-%dT%H:%M:%fZ','now'))""",
            (operator_id, owner["account_id"]),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
                 operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
               VALUES(?,'platform_owner','active',NULL,
                 strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL)""",
            (operator_id,),
        )
        connection.execute(
            """INSERT INTO workspace_gateway_placements(
                 workspace_gateway_placement_id,workspace_id,tenant_id,primary_region_code,
                 primary_gateway_cell_id,failover_policy,failover_gateway_cell_id,
                 residency_policy_version,revision,snapshot_generation,updated_by_operator_id,
                 idempotency_sha256,request_sha256,created_at,updated_at,
                 governance_generation)
               VALUES(?,?,?, ?,?,'deny',NULL,?,1,1,?,?,?,
                 strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                 strftime('%Y-%m-%dT%H:%M:%fZ','now'),1)""",
            (
                "wgp_" + "7" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                owner["region_code"],
                cell["gateway_cell_id"],
                owner["residency_policy_version"],
                operator_id,
                "7" * 64,
                "8" * 64,
            ),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT COUNT(*) FROM residency_policies WHERE status='active'"
        ).fetchone()[0] > 0
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> tuple[AppConfig, str, str, str]:
    database = tmp_path / "governance.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    with connect_database(config.database_url) as connection:
        owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,account.account_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_memberships membership
                   ON membership.workspace_id=binding.workspace_id
                  AND membership.status='active' AND membership.role_code='owner'
                 JOIN global_accounts account ON account.account_id=membership.account_id
                 ORDER BY binding.workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
    return config, str(owner["workspace_id"]), str(owner["tenant_id"]), str(owner["account_id"])


def _workspace_actor(
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    role: str,
    *permissions: str,
) -> WorkspaceSessionContext:
    marker = (role[0] if role else "x").lower()
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + marker * 32,
        global_session_id="gss_" + marker * 32,
        account_id=account_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        membership_id="wmb_" + marker * 32,
        legacy_user_id="usr_" + marker * 32,
        membership_role=role,
        legacy_role="owner" if role == "owner" else "viewer",
        account_email=f"{role}@example.invalid",
        display_name=role,
        tenant_slug="default",
        tenant_name="Default",
        permission_codes=permissions,
        permission_revision=28,
        authorization_fingerprint=marker * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
    )


def _platform_actor(account_id: str, *permissions: str) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + "p" * 32,
        operator_id="pto_" + "p" * 32,
        account_id=account_id,
        global_session_id="gss_" + "p" * 32,
        factor_id="mfa_" + "p" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_compliance",),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    holder: dict[str, WorkspaceSessionContext | PlatformContext],
):
    monkeypatch.setattr(
        governance_routes,
        "authenticate_workspace_session_context",
        lambda *args, **kwargs: holder["workspace"],
    )
    monkeypatch.setattr(
        governance_service,
        "authenticate_workspace_session_in_transaction",
        lambda *args, **kwargs: holder["workspace"],
    )
    monkeypatch.setattr(
        governance_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["platform"],
    )
    monkeypatch.setattr(
        governance_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: holder["platform"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-117 Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _workspace_headers(*, key: str | None = None, revision: int | None = None):
    headers = {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; "
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _platform_headers() -> dict[str, str]:
    return {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; "
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
        )
    }


def _route_request(cookie: str) -> Request:
    app = FastAPI()
    app.state.config = object()
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"cookie", cookie.encode("ascii"))],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 8001),
            "app": app,
        }
    )


def test_global_bootstrap_cookie_can_coexist_without_granting_governance_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace_actor(
        "wsp_" + "1" * 32,
        "tenant_" + "1" * 20,
        "acc_" + "1" * 32,
        "owner",
        "data_governance.read",
    )
    platform = _platform_actor("acc_" + "1" * 32, "platform.data_residency.read")
    monkeypatch.setattr(
        governance_routes,
        "authenticate_workspace_session_context",
        lambda *args, **kwargs: workspace,
    )
    monkeypatch.setattr(
        governance_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: platform,
    )
    workspace_request = _route_request(
        f"{SESSION_COOKIE}={GLOBAL_TOKEN}; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
    )
    platform_request = _route_request(
        f"{SESSION_COOKIE}={GLOBAL_TOKEN}; {PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
    )
    assert governance_routes.require_workspace_governance(workspace_request) is workspace
    assert governance_routes.require_platform_evidence(platform_request) is platform

    with pytest.raises(ApiError) as workspace_missing:
        governance_routes.require_workspace_governance(
            _route_request(f"{SESSION_COOKIE}={GLOBAL_TOKEN}")
        )
    assert workspace_missing.value.code == "workspace_session.required"
    with pytest.raises(ApiError) as platform_missing:
        governance_routes.require_platform_evidence(
            _route_request(f"{SESSION_COOKIE}={GLOBAL_TOKEN}")
        )
    assert platform_missing.value.code == "platform.session_required"


def _policy_payload(current: dict[str, object], *, confirmed: bool) -> dict[str, object]:
    retention = []
    for item in current["retention_policies"]:
        projected = {
            "classification_code": item["classification_code"],
            "retention_days": item["retention_days"],
            "deletion_mode": item["deletion_mode"],
            "backup_grace_days": item["backup_grace_days"],
        }
        if item["classification_code"] == "prompt":
            projected["retention_days"] = int(projected["retention_days"]) + 1
        retention.append(projected)
    policy = current["policy"]
    return {
        "allowed_residency_groups": policy["allowed_residency_groups"],
        "primary_residency_group": policy["primary_residency_group"],
        "cross_region_mode": policy["cross_region_mode"],
        "retention_policies": retention,
        "reason_code": "retention_update",
        "confirmed": confirmed,
    }


def _business_counts(config: AppConfig) -> tuple[int, ...]:
    with connect_database(config.database_url) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "residency_policies",
                "retention_policies",
                "data_governance_commands",
                "data_governance_events",
                "config_snapshots",
            )
        )


def test_fresh_seed_workspace_session_restores_governance_permissions(
    base_database: Path,
) -> None:
    config = _config(_url(base_database))
    with connect_database(config.database_url) as connection:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM workspace_memberships "
                "WHERE role_code='owner' AND status='active' ORDER BY workspace_id LIMIT 1"
            ).fetchone()[0]
        )
    seed = json.loads(
        (Path(__file__).resolve().parents[3] / "infra" / "seeds" / "default_seed.json")
        .read_text(encoding="utf-8")
    )
    password = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD", seed["admin"]["password"])
    identity = login_global_account(
        config,
        email=seed["admin"]["email"],
        password=password,
        request_id="req_prd117_workspace_roundtrip_login",
        trace_id="trc_prd117_workspace_roundtrip_login",
        user_agent="prd117-test",
        client_host="127.0.0.1",
    )
    _, workspace_token, _ = switch_workspace(
        config,
        global_session_token=identity.session_token,
        global_csrf_token=identity.csrf_token,
        workspace_id=workspace_id,
        request_id="req_prd117_workspace_roundtrip_switch",
        trace_id="trc_prd117_workspace_roundtrip_switch",
        client_host="127.0.0.1",
    )

    restored = get_workspace_session(
        config,
        session_token=workspace_token,
        request_id="req_prd117_workspace_roundtrip_restore",
        trace_id="trc_prd117_workspace_roundtrip_restore",
    )
    context = authenticate_workspace_session_context(config, workspace_token)

    assert restored.context.workspace_id == workspace_id
    assert context.membership_role == "owner"
    assert {
        "data_governance.read",
        "data_governance.manage",
        "legal_hold.read",
        "legal_hold.manage",
    } <= set(context.permission_codes)
    legacy = context.as_auth_context()
    assert set(context.permission_codes) - set(legacy.user.permission_codes) == {
        "data_governance.read",
        "data_governance.manage",
        "legal_hold.read",
        "legal_hold.manage",
    }
    assert set(legacy.extension_permission_codes) == {
        "data_governance.read",
        "data_governance.manage",
        "legal_hold.read",
        "legal_hold.manage",
    }
    navigation = get_console_navigation(
        config,
        legacy,
        requested_active_item_id=None,
        workspace_scoped=True,
        request_id="req_prd117_workspace_roundtrip_navigation",
        trace_id="trc_prd117_workspace_roundtrip_navigation",
    )
    assert navigation.navigation_items
    assert set(navigation.permission_codes) == set(context.permission_codes)
    assert navigation.authorization_fingerprint == context.authorization_fingerprint


def test_concurrent_stale_workspace_session_touches_serialize(
    base_database: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "workspace-touch.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    with connect_database(config.database_url) as connection:
        workspace_id = str(
            connection.execute(
                "SELECT workspace_id FROM workspace_memberships "
                "WHERE role_code='owner' AND status='active' ORDER BY workspace_id LIMIT 1"
            ).fetchone()[0]
        )
    seed = json.loads(
        (Path(__file__).resolve().parents[3] / "infra" / "seeds" / "default_seed.json")
        .read_text(encoding="utf-8")
    )
    identity = login_global_account(
        config,
        email=seed["admin"]["email"],
        password=os.environ.get("METAHUB_SEED_ADMIN_PASSWORD", seed["admin"]["password"]),
        request_id="req_prd117_concurrent_touch_login",
        trace_id="trc_prd117_concurrent_touch_login",
        user_agent="prd117-test",
        client_host="127.0.0.1",
    )
    _, workspace_token, _ = switch_workspace(
        config,
        global_session_token=identity.session_token,
        global_csrf_token=identity.csrf_token,
        workspace_id=workspace_id,
        request_id="req_prd117_concurrent_touch_switch",
        trace_id="trc_prd117_concurrent_touch_switch",
        client_host="127.0.0.1",
    )
    with connect_database(config.database_url) as connection:
        connection.execute(
            "UPDATE workspace_sessions SET last_seen_at=datetime('now','-2 minutes'),"
            "updated_at=datetime('now','-2 minutes') WHERE status='active'"
        )
        connection.commit()

    def restore(index: int) -> str:
        return get_workspace_session(
            config,
            session_token=workspace_token,
            request_id=f"req_prd117_concurrent_touch_{index:02d}",
            trace_id=f"trc_prd117_concurrent_touch_{index:02d}",
        ).context.workspace_id

    with ThreadPoolExecutor(max_workers=16) as executor:
        restored = list(executor.map(restore, range(32)))
    assert restored == [workspace_id] * 32


@pytest.mark.anyio
async def test_policy_simulation_apply_manifest_and_transaction_rollback(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    owner = _workspace_actor(
        workspace_id,
        tenant_id,
        account_id,
        "owner",
        "data_governance.read",
        "data_governance.manage",
        "legal_hold.read",
        "legal_hold.manage",
    )
    holder = {
        "workspace": owner,
        "platform": _platform_actor(account_id, "platform.data_residency.read"),
    }
    app = _app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        current = await client.get(
            f"/workspaces/{workspace_id}/data-governance", headers=_workspace_headers()
        )
        assert current.status_code == 200, current.text
        assert current.headers["etag"] == '"1"'
        assert current.headers["cache-control"] == "no-store, private"
        before = _business_counts(config)
        payload = _policy_payload(current.json(), confirmed=False)
        simulated = await client.patch(
            f"/workspaces/{workspace_id}/data-governance",
            headers=_workspace_headers(key="prd117-policy-simulate-0001", revision=1),
            json=payload,
        )
        assert simulated.status_code == 200, simulated.text
        simulation = simulated.json()["simulation"]
        assert simulation["next_generation"] == 2
        assert simulated.json()["applied"] is False
        assert _business_counts(config) == before

        payload["confirmed"] = True
        applied = await client.patch(
            f"/workspaces/{workspace_id}/data-governance",
            headers=_workspace_headers(key="prd117-policy-apply-0001", revision=1),
            json=payload,
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        assert applied_body["applied"] is True
        assert applied_body["policy"]["generation"] == 2
        assert applied_body["simulation"] == simulation
        assert applied.headers["etag"] == '"2"'

    with connect_database(config.database_url) as connection:
        row = connection.execute(
            """SELECT head.*,snapshot.manifest_envelope,snapshot.manifest_sha256
                 FROM config_snapshot_heads head
                 JOIN config_snapshots snapshot
                   ON snapshot.config_snapshot_id=head.config_snapshot_id
                WHERE head.workspace_id=? AND head.tenant_id=?""",
            (workspace_id, tenant_id),
        ).fetchone()
        assert row is not None
        manifest = open_config_value(
            config,
            str(row["manifest_envelope"]),
            expected_sha256=str(row["manifest_sha256"]),
            owner_id=str(row["config_snapshot_id"]),
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            kind="manifest",
        )
        assert manifest["data_governance"]["generation"] == 2
        assert manifest["data_governance"]["sha256"] == simulation["candidate_sha256"]
        assert len(manifest["data_governance"]["retention"]) == 6
        assert int(row["revocation_watermark"]) >= 1
        assert connection.execute(
            "SELECT COUNT(*) FROM residency_policies WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT governance_generation FROM workspace_gateway_placements "
            "WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()[0] == 2

    failing = config.model_copy(update={"tenant_data_encryption_secret": None})
    failing_app = _app(monkeypatch, failing, holder)
    with connect_database(config.database_url) as connection:
        revision = int(
            connection.execute(
                "SELECT revision FROM residency_policies WHERE workspace_id=? AND status='active'",
                (workspace_id,),
            ).fetchone()[0]
        )
    rollback_payload = {**payload, "reason_code": "compliance_update"}
    rollback_payload["retention_policies"][0]["retention_days"] += 1
    async with AsyncClient(
        transport=ASGITransport(app=failing_app), base_url="https://testserver"
    ) as client:
        failed = await client.patch(
            f"/workspaces/{workspace_id}/data-governance",
            headers=_workspace_headers(key="prd117-policy-rollback-0001", revision=revision),
            json=rollback_payload,
        )
        assert failed.status_code == 503
        assert failed.json()["code"] == "config_distribution.kms_unavailable"
    with connect_database(config.database_url) as connection:
        active = connection.execute(
            "SELECT revision,generation FROM residency_policies "
            "WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        ).fetchone()
        assert tuple(active) == (revision, 2)
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_commands WHERE action_code='policy.apply'"
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_policy_simulation_marks_missing_gateway_placement_for_migration(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    owner = _workspace_actor(
        workspace_id,
        tenant_id,
        account_id,
        "owner",
        "data_governance.read",
        "data_governance.manage",
        "legal_hold.read",
        "legal_hold.manage",
    )
    holder = {
        "workspace": owner,
        "platform": _platform_actor(account_id, "platform.data_residency.read"),
    }
    with connect_database(config.database_url) as connection:
        connection.execute("DROP TRIGGER trg_gateway_placements_delete_prd101")
        assert connection.execute(
            "DELETE FROM workspace_gateway_placements "
            "WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).rowcount == 1
        connection.commit()

    app = _app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        current = await client.get(
            f"/workspaces/{workspace_id}/data-governance", headers=_workspace_headers()
        )
        assert current.status_code == 200, current.text
        before = _business_counts(config)
        simulated = await client.patch(
            f"/workspaces/{workspace_id}/data-governance",
            headers=_workspace_headers(key="prd117-policy-no-placement-0001", revision=1),
            json=_policy_payload(current.json(), confirmed=False),
        )

    assert simulated.status_code == 200, simulated.text
    body = simulated.json()
    simulation = body["simulation"]
    assert len(simulation["candidate_sha256"]) == 64
    assert set(simulation["candidate_sha256"]) <= set("0123456789abcdef")
    assert simulation["next_generation"] == 2
    assert simulation["placement_compliant"] is False
    assert simulation["migration_required_components"] == ["disaster_recovery", "gateway"]
    assert body["applied"] is False
    assert _business_counts(config) == before


@pytest.mark.anyio
async def test_role_matrix_and_credential_domain_isolation(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    holder: dict[str, WorkspaceSessionContext | PlatformContext] = {
        "workspace": _workspace_actor(
            workspace_id,
            tenant_id,
            account_id,
            "owner",
            "data_governance.read",
            "data_governance.manage",
            "legal_hold.read",
            "legal_hold.manage",
        ),
        "platform": _platform_actor(account_id, "platform.data_residency.read"),
    }
    app = _app(monkeypatch, config, holder)
    matrix = {
        "owner": (True, True, True),
        "security_admin": (True, True, False),
        "legal_admin": (True, False, True),
        "auditor": (True, False, False),
        "developer": (False, False, False),
    }
    permissions = {
        "owner": (
            "data_governance.read",
            "data_governance.manage",
            "legal_hold.read",
            "legal_hold.manage",
        ),
        "security_admin": (
            "data_governance.read",
            "data_governance.manage",
            "legal_hold.read",
        ),
        "legal_admin": ("data_governance.read", "legal_hold.read", "legal_hold.manage"),
        "auditor": ("data_governance.read", "legal_hold.read"),
        "developer": (),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        owner_view = await client.get(
            f"/workspaces/{workspace_id}/data-governance", headers=_workspace_headers()
        )
        payload = _policy_payload(owner_view.json(), confirmed=False)
        for index, (role, expected) in enumerate(matrix.items(), 1):
            holder["workspace"] = _workspace_actor(
                workspace_id, tenant_id, account_id, role, *permissions[role]
            )
            read = await client.get(
                f"/workspaces/{workspace_id}/data-governance", headers=_workspace_headers()
            )
            assert (read.status_code == 200) is expected[0]
            manage = await client.patch(
                f"/workspaces/{workspace_id}/data-governance",
                headers=_workspace_headers(key=f"prd117-role-policy-{index:04d}", revision=1),
                json=payload,
            )
            assert (manage.status_code == 200) is expected[1]
            hold = await client.post(
                f"/workspaces/{workspace_id}/legal-holds",
                headers=_workspace_headers(key=f"prd117-role-hold-{index:04d}"),
                json={
                    "classification_code": "file",
                    "scope_kind": "object_digest",
                    "selector_sha256": f"{index:x}" * 64,
                    "reason_code": "preservation_notice",
                    "expires_at": None,
                    "confirmed": True,
                },
            )
            assert (hold.status_code == 201) is expected[2]

        holder["workspace"] = _workspace_actor(
            workspace_id,
            tenant_id,
            account_id,
            "owner",
            "data_governance.read",
            "data_governance.manage",
            "legal_hold.read",
            "legal_hold.manage",
        )
        mixed_workspace = await client.get(
            f"/workspaces/{workspace_id}/data-governance",
            headers={
                **_workspace_headers(),
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
                ),
            },
        )
        assert mixed_workspace.status_code == 400
        assert mixed_workspace.json()["code"] == "workspace_session.credential_conflict"

        evidence = await client.get(
            "/platform/data-residency/evidence?limit=1", headers=_platform_headers()
        )
        assert evidence.status_code == 200
        serialized = json.dumps(evidence.json(), ensure_ascii=True).lower()
        assert all(
            forbidden not in serialized
            for forbidden in ("prompt_text", "response_text", "object_key", "provider_key", "token")
        )
        mixed_platform = await client.get(
            "/platform/data-residency/evidence",
            headers={
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
                )
            },
        )
        assert mixed_platform.status_code == 400
        assert mixed_platform.json()["code"] == "platform.credential_conflict"


@pytest.mark.anyio
async def test_legal_hold_blocks_scheduler_job_then_release_requeues(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    legal = _workspace_actor(
        workspace_id,
        tenant_id,
        account_id,
        "legal_admin",
        "data_governance.read",
        "legal_hold.read",
        "legal_hold.manage",
    )
    holder = {
        "workspace": legal,
        "platform": _platform_actor(account_id, "platform.data_residency.read"),
    }
    app = _app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            f"/workspaces/{workspace_id}/legal-holds",
            headers=_workspace_headers(key="prd117-scheduler-hold-0001"),
            json={
                "classification_code": "audit",
                "scope_kind": "classification",
                "selector_sha256": None,
                "reason_code": "litigation",
                "expires_at": None,
                "confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        hold = created.json()["hold"]

        with connect_database(config.database_url) as connection:
            updated_at = datetime.fromisoformat(
                str(
                    connection.execute(
                        "SELECT MIN(updated_at) FROM retention_policies "
                        "WHERE workspace_id=? AND status='active'",
                        (workspace_id,),
                    ).fetchone()[0]
                ).replace("Z", "+00:00")
            )
        scheduler_now = updated_at + timedelta(days=2600)
        assert enqueue_due_deletion_jobs(config, now=scheduler_now) == 6
        with connect_database(config.database_url) as connection:
            due_at = datetime.fromisoformat(
                str(
                    connection.execute(
                        "SELECT MIN(due_at) FROM deletion_schedules WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchone()[0]
                ).replace("Z", "+00:00")
            )
            assert timedelta(0) <= scheduler_now - due_at < timedelta(hours=24)
        while run_platform_job_once(config, worker_code="prd117-retention-worker"):
            pass

        with connect_database(config.database_url) as connection:
            audit = connection.execute(
                "SELECT * FROM deletion_schedules WHERE workspace_id=? "
                "AND classification_code='audit'",
                (workspace_id,),
            ).fetchone()
            assert audit is not None
            assert audit["status"] == "blocked"
            assert audit["safe_outcome_code"] == "data_governance.hold_active"
            replay_payload = DataRetentionDeleteInput(
                schedule_id=str(audit["deletion_schedule_id"]),
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                policy_generation=int(audit["policy_generation"]),
                hold_generation=int(audit["hold_generation"]),
            )

            class ReplayContext:
                def __init__(self) -> None:
                    self.config = config
                    self.lease = type(
                        "ReplayLease", (), {"job_id": str(audit["platform_job_id"])}
                    )()

                def checkpoint(self, value: object) -> None:
                    assert value["schedule_id"] == audit["deletion_schedule_id"]

                def commit_effect(self, *args, **kwargs) -> bool:
                    return False

            assert governance_runtime._delete_handler(
                ReplayContext(), replay_payload
            ) == "data_governance.hold_active"
            assert connection.execute(
                "SELECT COUNT(*) FROM deletion_schedules WHERE workspace_id=? "
                "AND status='completed'",
                (workspace_id,),
            ).fetchone()[0] == 5

        released = await client.request(
            "DELETE",
            f"/workspaces/{workspace_id}/legal-holds/{hold['legal_hold_id']}",
            headers=_workspace_headers(
                key="prd117-scheduler-release-0001", revision=hold["revision"]
            ),
            json={"reason_code": "matter_closed", "confirmed": True},
        )
        assert released.status_code == 200, released.text
        assert released.json()["hold"]["status"] == "released"

    assert enqueue_due_deletion_jobs(config, now=scheduler_now) == 1
    assert run_platform_job_once(config, worker_code="prd117-retention-retry") is True
    assert run_platform_job_once(config, worker_code="prd117-retention-retry") is False
    with connect_database(config.database_url) as connection:
        audit = connection.execute(
            "SELECT * FROM deletion_schedules WHERE workspace_id=? "
            "AND classification_code='audit'",
            (workspace_id,),
        ).fetchone()
        assert audit["status"] == "completed"
        assert audit["safe_outcome_code"] == "data_governance.deletion_completed"
        assert audit["manifest_sha256"] is not None
        assert connection.execute(
            "SELECT COUNT(*) FROM data_governance_events "
            "WHERE workspace_id=? AND event_type='deletion.executed'",
            (workspace_id,),
        ).fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_scheduler_fails_closed_when_tenant_state_is_missing(
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, _account_id = runtime
    with connect_database(config.database_url) as connection:
        connection.execute("DROP TRIGGER trg_tenant_data_states_delete_prd089")
        connection.execute("DELETE FROM tenant_data_states WHERE tenant_id=?", (tenant_id,))
        connection.commit()
        retention = connection.execute(
            "SELECT * FROM retention_policies WHERE workspace_id=? AND status='active' "
            "ORDER BY classification_code LIMIT 1",
            (workspace_id,),
        ).fetchone()
        assert retention is not None
        current = datetime.fromisoformat(str(retention["updated_at"]).replace("Z", "+00:00"))
    assert enqueue_due_deletion_jobs(config, now=current + timedelta(days=2600)) == 0


def test_gateway_blocked_evidence_and_backup_generation_claim(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    platform_actor = replace(
        _platform_actor(
            account_id,
            "platform.gateway.manage",
            "platform.backup.create",
        ),
        operator_id="pto_" + "7" * 32,
    )
    monkeypatch.setattr(
        gateway_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: platform_actor,
    )
    monkeypatch.setattr(
        infrastructure_service,
        "authenticate_platform_session_in_transaction",
        lambda *args, **kwargs: platform_actor,
    )

    backup = infrastructure_service.create_backup(
        config,
        session_token=PLATFORM_TOKEN,
        csrf_token="platform-csrf",
        payload=BackupCreateRequest(
            backup_type="full",
            scope_kind="workspace",
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            retention_days=1,
            confirmed=True,
        ),
        idempotency_key="prd117-backup-generation-0001",
        request_id="request-prd117-backup-generation",
        trace_id="trace-prd117-backup-generation",
    )
    backup_id = backup.backup.backup_id
    with connect_database(config.database_url) as connection:
        policy = connection.execute(
            "SELECT * FROM residency_policies WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        ).fetchone()
        backup_row = connection.execute(
            "SELECT * FROM backup_sets WHERE backup_id=?", (backup_id,)
        ).fetchone()
        assert policy is not None and backup_row is not None
        assert int(backup_row["governance_generation"]) == int(policy["generation"])
        assert backup_row["residency_group"] == policy["primary_residency_group"]
        assert connection.execute(
            "SELECT COUNT(*) FROM residency_evidence WHERE workspace_id=? "
            "AND component_code='backup' AND outcome='compliant'",
            (workspace_id,),
        ).fetchone()[0] == 1
        connection.execute(
            "UPDATE backup_sets SET governance_generation=governance_generation+1 "
            "WHERE backup_id=?",
            (backup_id,),
        )
        connection.commit()

    assert infrastructure_service.claim_backup(config, worker_id="prd117-backup-worker") is None
    with connect_database(config.database_url) as connection:
        backup_row = connection.execute(
            "SELECT status,error_code FROM backup_sets WHERE backup_id=?", (backup_id,)
        ).fetchone()
        assert tuple(backup_row) == ("failed", "infrastructure.residency_conflict")
        blocked = connection.execute(
            "SELECT * FROM residency_evidence WHERE workspace_id=? "
            "AND component_code='backup' AND outcome='blocked' "
            "ORDER BY created_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        assert blocked is not None
        assert blocked["violation_code"] == "generation_stale"

        placement = connection.execute(
            """SELECT placement.*,cell.cell_code
                 FROM workspace_gateway_placements placement
                 JOIN gateway_cells cell
                   ON cell.gateway_cell_id=placement.primary_gateway_cell_id
                WHERE placement.workspace_id=?""",
            (workspace_id,),
        ).fetchone()
        assert placement is not None
        connection.execute("DROP TRIGGER trg_residency_policies_transition_prd117")
        connection.execute(
            """UPDATE residency_policies
                  SET allowed_residency_groups_json='["blocked-group"]',
                      primary_residency_group='blocked-group'
                WHERE workspace_id=? AND status='active'""",
            (workspace_id,),
        )
        connection.commit()

    with pytest.raises(ApiError) as blocked_placement:
        gateway_service.update_workspace_placement(
            config,
            session_token=PLATFORM_TOKEN,
            csrf_token="platform-csrf",
            workspace_id=workspace_id,
            expected_revision=int(placement["revision"]),
            idempotency_key="prd117-gateway-blocked-0001",
            payload=GatewayPlacementUpdateRequest(
                primary_region_code=str(placement["primary_region_code"]),
                primary_cell_code=str(placement["cell_code"]),
                failover_policy="deny",
                failover_cell_code=None,
                residency_policy_version=str(placement["residency_policy_version"]),
                reason_code="capacity_rebalance",
                confirmed=True,
            ),
            request_id="request-prd117-gateway-blocked",
            trace_id="trace-prd117-gateway-blocked",
        )
    assert blocked_placement.value.code == "platform_gateway.residency_conflict"
    with connect_database(config.database_url) as connection:
        assert connection.execute(
            "SELECT revision FROM workspace_gateway_placements WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0] == placement["revision"]
        evidence = connection.execute(
            "SELECT * FROM residency_evidence WHERE workspace_id=? "
            "AND component_code='gateway' AND outcome='blocked' "
            "ORDER BY created_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        assert evidence is not None
        assert evidence["violation_code"] == "region_not_allowed"


@pytest.mark.anyio
async def test_legal_hold_and_tenant_deletion_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, account_id = runtime
    holder = {
        "workspace": _workspace_actor(
            workspace_id,
            tenant_id,
            account_id,
            "legal_admin",
            "data_governance.read",
            "legal_hold.read",
            "legal_hold.manage",
        ),
        "platform": _platform_actor(account_id, "platform.data_residency.read"),
    }
    app = _app(monkeypatch, config, holder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        created = await client.post(
            f"/workspaces/{workspace_id}/legal-holds",
            headers=_workspace_headers(key="prd117-race-hold-first-0001"),
            json={
                "classification_code": "prompt",
                "scope_kind": "classification",
                "selector_sha256": None,
                "reason_code": "litigation",
                "expires_at": None,
                "confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        hold = created.json()["hold"]
        with connect_database(config.database_url) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT deletion_generation FROM data_governance_fences "
                "WHERE workspace_id=? AND tenant_id=?",
                (workspace_id, tenant_id),
            ).fetchone()[0]
            with pytest.raises(ApiError) as held:
                claim_deletion_fence_in_transaction(
                    connection,
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                )
            assert held.value.code == "data_governance.hold_active"
            assert connection.execute(
                "SELECT deletion_generation FROM data_governance_fences "
                "WHERE workspace_id=? AND tenant_id=?",
                (workspace_id, tenant_id),
            ).fetchone()[0] == before
            connection.rollback()

        released = await client.request(
            "DELETE",
            f"/workspaces/{workspace_id}/legal-holds/{hold['legal_hold_id']}",
            headers=_workspace_headers(
                key="prd117-race-release-0001", revision=hold["revision"]
            ),
            json={"reason_code": "matter_closed", "confirmed": True},
        )
        assert released.status_code == 200, released.text

        with connect_database(config.database_url) as connection:
            connection.execute(
                """INSERT INTO deletion_requests(
                     deletion_request_id,tenant_id,actor_id_hash,request_id,trace_id,
                     idempotency_key_hash,request_fingerprint,status,tenant_revision,
                     confirmation_hash,retention_policy_version,schema_manifest_sha256,
                     generation,lease_owner_id,lease_token,lease_expires_at,created_at,updated_at)
                   VALUES(?,?,?,'request-prd117-race','trace-prd117-race',?,?,'deleting',1,
                     ?,1,?,2,'prd117-race-worker','prd117-race-lease',
                     datetime('now','+2 minutes'),datetime('now'),datetime('now'))""",
                (
                    "del_" + "9" * 32,
                    tenant_id,
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                    "4" * 64,
                    "5" * 64,
                ),
            )
            connection.commit()

        blocked = await client.post(
            f"/workspaces/{workspace_id}/legal-holds",
            headers=_workspace_headers(key="prd117-race-delete-first-0001"),
            json={
                "classification_code": "file",
                "scope_kind": "classification",
                "selector_sha256": None,
                "reason_code": "preservation_notice",
                "expires_at": None,
                "confirmed": True,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "data_governance.deletion_conflict"
    with connect_database(config.database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM legal_holds WHERE workspace_id=? "
            "AND classification_code='file' AND status='active'",
            (workspace_id,),
        ).fetchone()[0] == 0


def test_residency_policy_decision_p95_is_below_ten_milliseconds(
    runtime: tuple[AppConfig, str, str, str],
) -> None:
    config, workspace_id, tenant_id, _account_id = runtime
    samples: list[float] = []
    with connect_database(config.database_url) as connection:
        region_code = str(
            connection.execute(
                "SELECT region_code FROM workspace_regions WHERE workspace_id=? AND tenant_id=?",
                (workspace_id, tenant_id),
            ).fetchone()[0]
        )
        for _index in range(250):
            started = time.perf_counter_ns()
            policy, group = require_region_allowed_in_transaction(
                connection,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                region_code=region_code,
            )
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        assert policy.generation == 1
        assert group in policy.allowed_groups
    p95 = statistics.quantiles(samples, n=100, method="inclusive")[94]
    assert p95 < 10.0
