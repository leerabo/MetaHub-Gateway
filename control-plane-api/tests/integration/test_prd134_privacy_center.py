"""PRD-134 dual-session preferences, DSR orchestration, and one-time export."""

from __future__ import annotations

import os
import secrets
import shutil
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.identity.service import CSRF_COOKIE, SESSION_COOKIE
from metahub_control.platform_access.constants import PLATFORM_SESSION_COOKIE
from metahub_control.platform_access.service import PlatformContext
from metahub_control.privacy_center import service
from metahub_control.privacy_center.routes import router
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_access.service import WorkspaceSessionContext
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.schema import foreign_key_violation_count
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
GLOBAL_TOKEN = "g" * 43
WORKSPACE_TOKEN = "w" * 43
GLOBAL_CSRF = "c" * 43
PLATFORM_TOKEN = "p" * 43


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
        identity_session_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        support_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(48),
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd134-api-base") / "base.sqlite3"
    database_url = _url(database)
    supplied = os.environ.get("METAHUB_TEST_MIGRATED_BASELINE")
    if supplied:
        shutil.copy2(Path(supplied).resolve(strict=True), database)
        apply_migrations(database_url)
        return database
    assert "0126" in apply_migrations(database_url).applied
    assert not apply_migrations(database_url).applied
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppConfig, WorkspaceSessionContext, PlatformContext]:
    database = tmp_path / "prd134.sqlite3"
    shutil.copy2(base_database, database)
    return _runtime(_config(_url(database)), monkeypatch)


