"""PRD-131 assessment, migration run, cutover, rollback, and Support JIT API."""

from __future__ import annotations

import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.migration_operations import service as migration_service
from metahub_control.migration_operations.routes import router
from metahub_control.platform_access import service as platform_service
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43
STEP_UP = "u" * 43
SUPPORT_TOKEN = "support_" + "s" * 50
BREAK_GLASS_TOKEN = "support_" + "b" * 50
SUPPORT_ACCOUNT_ID = "gac_" + "a" * 32
APPROVER_ACCOUNT_ID = "gac_" + "b" * 32
SUPPORT_OPERATOR_ID = "pto_" + "a" * 32
APPROVER_OPERATOR_ID = "pto_" + "b" * 32
SUPPORT_FACTOR_ID = "mfa_" + "a" * 32
SUPPORT_GRANT_ID = "sgr_" + "a" * 32
SUPPORT_SESSION_ID = "sps_" + "a" * 32
BREAK_GLASS_ID = "bgs_" + "b" * 32
BREAK_GLASS_SESSION_ID = "sps_" + "b" * 32
_TEST_PASSWORD = f"Prd131!Aa9{secrets.token_urlsafe(18)}"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd131-api-base") / "base.sqlite3"
    database_url = _url(database)
    assert "0123" in apply_migrations(database_url).applied
    assert not apply_migrations(database_url).applied
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(base_database: Path, tmp_path: Path) -> tuple[AppConfig, WorkspaceSessionContext]:
    database = tmp_path / "prd131.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    return config, _owner_actor(config)


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """
            SELECT membership.workspace_membership_id,membership.workspace_id,
              membership.tenant_id,membership.account_id,membership.legacy_user_id,
              tenant.slug,tenant.name,legacy_user.email,legacy_user.display_name
            FROM workspace_memberships membership
            JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
            JOIN users legacy_user ON legacy_user.user_id=membership.legacy_user_id
              AND legacy_user.tenant_id=membership.tenant_id
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
    assert {"migration.read", "migration.manage"} <= set(snapshot.permission_codes)
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


def _app(
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig,
    actor_holder: dict[str, WorkspaceSessionContext],
):
    monkeypatch.setattr(
        migration_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor_holder["context"],
    )
    consumed: list[tuple[str, str, str]] = []

    def consume(*_args, **kwargs):
        assert kwargs["grant"] == STEP_UP
        consumed.append((kwargs["action_code"], kwargs["resource_type"], kwargs["resource_id"]))
        return "totp"

    monkeypatch.setattr(migration_service, "consume_step_up_grant_in_transaction", consume)
    app = create_service_app(
        "control-plane-api",
        "PRD-131 Migration Test",
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
    return app, consumed


def _workspace_headers(
    *,
    key: str | None = None,
    revision: int | None = None,
    step_up: bool = False,
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
    if step_up:
        headers["X-Step-Up-Grant"] = STEP_UP
    return headers


def _assessment_payload(*, manifest: str = "a" * 64) -> dict[str, object]:
    return {
        "source_kind": "legacy_tenant",
        "manifest_version": "legacy-v1",
        "source_manifest_sha256": manifest,
        "domains": ["audit"],
        "reason_code": "initial_assessment",
        "confirmed": True,
    }


def _run_payload(assessment_id: str) -> dict[str, object]:
    return {
        "assessment_id": assessment_id,
        "expected_assessment_revision": 1,
        "reason_code": "customer_approved",
        "confirmed": True,
    }


def _transition_payload(revision: int, reason: str) -> dict[str, object]:
    return {"expected_revision": revision, "reason_code": reason, "confirmed": True}


@pytest.mark.anyio
async def test_assessment_run_cutover_rollback_and_owner_isolation(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext],
) -> None:
    config, owner = runtime
    actor_holder = {"context": owner}
    app, consumed = _app(monkeypatch, config, actor_holder)
    payload = _assessment_payload()
    payload["domains"] = list(migration_service._DOMAINS)
    responses: list[str] = []
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        unknown = await client.post(
            "/migrations/assessments",
            headers=_workspace_headers(key="prd131-assessment-unknown-01"),
            json={**payload, "source_sql": "forbidden"},
        )
        assert unknown.status_code == 400
        assert unknown.json()["code"] == "migration.request_invalid"

        mixed_headers = _workspace_headers(key="prd131-assessment-mixed-001")
        mixed_headers["Authorization"] = "Bearer " + "z" * 43
        mixed = await client.post("/migrations/assessments", headers=mixed_headers, json=payload)
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"

        headers = _workspace_headers(key="prd131-assessment-create-01")
        created = await client.post("/migrations/assessments", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        responses.append(created.text)
        assessment = created.json()["assessment"]
        assessment_id = str(assessment["assessment_id"])
        assert assessment["status"] == "ready"
        assert assessment["write_watermark_before"] == assessment["write_watermark_after"]
        assert assessment["blocking_difference_count"] == 0
        assert assessment["permission_expansion_count"] == 0
        assert assessment["plan"]["status"] == "ready"
        assert created.headers["ETag"] == '"1"'

        replay = await client.post("/migrations/assessments", headers=headers, json=payload)
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["assessment"]["assessment_id"] == assessment_id

        conflict = await client.post(
            "/migrations/assessments",
            headers=headers,
            json=_assessment_payload(manifest="b" * 64),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "migration.idempotency_conflict"

        developer = replace(
            owner,
            membership_role="developer",
            legacy_role="developer",
            permission_codes=("migration.read",),
        )
        actor_holder["context"] = developer
        denied = await client.post(
            "/migrations/assessments",
            headers=_workspace_headers(key="prd131-developer-denied-01"),
            json=payload,
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "migration.permission_denied"
        readable = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers=_workspace_headers(),
        )
        assert readable.status_code == 200
        assert readable.json()["items"] == []

        actor_holder["context"] = replace(
            developer,
            workspace_id="wsp_" + "f" * 32,
            tenant_id="ten_" + "f" * 32,
        )
        hidden = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers=_workspace_headers(),
        )
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "migration.not_found"
        actor_holder["context"] = owner

        run_created = await client.post(
            "/migrations/runs",
            headers=_workspace_headers(key="prd131-run-create-000001", revision=1),
            json=_run_payload(assessment_id),
        )
        assert run_created.status_code == 201, run_created.text
        responses.append(run_created.text)
        run = run_created.json()["run"]
        run_id = str(run["run_id"])
        assert run["status"] == "observing"
        assert [item["phase_code"] for item in run["checkpoints"]] == [
            "assessment",
            "backfill",
            "dual_read",
            "freeze",
        ]
        assert run["rollback_plan"]["status"] == "ready"

        missing_step_up = await client.post(
            f"/migrations/{run_id}/cutover",
            headers=_workspace_headers(key="prd131-cutover-no-stepup-01", revision=1),
            json=_transition_payload(1, "customer_approved"),
        )
        assert missing_step_up.status_code == 403
        assert missing_step_up.json()["code"] == "step_up.required"

        cutover = await client.post(
            f"/migrations/{run_id}/cutover",
            headers=_workspace_headers(key="prd131-cutover-execute-01", revision=1, step_up=True),
            json=_transition_payload(1, "customer_approved"),
        )
        assert cutover.status_code == 200, cutover.text
        responses.append(cutover.text)
        assert cutover.json()["run"]["status"] == "completed"
        assert cutover.json()["run"]["revision"] == 2

        rollback = await client.post(
            f"/migrations/{run_id}/rollback",
            headers=_workspace_headers(key="prd131-rollback-execute-01", revision=2, step_up=True),
            json=_transition_payload(2, "customer_requested_rollback"),
        )
        assert rollback.status_code == 200, rollback.text
        responses.append(rollback.text)
        final_run = rollback.json()["run"]
        assert final_run["status"] == "rolled_back"
        assert final_run["revision"] == 4
        assert len(final_run["checkpoints"]) == 6
        assert final_run["rollback_plan"]["status"] == "completed"

    assert consumed == [
        ("migration.cutover.execute", "migration_run", run_id),
        ("migration.rollback.execute", "migration_run", run_id),
    ]
    forbidden = (_TEST_PASSWORD, WORKSPACE_TOKEN, WORKSPACE_CSRF, STEP_UP)
    assert all(value not in body for body in responses for value in forbidden)
    with closing(connect_database(config.database_url)) as connection:
        head = connection.execute(
            "SELECT * FROM migration_cutover_heads WHERE workspace_id=? AND tenant_id=?",
            (owner.workspace_id, owner.tenant_id),
        ).fetchone()
        assert head is not None
        assert head["mode"] == "legacy"
        assert head["active_migration_run_id"] is None
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM migration_events "
                "WHERE migration_run_id=? ORDER BY occurred_at",
                (run_id,),
            ).fetchall()
        ] == [
            "migration.started",
            "migration.cutover_completed",
            "migration.rolled_back",
        ]
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM migration_event_outbox outbox
               JOIN migration_events event
                 ON event.migration_event_id=outbox.migration_event_id
               WHERE event.migration_run_id=?""",
                (run_id,),
            ).fetchone()[0]
            == 6
        )
        stored = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT idempotency_sha256 FROM migration_action_commands"
            ).fetchall()
        )
        assert "prd131-" not in stored


