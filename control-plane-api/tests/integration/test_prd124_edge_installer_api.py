"""PRD-124 Workspace installer API, isolation, and lifecycle acceptance."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.service import (
    authenticate_bearer,
    current_user_response,
    login_admin,
)
from metahub_control.edge_management import installer_service
from metahub_control.edge_management import routes as edge_routes
from metahub_control.edge_management import service as edge_service
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
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.private_gateway import (
    PREFLIGHT_CHECK_CODES,
    InstallerContractError,
    installer_manifest_sha256,
    installer_signature_payload,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43
PLATFORM_CSRF = "d" * 43
RELEASE_ID = "irl_12400000000000000000000000000000"
PROFILE_ID = "ipr_12400000000000000000000000000001"
TEST_RELEASE_ID = "irl_12410000000000000000000000000000"
TEST_KEY_ID = "prd124-test-release-key"
_TEST_PASSWORD = f"Prd124!Aa9{secrets.token_urlsafe(18)}"


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
        provider_key_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd124-installer-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0116"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture(scope="module")
def runtime(
    base_database: Path,
) -> tuple[AppConfig, dict[str, WorkspaceSessionContext]]:
    config = _config(_url(base_database))
    return config, {"actor": _owner_actor(config)}


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """
            SELECT membership.workspace_membership_id,membership.workspace_id,
              membership.tenant_id,membership.account_id,membership.legacy_user_id,
              tenant.slug,tenant.name,user.email,user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users user ON user.user_id=membership.legacy_user_id
              AND user.tenant_id=membership.tenant_id
            WHERE membership.role_code='owner' AND membership.status='active'
            ORDER BY membership.workspace_id LIMIT 1
            """
        ).fetchone()
        assert row is not None
        connection.execute("BEGIN")
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
        )
        connection.rollback()
    assert {"edge.installer.read", "edge.installation.manage"} <= set(
        snapshot.permission_codes
    )
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_id=str(row["workspace_membership_id"]),
        legacy_user_id=str(row["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(row["email"]),
        display_name=str(row["display_name"]),
        tenant_slug=str(row["slug"]),
        tenant_name=str(row["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def test_legacy_console_projects_active_workspace_owner_installer_permissions(
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, _actor_holder = runtime
    login = login_admin(
        config,
        tenant_slug="default",
        email="admin@metahub.local",
        password=_TEST_PASSWORD,
        request_id="req_prd124_owner_projection_login",
        trace_id="trc_prd124_owner_projection_login",
        user_agent="PRD-124 test",
        client_host="127.0.0.1",
    )
    assert {"edge.installer.read", "edge.installation.manage"} <= set(
        login.permission_codes
    )
    context = authenticate_bearer(
        config,
        f"Bearer {login.access_token}",
        update_last_seen=False,
    )
    current = current_user_response(
        context,
        request_id="req_prd124_owner_projection_me",
        trace_id="trc_prd124_owner_projection_me",
    )
    assert {"edge.installer.read", "edge.installation.manage"} <= set(
        current.permission_codes
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor_holder: dict[str, WorkspaceSessionContext],
    platform_holder: dict[str, PlatformContext] | None = None,
):
    monkeypatch.setattr(
        edge_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor_holder["actor"],
    )
    if platform_holder is not None:
        monkeypatch.setattr(
            edge_routes,
            "authenticate_platform_session_context",
            lambda *_args, **_kwargs: platform_holder["context"],
        )
    app = create_service_app(
        "control-plane-api",
        "PRD-124 Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = config
    app.include_router(router)
    return app


def _headers(
    *, key: str | None = None, revision: int | None = None, step_up: str | None = None
) -> dict[str, str]:
    headers = {
        "Cookie": (
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
    if step_up is not None:
        headers["X-Step-Up-Grant"] = step_up
    return headers


def _platform_headers(
    *, key: str | None = None, revision: int | None = None, step_up: str | None = None
) -> dict[str, str]:
    headers = {
        "Cookie": (
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
    if step_up is not None:
        headers["X-Step-Up-Grant"] = step_up
    return headers


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, separators=(",", ": ")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _test_release(asset_root: Path) -> tuple[dict[str, object], bytes]:
    release_root = asset_root / "v1.0.1"
    shutil.copytree(ROOT / "infra" / "installers" / "v1.0.0", release_root)
    envelope = json.loads((release_root / "manifest.json").read_text(encoding="utf-8"))
    manifest = envelope["manifest"]
    manifest["release_id"] = TEST_RELEASE_ID
    manifest["version"] = "1.0.1"
    for index, artifact in enumerate(manifest["artifacts"], start=11):
        artifact["artifact_id"] = "ira_" + f"{index:032x}"
    for index, profile in enumerate(manifest["profiles"], start=11):
        profile["install_profile_id"] = "ipr_" + f"{index:032x}"

    _write_json(
        release_root / "profiles.json",
        {
            "schema_version": "metahub.install-profiles/v1",
            "profiles": manifest["profiles"],
        },
    )
    sbom_path = release_root / "sbom.spdx.json"
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    sbom["name"] = "MetaHub private gateway installer 1.0.1"
    sbom["documentNamespace"] = (
        "https://metahub.example/spdx/private-gateway-installer/1.0.1"
    )
    for package in sbom["packages"]:
        package["versionInfo"] = "1.0.1"
    _write_json(sbom_path, sbom)
    sbom_sha256 = hashlib.sha256(sbom_path.read_bytes()).hexdigest()
    for artifact in manifest["artifacts"]:
        path = release_root / artifact["filename"]
        artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact["sbom_sha256"] = sbom_sha256
        artifact["size_bytes"] = path.stat().st_size

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    envelope["manifest_sha256"] = installer_manifest_sha256(manifest)
    envelope["signature"] = {
        "algorithm": "Ed25519",
        "key_id": TEST_KEY_ID,
        "public_key": _b64(public_key),
        "value": _b64(private_key.sign(installer_signature_payload(manifest))),
    }
    _write_json(release_root / "manifest.json", envelope)
    return envelope, public_key


def _platform_context(marker: str) -> PlatformContext:
    return PlatformContext(
        platform_session_id="pts_" + marker * 32,
        operator_id="pto_" + marker * 32,
        account_id="gac_" + marker * 32,
        global_session_id="gss_" + marker * 32,
        factor_id="mfa_" + marker * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("release_manager",),
        permissions=(
            "platform.edge_installer.read",
            "platform.edge_installer.manage",
            "platform.edge_installer.approve",
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _prepare_platform_operators(
    config: AppConfig, public_key: bytes
) -> tuple[PlatformContext, ...]:
    contexts = tuple(_platform_context(marker) for marker in ("a", "b"))
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
        ).fetchone()
        assert source is not None
        for index, context in enumerate(contexts):
            values = dict(source)
            values.update(
                {
                    "account_id": context.account_id,
                    "email_normalized": f"prd124-release-manager-{index}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD-124 release manager {index}",
                    "status": "active",
                    "login_enabled": 1,
                    "failed_login_count": 0,
                    "locked_until": None,
                    "last_login_at": None,
                }
            )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO global_accounts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            connection.execute(
                """
                INSERT INTO platform_operators(
                  operator_id,account_id,status,revision,created_by_operator_id,
                  created_at,updated_at)
                VALUES(?,?,'active',1,NULL,'2026-08-27T00:00:00Z',
                  '2026-08-27T00:00:00Z')
                """,
                (context.operator_id, context.account_id),
            )
            connection.execute(
                """
                INSERT INTO platform_operator_role_bindings(
                  operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                VALUES(?,'release_manager','active',NULL,'2026-08-27T00:00:00Z',NULL)
                """,
                (context.operator_id,),
            )
        connection.execute("DROP TRIGGER trg_installer_trusted_keys_insert_prd124")
        connection.execute(
            """
            INSERT INTO installer_trusted_release_keys(
              key_id,algorithm,public_key_b64,signature_purpose,status,created_at)
            VALUES(?,'Ed25519',?,'metahub.edge-installer.release/v1','active',
              '2026-08-27T00:00:00Z')
            """,
            (TEST_KEY_ID, _b64(public_key)),
        )
        connection.execute(
            """
            CREATE TRIGGER trg_installer_trusted_keys_insert_prd124
            BEFORE INSERT ON installer_trusted_release_keys
            BEGIN SELECT RAISE(ABORT,'installer_trusted_key_immutable'); END
            """
        )
        connection.commit()
    return contexts


def _report(deployment_id: str, report_id: str, generation: int, *, passed: bool):
    checks = [
        {
            "code": code,
            "outcome": "passed" if passed or index else "failed",
            "required": True,
            "reason_code": "check_passed" if passed or index else "dns_unavailable",
            "duration_ms": 1,
        }
        for index, code in enumerate(PREFLIGHT_CHECK_CODES)
    ]
    return {
        "deployment_id": deployment_id,
        "report": {
            "schema_version": "metahub.preflight/v1",
            "preflight_report_id": report_id,
            "release_id": RELEASE_ID,
            "install_profile_id": PROFILE_ID,
            "generation": generation,
            "outcome": "passed" if passed else "failed",
            "environment_sha256": "a" * 64,
            "checks": checks,
            "started_at": "2026-08-27T04:00:00.000Z",
            "completed_at": "2026-08-27T04:00:00.014Z",
            "duration_ms": 14,
        },
    }


async def _deployment(client: AsyncClient, *, key: str) -> str:
    response = await client.post(
        "/edge/registrations",
        headers=_headers(key=key),
        json={
            "display_name": "PRD-124 installer gateway",
            "region_code": "cn-east-1",
            "mode": "connected",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["deployment"]["deployment_id"])


@pytest.mark.anyio
async def test_catalog_download_preflight_and_installation_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, actor_holder = runtime
    app = _app(monkeypatch, config, actor_holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        deployment_id = await _deployment(
            client, key="prd124-create-deployment-lifecycle-0001"
        )

        catalog = await client.get("/edge/installers", headers=_headers())
        assert catalog.status_code == 200
        assert catalog.headers["cache-control"] == "no-store, private"
        catalog_body = catalog.json()
        assert [item["manifest"]["release_id"] for item in catalog_body["releases"]] == [
            RELEASE_ID
        ]
        assert len(catalog_body["downloads"]) == 5
        download = catalog_body["downloads"][0]
        artifact = await client.get(download["download_path"], headers=_headers())
        assert artifact.status_code == 200
        assert artifact.headers["cache-control"] == "no-store, private"
        assert artifact.headers["x-content-type-options"] == "nosniff"
        assert hashlib.sha256(artifact.content).hexdigest() == download["sha256"]

        release_root = installer_service._release_asset_root

        def reject_untrusted_root(_version: str) -> Path:
            raise InstallerContractError("installer.artifact_missing")

        monkeypatch.setattr(installer_service, "_release_asset_root", reject_untrusted_root)
        rejected_artifact = await client.get(download["download_path"], headers=_headers())
        assert rejected_artifact.status_code == 503
        assert rejected_artifact.json()["code"] == "edge.installer_integrity_failed"
        monkeypatch.setattr(installer_service, "_release_asset_root", release_root)

        failed_id = "pfr_" + "1" * 32
        failed = await client.post(
            "/edge/preflights",
            headers=_headers(key="prd124-preflight-failed-0001"),
            json=_report(deployment_id, failed_id, 1, passed=False),
        )
        assert failed.status_code == 201, failed.text
        blocked = await client.post(
            "/edge/installations",
            headers=_headers(key="prd124-install-blocked-0001"),
            json={
                "installation_id": "ins_" + "1" * 32,
                "deployment_id": deployment_id,
                "preflight_report_id": failed_id,
                "generation": 1,
                "journal_sha256": "b" * 64,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "edge.preflight_failed"

        report_id = "pfr_" + "2" * 32
        preflight_payload = _report(deployment_id, report_id, 2, passed=True)
        preflight = await client.post(
            "/edge/preflights",
            headers=_headers(key="prd124-preflight-passed-0001"),
            json=preflight_payload,
        )
        assert preflight.status_code == 201, preflight.text
        replay = await client.post(
            "/edge/preflights",
            headers=_headers(key="prd124-preflight-passed-0001"),
            json=preflight_payload,
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True

        installation_id = "ins_" + "2" * 32
        started = await client.post(
            "/edge/installations",
            headers=_headers(key="prd124-install-start-0001"),
            json={
                "installation_id": installation_id,
                "deployment_id": deployment_id,
                "preflight_report_id": report_id,
                "generation": 2,
                "journal_sha256": "c" * 64,
            },
        )
        assert started.status_code == 201, started.text
        assert started.headers["etag"] == '"1"'

        skipped_steps = await client.patch(
            f"/edge/installations/{installation_id}",
            headers=_headers(key="prd124-install-skip-steps-0001", revision=1),
            json={
                "status": "active",
                "current_step": "initial_snapshot",
                "journal_sha256": "3" * 64,
                "receipt_sha256": "9" * 64,
                "compensation_status": "not_required",
                "retain_config": True,
                "retain_audit": True,
                "retain_secrets": True,
                "reason_code": None,
            },
        )
        assert skipped_steps.status_code == 409
        assert skipped_steps.json()["code"] == "edge.state_conflict"

        revision = 1
        transitions = (
            ("installing", "staging_prepared"),
            ("installed", "release_committed"),
            ("connecting", "device_registered"),
            ("active", "initial_snapshot"),
        )
        for sequence, (next_status, step) in enumerate(transitions, start=1):
            response = await client.patch(
                f"/edge/installations/{installation_id}",
                headers=_headers(
                    key=f"prd124-install-transition-{sequence:04d}", revision=revision
                ),
                json={
                    "status": next_status,
                    "current_step": step,
                    "journal_sha256": f"{sequence + 2:x}" * 64,
                    "receipt_sha256": f"{sequence + 8:x}" * 64,
                    "compensation_status": "not_required",
                    "retain_config": True,
                    "retain_audit": True,
                    "retain_secrets": True,
                    "reason_code": None,
                },
            )
            assert response.status_code == 200, response.text
            revision += 1
            assert response.headers["etag"] == f'"{revision}"'

        missing_step_up = await client.patch(
            f"/edge/installations/{installation_id}",
            headers=_headers(key="prd124-uninstall-start-0001", revision=revision),
            json={
                "status": "uninstalling",
                "current_step": None,
                "journal_sha256": "d" * 64,
                "receipt_sha256": None,
                "compensation_status": "not_required",
                "retain_config": False,
                "retain_audit": True,
                "retain_secrets": True,
                "reason_code": None,
            },
        )
        assert missing_step_up.status_code == 403
        assert missing_step_up.json()["code"] == "step_up.required"

        consumed: list[str] = []

        def consume(*_args, **kwargs) -> None:
            consumed.append(str(kwargs["action_code"]))

        monkeypatch.setattr(
            installer_service, "consume_step_up_grant_in_transaction", consume
        )
        uninstalling = await client.patch(
            f"/edge/installations/{installation_id}",
            headers=_headers(
                key="prd124-uninstall-start-0002",
                revision=revision,
                step_up="s" * 43,
            ),
            json={
                "status": "uninstalling",
                "current_step": None,
                "journal_sha256": "d" * 64,
                "receipt_sha256": None,
                "compensation_status": "not_required",
                "retain_config": False,
                "retain_audit": True,
                "retain_secrets": True,
                "reason_code": None,
            },
        )
        assert uninstalling.status_code == 200, uninstalling.text
        assert consumed == ["edge.installation.purge"]

        listed = await client.get("/edge/installers", headers=_headers())
        listed_body = listed.json()
        installation = listed_body["installations"][0]
        assert installation["installation_id"] == installation_id
        assert installation["status"] == "uninstalling"
        assert installation["retain_config"] is False
        assert [item["outcome"] for item in listed_body["preflights"]] == [
            "passed",
            "failed",
        ]
        assert [item["code"] for item in listed_body["preflights"][0]["checks"]] == list(
            PREFLIGHT_CHECK_CODES
        )

    with closing(connect_database(config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_install_audits WHERE installation_id=?",
            (installation_id,),
        ).fetchone()[0] == 6
        audit_type_counts = {
            str(row["event_type"]): int(row["event_count"])
            for row in connection.execute(
                "SELECT event_type,COUNT(*) event_count FROM edge_install_audits "
                "WHERE installation_id=? GROUP BY event_type",
                (installation_id,),
            ).fetchall()
        }
        assert audit_type_counts == {
            "edge.install_started": 5,
            "edge.install_completed": 1,
        }
        events = {
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM edge_install_outbox WHERE installation_id=?",
                (installation_id,),
            ).fetchall()
        }
        assert events == {"edge.install_started", "edge.install_completed"}


@pytest.mark.anyio
async def test_installer_api_rejects_unknown_fields_stale_revision_and_cross_tenant_access(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, actor_holder = runtime
    app = _app(monkeypatch, config, actor_holder)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        deployment_id = await _deployment(
            client, key="prd124-create-deployment-isolation-0001"
        )
        report_id = "pfr_" + "3" * 32
        invalid = _report(deployment_id, report_id, 1, passed=True)
        invalid["secret"] = "must-not-be-accepted"
        rejected = await client.post(
            "/edge/preflights",
            headers=_headers(key="prd124-preflight-invalid-0001"),
            json=invalid,
        )
        assert rejected.status_code == 400
        assert rejected.json()["code"] == "edge.request_invalid"

        accepted = await client.post(
            "/edge/preflights",
            headers=_headers(key="prd124-preflight-valid-0001"),
            json=_report(deployment_id, report_id, 1, passed=True),
        )
        assert accepted.status_code == 201, accepted.text
        installation_id = "ins_" + "3" * 32
        started = await client.post(
            "/edge/installations",
            headers=_headers(key="prd124-install-cross-0001"),
            json={
                "installation_id": installation_id,
                "deployment_id": deployment_id,
                "preflight_report_id": report_id,
                "generation": 1,
                "journal_sha256": "e" * 64,
            },
        )
        assert started.status_code == 201, started.text

        update = {
            "status": "installing",
            "current_step": "staging_prepared",
            "journal_sha256": "f" * 64,
            "receipt_sha256": "1" * 64,
            "compensation_status": "not_required",
            "retain_config": True,
            "retain_audit": True,
            "retain_secrets": True,
            "reason_code": None,
        }
        stale = await client.patch(
            f"/edge/installations/{installation_id}",
            headers=_headers(key="prd124-install-stale-0001", revision=2),
            json=update,
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "edge.revision_conflict"

        owner = actor_holder["actor"]
        actor_holder["actor"] = replace(
            owner,
            workspace_id="wsp_" + "9" * 32,
            tenant_id="ten_" + "9" * 32,
            workspace_session_id="wss_" + "9" * 32,
        )
        hidden = await client.patch(
            f"/edge/installations/{installation_id}",
            headers=_headers(key="prd124-install-hidden-0001", revision=1),
            json=update,
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "edge.installation_not_found"
        actor_holder["actor"] = owner


@pytest.mark.anyio
async def test_platform_release_requires_two_people_and_controls_workspace_visibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: tuple[AppConfig, dict[str, WorkspaceSessionContext]],
) -> None:
    config, actor_holder = runtime
    actor_holder["actor"] = _owner_actor(config)
    envelope, public_key = _test_release(tmp_path / "installers")
    submitter, approver = _prepare_platform_operators(config, public_key)
    platform_holder = {"context": submitter}
    monkeypatch.setattr(installer_service, "_INSTALLER_ASSET_ROOT", tmp_path / "installers")
    consumed: list[tuple[str, str]] = []

    def consume(*_args, **kwargs) -> None:
        consumed.append((str(kwargs["account_id"]), str(kwargs["action_code"])))

    monkeypatch.setattr(
        installer_service, "consume_global_step_up_grant_in_transaction", consume
    )
    app = _app(monkeypatch, config, actor_holder, platform_holder)
    payload = {"release": envelope, "reason_code": "release_candidate"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get(
            "/platform/edge/installers", headers=_platform_headers()
        )
        assert listed.status_code == 200, listed.text
        assert listed.headers["cache-control"] == "no-store, private"
        assert listed.json()["security_projection"] is False

        conflicting = _platform_headers()
        conflicting["Cookie"] += f"; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"
        rejected_credential = await client.get(
            "/platform/edge/installers", headers=conflicting
        )
        assert rejected_credential.status_code == 400
        assert rejected_credential.json()["code"] == "platform.credential_conflict"

        missing_origin_headers = _platform_headers(key="prd124-platform-submit-origin-0001")
        missing_origin_headers.pop("Origin")
        missing_origin = await client.post(
            "/platform/edge/installers",
            headers=missing_origin_headers,
            json=payload,
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json()["code"] == "platform.origin_invalid"

        submitted = await client.post(
            "/platform/edge/installers",
            headers=_platform_headers(key="prd124-platform-submit-release-0001"),
            json=payload,
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.headers["etag"] == '"1"'
        assert submitted.headers["cache-control"] == "no-store, private"
        assert submitted.json()["release"]["status"] == "approval_pending"
        assert submitted.json()["release"]["release"]["manifest"]["release_id"] == (
            TEST_RELEASE_ID
        )

        replay = await client.post(
            "/platform/edge/installers",
            headers=_platform_headers(key="prd124-platform-submit-release-0001"),
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True

        workspace_pending = await client.get("/edge/installers", headers=_headers())
        assert workspace_pending.status_code == 200, workspace_pending.text
        assert TEST_RELEASE_ID not in {
            item["manifest"]["release_id"]
            for item in workspace_pending.json()["releases"]
        }

        self_approval = await client.patch(
            f"/platform/edge/installers/{TEST_RELEASE_ID}",
            headers=_platform_headers(
                key="prd124-platform-self-approve-0001",
                revision=1,
                step_up="s" * 43,
            ),
            json={"action": "approve", "reason_code": "release_approved"},
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == (
            "platform.edge_installer_self_approval_denied"
        )
        assert consumed == []

        platform_holder["context"] = approver
        approved = await client.patch(
            f"/platform/edge/installers/{TEST_RELEASE_ID}",
            headers=_platform_headers(
                key="prd124-platform-approve-release-0001",
                revision=1,
                step_up="t" * 43,
            ),
            json={"action": "approve", "reason_code": "release_approved"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.headers["etag"] == '"2"'
        assert approved.json()["release"]["status"] == "released"
        assert approved.json()["release"]["approved_by_account_id"] == approver.account_id

        approval_replay = await client.patch(
            f"/platform/edge/installers/{TEST_RELEASE_ID}",
            headers=_platform_headers(
                key="prd124-platform-approve-release-0001",
                revision=1,
                step_up="t" * 43,
            ),
            json={"action": "approve", "reason_code": "release_approved"},
        )
        assert approval_replay.status_code == 200, approval_replay.text
        assert approval_replay.json()["idempotent_replay"] is True

        workspace_released = await client.get("/edge/installers", headers=_headers())
        assert workspace_released.status_code == 200, workspace_released.text
        assert TEST_RELEASE_ID in {
            item["manifest"]["release_id"]
            for item in workspace_released.json()["releases"]
        }
        test_download = next(
            item
            for item in workspace_released.json()["downloads"]
            if TEST_RELEASE_ID in item["download_path"]
        )
        downloaded = await client.get(test_download["download_path"], headers=_headers())
        assert downloaded.status_code == 200, downloaded.text
        assert hashlib.sha256(downloaded.content).hexdigest() == test_download["sha256"]

        platform_holder["context"] = submitter
        withdrawn = await client.patch(
            f"/platform/edge/installers/{TEST_RELEASE_ID}",
            headers=_platform_headers(
                key="prd124-platform-withdraw-release-0001",
                revision=2,
                step_up="u" * 43,
            ),
            json={"action": "withdraw", "reason_code": "superseded"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.headers["etag"] == '"3"'
        assert withdrawn.json()["release"]["status"] == "withdrawn"

        workspace_withdrawn = await client.get("/edge/installers", headers=_headers())
        assert workspace_withdrawn.status_code == 200, workspace_withdrawn.text
        assert TEST_RELEASE_ID not in {
            item["manifest"]["release_id"]
            for item in workspace_withdrawn.json()["releases"]
        }

        platform_holder["context"] = replace(
            submitter, permissions=("platform.edge_installer.read",)
        )
        denied = await client.post(
            "/platform/edge/installers",
            headers=_platform_headers(key="prd124-platform-read-only-denied-0001"),
            json=payload,
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "platform.permission_denied"

    assert consumed == [
        (approver.account_id, "platform.edge_installer.approve"),
        (submitter.account_id, "platform.edge_installer.withdraw"),
    ]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            "SELECT * FROM installer_releases WHERE installer_release_id=?",
            (TEST_RELEASE_ID,),
        ).fetchone()
        assert row is not None
        assert (row["status"], row["revision"]) == ("withdrawn", 3)
        assert row["submitted_by_account_id"] == submitter.account_id
        assert row["approved_by_account_id"] == approver.account_id
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_installer_release_commands "
            "WHERE installer_release_id=?",
            (TEST_RELEASE_ID,),
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_audit_logs "
            "WHERE object_type='installer_release' AND object_id=?",
            (TEST_RELEASE_ID,),
        ).fetchone()[0] == 3
