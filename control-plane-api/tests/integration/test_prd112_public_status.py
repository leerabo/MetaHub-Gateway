"""PRD-112 status publication, isolation, approval, and subscription contracts."""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.service import SESSION_COOKIE
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.public_status import routes as status_routes
from metahub_control.public_status import service as status_service
from metahub_control.public_status.routes import router
from metahub_control.public_status.scheduler import refresh_status_bundle_once
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.status_bundle import (
    StatusBundleError,
    build_status_envelope,
    signed_status_document,
    verify_status_envelope,
)
from metahub_shared.status_bundle import test_kms_status_public_key as status_public_key
from metahub_status.app import StatusPageConfig, create_status_app

ORIGIN = "http://localhost:5173"
PLATFORM_TOKEN = "s" * 43
PLATFORM_CSRF = "c" * 43
WORKSPACE_TOKEN = "w" * 43
GLOBAL_TOKEN = "g" * 43
OPS_ID = "pto_" + "d112" * 8
SECURITY_ID = "pto_" + "e112" * 8
OPS_ACCOUNT_ID = "gac_" + "d112" * 8
SECURITY_ACCOUNT_ID = "gac_" + "e112" * 8
WORKSPACE_ID = "wsp_aba85648d6c3c0119b4a41bddf82762b"
TENANT_ID = "944f3e6d-8b66-584e-b095-7cac890a5f87"
ACCOUNT_ID = "gac_2abeaa4602635ac0bd3ad56a7b19320a"
CONTACT = "workspace-owner@example.invalid"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str, bundle_root: Path) -> AppConfig:
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
        platform_session_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
        status_bundle_root=str(bundle_root),
        status_bundle_ttl_seconds=3600,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _platform_context(operator_id: str, account_id: str, role: str, *permissions: str):
    marker = "d" if operator_id == OPS_ID else "e"
    return PlatformContext(
        platform_session_id="pts_" + marker * 32,
        operator_id=operator_id,
        account_id=account_id,
        global_session_id="gss_" + marker * 32,
        factor_id="mfa_" + marker * 32,
        factor_revision=1,
        operator_revision=1,
        roles=(role,),
        permissions=permissions,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


OPS = _platform_context(
    OPS_ID,
    OPS_ACCOUNT_ID,
    "platform_owner",
    "platform.status.read",
    "platform.status.write",
    "platform.status.security_approve",
)
SECURITY = _platform_context(
    SECURITY_ID,
    SECURITY_ACCOUNT_ID,
    "platform_security",
    "platform.status.read",
    "platform.status.security_approve",
)
READ_ONLY_OPS = _platform_context(
    OPS_ID,
    OPS_ACCOUNT_ID,
    "platform_ops",
    "platform.status.read",
)


def _workspace_context(
    *,
    workspace_id: str = WORKSPACE_ID,
    tenant_id: str = TENANT_ID,
) -> WorkspaceSessionContext:
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=ACCOUNT_ID,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        membership_id="wmb_0a1355f1e054acafb7fe2eaf001797b8",
        legacy_user_id="009cc16d-d981-5969-9a91-bf52dbc96715",
        membership_role="owner",
        legacy_role="admin",
        account_email=CONTACT,
        display_name="Workspace owner",
        tenant_slug="seed-workspace",
        tenant_name="Seed workspace",
        permission_codes=(),
        permission_revision=1,
        authorization_fingerprint="f" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd112-status-base")
    database = root / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0109"
    apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        for marker, account_id, operator_id, role in (
            ("ops", OPS_ACCOUNT_ID, OPS_ID, "platform_owner"),
            ("security", SECURITY_ACCOUNT_ID, SECURITY_ID, "platform_security"),
        ):
            account = dict(source)
            account.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"prd112-{marker}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD112 {marker}",
                    "status": "active",
                    "login_enabled": 1,
                    "failed_login_count": 0,
                    "locked_until": None,
                    "last_login_at": None,
                }
            )
            columns = tuple(account)
            connection.execute(
                f"INSERT INTO global_accounts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(account[column] for column in columns),
            )
            connection.execute(
                """INSERT INTO platform_operators(
                     operator_id,account_id,status,revision,created_by_operator_id,
                     created_at,updated_at)
                   VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))""",
                (operator_id, account_id),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                     operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                   VALUES(?,?,'active',NULL,datetime('now'),NULL)""",
                (operator_id, role),
            )
        connection.commit()
    finally:
        connection.close()
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> AppConfig:
    database = tmp_path / "runtime.sqlite3"
    shutil.copy2(base_database, database)
    return _config(_url(database), tmp_path / "status-bundles")


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    holder: dict[str, object],
):
    monkeypatch.setattr(
        status_routes,
        "authenticate_platform_session_context",
        lambda *args, **kwargs: holder["route_platform"],
    )
    monkeypatch.setattr(
        status_routes,
        "authenticate_workspace_session_context",
        lambda *args, **kwargs: holder["workspace"],
    )

    def platform_auth(*args, **kwargs):
        value = holder["service_platform"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        status_service,
        "authenticate_platform_session_in_transaction",
        platform_auth,
    )
    monkeypatch.setattr(
        status_service,
        "authenticate_workspace_session_in_transaction",
        lambda *args, **kwargs: holder["workspace"],
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-112 Test",
        extra_cors_allow_headers=("If-Match", "Idempotency-Key", "X-CSRF-Token"),
        cors_allow_credentials=True,
        extra_cors_expose_headers=("ETag",),
    )
    app.state.config = config
    app.include_router(router)
    return app


@pytest.mark.anyio
async def test_system_publishes_initial_and_expiring_operational_bundle(runtime: AppConfig) -> None:
    started = datetime.now(UTC)
    assert refresh_status_bundle_once(runtime, now=started) is True
    assert refresh_status_bundle_once(runtime, now=started + timedelta(minutes=10)) is False
    assert refresh_status_bundle_once(runtime, now=started + timedelta(minutes=41)) is True

    public_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://status"
    ) as client:
        response = await client.get("/public/status")
    assert response.status_code == 200
    assert response.json()["overall_status"] == "operational"
    assert response.json()["incidents"] == []
    assert response.json()["maintenance_windows"] == []

    connection = connect_database(runtime.database_url)
    try:
        rows = connection.execute(
            """SELECT generation,publisher_kind,created_by_operator_id,status
               FROM status_publication_bundles ORDER BY generation"""
        ).fetchall()
    finally:
        connection.close()
    assert [tuple(row) for row in rows] == [
        (1, "system", None, "superseded"),
        (2, "system", None, "published"),
    ]


def _holder() -> dict[str, object]:
    return {
        "route_platform": OPS,
        "service_platform": OPS,
        "workspace": _workspace_context(),
    }


def _platform_headers(*, key: str | None = None, revision: int | None = None):
    headers = {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; "
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; "
            f"{PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _workspace_headers() -> dict[str, str]:
    return {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; "
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
        )
    }


def _incident_payload(*, visibility: str = "public", classification: str = "reliability"):
    return {
        "owner_workspace_id": WORKSPACE_ID,
        "title": f"{classification.title()} gateway incident",
        "classification": classification,
        "visibility": visibility,
        "severity": "major",
        "acknowledged_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat(),
        "initial_update": (
            "Gateway requests are being investigated."
            if classification == "reliability"
            else None
        ),
        "affected_components": ["api-gateway"],
        "affected_regions": ["cn-north-1"],
    }


def _maintenance_payload() -> dict[str, object]:
    return {
        "owner_workspace_id": WORKSPACE_ID,
        "title": "Gateway certificate maintenance",
        "public_message": "Certificate rotation may briefly delay new connections.",
        "visibility": "public",
        "affected_components": ["api-gateway"],
        "affected_regions": ["cn-north-1"],
        "starts_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "ends_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }


def _public_app(config: AppConfig):
    assert config.kms_test_master_key is not None
    return create_status_app(
        StatusPageConfig(
            bundle_root=Path(config.status_bundle_root),
            trusted_keys={"test-v1": status_public_key(config.kms_test_master_key)},
        )
    )


@pytest.mark.anyio
async def test_reliability_publication_private_isolation_and_maintenance(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        public_incident = await client.post(
            "/platform/incidents",
            headers=_platform_headers(key="prd112-public-incident-0001"),
            json=_incident_payload(),
        )
        assert public_incident.status_code == 201
        public_id = public_incident.json()["incident"]["incident_id"]
        assert public_incident.json()["incident"]["updates"]

        private_incident = await client.post(
            "/platform/incidents",
            headers=_platform_headers(key="prd112-private-incident-0001"),
            json=_incident_payload(visibility="workspace"),
        )
        assert private_incident.status_code == 201
        private_id = private_incident.json()["incident"]["incident_id"]

        workspace_view = await client.get("/incidents", headers=_workspace_headers())
        assert workspace_view.status_code == 200
        assert {item["incident_id"] for item in workspace_view.json()["incidents"]} == {
            public_id,
            private_id,
        }

        holder["workspace"] = _workspace_context(
            workspace_id="wsp_" + "f112" * 8,
            tenant_id="ten_" + "f112" * 8,
        )
        other_view = await client.get("/incidents", headers=_workspace_headers())
        assert other_view.status_code == 200
        assert {item["incident_id"] for item in other_view.json()["incidents"]} == {public_id}

        maintenance_payload = _maintenance_payload()
        maintenance = await client.post(
            "/platform/maintenance-windows",
            headers=_platform_headers(key="prd112-maintenance-0001"),
            json=maintenance_payload,
        )
        assert maintenance.status_code == 201
        maintenance_id = maintenance.json()["maintenance_window"]["maintenance_window_id"]
        maintenance_replay = await client.post(
            "/platform/maintenance-windows",
            headers=_platform_headers(key="prd112-maintenance-0001"),
            json=maintenance_payload,
        )
        assert maintenance_replay.status_code == 200
        assert maintenance_replay.json()["idempotent_replay"] is True
        assert (
            maintenance_replay.json()["maintenance_window"]["maintenance_window_id"]
            == maintenance_id
        )

    public_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://status"
    ) as client:
        response = await client.get("/public/status")
    assert response.status_code == 200
    body = response.json()
    assert len(body["incidents"]) == 1
    assert body["incidents"][0]["reference"] == public_incident.json()["incident"]["reference"]
    assert len(body["maintenance_windows"]) == 1
    assert "incident_id" not in body["incidents"][0]
    assert "maintenance_window_id" not in body["maintenance_windows"][0]

    connection = connect_database(runtime.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM maintenance_windows").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM incidents WHERE incident_id=?", (maintenance_id,)
        ).fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.anyio
async def test_security_requires_distinct_current_approver_and_dynamic_permission(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    assert refresh_status_bundle_once(runtime) is True
    baseline_head = Path(runtime.status_bundle_root, "head.json").read_bytes()
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/platform/incidents",
            headers=_platform_headers(key="prd112-security-create-0001"),
            json=_incident_payload(classification="security"),
        )
        assert created.status_code == 201
        incident = created.json()["incident"]
        incident_id = incident["incident_id"]
        assert incident["status"] == "pending_security_review"
        assert incident["updates"] == []
        assert incident["security_approval_available"] is False
        assert Path(runtime.status_bundle_root, "head.json").read_bytes() == baseline_head

        self_approval = await client.post(
            f"/platform/incidents/{incident_id}/security-approval",
            headers=_platform_headers(key="prd112-security-self-0001", revision=1),
            json={
                "revision": 1,
                "public_message": "The security impact is contained and safe to disclose.",
                "affected_components": ["identity"],
                "affected_regions": ["cn-north-1"],
            },
        )
        assert self_approval.status_code == 409
        assert self_approval.json()["code"] == "status.approver_conflict"
        assert Path(runtime.status_bundle_root, "head.json").read_bytes() == baseline_head

        holder["route_platform"] = SECURITY
        holder["service_platform"] = SECURITY
        platform_view = await client.get(
            "/platform/status-incidents", headers=_platform_headers()
        )
        pending = next(
            item for item in platform_view.json()["incidents"] if item["incident_id"] == incident_id
        )
        assert pending["security_approval_available"] is True
        approved = await client.post(
            f"/platform/incidents/{incident_id}/security-approval",
            headers=_platform_headers(key="prd112-security-approve-0001", revision=1),
            json={
                "revision": 1,
                "public_message": "The security impact is contained and safe to disclose.",
                "affected_components": ["identity"],
                "affected_regions": ["cn-north-1"],
            },
        )
        assert approved.status_code == 201
        assert approved.json()["incident"]["status"] == "published"
        assert len(approved.json()["incident"]["updates"]) == 1

        holder["route_platform"] = OPS
        holder["service_platform"] = READ_ONLY_OPS
        before = connect_database(runtime.database_url)
        try:
            count = before.execute("SELECT COUNT(*) FROM maintenance_windows").fetchone()[0]
        finally:
            before.close()
        denied = await client.post(
            "/platform/maintenance-windows",
            headers=_platform_headers(key="prd112-revoked-write-0001"),
            json=_maintenance_payload(),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"

    connection = connect_database(runtime.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM maintenance_windows").fetchone()[0] == count
        audit = connection.execute(
            """SELECT actor_id FROM platform_audit_logs
               WHERE action_code='incident.security_approved'"""
        ).fetchone()
        assert audit is not None and audit["actor_id"] == SECURITY_ID
    finally:
        connection.close()

    public_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://status"
    ) as client:
        response = await client.get("/public/status")
    assert response.status_code == 200
    assert len(response.json()["incidents"]) == 1


@pytest.mark.anyio
async def test_publication_failure_replays_persisted_fact_without_duplication(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    original = status_service.publish_status_bundle
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiError("status.publication_unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(status_service, "publish_status_bundle", fail_once)
    payload = _incident_payload()
    headers = _platform_headers(key="prd112-publication-retry-0001")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post("/platform/incidents", headers=headers, json=payload)
        assert failed.status_code == 503
        assert failed.json()["code"] == "status.publication_unavailable"
        replayed = await client.post("/platform/incidents", headers=headers, json=payload)
        assert replayed.status_code == 200
        assert replayed.json()["idempotent_replay"] is True

    connection = connect_database(runtime.database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM incident_updates").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM status_commands").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM status_event_outbox").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM status_publication_bundles WHERE status='published'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_incident_resolution_and_postmortem_publish_complete_timeline(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/platform/incidents",
            headers=_platform_headers(key="prd112-timeline-create-0001"),
            json=_incident_payload(),
        )
        assert created.status_code == 201
        incident_id = created.json()["incident"]["incident_id"]
        monitoring = await client.post(
            f"/platform/incidents/{incident_id}/updates",
            headers=_platform_headers(key="prd112-timeline-monitor-0001", revision=1),
            json={
                "revision": 1,
                "status": "monitoring",
                "public_message": "Gateway traffic has recovered and remains under observation.",
                "affected_components": ["api-gateway"],
                "affected_regions": ["cn-north-1"],
            },
        )
        assert monitoring.status_code == 201
        resolved = await client.post(
            f"/platform/incidents/{incident_id}/updates",
            headers=_platform_headers(key="prd112-timeline-resolve-0001", revision=2),
            json={
                "revision": 2,
                "status": "resolved",
                "public_message": "Gateway traffic is stable and the incident is resolved.",
                "affected_components": ["api-gateway"],
                "affected_regions": ["cn-north-1"],
            },
        )
        assert resolved.status_code == 201
        postmortem = await client.post(
            f"/platform/incidents/{incident_id}/postmortem",
            headers=_platform_headers(key="prd112-timeline-postmortem-0001", revision=3),
            json={
                "revision": 3,
                "summary": "A capacity threshold delayed gateway routing recovery.",
                "timeline": [
                    "08:00 UTC elevated failures detected",
                    "08:08 UTC capacity restored",
                ],
                "cause_category": "capacity",
                "corrective_actions": [
                    "Increase automated capacity headroom before the next peak."
                ],
            },
        )
        assert postmortem.status_code == 201
        incident = postmortem.json()["incident"]
        assert incident["status"] == "postmortem_published"
        assert len(incident["updates"]) == 3
        assert incident["postmortem"]["cause_category"] == "capacity"

    public_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://status"
    ) as client:
        response = await client.get("/public/status")
    assert response.status_code == 200
    public_incident = response.json()["incidents"][0]
    assert public_incident["status"] == "postmortem_published"
    assert len(public_incident["updates"]) == 3
    assert public_incident["postmortem"]["cause_category"] == "capacity"


@pytest.mark.anyio
async def test_bundle_verification_expiry_tamper_and_cached_storage_failure(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/platform/incidents",
            headers=_platform_headers(key="prd112-cache-source-0001"),
            json=_incident_payload(),
        )
        assert created.status_code == 201

    public_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=public_app), base_url="http://status"
    ) as client:
        first = await client.get("/public/status")
        assert first.status_code == 200
        Path(runtime.status_bundle_root, "head.json").write_text("{}", encoding="ascii")
        cached = await client.get("/public/status")
        assert cached.status_code == 200
        assert cached.json()["generation"] == first.json()["generation"]

    cold_app = _public_app(runtime)
    async with AsyncClient(
        transport=ASGITransport(app=cold_app), base_url="http://status"
    ) as client:
        unavailable = await client.get("/public/status")
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "code": "status.unavailable",
        "message": "Current service status is temporarily unavailable.",
    }

    now = datetime.now(UTC)
    payload = {
        "overall_status": "operational",
        "components": [],
        "incidents": [],
        "maintenance_windows": [],
    }
    signed = signed_status_document(
        generation=99,
        published_at=(now - timedelta(hours=2)).isoformat(),
        expires_at=(now - timedelta(hours=1)).isoformat(),
        payload=payload,
    )
    with pytest.raises(StatusBundleError, match="status.bundle_expired"):
        verify_status_envelope(
            build_status_envelope(
                signed,
                algorithm="ed25519",
                key_version="test-v1",
                signature="a" * 86,
            ),
            trusted_keys={"test-v1": b"x" * 32},
            now=now,
        )
    source = Path(__file__).resolve().parents[2] / "src" / "metahub_status" / "app.py"
    assert "metahub_control" not in source.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_subscription_create_verify_unsubscribe_replay_and_secrecy(
    monkeypatch: pytest.MonkeyPatch,
    runtime: AppConfig,
) -> None:
    token = "v" * 43
    monkeypatch.setattr(status_service.secrets, "token_urlsafe", lambda length: token)
    holder = _holder()
    app = _app(monkeypatch, runtime, holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get("/status/subscriptions", headers=_workspace_headers())
        assert listing.status_code == 200
        intent = listing.json()["create_intent"]
        created = await client.post(
            "/status/subscriptions",
            json={
                "operation": "create",
                "create_intent": intent,
                "channel": "email",
                "contact": CONTACT,
                "scope": {
                    "components": ["api-gateway"],
                    "regions": ["cn-north-1"],
                    "severities": ["major", "critical"],
                },
                "token": None,
            },
        )
        assert created.status_code == 202
        assert created.json()["accepted"] is True

        verify_payload = {
            "operation": "verify",
            "create_intent": None,
            "channel": None,
            "contact": None,
            "scope": None,
            "token": token,
        }
        verified = await client.post("/status/subscriptions", json=verify_payload)
        replayed = await client.post("/status/subscriptions", json=verify_payload)
        assert verified.status_code == replayed.status_code == 202
        assert verified.json()["accepted"] is replayed.json()["accepted"] is True

        unsubscribe_payload = {**verify_payload, "operation": "unsubscribe"}
        unsubscribed = await client.post("/status/subscriptions", json=unsubscribe_payload)
        unsubscribe_replay = await client.post(
            "/status/subscriptions", json=unsubscribe_payload
        )
        assert unsubscribed.status_code == unsubscribe_replay.status_code == 202
        final = await client.get("/status/subscriptions", headers=_workspace_headers())
        assert final.status_code == 200
        assert final.json()["subscriptions"][0]["status"] == "unsubscribed"
        assert CONTACT not in json.dumps(final.json(), sort_keys=True)

    connection = connect_database(runtime.database_url)
    try:
        row = connection.execute("SELECT * FROM status_subscriptions").fetchone()
        outbox = connection.execute("SELECT * FROM notification_outbox").fetchone()
        assert row is not None and outbox is not None
        assert row["contact_ciphertext"].startswith("s1.")
        assert CONTACT not in row["contact_ciphertext"]
        assert row["verification_token_hash"] is None
        assert row["unsubscribe_token_hash"] is None
        assert CONTACT not in outbox["safe_parameters_ciphertext"]
        assert token not in outbox["safe_parameters_ciphertext"]
        assert connection.execute("SELECT COUNT(*) FROM status_event_outbox").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM platform_audit_logs").fetchone()[0] == 0
    finally:
        connection.close()