@pytest.mark.anyio
async def test_permission_expansion_and_projection_drift_are_blocking(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext],
) -> None:
    config, owner = runtime
    actor_holder = {"context": owner}
    app, _ = _app(monkeypatch, config, actor_holder)
    original_permission_count = migration_service._permission_expansion_count
    original_capture = migration_service._capture_snapshots
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        monkeypatch.setattr(migration_service, "_permission_expansion_count", lambda *_args: 1)
        blocked = await client.post(
            "/migrations/assessments",
            headers=_workspace_headers(key="prd131-permission-expansion-01"),
            json=_assessment_payload(manifest="c" * 64),
        )
        assert blocked.status_code == 201, blocked.text
        blocked_assessment = blocked.json()["assessment"]
        assert blocked_assessment["status"] == "validation_failed"
        assert blocked_assessment["permission_expansion_count"] == 1
        assert blocked_assessment["blocking_difference_count"] == 1
        blocked_id = str(blocked_assessment["assessment_id"])
        differences = await client.get(
            f"/migrations/{blocked_id}/differences", headers=_workspace_headers()
        )
        assert differences.status_code == 200
        assert [item["difference_code"] for item in differences.json()["items"]] == [
            "permission_expansion"
        ]
        rejected_run = await client.post(
            "/migrations/runs",
            headers=_workspace_headers(key="prd131-blocked-run-00001", revision=1),
            json=_run_payload(blocked_id),
        )
        assert rejected_run.status_code == 409
        assert rejected_run.json()["code"] == "migration.assessment_conflict"

        monkeypatch.setattr(
            migration_service, "_permission_expansion_count", original_permission_count
        )
        ready = await client.post(
            "/migrations/assessments",
            headers=_workspace_headers(key="prd131-drift-assessment-01"),
            json=_assessment_payload(manifest="d" * 64),
        )
        assert ready.status_code == 201, ready.text
        ready_id = str(ready.json()["assessment"]["assessment_id"])

        def drifted(connection, context, domains):
            snapshots = original_capture(connection, context, domains)
            return tuple(
                replace(
                    item,
                    source_count=item.source_count + 1,
                    source_sha256="f" * 64,
                    source_sequence=item.source_sequence + 1,
                    source_watermark=item.source_watermark + 1,
                )
                for item in snapshots
            )

        monkeypatch.setattr(migration_service, "_capture_snapshots", drifted)
        drift_run = await client.post(
            "/migrations/runs",
            headers=_workspace_headers(key="prd131-drift-run-create-01", revision=1),
            json=_run_payload(ready_id),
        )
        assert drift_run.status_code == 201, drift_run.text
        run = drift_run.json()["run"]
        assert run["status"] == "failed"
        assert run["blocking_difference_count"] == 4
        assert run["checkpoints"] == []
        assert run["rollback_plan"] is None
        drift_differences = await client.get(
            f"/migrations/{run['run_id']}/differences", headers=_workspace_headers()
        )
        assert drift_differences.status_code == 200
        assert {item["difference_code"] for item in drift_differences.json()["items"]} == {
            "count_mismatch",
            "hash_mismatch",
            "sequence_mismatch",
            "watermark_mismatch",
        }


