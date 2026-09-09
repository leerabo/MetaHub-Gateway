"""PRD-146 navigation, task home, preferences, and advanced-tool contracts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import metahub_control.workspace_access.service as workspace_access_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.console import routes as console_routes
from metahub_control.console_experience import scheduler as console_scheduler
from metahub_control.identity.models import GlobalAccountSummary, GlobalSessionSummary
from metahub_control.identity.service import (
    CSRF_COOKIE as IDENTITY_CSRF_COOKIE,
)
from metahub_control.identity.service import (
    SESSION_COOKIE as IDENTITY_SESSION_COOKIE,
)
from metahub_control.identity.service import AuthenticatedIdentity
from metahub_control.platform_access.constants import PLATFORM_SESSION_COOKIE
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_control.workspace_navigation import routes, service
from metahub_control.workspace_navigation import scheduler as navigation_scheduler
from metahub_control.workspace_navigation.catalog import (
    CONTRACT_SPECS,
    LEGACY_SOURCE_IDS_0137,
)
from metahub_control.workspace_navigation.routes import router
from metahub_control.workspace_navigation.scheduler import (
    NavigationLeaseProof,
    run_navigation_queue_once,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.migrate import _default_migrations_dir, _load_migration_files
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from starlette.requests import Request

ORIGIN = "http://localhost:5173"
STAMP = "2026-09-03T08:00:00Z"
NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _url(path) -> str:
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
            loaded_at=NOW,
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        approval_hmac_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("prd146-api") / "prd146.sqlite3"
    database_url = _url(database)
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        shutil.copyfile(Path(supplied).resolve(strict=True), database)
    apply_migrations(database_url)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        seed = apply_seed_data(database_url)
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        assert {tuple(row) for row in connection.execute(
            "SELECT version,checksum FROM schema_migrations WHERE status='applied'"
        )} == {
            (migration.version, migration.checksum)
            for migration in _load_migration_files(_default_migrations_dir())
        }
        owner = connection.execute(
            """SELECT membership.*,tenant.slug,tenant.name,user.email,user.display_name
               FROM workspace_memberships membership
               JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
               JOIN users user ON user.tenant_id=membership.tenant_id
                 AND user.user_id=membership.legacy_user_id
               WHERE membership.tenant_id=? AND membership.role_code='owner'
                 AND membership.status='active' LIMIT 1""",
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        connection.execute("BEGIN")
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=owner["account_id"],
            workspace_id=owner["workspace_id"],
            tenant_id=owner["tenant_id"],
        )
        connection.execute(
            """INSERT INTO edge_deployments(
                 edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
                 status,revision,revocation_generation,agent_version,health_code,
                 last_connected_at,certificate_expires_at,created_by_account_id,
                 revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
               VALUES(?,?,?,'PRD-146 Edge','cn-east-1','connected','active',1,1,
                 '1.0.0','healthy',?,NULL,?,NULL,NULL,?,?,NULL)""",
            (
                "edp_" + "6" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                STAMP,
                owner["account_id"],
                STAMP,
                STAMP,
            ),
        )
        connection.commit()
    expires = NOW + timedelta(hours=1)
    workspace_token = "w" * 43
    identity_token = "i" * 43
    workspace = WorkspaceSessionContext(
        "wss_" + "6" * 32,
        "gss_" + "6" * 32,
        owner["account_id"],
        owner["workspace_id"],
        owner["tenant_id"],
        owner["workspace_membership_id"],
        owner["legacy_user_id"],
        owner["role_code"],
        "admin",
        owner["email"],
        owner["display_name"],
        owner["slug"],
        owner["name"],
        snapshot.permission_codes,
        snapshot.permission_revision,
        snapshot.authorization_fingerprint,
        expires,
        workspace_token,
        snapshot.authorization_fingerprint,
    )
    identity = AuthenticatedIdentity(
        account_id=owner["account_id"],
        session_id="gss_" + "6" * 32,
        account=GlobalAccountSummary(
            email=owner["email"],
            display_name=owner["display_name"],
            status="active",
            mfa_configured=False,
        ),
        current_session=GlobalSessionSummary(
            session_id="gss_" + "6" * 32,
            device_family="chrome",
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=expires,
            is_current=True,
            revoke_generation=1,
        ),
    )
    return {
        "config": config,
        "database": database,
        "identity": identity,
        "identity_token": identity_token,
        "workspace": workspace,
        "workspace_token": workspace_token,
    }


def _api_app(monkeypatch: pytest.MonkeyPatch, runtime):
    monkeypatch.setattr(
        routes,
        "authenticate_workspace_session_context",
        lambda _config, _token, **_kwargs: runtime["workspace"],
    )
    monkeypatch.setattr(
        routes,
        "authenticate_global_session",
        lambda _config, _token, **_kwargs: runtime["identity"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda _connection, _config, **_kwargs: runtime["workspace"],
    )
    monkeypatch.setattr(
        service,
        "authenticate_global_session_in_transaction",
        lambda _connection, _config, **_kwargs: (runtime["identity"], NOW),
    )
    monkeypatch.setattr(
        workspace_access_service,
        "authenticate_workspace_session_as_auth_context",
        lambda _config, _token, **_kwargs: runtime["workspace"].as_auth_context(),
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-146 Workspace Navigation Test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        extra_cors_expose_headers=("ETag", "Vary", "X-Permission-Fingerprint"),
        cors_allow_credentials=True,
    )
    app.state.config = runtime["config"]
    app.include_router(router)
    app.include_router(console_routes.router)
    return app


def _workspace_headers(runtime, *, write: bool = False) -> dict[str, str]:
    cookie = f"{WORKSPACE_SESSION_COOKIE}={runtime['workspace_token']}"
    headers = {"Cookie": cookie}
    if write:
        csrf = "c" * 43
        headers.update(
            {
                "Cookie": f"{cookie}; {WORKSPACE_CSRF_COOKIE}={csrf}",
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": csrf,
            }
        )
    return headers


def _identity_headers(
    runtime,
    *,
    revision: int,
    idempotency_key: str,
) -> dict[str, str]:
    csrf = "d" * 43
    workspace_csrf = "c" * 43
    return {
        "Cookie": (
            f"{IDENTITY_SESSION_COOKIE}={runtime['identity_token']}; "
            f"{IDENTITY_CSRF_COOKIE}={csrf}; "
            f"{WORKSPACE_SESSION_COOKIE}={runtime['workspace_token']}; "
            f"{WORKSPACE_CSRF_COOKIE}={workspace_csrf}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": csrf,
        "If-Match": f'"{revision}"',
        "Idempotency-Key": idempotency_key,
    }


def _run_navigation_queue(config: AppConfig, queue_code: str) -> bool:
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """UPDATE workspace_navigation_scheduler_state
               SET next_attempt_at='2020-01-01T00:00:00Z',
                 lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL
               WHERE queue_code=?""",
            (queue_code,),
        )
        connection.commit()
    lease = console_scheduler._claim_navigation(config, queue_code)
    assert lease is not None
    proof = NavigationLeaseProof(
        queue_code=queue_code,
        lease_owner_id=console_scheduler.WORKER_ID,
        fence_generation=lease.generation,
        lease_token_hmac=console_scheduler._navigation_lease_hmac(
            config,
            queue_code,
            lease.token,
        ),
    )
    worked = run_navigation_queue_once(config, proof)
    console_scheduler._release_navigation(config, lease, worked=worked)
    return worked


@pytest.mark.anyio
async def test_disconnected_request_bodies_are_fail_closed() -> None:
    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/identity/preferences/navigation",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
            ],
        },
        receive=receive,
    )
    with pytest.raises(ApiError) as raised:
        await routes._strict_json(request, "identity.request_invalid")
    assert raised.value.code == "identity.request_invalid"

    open_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/workspace/advanced-tools/debug_console/open",
            "headers": [(b"content-length", b"0")],
        },
        receive=receive,
    )
    with pytest.raises(ApiError) as raised:
        await routes._reject_body(open_request, "workspace_session.request_invalid")
    assert raised.value.code == "workspace_session.request_invalid"


def test_scheduler_lost_lease_rolls_back_late_projection_write(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    config = runtime["config"].model_copy(
        update={"workspace_navigation_scheduler_enabled": True}
    )
    queue_code = "workspace_navigation_projection"
    lease = console_scheduler._claim_navigation(config, queue_code)
    assert lease is not None
    proof = NavigationLeaseProof(
        queue_code=queue_code,
        lease_owner_id=console_scheduler.WORKER_ID,
        fence_generation=lease.generation,
        lease_token_hmac=console_scheduler._navigation_lease_hmac(
            config,
            queue_code,
            lease.token,
        ),
    )
    with closing(connect_database(config.database_url)) as connection:
        before = tuple(
            connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in (
                "workspace_navigation_contract_snapshots",
                "workspace_navigation_projection_versions",
                "workspace_navigation_preferences",
            )
        )
    original = navigation_scheduler._require_lease
    checks = 0

    def lose_before_commit(connection, current_proof) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            connection.execute(
                """UPDATE workspace_navigation_scheduler_state
                   SET fence_generation=fence_generation+1
                   WHERE queue_code=?""",
                (queue_code,),
            )
        original(connection, current_proof)

    monkeypatch.setattr(navigation_scheduler, "_require_lease", lose_before_commit)
    with pytest.raises(ApiError) as lost_lease:
        run_navigation_queue_once(config, proof)
    assert lost_lease.value.code == "platform.unavailable"
    monkeypatch.setattr(navigation_scheduler, "_require_lease", original)
    with closing(connect_database(config.database_url)) as connection:
        after = tuple(
            connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in (
                "workspace_navigation_contract_snapshots",
                "workspace_navigation_projection_versions",
                "workspace_navigation_preferences",
            )
        )
    assert checks == 2
    assert after == before
    console_scheduler._release_navigation(config, lease, worked=False)


def test_scheduler_fenced_backfill_outbox_and_reconciliation(runtime) -> None:
    assert runtime["config"].workspace_navigation_scheduler_enabled is False
    config = runtime["config"].model_copy(update={"workspace_navigation_scheduler_enabled": True})
    for queue_code in (
        "workspace_navigation_projection",
        "role_task_home",
        "advanced_tool_entitlement",
    ):
        for _ in range(20):
            if not _run_navigation_queue(config, queue_code):
                break
        else:
            pytest.fail(f"{queue_code} did not converge")
    workspace = runtime["workspace"]
    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_contract_items"
            ).fetchone()[0]
            == 68
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_preference_source_mappings"
            ).fetchone()[0]
            == connection.execute("SELECT count(*) FROM user_preferences").fetchone()[0]
        )
        assert (
            connection.execute("SELECT count(*) FROM workspace_role_task_home_heads").fetchone()[0]
            == connection.execute(
                """SELECT count(*) FROM workspace_memberships
               WHERE status='active' AND role_code!='service_account'
                 AND account_id IS NOT NULL AND legacy_user_id IS NOT NULL"""
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_advanced_tool_entitlement_heads"
            ).fetchone()[0]
            == 7
        )
        snapshot_id = connection.execute(
            """SELECT navigation_contract_snapshot_id
               FROM workspace_navigation_contract_heads
               WHERE workspace_id=? AND tenant_id=?""",
            (workspace.workspace_id, workspace.tenant_id),
        ).fetchone()[0]
        payload = json.dumps(
            {
                "contract_snapshot_id": snapshot_id,
                "navigation_epoch": 1,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_id = "wne_" + "7" * 32
        stamp = "2026-09-03T08:00:00Z"
        connection.execute(
            """INSERT INTO workspace_navigation_events(
                 workspace_navigation_event_id,workspace_id,tenant_id,event_type,
                 aggregate_type,aggregate_id,generation,event_key_sha256,
                 idempotency_key_hmac,request_sha256,payload_json,
                 request_id,trace_id,created_at)
               VALUES(?,?,?,'workspace_navigation.changed','navigation',?,1,?,
                 NULL,NULL,?,'scheduler-test','scheduler-test',?)""",
            (
                event_id,
                workspace.workspace_id,
                workspace.tenant_id,
                workspace.membership_id,
                hashlib.sha256(b"prd146-scheduler-event").hexdigest(),
                payload,
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO workspace_navigation_event_outbox(
                 workspace_navigation_event_outbox_id,
                 workspace_navigation_event_id,status,attempt_count,next_attempt_at,
                 lease_owner_id,lease_generation,lease_token_hmac,lease_expires_at,
                 dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
            ("wno_" + "7" * 32, event_id, stamp, stamp, stamp),
        )
        connection.commit()
    assert _run_navigation_queue(config, "workspace_navigation_outbox")
    assert _run_navigation_queue(config, "navigation_reconciliation")
    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_event_outbox
               WHERE status='dispatched'"""
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_reconciliations
               WHERE workspace_id=? AND tenant_id=?""",
                (workspace.workspace_id, workspace.tenant_id),
            ).fetchone()[0]
            == 7
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_reconciliations
               WHERE outcome!='matched'"""
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """SELECT state FROM workspace_navigation_migration_cursors
               WHERE source_kind='cutover'"""
            ).fetchone()[0]
            == "cutover"
        )