def _runtime(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    with closing(connect_database(config.database_url)) as connection:
        owner = connection.execute(
            """SELECT member.workspace_membership_id,member.workspace_id,member.tenant_id,
                      member.account_id,member.legacy_user_id,account.email_normalized,
                      account.display_name,tenant.slug,tenant.name
                 FROM workspace_memberships member
                 JOIN global_accounts account ON account.account_id=member.account_id
                 JOIN tenants tenant ON tenant.tenant_id=member.tenant_id
                WHERE member.status='active' AND member.role_code='owner'
                ORDER BY member.workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
    expires = datetime.now(UTC) + timedelta(hours=1)
    actor = WorkspaceSessionContext(
        workspace_session_id="wss_" + "a" * 32,
        global_session_id="gss_" + "a" * 32,
        account_id=str(owner["account_id"]),
        workspace_id=str(owner["workspace_id"]),
        tenant_id=str(owner["tenant_id"]),
        membership_id=str(owner["workspace_membership_id"]),
        legacy_user_id=str(owner["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email=str(owner["email_normalized"]),
        display_name=str(owner["display_name"]),
        tenant_slug=str(owner["slug"]),
        tenant_name=str(owner["name"]),
        permission_codes=(
            "privacy.preference.read",
            "privacy.preference.manage",
            "privacy.dsr.read",
            "privacy.dsr.create",
            "privacy.dsr.cancel",
        ),
        permission_revision=50,
        authorization_fingerprint="a" * 64,
        expires_at=expires,
        token=WORKSPACE_TOKEN,
    )
    platform = PlatformContext(
        platform_session_id="pts_" + "b" * 32,
        operator_id="pto_" + "b" * 32,
        account_id=actor.account_id,
        global_session_id="gss_" + "b" * 32,
        factor_id="mfa_" + "b" * 32,
        factor_revision=1,
        operator_revision=1,
        roles=("platform_privacy",),
        permissions=("platform.privacy.read",),
        expires_at=expires,
    )

    def global_auth(_connection, _config, **_kwargs):
        return SimpleNamespace(
            account_id=actor.account_id, session_id=actor.global_session_id
        ), datetime.now(UTC)

    monkeypatch.setattr(service, "authenticate_global_session_in_transaction", global_auth)
    monkeypatch.setattr(
        service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *_args, **_kwargs: platform,
    )
    monkeypatch.setattr(
        service,
        "consume_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: "totp",
    )
    return config, actor, platform


def _app(config: AppConfig):
    app = create_service_app(
        "control-plane-api",
        "PRD-134 Privacy Test",
        extra_cors_allow_headers=(
            "If-Match",
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Privacy-Export-Receipt",
        ),
        cors_allow_credentials=True,
        extra_cors_expose_headers=(
            "ETag",
            "Digest",
            "X-Artifact-Receipt-SHA256",
            "Content-Disposition",
        ),
    )
    app.state.config = config
    app.include_router(router)
    return app


def _identity_headers(*, write: bool = False, key: str | None = None) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{SESSION_COOKIE}={GLOBAL_TOKEN}; {WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{CSRF_COOKIE}={GLOBAL_CSRF}"
        )
    }
    if write:
        headers.update(
            {
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-site",
                "X-CSRF-Token": GLOBAL_CSRF,
            }
        )
    if key:
        headers["Idempotency-Key"] = key
    return headers


@pytest.mark.anyio
async def test_preferences_export_once_and_minimal_platform_queue(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, actor, _platform = runtime
    async with AsyncClient(
        transport=ASGITransport(app=_app(config)), base_url="http://test"
    ) as client:
        initial = await client.get("/identity/privacy-preferences", headers=_identity_headers())
        assert initial.status_code == 200, initial.text
        assert initial.headers["cache-control"] == "no-store, private"
        assert initial.json()["preference"]["revision"] == 1
        updated = await client.patch(
            "/identity/privacy-preferences",
            headers={
                **_identity_headers(write=True, key="prd134-preference-0001"),
                "If-Match": '"1"',
            },
            json={
                "analytics_enabled": True,
                "product_updates_enabled": False,
                "model_improvement_enabled": False,
                "terms_version": "privacy-2026-08",
                "locale": "zh-cn",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["preference"]["analytics_enabled"] is True
        assert any(
            item["purpose"] == "analytics" and item["decision"] == "granted"
            for item in updated.json()["consent_records"]
        )

        created = await client.post(
            "/identity/data-subject-requests",
            headers=_identity_headers(write=True, key="prd134-export-0001"),
            json={
                "request_kind": "export",
                "step_up_grant": "s" * 43,
                "confirmed": True,
            },
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["request"]["data_subject_request_id"]
        assert len(created.json()["request"]["owner_progress"]) == 7
        assert service.process_privacy_once(config, worker_id="prd134-test", limit=8) >= 7

        ready = await client.get(
            f"/identity/data-subject-requests/{request_id}", headers=_identity_headers()
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["request"]["status"] == "ready"
        export = ready.json()["request"]["identity_export"]
        receipt_headers = {
            **_identity_headers(write=True, key="prd134-export-receipt-0001"),
            "If-Match": f'"{export["revision"]}"',
        }
        receipt_response = await client.post(
            f"/identity/data-subject-requests/{request_id}/export-receipts",
            headers=receipt_headers,
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = receipt_response.json()["receipt"]
        receipt_replay = await client.post(
            f"/identity/data-subject-requests/{request_id}/export-receipts",
            headers=receipt_headers,
        )
        assert receipt_replay.status_code == 200, receipt_replay.text
        assert receipt_replay.json()["receipt"] == receipt
        with closing(connect_database(config.database_url)) as connection:
            stored_export = connection.execute(
                "SELECT receipt_hmac FROM identity_exports WHERE identity_export_id=?",
                (export["identity_export_id"],),
            ).fetchone()
            stored_commands = connection.execute(
                "SELECT idempotency_sha256,request_sha256,result_object_id "
                "FROM privacy_action_commands WHERE action_code='export.receipt.issue'"
            ).fetchall()
            assert stored_export is not None
            assert len(str(stored_export["receipt_hmac"])) == 64
            assert len(stored_commands) == 1
            stored_facts = (tuple(stored_export), [tuple(row) for row in stored_commands])
            assert receipt not in repr(stored_facts)
        downloaded = await client.get(
            f"/identity/exports/{export['identity_export_id']}/download",
            headers={**_identity_headers(), "X-Privacy-Export-Receipt": receipt},
        )
        assert downloaded.status_code == 200, downloaded.text
        artifact = downloaded.json()
        assert artifact["subject"] == {
            "account_id": actor.account_id,
            "workspace_id": actor.workspace_id,
        }
        assert set(artifact["owners"]) == {
            "identity",
            "membership",
            "audit",
            "billing",
            "support",
            "edge",
            "data_governance",
        }
        assert artifact["owners"]["data_governance"] == {"subject_data_collected": False}
        assert "active_hold_count" not in downloaded.text
        assert "policy_generation" not in downloaded.text
        assert "password" not in downloaded.text.lower()
        assert "prompt" not in downloaded.text.lower()
        replay = await client.get(
            f"/identity/exports/{export['identity_export_id']}/download",
            headers={**_identity_headers(), "X-Privacy-Export-Receipt": receipt},
        )
        assert replay.status_code == 403
        assert replay.json()["code"] == "privacy.export_receipt_invalid"

        queue = await client.get(
            "/platform/privacy/queue",
            headers={"Cookie": f"{PLATFORM_SESSION_COOKIE}={PLATFORM_TOKEN}"},
        )
        assert queue.status_code == 200, queue.text
        queue_text = queue.text
        assert actor.account_id not in queue_text
        assert actor.workspace_id not in queue_text
        assert actor.account_email not in queue_text
        assert queue.json()["items"][0]["request_code"].startswith("prq_")


@pytest.mark.anyio
async def test_erasure_decisions_retention_exceptions_and_idempotency(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, _actor, _platform = runtime
    async with AsyncClient(
        transport=ASGITransport(app=_app(config)), base_url="http://test"
    ) as client:
        headers = _identity_headers(write=True, key="prd134-erase-0001")
        body = {"request_kind": "erase", "step_up_grant": "e" * 43, "confirmed": True}
        created = await client.post("/identity/data-subject-requests", headers=headers, json=body)
        assert created.status_code == 201, created.text
        replay = await client.post("/identity/data-subject-requests", headers=headers, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        request_id = created.json()["request"]["data_subject_request_id"]
        assert service.process_privacy_once(config, worker_id="prd134-erase-test", limit=8) >= 7
        result = await client.get(
            f"/identity/data-subject-requests/{request_id}", headers=_identity_headers()
        )
        assert result.status_code == 200, result.text
        request = result.json()["request"]
        assert request["status"] == "partially_denied"
        assert len(request["erasure_decisions"]) == 7
        assert {item["outcome"] for item in request["erasure_decisions"]} >= {
            "anonymize",
            "retain",
            "erase",
            "not_found",
        }
        assert request["retention_exceptions"]
        with closing(connect_database(config.database_url)) as connection:
            assert foreign_key_violation_count(connection) == 0
            if getattr(connection, "backend", "sqlite") == "sqlite":
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_scheduler_lease_fences_concurrent_scans(
    runtime: tuple[AppConfig, WorkspaceSessionContext, PlatformContext],
) -> None:
    config, _actor, _platform = runtime
    now = datetime.now(UTC)
    with closing(connect_database(config.database_url)) as connection:
        state = connection.execute(
            "SELECT revision,updated_at,fencing_generation FROM privacy_scheduler_state "
            "WHERE singleton_id=1"
        ).fetchone()
        assert state is not None
        stamp = max(now, datetime.fromisoformat(str(state["updated_at"]).replace("Z", "+00:00")))
        stamp += timedelta(milliseconds=1)
        connection.execute(
            """UPDATE privacy_scheduler_state
                  SET lease_owner_id='other-worker',lease_token_sha256=?,lease_expires_at=?,
                      fencing_generation=fencing_generation+1,revision=revision+1,updated_at=?
                WHERE singleton_id=1""",
            (
                "a" * 64,
                (stamp + timedelta(minutes=2))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            ),
        )
        connection.commit()
        blocked_generation = int(state["fencing_generation"]) + 1

    assert service.process_privacy_once(config, worker_id="fenced-worker", limit=0) == 0
    with closing(connect_database(config.database_url)) as connection:
        blocked = connection.execute(
            "SELECT * FROM privacy_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        assert blocked["lease_owner_id"] == "other-worker"
        assert int(blocked["fencing_generation"]) == blocked_generation
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        changed_at = max(
            datetime.now(UTC),
            datetime.fromisoformat(str(blocked["updated_at"]).replace("Z", "+00:00")),
        ) + timedelta(milliseconds=1)
        connection.execute(
            """UPDATE privacy_scheduler_state
                  SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE singleton_id=1""",
            (
                expired_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                changed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            ),
        )
        connection.commit()

    assert service.process_privacy_once(config, worker_id="takeover-worker", limit=0) == 0
    with closing(connect_database(config.database_url)) as connection:
        released = connection.execute(
            "SELECT * FROM privacy_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        assert released["lease_owner_id"] is None
        assert released["lease_token_sha256"] is None
        assert released["lease_expires_at"] is None
        assert int(released["fencing_generation"]) == blocked_generation + 1