def _prepare_support_principals(config: AppConfig, actor: WorkspaceSessionContext) -> None:
    now = datetime.now(UTC)
    stamp = _time(now)
    with closing(connect_database(config.database_url)) as connection:
        source = connection.execute(
            "SELECT * FROM global_accounts WHERE account_id=?", (actor.account_id,)
        ).fetchone()
        assert source is not None
        for account_id, operator_id, role, suffix in (
            (SUPPORT_ACCOUNT_ID, SUPPORT_OPERATOR_ID, "platform_support", "support"),
            (APPROVER_ACCOUNT_ID, APPROVER_OPERATOR_ID, "platform_security", "approver"),
        ):
            values = dict(source)
            values.update(
                {
                    "account_id": account_id,
                    "email_normalized": f"prd131-{suffix}@example.invalid",
                    "phone_e164": None,
                    "display_name": f"PRD-131 {suffix}",
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
                """INSERT INTO platform_operators(
                     operator_id,account_id,status,revision,created_by_operator_id,
                     created_at,updated_at)
                   VALUES(?,?,'active',1,NULL,?,?)""",
                (operator_id, account_id, stamp, stamp),
            )
            connection.execute(
                """INSERT INTO platform_operator_role_bindings(
                     operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
                   VALUES(?,?,'active',NULL,?,NULL)""",
                (operator_id, role, stamp),
            )
        connection.execute(
            """INSERT INTO account_mfa_factors(
                 factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
                 revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
               VALUES(?,?,'webauthn',NULL,NULL,'active',1,NULL,?,NULL,?,?)""",
            (SUPPORT_FACTOR_ID, SUPPORT_ACCOUNT_ID, stamp, stamp, stamp),
        )
        workspace_revision = int(
            connection.execute(
                "SELECT revision FROM workspaces WHERE workspace_id=?", (actor.workspace_id,)
            ).fetchone()[0]
        )
        membership_revision = int(
            connection.execute(
                "SELECT revision FROM workspace_memberships WHERE workspace_membership_id=?",
                (actor.membership_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO support_grants(
                 grant_id,requester_operator_id,workspace_id,tenant_id,scope_code,reason_code,
                 status,operator_revision,workspace_revision,expires_at,revoked_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,'workspace.diagnostics.read','migration_review',
                 'active',1,?,?,NULL,?,?)""",
            (
                SUPPORT_GRANT_ID,
                SUPPORT_OPERATOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                workspace_revision,
                _time(now + timedelta(minutes=15)),
                stamp,
                stamp,
            ),
        )
        connection.executemany(
            """INSERT INTO support_grant_approvals(
                 approval_id,grant_id,approval_domain,operator_id,membership_id,
                 authority_revision,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                (
                    "sap_" + "a" * 31 + "1",
                    SUPPORT_GRANT_ID,
                    "platform",
                    APPROVER_OPERATOR_ID,
                    None,
                    1,
                    stamp,
                ),
                (
                    "sap_" + "a" * 31 + "2",
                    SUPPORT_GRANT_ID,
                    "workspace",
                    None,
                    actor.membership_id,
                    membership_revision,
                    stamp,
                ),
            ),
        )
        connection.execute(
            """INSERT INTO support_sessions(
                 support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,
                 tenant_id,scope_code,token_hmac,status,operator_revision,factor_revision,
                 absolute_expires_at,revoked_at,created_at,updated_at)
               VALUES(?,'grant',?,?,?,?,?,'workspace.diagnostics.read',?,'active',1,1,
                 ?,NULL,?,?)""",
            (
                SUPPORT_SESSION_ID,
                SUPPORT_GRANT_ID,
                SUPPORT_OPERATOR_ID,
                SUPPORT_FACTOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                platform_service._support_hmac(config, SUPPORT_TOKEN),
                _time(now + timedelta(minutes=10)),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO break_glass_sessions(
                 break_glass_id,requester_operator_id,workspace_id,tenant_id,reason_code,
                 status,expires_at,review_due_at,created_at,updated_at)
               VALUES(?,?,?,?,'migration_outage','active',?,?,?,?)""",
            (
                BREAK_GLASS_ID,
                SUPPORT_OPERATOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                _time(now + timedelta(minutes=10)),
                _time(now + timedelta(hours=24)),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO support_sessions(
                 support_session_id,source_kind,source_id,operator_id,factor_id,workspace_id,
                 tenant_id,scope_code,token_hmac,status,operator_revision,factor_revision,
                 absolute_expires_at,revoked_at,created_at,updated_at)
               VALUES(?,'break_glass',?,?,?,?,?,'workspace.diagnostics.read',?,'active',1,1,
                 ?,NULL,?,?)""",
            (
                BREAK_GLASS_SESSION_ID,
                BREAK_GLASS_ID,
                SUPPORT_OPERATOR_ID,
                SUPPORT_FACTOR_ID,
                actor.workspace_id,
                actor.tenant_id,
                platform_service._support_hmac(config, BREAK_GLASS_TOKEN),
                _time(now + timedelta(minutes=10)),
                stamp,
                stamp,
            ),
        )
        connection.commit()


@pytest.mark.anyio
async def test_support_jit_summary_revocation_and_break_glass_rejection(
    monkeypatch: pytest.MonkeyPatch,
    runtime: tuple[AppConfig, WorkspaceSessionContext],
) -> None:
    config, owner = runtime
    actor_holder = {"context": owner}
    app, _ = _app(monkeypatch, config, actor_holder)
    original_permission_count = migration_service._permission_expansion_count
    monkeypatch.setattr(migration_service, "_permission_expansion_count", lambda *_args: 1)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/migrations/assessments",
            headers=_workspace_headers(key="prd131-support-assessment-01"),
            json=_assessment_payload(manifest="e" * 64),
        )
        assert created.status_code == 201, created.text
        assessment_id = str(created.json()["assessment"]["assessment_id"])
        monkeypatch.setattr(
            migration_service, "_permission_expansion_count", original_permission_count
        )
        _prepare_support_principals(config, owner)

        support = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers={"Cookie": f"{SUPPORT_SESSION_COOKIE}={SUPPORT_TOKEN}"},
        )
        assert support.status_code == 200, support.text
        body = support.json()
        assert body["support_scoped"] is True
        assert len(body["items"]) == 1
        assert body["items"][0]["difference_code"] == "permission_expansion"
        forbidden_fields = {
            "workspace_id",
            "tenant_id",
            "actor_account_id",
            "credential",
            "source_manifest_sha256",
        }
        assert forbidden_fields.isdisjoint(body)
        assert SUPPORT_TOKEN not in support.text

        with closing(connect_database(config.database_url)) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM migration_support_access_audits "
                    "WHERE support_session_id=? AND field_scope='difference_summary'",
                    (SUPPORT_SESSION_ID,),
                ).fetchone()[0]
                == 1
            )
            now = _time(datetime.now(UTC) + timedelta(milliseconds=1))
            connection.execute(
                "UPDATE support_grants SET status='revoked',revoked_at=?,updated_at=? "
                "WHERE grant_id=?",
                (now, now, SUPPORT_GRANT_ID),
            )
            connection.commit()

        revoked = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers={"Cookie": f"{SUPPORT_SESSION_COOKIE}={SUPPORT_TOKEN}"},
        )
        assert revoked.status_code == 401
        assert revoked.json()["code"] == "support.session_invalid"

        break_glass = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers={"Cookie": f"{SUPPORT_SESSION_COOKIE}={BREAK_GLASS_TOKEN}"},
        )
        assert break_glass.status_code == 401
        assert break_glass.json()["code"] == "support.session_invalid"

        mixed = await client.get(
            f"/migrations/{assessment_id}/differences",
            headers={
                "Cookie": (
                    f"{SUPPORT_SESSION_COOKIE}={BREAK_GLASS_TOKEN}; "
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{PLATFORM_SESSION_COOKIE}={'p' * 43}"
                )
            },
        )
        assert mixed.status_code == 400
        assert mixed.json()["code"] == "workspace_session.credential_conflict"