@pytest.mark.anyio
async def test_navigation_catalog_task_truth_and_placement(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            "UPDATE account_preferences SET locale='en-US' WHERE account_id=?",
            (runtime["workspace"].account_id,),
        )
        connection.execute(
            """UPDATE user_preferences SET locale='zh-CN'
               WHERE tenant_id=? AND user_id=?""",
            (runtime["workspace"].tenant_id, runtime["workspace"].legacy_user_id),
        )
        connection.commit()
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        navigation = await client.get(
            "/workspace/navigation",
            headers={**_workspace_headers(runtime), "Origin": ORIGIN},
        )
        task_home = await client.get(
            "/workspace/getting-started",
            headers=_workspace_headers(runtime),
        )
        advanced = await client.get(
            "/workspace/advanced-tools",
            headers=_workspace_headers(runtime),
        )
    assert navigation.status_code == task_home.status_code == advanced.status_code == 200
    exposed_headers = {
        item.strip().lower()
        for item in navigation.headers["access-control-expose-headers"].split(",")
    }
    assert navigation.headers["access-control-allow-origin"] == ORIGIN
    assert {"etag", "vary", "x-permission-fingerprint"} <= exposed_headers
    for response in (navigation, task_home, advanced):
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["etag"].startswith('"')
        assert response.headers["x-permission-fingerprint"]
    body = navigation.json()
    assert body["account_locale"] == "en-US"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        expected_locale = connection.execute(
            "SELECT locale FROM account_preferences WHERE account_id=? AND status='active'",
            (runtime["workspace"].account_id,),
        ).fetchone()[0]
    assert body["account_locale"] == expected_locale
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        compatible = await client.get(
            "/console/navigation",
            headers=_workspace_headers(runtime),
        )
        incompatible_query = await client.get(
            "/console/navigation?active_item_id=providers",
            headers=_workspace_headers(runtime),
        )
    assert compatible.status_code == 200
    compatible_body = compatible.json()
    assert {
        key: value
        for key, value in compatible_body.items()
        if key not in {"request_id", "trace_id"}
    } == {
        key: value
        for key, value in body.items()
        if key not in {"request_id", "trace_id"}
    }
    assert compatible_body["request_id"].startswith("req_")
    assert compatible_body["trace_id"].startswith("trc_")
    assert compatible.headers["cache-control"] == "no-store, private"
    assert compatible.headers["etag"] == navigation.headers["etag"]
    assert (
        compatible.headers["x-permission-fingerprint"]
        == navigation.headers["x-permission-fingerprint"]
    )
    assert "navigation_items" not in compatible_body
    assert "permission_codes" not in compatible_body
    assert "gateway_base_url" not in compatible_body
    assert incompatible_query.status_code == 422
    assert incompatible_query.json()["code"] == "workspace_session.request_invalid"
    primary_routes = {item["route_code"] for group in body["groups"] for item in group["items"]}
    all_routes = (
        primary_routes
        | set(body["detail_route_codes"])
        | set(body["developer_route_codes"])
        | set(body["account_action_codes"])
        | {tool["route_code"] for tool in advanced.json()["tools"]}
    )
    restored = {
        "workspace.gateway.providers",
        "workspace.gateway.private-endpoints",
        "workspace.gateway.provider-health",
        "workspace.advanced.provider-errors",
        "workspace.finance.disputes",
    }
    assert restored <= all_routes
    retired = {item.route_code for item in CONTRACT_SPECS if item.placement == "retired_internal"}
    assert retired.isdisjoint(all_routes)
    task_body = task_home.json()
    assert task_body["role_code"] == "owner"
    assert task_body["source_freshness"] == "fresh"
    assert all(task["source_freshness"] == "fresh" for task in task_body["tasks"])
    assert set(task_body["tasks"][0]) == {
        "task_code",
        "title_key",
        "description_key",
        "status",
        "priority",
        "action_route_code",
        "action_enabled",
        "source_freshness",
    }
    with closing(connect_database(runtime["config"].database_url)) as connection:
        snapshot_id = body["contract_snapshot_id"]
        counts = connection.execute(
            """SELECT source_kind,count(*) FROM workspace_navigation_contract_items
               WHERE navigation_contract_snapshot_id=? GROUP BY source_kind""",
            (snapshot_id,),
        ).fetchall()
        assert {row[0]: row[1] for row in counts} == {"excel": 64, "extra": 4}
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_projection_items
               WHERE workspace_navigation_projection_id=(
                 SELECT workspace_navigation_projection_id
                 FROM workspace_navigation_projection_heads
                 WHERE workspace_id=? AND tenant_id=?)""",
                (runtime["workspace"].workspace_id, runtime["workspace"].tenant_id),
            ).fetchone()[0]
            == 60
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_events
               WHERE event_type='advanced_tool.opened'"""
            ).fetchone()[0]
            == 0
        )
    assert len(CONTRACT_SPECS) == 68
    assert len(LEGACY_SOURCE_IDS_0137) == 63


