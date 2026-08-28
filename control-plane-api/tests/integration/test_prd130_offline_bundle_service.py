"""PRD-130 approved isolated exchange and conflict-resolution smoke flow."""

from __future__ import annotations

import secrets
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.edge_management import offline_bundle_service as service
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management.offline_bundle_models import (
    PlatformOfflineDecisionRequest,
    WorkspaceOfflineBundleRequest,
    WorkspaceOfflineResolutionRequest,
)
from metahub_control.edge_management.routes import router
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.private_gateway import (
    OfflineBundleKeyEnvelope,
    OfflineDomainVersion,
    build_offline_bundle_archive,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
DEPLOYMENT_ID = "edp_" + "1" * 32
PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
CONTENT_KEY = bytes(range(32, 64))
NOW = datetime.now(UTC)
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "q" * 43
STEP_UP = "u" * 43


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
            loaded_at=NOW,
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd130-service-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0122"
    apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppConfig, SimpleNamespace, PlatformContext, Path]:
    database = tmp_path / "prd130.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
            """SELECT membership.workspace_id,membership.tenant_id,membership.account_id
                 FROM workspace_memberships membership
                 WHERE membership.role_code='owner' AND membership.status='active'
                 ORDER BY membership.workspace_id LIMIT 1"""
        ).fetchone()
        operator = connection.execute(
            "SELECT operator_id FROM platform_operators ORDER BY operator_id LIMIT 1"
        ).fetchone()
        assert owner is not None
        now = _time(NOW)
        platform_account_id = "gac_" + "8" * 32
        operator_id = "pto_" + "8" * 32
        if operator is None:
            source = connection.execute(
                "SELECT * FROM global_accounts WHERE account_id=?",
                (owner["account_id"],),
            ).fetchone()
            assert source is not None
            account = dict(source)
            account.update(
                {
                    "account_id": platform_account_id,
                    "email_normalized": "prd130-platform@example.invalid",
                    "phone_e164": None,
                    "display_name": "PRD-130 platform approver",
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
                   VALUES(?,?,'active',1,NULL,?,?)""",
                (operator_id, platform_account_id, now, now),
            )
        else:
            operator_id = str(operator["operator_id"])
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-130 isolated','cn-test','isolated','active',1,1,
              '1.0.0','unknown',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                owner["workspace_id"],
                owner["tenant_id"],
                owner["account_id"],
                now,
                now,
            ),
        )
        connection.commit()
    workspace = SimpleNamespace(
        account_id=str(owner["account_id"]),
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        permission_codes=(
            "edge.offline_bundle.read",
            "edge.offline_bundle.transfer",
            "edge.offline_conflict.resolve",
        ),
    )
    platform = PlatformContext(
        platform_session_id="pts_" + "8" * 32,
        operator_id=operator_id,
        account_id=platform_account_id,
        global_session_id="gss_" + "8" * 32,
        factor_id="mfa_" + "8" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_ops",),
        permissions=(
            "platform.edge_offline_bundle.read",
            "platform.edge_offline_bundle.manage",
        ),
        expires_at=NOW + timedelta(hours=1),
    )
    monkeypatch.setattr(service, "_workspace_context", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(
        service, "consume_step_up_grant_in_transaction", lambda *_args, **_kwargs: "totp"
    )
    return config, workspace, platform, tmp_path


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _manifest(
    root: Path,
    workspace: SimpleNamespace,
    *,
    marker: str,
    direction: str,
    base_generation: int,
    base_sha256: str | None,
    generation: int,
    value_sha256: str,
):
    vector = OfflineDomainVersion(
        domain="desired_state",
        base_generation=base_generation,
        base_sha256=base_sha256,
        generation=generation,
        value_sha256=value_sha256,
    )
    signed = build_offline_bundle_archive(
        root / f"{marker}.mhb",
        direction=direction,
        workspace_id=workspace.workspace_id,
        tenant_id=workspace.tenant_id,
        deployment_id=DEPLOYMENT_ID,
        version_vector=(vector,),
        entries=(("desired_state", "application/json", f'{{"marker":"{marker}"}}'.encode()),),
        content_key=CONTENT_KEY,
        key_envelope=OfflineBundleKeyEnvelope(
            encryption_key_version=1,
            kms_key_id_sha256="a" * 64,
            wrapped_key="cHJkMTMwLXdyYXBwZWQta2V5LW1hdGVyaWFs",
        ),
        private_key=PRIVATE_KEY,
        key_id="test-offline-builder-v1",
        created_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(days=1),
        bundle_id="obb_" + marker * 32,
    )
    return signed.manifest


def _request(
    config: AppConfig,
    operation: str,
    manifest,
    marker: str,
):
    return service.request_workspace_offline_bundle(
        config,
        operation,
        WorkspaceOfflineBundleRequest(
            manifest=manifest,
            reason_code="scheduled_media_exchange",
        ),
        session_token="workspace-session",
        csrf_token="workspace-csrf",
        step_up_grant="step-up",
        idempotency_key=f"workspace-{marker}-idempotency",
        request_id=f"request-{marker}-workspace",
        trace_id=f"trace-{marker}-workspace",
    )


def _approve(config: AppConfig, platform: PlatformContext, request_id: str, marker: str):
    return service.decide_platform_offline_request(
        config,
        platform,
        request_id,
        PlatformOfflineDecisionRequest(
            action="approve", expected_revision=1, reason_code="dual_control_approved"
        ),
        idempotency_key=f"platform-{marker}-idempotency",
        request_id=f"request-{marker}-platform",
        trace_id=f"trace-{marker}-platform",
    )


def _workspace_headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "X-Step-Up-Grant": STEP_UP,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _platform_headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}; {PLATFORM_CSRF_COOKIE}={PLATFORM_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": PLATFORM_CSRF,
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _app(config: AppConfig, platform: PlatformContext, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        edge_routes,
        "authenticate_platform_session_context",
        lambda *_args, **_kwargs: platform,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-130 Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def test_import_detects_dual_progress_and_resolution_creates_new_signed_generation(
    runtime: tuple[AppConfig, SimpleNamespace, PlatformContext, Path],
) -> None:
    config, workspace, platform, root = runtime
    initial_hash = "1" * 64
    local_hash = "2" * 64
    cloud_hash = "3" * 64

    initial = _manifest(
        root,
        workspace,
        marker="1",
        direction="edge_to_cloud",
        base_generation=0,
        base_sha256=None,
        generation=1,
        value_sha256=initial_hash,
    )
    initial_request = _request(config, "import", initial, "1")
    initial_decision = _approve(config, platform, initial_request.requests[0].request_id, "1")
    assert initial_decision.signed_bundle is not None

    cloud = _manifest(
        root,
        workspace,
        marker="2",
        direction="cloud_to_edge",
        base_generation=1,
        base_sha256=initial_hash,
        generation=2,
        value_sha256=cloud_hash,
    )
    cloud_request = _request(config, "export", cloud, "2")
    _approve(config, platform, cloud_request.requests[0].request_id, "2")

    local = _manifest(
        root,
        workspace,
        marker="3",
        direction="edge_to_cloud",
        base_generation=1,
        base_sha256=initial_hash,
        generation=2,
        value_sha256=local_hash,
    )
    local_request = _request(config, "import", local, "3")
    _approve(config, platform, local_request.requests[0].request_id, "3")
    state = service.get_workspace_offline_state(
        config,
        DEPLOYMENT_ID,
        session_token="workspace-session",
        request_id="request-conflict-state",
        trace_id="trace-conflict-state",
    )
    assert state.asynchronous_management is True
    assert len(state.conflicts) == 1
    conflict = state.conflicts[0]
    assert conflict.cloud_sha256 == cloud_hash
    assert conflict.local_sha256 == local_hash
    assert conflict.resolution is None
    local_bundle = next(item for item in state.bundles if item.bundle_id == local.bundle_id)
    assert local_bundle.transfer_status == "conflict"

    resolution_manifest = _manifest(
        root,
        workspace,
        marker="4",
        direction="cloud_to_edge",
        base_generation=2,
        base_sha256=cloud_hash,
        generation=3,
        value_sha256=cloud_hash,
    )
    resolution_state = service.request_workspace_offline_resolution(
        config,
        conflict.conflict_id,
        WorkspaceOfflineResolutionRequest(
            resolution="keep_cloud",
            manifest=resolution_manifest,
            expected_current_generation=2,
            reason_code="preserve_approved_cloud_head",
        ),
        session_token="workspace-session",
        csrf_token="workspace-csrf",
        step_up_grant="step-up",
        idempotency_key="workspace-resolution-idempotency",
        request_id="request-resolution-workspace",
        trace_id="trace-resolution-workspace",
    )
    resolution_request = next(
        item for item in resolution_state.requests if item.operation == "resolve"
    )
    resolution_decision = _approve(config, platform, resolution_request.request_id, "4")
    assert resolution_decision.signed_bundle is not None
    assert resolution_decision.signed_bundle.manifest.producer_generation == 3

    final = service.get_workspace_offline_state(
        config,
        DEPLOYMENT_ID,
        session_token="workspace-session",
        request_id="request-final-state",
        trace_id="trace-final-state",
    )
    assert final.conflicts[0].resolution == "keep_cloud"
    assert final.conflicts[0].new_generation == 3
    with closing(connect_database(config.database_url)) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM edge_offline_conflict_resolutions").fetchone()[
                0
            ]
            == 1
        )
        events = connection.execute(
            "SELECT event_type FROM edge_offline_bundle_events ORDER BY occurred_at"
        ).fetchall()
        assert len(events) == 5
        assert {str(row["event_type"]) for row in events} == {
            "edge.offline_bundle_applied",
            "edge.offline_bundle_exported",
            "edge.offline_conflict_detected",
            "edge.offline_conflict_resolved",
        }


@pytest.mark.anyio
async def test_offline_bundle_http_cookie_csrf_idempotency_and_credential_boundaries(
    runtime: tuple[AppConfig, SimpleNamespace, PlatformContext, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, workspace, platform, root = runtime
    manifest = _manifest(
        root,
        workspace,
        marker="5",
        direction="cloud_to_edge",
        base_generation=0,
        base_sha256=None,
        generation=1,
        value_sha256="5" * 64,
    )
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "reason_code": "scheduled_media_exchange",
    }
    app = _app(config, platform, monkeypatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_csrf = _workspace_headers(idempotency_key="prd130-http-missing-csrf-01")
        missing_csrf.pop("X-CSRF-Token")
        rejected = await client.post(
            "/edge/offline-bundles/export", headers=missing_csrf, json=payload
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "workspace_session.csrf_invalid"

        mixed_workspace = _workspace_headers(idempotency_key="prd130-http-mixed-workspace-01")
        mixed_workspace["Cookie"] += f"; {PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"
        rejected = await client.post(
            "/edge/offline-bundles/export", headers=mixed_workspace, json=payload
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "workspace_session.credential_conflict"

        request_headers = _workspace_headers(idempotency_key="prd130-http-export-request-01")
        created = await client.post(
            "/edge/offline-bundles/export", headers=request_headers, json=payload
        )
        assert created.status_code == 201, created.text
        created_body = created.json()
        request_item = next(
            item for item in created_body["requests"] if item["operation"] == "export"
        )
        assert request_item["status"] == "pending_platform"
        assert "private" in created.headers["Cache-Control"]
        assert "no-store" in created.headers["Cache-Control"]

        replay = await client.post(
            "/edge/offline-bundles/export", headers=request_headers, json=payload
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        mixed_platform = _platform_headers(idempotency_key="prd130-http-mixed-platform-01")
        mixed_platform["Cookie"] += f"; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
        rejected = await client.post(
            f"/platform/edge/offline-bundles/{request_item['request_id']}/decisions",
            headers=mixed_platform,
            json={
                "action": "approve",
                "expected_revision": 1,
                "reason_code": "dual_control_approved",
            },
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "platform.credential_conflict"

        platform_list = await client.get(
            "/platform/edge/offline-bundles", headers=_platform_headers()
        )
        assert platform_list.status_code == 200, platform_list.text
        assert any(
            request["request_id"] == request_item["request_id"]
            for item in platform_list.json()["items"]
            for request in item["state"]["requests"]
        )

        decided = await client.post(
            f"/platform/edge/offline-bundles/{request_item['request_id']}/decisions",
            headers=_platform_headers(idempotency_key="prd130-http-approve-request-01"),
            json={
                "action": "approve",
                "expected_revision": 1,
                "reason_code": "dual_control_approved",
            },
        )
        assert decided.status_code == 200, decided.text
        decision = decided.json()
        assert decision["request"]["status"] == "approved"
        assert decision["signed_bundle"]["manifest"]["bundle_id"] == manifest.bundle_id
        assert "wrapped_key" not in decided.text

        workspace_state = await client.get(
            f"/edge/deployments/{DEPLOYMENT_ID}/offline-bundles",
            headers=_workspace_headers(),
        )
        assert workspace_state.status_code == 200
        assert workspace_state.json()["asynchronous_management"] is True