@pytest.mark.anyio
async def test_navigation_preference_cas_and_exact_replay(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        navigation = await client.get(
            "/workspace/navigation",
            headers=_workspace_headers(runtime),
        )
        nav = navigation.json()
        payload = {
            "workspace_id": runtime["workspace"].workspace_id,
            "contract_snapshot_id": nav["contract_snapshot_id"],
            "last_primary_route_code": "workspace.home.overview",
            "expanded_group_code": "home",
            "sidebar_collapsed": not nav["preference"]["sidebar_collapsed"],
        }
        initial_revision = nav["preference"]["revision"]
        key = "prd146-navigation-preference-0001"
        headers = _identity_headers(
            runtime,
            revision=initial_revision,
            idempotency_key=key,
        )
        changed = await client.patch(
            "/identity/preferences/navigation",
            headers=headers,
            json=payload,
        )
        replay = await client.patch(
            "/identity/preferences/navigation",
            headers=headers,
            json=payload,
        )
        conflict = await client.patch(
            "/identity/preferences/navigation",
            headers=headers,
            json={**payload, "sidebar_collapsed": False},
        )
        stale = await client.patch(
            "/identity/preferences/navigation",
            headers=_identity_headers(
                runtime,
                revision=initial_revision,
                idempotency_key="prd146-navigation-preference-stale",
            ),
            json=payload,
        )
    assert changed.status_code == 200, changed.text
    assert changed.json()["changed"] is True
    assert changed.json()["idempotent_replay"] is False
    assert changed.json()["permission_fingerprint"] == (
        runtime["workspace"].authorization_fingerprint
    )
    assert changed.headers["etag"] == f'"{initial_revision + 1}"'
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "identity.idempotency_conflict"
    assert stale.status_code == 412
    assert stale.json()["code"] == "identity.revision_conflict"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        command = connection.execute(
            "SELECT * FROM workspace_navigation_preference_commands"
        ).fetchone()
        assert command is not None
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_preference_commands"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_events
               WHERE event_type='workspace_navigation.changed'"""
            ).fetchone()[0]
            == 1
        )
        contract = connection.execute(
            """SELECT snapshot.* FROM workspace_navigation_contract_heads head
               JOIN workspace_navigation_contract_snapshots snapshot
                 ON snapshot.navigation_contract_snapshot_id=head.navigation_contract_snapshot_id
               WHERE head.workspace_id=? AND head.tenant_id=?""",
            (
                runtime["workspace"].workspace_id,
                runtime["workspace"].tenant_id,
            ),
        ).fetchone()
        assert contract is not None
        with pytest.raises(ApiError) as drifted:
            service._preference_replay(
                command,
                runtime["workspace"],
                contract,
                changed.json()["membership_revision"] + 1,
                "request-drift",
                "trace-drift",
            )
        assert drifted.value.code == "identity.revision_conflict"
    assert service._idempotency_hmac(
        runtime["config"],
        "preference",
        runtime["workspace"].membership_id,
        key,
    ) != service._idempotency_hmac(
        runtime["config"],
        "preference",
        "wsm_" + "9" * 32,
        key,
    )


@pytest.mark.anyio
async def test_advanced_open_transport_event_and_projection_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
) -> None:
    app = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        listed = await client.get(
            "/workspace/advanced-tools",
            headers=_workspace_headers(runtime),
        )
        tool = next(
            item
            for item in listed.json()["tools"]
            if item["tool_code"] == "deployment_summary" and item["action_enabled"]
        )
        headers = _workspace_headers(runtime, write=True)
        headers["If-Match"] = f'"{tool["entitlement_revision"]}"'
        headers["Idempotency-Key"] = "prd146-advanced-tool-open-0001"
        opened = await client.post(
            f"/workspace/advanced-tools/{tool['tool_code']}/open",
            headers=headers,
        )
        replay = await client.post(
            f"/workspace/advanced-tools/{tool['tool_code']}/open",
            headers=headers,
        )
        with_body = await client.post(
            f"/workspace/advanced-tools/{tool['tool_code']}/open",
            headers={**headers, "Idempotency-Key": "prd146-advanced-with-body-0001"},
            json={},
        )
        bad_query = await client.get(
            "/workspace/navigation?role=owner",
            headers=_workspace_headers(runtime),
        )
        mixed = await client.get(
            "/workspace/navigation",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={runtime['workspace_token']}; "
                    f"{PLATFORM_SESSION_COOKIE}={'p' * 43}"
                )
            },
        )
    assert opened.status_code == 200, opened.text
    assert opened.json()["tool_code"] == tool["tool_code"]
    assert opened.json()["idempotent_replay"] is False
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert with_body.status_code == 422
    assert with_body.json()["code"] == "workspace_session.request_invalid"
    assert bad_query.status_code == 422
    assert bad_query.json()["code"] == "workspace_session.request_invalid"
    assert mixed.status_code == 400
    assert mixed.json()["code"] == "workspace_session.credential_conflict"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """UPDATE edge_deployments SET status='retired',revision=revision+1,
                 updated_at=? WHERE workspace_id=? AND tenant_id=? AND status='active'""",
            (
                STAMP,
                runtime["workspace"].workspace_id,
                runtime["workspace"].tenant_id,
            ),
        )
        connection.commit()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://control",
        ) as client:
            drifted_replay = await client.post(
                f"/workspace/advanced-tools/{tool['tool_code']}/open",
                headers=headers,
            )
    finally:
        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE edge_deployments SET status='active',revision=revision+1,
                     updated_at=? WHERE workspace_id=? AND tenant_id=? AND status='retired'""",
                (
                    STAMP,
                    runtime["workspace"].workspace_id,
                    runtime["workspace"].tenant_id,
                ),
            )
            connection.commit()
    assert drifted_replay.status_code == 412
    assert drifted_replay.json()["code"] == "identity.revision_conflict"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_events
               WHERE event_type='advanced_tool.opened'"""
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            """SELECT count(*) FROM workspace_navigation_event_outbox outbox
               JOIN workspace_navigation_events event
                 ON event.workspace_navigation_event_id=
                   outbox.workspace_navigation_event_id
               WHERE event.event_type='advanced_tool.opened'"""
        ).fetchone()[0] == 1
        projection_id = connection.execute(
            """SELECT workspace_navigation_projection_id
               FROM workspace_navigation_projection_heads
               WHERE workspace_id=? AND tenant_id=?""",
            (runtime["workspace"].workspace_id, runtime["workspace"].tenant_id),
        ).fetchone()[0]
        connection.execute(
            """UPDATE workspace_navigation_projection_sources SET freshness='stale'
               WHERE workspace_navigation_projection_id=? AND source_code='catalog'""",
            (projection_id,),
        )
        connection.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://control",
    ) as client:
        unavailable = await client.get(
            "/workspace/navigation",
            headers=_workspace_headers(runtime),
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "workspace_session.unavailable"
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """UPDATE workspace_navigation_projection_sources SET freshness='fresh'
               WHERE workspace_navigation_projection_id=? AND source_code='catalog'""",
            (projection_id,),
        )
        connection.commit()
