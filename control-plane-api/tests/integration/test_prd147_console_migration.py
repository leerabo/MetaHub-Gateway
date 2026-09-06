"""PRD-147 console migration API, authority, and transition contracts."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.console_migration import outbox as migration_outbox
from metahub_control.console_migration import routes, service, worker
from metahub_control.console_migration import scheduler as migration_scheduler
from metahub_control.console_migration.manifest import MANDATORY_RECONCILIATION_CHECKS
from metahub_control.console_migration.outbox import (
    claim_console_migration_event,
    dispatch_console_migration_event_once,
    finalize_console_migration_event,
    renew_console_migration_event,
)
from metahub_control.console_migration.routes import router
from metahub_control.console_migration.scheduler import (
    claim_scheduler_lease,
    release_scheduler_lease,
)
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.runtime import PlatformJobOuterFence, run_platform_job_once
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_navigation import service as workspace_navigation_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ORIGIN = "http://localhost:5173"
NOW = datetime.now(UTC).replace(microsecond=0)
STAMP = NOW.isoformat().replace("+00:00", "Z")
INDIRECT_OWNER_TABLES = (
    "workspace_console_event_outbox",
    "workspace_role_task_home_sources",
    "workspace_advanced_tool_entitlement_sources",
    "workspace_navigation_event_outbox",
)


def _assert_indirect_owner_evidence(connection, monkeypatch, table):
    domain, spec = next(
        (domain, spec)
        for domain, specs in worker._OWNER_EVIDENCE_TABLES.items()
        for spec in specs if spec.table == table
    )
    monkeypatch.setitem(worker._OWNER_EVIDENCE_TABLES, domain, (spec,))
    connection.execute(
        "CREATE TABLE workspace_tenant_bindings(workspace_id TEXT,tenant_id TEXT,status TEXT)"
    )
    connection.execute(
        "INSERT INTO workspace_tenant_bindings VALUES('own','tenant-a','active'),"
        "('foreign','tenant-b','active')"
    )
    connection.execute(
        f'CREATE TABLE "{spec.owner_table}"("{spec.owner_key}" TEXT PRIMARY KEY,'
        'workspace_id TEXT,tenant_id TEXT)'
    )
    connection.execute(
        f'INSERT INTO "{spec.owner_table}" VALUES'
        "('p1','own','tenant-a'),('p2','foreign','tenant-b')"
    )
    connection.execute(
        f'CREATE TABLE "{spec.table}"("{spec.owner_key}" TEXT REFERENCES '
        f'"{spec.owner_table}"("{spec.owner_key}"),source_code TEXT,watermark TEXT,'
        f'PRIMARY KEY("{spec.owner_key}",source_code))'
    )
    connection.execute(
        f'INSERT INTO "{spec.table}" VALUES'
        "('p1','b','own-b'),('p2','a','foreign-marker'),('p1','a','own-a')"
    )
    source, target = worker._owner_evidence_rows(connection, domain, "own", "tenant-a")
    assert source == target
    assert [dict(row[1])["watermark"] for row in source[1:]] == ["own-a", "own-b"]
    connection.execute(
        f'UPDATE "{spec.table}" SET watermark=? WHERE "{spec.owner_key}"=?',
        ("foreign-change", "p2"),
    )
    assert worker._owner_evidence_rows(connection, domain, "own", "tenant-a") == (source, target)
    connection.execute(
        f'UPDATE "{spec.table}" SET watermark=? WHERE "{spec.owner_key}"=?',
        ("own-change", "p1"),
    )
    changed, changed_target = worker._owner_evidence_rows(connection, domain, "own", "tenant-a")
    assert changed == changed_target and changed != source
    monkeypatch.setitem(
        worker._OWNER_EVIDENCE_TABLES, domain, (replace(spec, workspace_column="missing_owner"),)
    )
    with pytest.raises(ApiError) as missing:
        worker._owner_evidence_rows(connection, domain, "own", "tenant-a")
    assert missing.value.code == "console_migration.contract_not_ready"


@pytest.mark.parametrize("table", INDIRECT_OWNER_TABLES)
def test_indirect_owner_evidence_excludes_other_workspaces(monkeypatch, table):
    with closing(connect_database("sqlite:///:memory:")) as connection:
        _assert_indirect_owner_evidence(connection, monkeypatch, table)


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
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(48),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


def _insert_actor(connection, marker: str, role: str) -> PlatformContext:
    account_id = "gac_" + marker * 32
    operator_id = "pto_" + marker * 32
    connection.execute(
        """INSERT INTO global_accounts(
             account_id,email_normalized,phone_e164,display_name,status,login_enabled,
             credential_revision,session_revision,mfa_state,failed_login_count,
             locked_until,last_login_at,created_at,updated_at)
           VALUES(?,?,NULL,?,'active',1,1,1,'not_enrolled',0,NULL,NULL,?,?)""",
        (
            account_id,
            f"prd147-{marker}@example.invalid",
            f"PRD147 {marker}",
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        """INSERT INTO platform_operators(
             operator_id,account_id,status,revision,created_by_operator_id,
             created_at,updated_at)
           VALUES(?,?,'active',1,NULL,?,?)""",
        (operator_id, account_id, STAMP, STAMP),
    )
    connection.execute(
        """INSERT INTO platform_operator_role_bindings(
             operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
           VALUES(?,?,'active',NULL,?,NULL)""",
        (operator_id, role, STAMP),
    )
    roles, permissions, _fingerprint = platform_access_service._authority(
        connection, operator_id
    )
    factor_id = "mfa_" + marker * 32
    global_session_id = "gss_" + marker * 32
    platform_session_id = "pts_" + marker * 32
    expiry = (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    connection.execute(
        """INSERT INTO account_mfa_factors(
           factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
           revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
           VALUES(?,?,'totp',?,1,'active',1,0,?,NULL,?,?)""",
        (factor_id, account_id, "x" * 80, STAMP, STAMP, STAMP),
    )
    connection.execute(
        """INSERT INTO global_sessions(
           session_id,account_id,token_hmac,csrf_hmac,status,credential_revision,
           session_revision,device_family,client_risk_hmac,expires_at,idle_expires_at,
           last_seen_at,revoked_at,revoked_reason,created_at,updated_at)
           VALUES(?,?,?,?,'active',1,1,'prd147-test',NULL,?,?,?,NULL,NULL,?,?)""",
        (
            global_session_id,
            account_id,
            marker * 64,
            marker * 64,
            expiry,
            expiry,
            STAMP,
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        """INSERT INTO platform_sessions(
           platform_session_id,operator_id,account_id,global_session_id,factor_id,
           token_hmac,csrf_hmac,status,operator_revision,factor_revision,role_fingerprint,
           last_seen_at,absolute_expires_at,idle_expires_at,revoked_at,revoked_reason,
           created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,'active',1,1,?,?,?,?,NULL,NULL,?,?)""",
        (
            platform_session_id,
            operator_id,
            account_id,
            global_session_id,
            factor_id,
            hashlib.sha256(f"platform/{marker}".encode()).hexdigest(),
            hashlib.sha256(f"csrf/{marker}".encode()).hexdigest(),
            _fingerprint,
            STAMP,
            expiry,
            expiry,
            STAMP,
            STAMP,
        ),
    )
    return PlatformContext(
        platform_session_id=platform_session_id,
        operator_id=operator_id,
        account_id=account_id,
        global_session_id=global_session_id,
        factor_id=factor_id,
        factor_revision=1,
        operator_revision=1,
        roles=roles,
        permissions=permissions,
        expires_at=NOW + timedelta(hours=1),
    )


@pytest.fixture(scope="module")
def baseline_runtime(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("prd147-api-baseline") / "prd147.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0139"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        seed = apply_seed_data(database_url)
    config = _config(database_url)
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
               WHERE tenant_id=? AND status='active' LIMIT 1""",
            (seed.tenant_id,),
        ).fetchone()
        assert owner is not None
        actors = {
            "preparer": _insert_actor(connection, "1", "platform_ops"),
            "executor": _insert_actor(connection, "2", "platform_ops"),
            "owner": _insert_actor(connection, "3", "platform_owner"),
            "security": _insert_actor(connection, "4", "platform_security"),
        }
        connection.commit()
    return {
        "actors": actors,
        "config": config,
        "database": database,
        "owner": dict(owner),
        "platform_token": "p" * 43,
    }


@pytest.fixture
def runtime(tmp_path, baseline_runtime):
    database = tmp_path / "prd147.sqlite3"
    shutil.copy2(baseline_runtime["database"], database)
    config = baseline_runtime["config"].model_copy(
        update={"database_url": _url(database)}
    )
    owner = baseline_runtime["owner"]
    return {
        **baseline_runtime,
        "config": config,
        "database": database,
        "workspace_ref": service._workspace_ref(
            config, owner["workspace_id"], owner["tenant_id"]
        ),
    }


def _api_app(monkeypatch: pytest.MonkeyPatch, runtime):
    current = [runtime["actors"]["owner"]]
    monkeypatch.setattr(
        routes,
        "authenticate_platform_session_context",
        lambda _config, _token, **_kwargs: current[0],
    )
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda _connection, _config, **_kwargs: current[0],
    )
    monkeypatch.setattr(
        service,
        "consume_global_step_up_grant_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    app = create_service_app(
        "control-plane-api",
        "PRD-147 Console Migration Test",
        extra_cors_allow_headers=(
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
        ),
        extra_cors_expose_headers=(
            "ETag",
            "X-Permission-Fingerprint",
            "X-Polling-Location-Code",
        ),
        cors_allow_credentials=True,
    )
    app.state.config = runtime["config"]
    app.include_router(router)
    return app, current


def test_workspace_evidence_is_owner_scoped_for_all_domains(runtime) -> None:
    owner = runtime["owner"]
    digests: set[str] = set()
    with closing(connect_database(runtime["config"].database_url)) as connection:
        workspace_navigation_service._ensure_contract(
            connection, owner["workspace_id"], owner["tenant_id"]
        )
        connection.commit()
        for definition in worker.DOMAIN_BY_CODE.values():
            migration = {
                "domain_code": definition.code,
                "workspace_id": owner["workspace_id"],
                "tenant_id": owner["tenant_id"],
            }
            evidence = worker._source_evidence(
                connection,
                runtime["config"],
                worker._SOURCE_EVIDENCE_TABLES[definition.code],
                migration,
            )
            assert evidence.source_count >= 1
            assert evidence.source_count == evidence.target_count
            assert evidence.source_digest == evidence.target_digest
            digests.add(evidence.source_digest)
        assert worker._navigation_contract_count(
            connection, owner["workspace_id"], owner["tenant_id"]
        ) == 1
    assert len(digests) == len(worker.DOMAIN_BY_CODE)


def _platform_headers(runtime, *, revision: int | None = None, key: str | None = None):
    cookie = f"{PLATFORM_SESSION_COOKIE}={runtime['platform_token']}"
    if revision is None:
        return {"Cookie": cookie}
    csrf = "c" * 43
    headers = {
        "Cookie": f"{cookie}; {PLATFORM_CSRF_COOKIE}={csrf}",
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": csrf,
        "X-Step-Up-Grant": "u" * 43,
        "If-Match": f'"{revision}"',
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _ready_migration(
    runtime,
    *,
    marker: str,
    domain: str,
    source_migration: str,
    target_generation: int = 2,
    omitted_check: str | None = None,
) -> tuple[str, str]:
    migration_id = "cdm_" + marker * 32
    reconciliation_id = "drc_" + marker * 32
    owner = runtime["owner"]
    digest = hashlib.sha256(f"prd147/{marker}".encode()).hexdigest()
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """INSERT INTO console_domain_migrations(
               console_domain_migration_id,workspace_id,tenant_id,domain_code,
               operation_generation,source_migration_version,target_migration_version,
               source_inventory_revision,target_generation,contract_epoch,state,
               progress_basis_points,reconciliation_id,reconciliation_digest,
               route_manifest_revision,scheduler_manifest_revision,approved_generation,
               reviewed_generation,last_error_code,revision,created_at,updated_at,
               completed_at)
               VALUES(?,?,?,?,1,?,?,1,?,1,'ready',10000,?,?,1,1,NULL,NULL,NULL,1,?,?,NULL)""",
            (
                migration_id,
                owner["workspace_id"],
                owner["tenant_id"],
                domain,
                source_migration,
                source_migration,
                target_generation,
                reconciliation_id,
                digest,
                STAMP,
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO console_domain_migration_heads(
               workspace_id,tenant_id,domain_code,console_domain_migration_id,
               operation_generation,revision,updated_at)
               VALUES(?,?,?,?,1,1,?)""",
            (
                owner["workspace_id"],
                owner["tenant_id"],
                domain,
                migration_id,
                STAMP,
            ),
        )
        _insert_reconciliation_rows(
            connection,
            migration_id=migration_id,
            reconciliation_id=reconciliation_id,
            digest=digest,
            marker=marker,
            omitted_check=omitted_check,
        )
        connection.commit()
    return migration_id, reconciliation_id


def _insert_reconciliation_rows(
    connection,
    *,
    migration_id: str,
    reconciliation_id: str,
    digest: str,
    marker: str,
    omitted_check: str | None = None,
) -> None:
    migration = connection.execute(
        """SELECT workspace_id,tenant_id,domain_code,operation_generation
           FROM console_domain_migrations WHERE console_domain_migration_id=?""",
        (migration_id,),
    ).fetchone()
    assert migration is not None
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    connection.execute(
        """INSERT INTO workspace_domain_reconciliations(
           domain_reconciliation_id,console_domain_migration_id,workspace_id,
           tenant_id,domain_code,operation_generation,source_watermark_hmac,
           target_watermark_hmac,contract_release_generation,check_profile,
           check_profile_version,status,reason_code,expected_count,observed_count,
           expected_digest_hmac,observed_digest_hmac,coverage_basis_points,
           revision,checked_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,0,'console-domain-cutover-v1',1,'matched',
             'all_checks_matched',1,1,?,?,10000,1,?,?)""",
        (
            reconciliation_id,
            migration_id,
            migration["workspace_id"],
            migration["tenant_id"],
            migration["domain_code"],
            migration["operation_generation"],
            digest,
            digest,
            digest,
            digest,
            checked_at,
            checked_at,
        ),
    )
    checks = [
        (
            "dck_"
            + hashlib.sha256(f"{marker}/{index}/{check}".encode()).hexdigest()[:32],
            reconciliation_id,
            migration["workspace_id"],
            migration["tenant_id"],
            migration["operation_generation"],
            check,
            hashlib.sha256(f"{marker}/{check}".encode()).hexdigest(),
            checked_at,
        )
        for index, check in enumerate(MANDATORY_RECONCILIATION_CHECKS, start=1)
        if check != omitted_check
    ]
    connection.executemany(
        """INSERT INTO workspace_domain_reconciliation_checks(
           domain_reconciliation_check_id,domain_reconciliation_id,workspace_id,
           tenant_id,operation_generation,check_type,status,reason_code,
           expected_count,observed_count,artifact_kind,artifact_digest,
           tool_version,retention_class,checked_at)
           VALUES(?,?,?,?,?,?,'matched','matched',1,1,'integration',?,
             'prd147-test-v1','security',?)""",
        checks,
    )


async def _post(client, path: str, runtime, current, actor: str, payload, key: str):
    current[0] = runtime["actors"][actor]
    return await client.post(
        path,
        headers=_platform_headers(runtime, revision=payload["expected_revision"], key=key),
        json=payload,
    )


@pytest.mark.anyio
async def test_status_strict_boundary_and_permission_before_body(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app, current = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        status = await client.get(
            "/platform/console-migration/status",
            headers=_platform_headers(runtime),
        )
        assert status.status_code == 200
        body = status.json()
        assert len(body["domains"]) == 8
        assert body["scheduler"]["baseline_slot_count"] == 41
        assert body["scheduler"]["final_installer_count"] == 36
        assert body["scheduler"]["cluster_context_count"] == 37
        assert body["scheduler"]["queue_contract_count"] == 64
        assert body["scheduler"]["manifest_matches"] is True
        assert len(body["runtime_surfaces"]) == 3
        assert status.headers["cache-control"] == "no-store, private"

        for headers in (
            {"Cookie": f"{WORKSPACE_SESSION_COOKIE}={'w' * 43}"},
            {"Cookie": f"{SUPPORT_SESSION_COOKIE}={'s' * 43}"},
            {"Authorization": "Bearer local-test-only"},
            {
                "Cookie": (
                    f"{PLATFORM_SESSION_COOKIE}={runtime['platform_token']}; "
                    f"{WORKSPACE_SESSION_COOKIE}={'w' * 43}"
                )
            },
        ):
            denied = await client.get(
                "/platform/console-migration/status", headers=headers
            )
            assert denied.status_code == 400
            assert denied.json()["code"] == "platform.credential_conflict"

        unknown = await client.get(
            "/platform/console-migration/status?unexpected=1",
            headers=_platform_headers(runtime),
        )
        duplicate = await client.get(
            "/platform/console-migration/status?limit=1&limit=2",
            headers=_platform_headers(runtime),
        )
        with_body = await client.request(
            "GET",
            "/platform/console-migration/status",
            headers={**_platform_headers(runtime), "Content-Type": "application/json"},
            content=b"{}",
        )
        assert unknown.status_code == duplicate.status_code == with_body.status_code == 422
        assert unknown.json()["code"] == "console_migration.invalid"

        current[0] = replace(runtime["actors"]["owner"], permissions=())
        malformed = await client.post(
            "/platform/console-migration/cutovers",
            headers={
                **_platform_headers(
                    runtime, revision=1, key="prd147-denied-before-json"
                ),
                "Content-Type": "application/json",
            },
            content=b'{"action":',
        )
        assert malformed.status_code == 403
        assert malformed.json()["code"] == "console_migration.permission_revoked"


@pytest.mark.anyio
async def test_backfill_idempotency_resume_and_cursor_binding(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    app, current = _api_app(monkeypatch, runtime)
    prepare = {
        "action": "prepare",
        "domain_code": "channel_attribution",
        "scope": "workspace",
        "workspace_ref": runtime["workspace_ref"],
        "expected_revision": 0,
        "resource_profile": "bounded-v1",
        "confirmed": True,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        first = await _post(
            client,
            "/platform/console-migration/backfills",
            runtime,
            current,
            "preparer",
            prepare,
            "prd147-backfill-prepare-0001",
        )
        assert first.status_code == 202, first.text
        assert first.json()["state"] == "inventoried"
        assert first.headers["etag"] == '"1"'
        migration_id = first.json()["migration_id"]

        replay = await _post(
            client,
            "/platform/console-migration/backfills",
            runtime,
            current,
            "preparer",
            prepare,
            "prd147-backfill-prepare-0001",
        )
        assert replay.status_code == 202
        assert replay.json()["migration_id"] == migration_id
        assert replay.json()["idempotent_replay"] is True

        changed = {**prepare, "domain_code": "customer_success"}
        conflict = await _post(
            client,
            "/platform/console-migration/backfills",
            runtime,
            current,
            "preparer",
            changed,
            "prd147-backfill-prepare-0001",
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "console_migration.conflict"

        revision = 1
        for action, expected_state in (
            ("start", "backfill_queued"),
            ("cancel", "blocked"),
            ("resume", "backfilling"),
        ):
            payload = {
                "action": action,
                "migration_id": migration_id,
                "expected_revision": revision,
                "reconciliation_generation": 0,
                "resource_profile": "bounded-v1",
                "confirmed": True,
            }
            response = await _post(
                client,
                "/platform/console-migration/backfills",
                runtime,
                current,
                "preparer",
                payload,
                f"prd147-backfill-{action}-0001",
            )
            assert response.status_code == 202, response.text
            assert response.json()["state"] == expected_state
            revision = response.json()["revision"]

        second = await _post(
            client,
            "/platform/console-migration/backfills",
            runtime,
            current,
            "preparer",
            changed,
            "prd147-backfill-prepare-0002",
        )
        assert second.status_code == 202

        current[0] = runtime["actors"]["preparer"]
        page = await client.get(
            "/platform/console-migration/status?limit=1",
            headers=_platform_headers(runtime),
        )
        assert page.status_code == 200
        cursor = page.json()["next_cursor"]
        assert cursor
        next_page = await client.get(
            "/platform/console-migration/status",
            params={"limit": "1", "cursor": cursor},
            headers=_platform_headers(runtime),
        )
        assert next_page.status_code == 200
        assert len(next_page.json()["items"]) == 1
        rebound = await client.get(
            "/platform/console-migration/status",
            params={"state": "blocked", "cursor": cursor},
            headers=_platform_headers(runtime),
        )
        assert rebound.status_code == 422
        assert rebound.json()["code"] == "console_migration.invalid"


@pytest.mark.anyio
async def test_all_active_backfill_fans_out_and_generates_reconciliation(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    with closing(connect_database(runtime["config"].database_url)) as connection:
        for owner in connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
               WHERE status='active' ORDER BY workspace_id,tenant_id"""
        ).fetchall():
            workspace_navigation_service._ensure_contract(
                connection,
                str(owner["workspace_id"]),
                str(owner["tenant_id"]),
            )
        connection.commit()
    app, current = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        prepared = await _post(
            client,
            "/platform/console-migration/backfills",
            runtime,
            current,
            "preparer",
            {
                "action": "prepare",
                "domain_code": "customer_success",
                "scope": "all_active",
                "workspace_ref": None,
                "expected_revision": 0,
                "resource_profile": "bounded-v1",
                "confirmed": True,
            },
            "prd147-all-active-prepare-0001",
        )
        assert prepared.status_code == 202, prepared.text
        batch_id = prepared.json()["operation_id"]
        assert batch_id.startswith("cmb_")
        assert prepared.json()["migration_id"] is None

        with closing(connect_database(runtime["config"].database_url)) as connection:
            active_total = int(
                connection.execute(
                    "SELECT count(*) FROM workspace_tenant_bindings WHERE status='active'"
                ).fetchone()[0]
            )
            assert active_total > 0
            batch = connection.execute(
                """SELECT * FROM platform_console_migration_batches
                   WHERE platform_console_migration_batch_id=?""",
                (batch_id,),
            ).fetchone()
            children = connection.execute(
                """SELECT item.*,migration.state,head.console_domain_migration_id head_id
                   FROM platform_console_migration_batch_items item
                   JOIN console_domain_migrations migration
                     USING(console_domain_migration_id)
                   JOIN console_domain_migration_heads head
                     ON head.workspace_id=item.workspace_id
                    AND head.tenant_id=item.tenant_id
                    AND head.domain_code=migration.domain_code
                   WHERE item.platform_console_migration_batch_id=?
                   ORDER BY item.sequence_number""",
                (batch_id,),
            ).fetchall()
            platform = connection.execute(
                """SELECT * FROM platform_console_domain_migrations
                   WHERE domain_code='customer_success'
                     AND operation_generation=?""",
                (batch["operation_generation"],),
            ).fetchone()
            assert int(batch["workspace_total"]) == active_total
            assert len(children) == active_total
            assert {int(item["sequence_number"]) for item in children} == set(
                range(1, active_total + 1)
            )
            assert all(item["head_id"] == item["console_domain_migration_id"] for item in children)
            assert all(item["state"] == item["status"] == "inventoried" for item in children)
            assert int(platform["workspace_total"]) == active_total
            child_ids = [str(item["console_domain_migration_id"]) for item in children]

        start_revisions = {}
        for index, migration_id in enumerate(child_ids, start=1):
            started = await _post(
                client,
                "/platform/console-migration/backfills",
                runtime,
                current,
                "preparer",
                {
                    "action": "start",
                    "migration_id": migration_id,
                    "expected_revision": 1,
                    "reconciliation_generation": 0,
                    "resource_profile": "bounded-v1",
                    "confirmed": True,
                },
                f"prd147-all-active-start-{index:04d}",
            )
            assert started.status_code == 202, started.text
            start_revisions[migration_id] = int(started.json()["revision"])

    worker_config = runtime["config"].model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    replica = "prd147-all-active-replica"
    registry_lease = claim_scheduler_lease(
        worker_config,
        scheduler_code="platform-job-runtime",
        replica_code=replica,
        lease_seconds=60,
    )
    assert registry_lease is not None
    outer_fence = PlatformJobOuterFence(
        scheduler_code=registry_lease.scheduler_code,
        partition_code=registry_lease.partition_code,
        lease_token=registry_lease.token,
        fencing_generation=registry_lease.generation,
        registry_generation=registry_lease.registry_generation,
    )
    for _migration_id in child_ids:
        assert run_platform_job_once(
            worker_config,
            worker_code="prd147-all-active-worker",
            outer_fence=outer_fence,
        )
    assert release_scheduler_lease(
        worker_config,
        registry_lease,
        replica_code=replica,
    )

    with closing(connect_database(runtime["config"].database_url)) as connection:
        batch = connection.execute(
            """SELECT * FROM platform_console_migration_batches
               WHERE platform_console_migration_batch_id=?""",
            (batch_id,),
        ).fetchone()
        children = connection.execute(
            """SELECT status FROM platform_console_migration_batch_items
               WHERE platform_console_migration_batch_id=?""",
            (batch_id,),
        ).fetchall()
        platform = connection.execute(
            """SELECT * FROM platform_console_domain_migrations
               WHERE domain_code='customer_success'
                 AND operation_generation=?""",
            (batch["operation_generation"],),
        ).fetchone()
        effects = {
            str(row["effect_key"])
            for row in connection.execute(
                """SELECT effect.effect_key FROM platform_job_effects effect
                   JOIN platform_jobs job USING(platform_job_id)
                   WHERE job.source_code='console-migration'"""
            ).fetchall()
        }
        assert batch["status"] == "completed"
        assert int(batch["workspace_completed"]) == len(child_ids)
        assert int(batch["workspace_blocked"]) == 0
        assert {item["status"] for item in children} == {"completed"}
        assert platform["state"] == "ready"
        assert int(platform["workspace_ready"]) == len(child_ids)
        assert int(platform["workspace_blocked"]) == 0
        reconciliations = connection.execute(
            """SELECT reconciliation.console_domain_migration_id,
                 reconciliation.status,count(checks.domain_reconciliation_check_id) check_count
               FROM workspace_domain_reconciliations reconciliation
               JOIN workspace_domain_reconciliation_checks checks
                 USING(domain_reconciliation_id)
               WHERE reconciliation.console_domain_migration_id IN (
                 SELECT console_domain_migration_id
                 FROM platform_console_migration_batch_items
                 WHERE platform_console_migration_batch_id=?)
               GROUP BY reconciliation.domain_reconciliation_id
               ORDER BY reconciliation.console_domain_migration_id""",
            (batch_id,),
        ).fetchall()
        assert len(reconciliations) == len(child_ids)
        assert {row["status"] for row in reconciliations} == {"matched"}
        assert {int(row["check_count"]) for row in reconciliations} == {
            len(MANDATORY_RECONCILIATION_CHECKS)
        }
        for migration_id in child_ids:
            assert (
                f"console-migration.backfill.{migration_id}."
                f"r{start_revisions[migration_id]}"
            ) in effects
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_events"
        ).fetchone()[0] == len(child_ids)


@pytest.mark.anyio
async def test_cutover_requires_four_independent_accounts_and_releases_contract(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    migration_id, reconciliation_id = _ready_migration(
        runtime,
        marker="a",
        domain="growth_analytics",
        source_migration="0133",
    )
    app, current = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        prepared = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "preparer",
            {
                "action": "prepare",
                "migration_id": migration_id,
                "expected_revision": 1,
                "confirmed": True,
            },
            "prd147-cutover-prepare-0001",
        )
        assert prepared.status_code == 202, prepared.text
        assert prepared.json()["state"] == "quiescing"
        assert prepared.json()["revision"] == 2

        approved = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "owner",
            {
                "action": "approve",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-cutover-approve-0001",
        )
        assert approved.status_code == 202, approved.text

        reviewed = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "security",
            {
                "action": "security_review",
                "migration_id": migration_id,
                "expected_revision": 2,
                "reconciliation_id": reconciliation_id,
                "reconciliation_revision": 1,
                "confirmed": True,
            },
            "prd147-cutover-review-0001",
        )
        assert reviewed.status_code == 202, reviewed.text

        with closing(connect_database(runtime["config"].database_url)) as connection:
            owner = runtime["owner"]
            digest = hashlib.sha256(b"prd147/active-backfill-lease").hexdigest()
            connection.execute(
                """INSERT INTO console_backfill_cursors(
                   console_backfill_cursor_id,console_domain_migration_id,workspace_id,
                   tenant_id,domain_code,mapping_code,source_table_code,
                   source_schema_version,target_table_code,target_schema_version,
                   source_upper_watermark_hmac,last_source_key_hmac,source_row_count,
                   target_row_count,source_digest_hmac,target_digest_hmac,
                   digest_key_version,owner_coverage_basis_points,
                   foreign_key_coverage_basis_points,event_watermark,outbox_watermark,
                   audit_watermark,scheduler_watermark,attempt_count,status,
                   fence_generation,lease_token_hmac,lease_expires_at,revision,
                   created_at,updated_at)
                   VALUES(?,?,?,?,?,'prd147-test-map','legacy-test','1','target-test','1',
                     ?,NULL,1,1,?,?,1,10000,10000,1,1,1,1,1,'running',1,?,?,1,?,?)""",
                (
                    "cbf_" + "f" * 32,
                    migration_id,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    "growth_analytics",
                    digest,
                    digest,
                    digest,
                    digest,
                    (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    STAMP,
                    STAMP,
                ),
            )
            connection.commit()

        leased = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "executor",
            {
                "action": "execute",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-cutover-execute-0001",
        )
        assert leased.status_code == 409
        assert leased.json()["code"] == "console_migration.lease_active"

        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE console_backfill_cursors SET lease_expires_at=?
                   WHERE console_domain_migration_id=?""",
                ("2000-01-01T00:00:00Z", migration_id),
            )
            connection.commit()

        executed = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "executor",
            {
                "action": "execute",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-cutover-execute-0001",
        )
        assert executed.status_code == 202, executed.text
        assert executed.json()["state"] == "cutting_over"
        assert executed.json()["revision"] == 3

    worker_config = runtime["config"].model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    registry_lease = claim_scheduler_lease(
        worker_config,
        scheduler_code="platform-job-runtime",
        replica_code="prd147-test-worker",
        lease_seconds=60,
    )
    assert registry_lease is not None
    outer_fence = PlatformJobOuterFence(
        scheduler_code=registry_lease.scheduler_code,
        partition_code=registry_lease.partition_code,
        lease_token=registry_lease.token,
        fencing_generation=registry_lease.generation,
        registry_generation=registry_lease.registry_generation,
    )
    assert run_platform_job_once(
        worker_config,
        worker_code="prd147-platform-job-worker",
        outer_fence=outer_fence,
    )
    assert release_scheduler_lease(
        worker_config,
        registry_lease,
        replica_code="prd147-test-worker",
    )

    with closing(connect_database(runtime["config"].database_url)) as connection:
        migration = connection.execute(
            """SELECT state,revision,target_generation FROM console_domain_migrations
               WHERE console_domain_migration_id=?""",
            (migration_id,),
        ).fetchone()
        contract = connection.execute(
            "SELECT * FROM console_ingress_contract_heads WHERE head_key='production'"
        ).fetchone()
        assert tuple(migration) == ("active", 5, 3)
        assert contract["status"] == "active"
        assert contract["legacy_write_fenced"] == 1
        assert connection.execute(
            "SELECT count(*) FROM console_contract_releases"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_audits"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_approvals WHERE status='consumed'"
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT count(*) FROM platform_console_migration_security_reviews
               WHERE status='consumed'"""
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM workspace_legacy_route_tombstones WHERE status='verified'"
        ).fetchone()[0] == 46
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_events"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_event_outbox WHERE status='pending'"
        ).fetchone()[0] == 2

    rollback_revision = int(migration["revision"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        for actor, action in (
            ("preparer", "prepare"),
            ("owner", "approve"),
            ("security", "security_review"),
            ("executor", "execute"),
        ):
            payload = {
                "action": action,
                "migration_id": migration_id,
                "expected_revision": rollback_revision,
                "reason_code": "release_regression",
                "target_generation": 2,
                "confirmed": True,
            }
            if action == "security_review":
                payload.update(
                    {
                        "reconciliation_id": reconciliation_id,
                        "reconciliation_revision": 1,
                    }
                )
            response = await _post(
                client,
                "/platform/console-migration/rollbacks",
                runtime,
                current,
                actor,
                payload,
                f"prd147-rollback-success-{action}-0001",
            )
            assert response.status_code == 202, response.text
            rollback_revision = int(response.json()["revision"])

    rollback_replica = "prd147-rollback-success-replica"
    rollback_lease = claim_scheduler_lease(
        worker_config,
        scheduler_code="platform-job-runtime",
        replica_code=rollback_replica,
        lease_seconds=60,
    )
    assert rollback_lease is not None
    rollback_fence = PlatformJobOuterFence(
        scheduler_code=rollback_lease.scheduler_code,
        partition_code=rollback_lease.partition_code,
        lease_token=rollback_lease.token,
        fencing_generation=rollback_lease.generation,
        registry_generation=rollback_lease.registry_generation,
    )
    assert run_platform_job_once(
        worker_config,
        worker_code="prd147-rollback-success-worker",
        outer_fence=rollback_fence,
    )
    assert release_scheduler_lease(
        worker_config,
        rollback_lease,
        replica_code=rollback_replica,
    )

    with closing(connect_database(runtime["config"].database_url)) as connection:
        rolled_back = connection.execute(
            """SELECT state,revision,target_generation FROM console_domain_migrations
               WHERE console_domain_migration_id=?""",
            (migration_id,),
        ).fetchone()
        assert tuple(rolled_back) == ("rolled_back", 8, 2)
        assert connection.execute(
            """SELECT count(*) FROM platform_console_migration_events
               WHERE event_type='console_domain.rolled_back'"""
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_event_outbox WHERE status='pending'"
        ).fetchone()[0] == 3


@pytest.mark.anyio
async def test_cutover_sod_reconciliation_and_rollback_fail_closed(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    migration_id, reconciliation_id = _ready_migration(
        runtime,
        marker="b",
        domain="provider_engineering",
        source_migration="0135",
    )
    broken_id, _broken_reconciliation_id = _ready_migration(
        runtime,
        marker="c",
        domain="brand_distribution",
        source_migration="0134",
        omitted_check="late_writer_fenced",
    )
    rollback_id, _rollback_reconciliation_id = _ready_migration(
        runtime,
        marker="d",
        domain="commercial_accounting",
        source_migration="0136",
        target_generation=1,
    )
    revoked_id, revoked_reconciliation_id = _ready_migration(
        runtime,
        marker="f",
        domain="workspace_navigation",
        source_migration="0138",
    )
    stale_id, _stale_reconciliation_id = _ready_migration(
        runtime,
        marker="e",
        domain="console_experience",
        source_migration="0137",
    )
    with closing(connect_database(runtime["config"].database_url)) as connection:
        connection.execute(
            """UPDATE workspace_domain_reconciliations SET checked_at=?
               WHERE console_domain_migration_id=?""",
            ("2000-01-01T00:00:00Z", stale_id),
        )
        connection.commit()
    app, current = _api_app(monkeypatch, runtime)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        for actor, payload, key in (
            (
                "preparer",
                {
                    "action": "prepare",
                    "migration_id": migration_id,
                    "expected_revision": 1,
                    "confirmed": True,
                },
                "prd147-sod-prepare-0001",
            ),
            (
                "owner",
                {
                    "action": "approve",
                    "migration_id": migration_id,
                    "expected_revision": 2,
                    "confirmed": True,
                },
                "prd147-sod-approve-0001",
            ),
            (
                "security",
                {
                    "action": "security_review",
                    "migration_id": migration_id,
                    "expected_revision": 2,
                    "reconciliation_id": reconciliation_id,
                    "reconciliation_revision": 1,
                    "confirmed": True,
                },
                "prd147-sod-review-0001",
            ),
        ):
            response = await _post(
                client,
                "/platform/console-migration/cutovers",
                runtime,
                current,
                actor,
                payload,
                key,
            )
            assert response.status_code == 202, response.text

        same_executor = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "preparer",
            {
                "action": "execute",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-sod-execute-0001",
        )
        assert same_executor.status_code == 409
        assert same_executor.json()["code"] == "console_migration.conflict"

        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE console_ingress_contract_heads
                   SET contract_generation=contract_generation+1,revision=revision+1,
                       updated_at=?
                   WHERE head_key='production'""",
                (STAMP,),
            )
            connection.commit()

        contract_drift = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "executor",
            {
                "action": "execute",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-contract-drift-execute-0001",
        )
        assert contract_drift.status_code == 409
        assert contract_drift.json()["code"] == "console_migration.contract_not_ready"

        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE console_ingress_contract_heads
                   SET contract_generation=contract_generation-1,revision=revision+1,
                       updated_at=?
                   WHERE head_key='production'""",
                (STAMP,),
            )
            connection.execute(
                """UPDATE scheduler_registry_heads
                   SET generation=generation+1,revision=revision+1,updated_at=?
                   WHERE head_key='production'""",
                (STAMP,),
            )
            connection.commit()

        registry_drift = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "executor",
            {
                "action": "execute",
                "migration_id": migration_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-registry-drift-execute-0001",
        )
        assert registry_drift.status_code == 409
        assert registry_drift.json()["code"] == "console_migration.registry_drift"

        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE scheduler_registry_heads
                   SET generation=generation-1,revision=revision+1,updated_at=?
                   WHERE head_key='production'""",
                (STAMP,),
            )
            connection.commit()

        missing_check = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "preparer",
            {
                "action": "prepare",
                "migration_id": broken_id,
                "expected_revision": 1,
                "confirmed": True,
            },
            "prd147-reconciliation-missing-0001",
        )
        assert missing_check.status_code == 409
        assert missing_check.json()["code"] == "console_migration.reconciliation_failed"

        stale_check = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "preparer",
            {
                "action": "prepare",
                "migration_id": stale_id,
                "expected_revision": 1,
                "confirmed": True,
            },
            "prd147-reconciliation-stale-0001",
        )
        assert stale_check.status_code == 409
        assert stale_check.json()["code"] == "console_migration.reconciliation_failed"

        unavailable = await _post(
            client,
            "/platform/console-migration/rollbacks",
            runtime,
            current,
            "preparer",
            {
                "action": "prepare",
                "migration_id": rollback_id,
                "expected_revision": 1,
                "reason_code": "operator_recovery",
                "target_generation": 1,
                "confirmed": True,
            },
            "prd147-rollback-unavailable-0001",
        )
        assert unavailable.status_code == 409
        assert unavailable.json()["code"] == "console_migration.rollback_unavailable"

        for actor, payload, key in (
            (
                "preparer",
                {
                    "action": "prepare",
                    "migration_id": revoked_id,
                    "expected_revision": 1,
                    "confirmed": True,
                },
                "prd147-revoked-prepare-0001",
            ),
            (
                "owner",
                {
                    "action": "approve",
                    "migration_id": revoked_id,
                    "expected_revision": 2,
                    "confirmed": True,
                },
                "prd147-revoked-approve-0001",
            ),
            (
                "security",
                {
                    "action": "security_review",
                    "migration_id": revoked_id,
                    "expected_revision": 2,
                    "reconciliation_id": revoked_reconciliation_id,
                    "reconciliation_revision": 1,
                    "confirmed": True,
                },
                "prd147-revoked-review-0001",
            ),
        ):
            response = await _post(
                client,
                "/platform/console-migration/cutovers",
                runtime,
                current,
                actor,
                payload,
                key,
            )
            assert response.status_code == 202, response.text

        with closing(connect_database(runtime["config"].database_url)) as connection:
            connection.execute(
                """UPDATE platform_sessions SET status='revoked',revoked_at=?,
                     revoked_reason='authority_changed',updated_at=?
                   WHERE platform_session_id=?""",
                (STAMP, STAMP, runtime["actors"]["owner"].platform_session_id),
            )
            connection.commit()

        revoked = await _post(
            client,
            "/platform/console-migration/cutovers",
            runtime,
            current,
            "executor",
            {
                "action": "execute",
                "migration_id": revoked_id,
                "expected_revision": 2,
                "confirmed": True,
            },
            "prd147-revoked-execute-0001",
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "console_migration.permission_revoked"


def _outbox_clock(monkeypatch):
    clock = [datetime.now(UTC).replace(microsecond=0)]
    # Exercise deadline boundaries independently of native/emulated database speed.
    monkeypatch.setattr(migration_outbox, "_database_now", lambda _connection: clock[0])
    monkeypatch.setattr(migration_scheduler, "_database_now", lambda _connection: clock[0])
    return clock


def test_event_outbox_recovers_expired_claim_and_stops_after_sent(runtime, monkeypatch) -> None:
    _outbox_clock(monkeypatch)
    worker_config = runtime["config"].model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    registry_lease = claim_scheduler_lease(
        worker_config,
        scheduler_code="platform-job-runtime",
        replica_code="prd147-outbox-replica",
        lease_seconds=60,
    )
    assert registry_lease is not None
    outer_fence = PlatformJobOuterFence(
        scheduler_code=registry_lease.scheduler_code,
        partition_code=registry_lease.partition_code,
        lease_token=registry_lease.token,
        fencing_generation=registry_lease.generation,
        registry_generation=registry_lease.registry_generation,
    )
    event_id = "cme_" + "9" * 32
    outbox_id = "cmo_" + "9" * 32
    payload = {
        "aggregate_ref": "8" * 64,
        "contract_generation": 1,
        "domain_code": "workspace_navigation",
        "event_id": event_id,
        "event_type": "console_domain.backfilled",
        "occurred_at": STAMP,
        "operation_generation": 1,
        "reconciliation_digest": "7" * 64,
        "schema_version": 1,
    }
    with closing(connect_database(worker_config.database_url)) as connection:
        connection.execute(
            """INSERT INTO platform_console_migration_events(
               platform_console_migration_event_id,workspace_id,tenant_id,aggregate_ref,
               event_type,schema_version,domain_code,operation_generation,
               contract_generation,reconciliation_digest,payload_json,occurred_at)
               VALUES(?,?,?,?,?,1,?,1,1,?,?,?)""",
            (
                event_id,
                runtime["owner"]["workspace_id"],
                runtime["owner"]["tenant_id"],
                "8" * 64,
                "console_domain.backfilled",
                "workspace_navigation",
                "7" * 64,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO platform_console_migration_event_outbox(
               platform_console_migration_event_outbox_id,
               platform_console_migration_event_id,status,attempt_count,next_attempt_at,
               lease_owner_id,lease_generation,lease_token_hmac,lease_expires_at,
               dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
            (outbox_id, event_id, STAMP, STAMP, STAMP),
        )
        connection.commit()

    first = claim_console_migration_event(
        worker_config,
        worker_code="prd147-outbox-worker-one",
        outer_fence=outer_fence,
    )
    assert first is not None
    assert first.attempt_count == first.lease_generation == 1
    renewed = renew_console_migration_event(
        worker_config,
        first,
        outer_fence=outer_fence,
    )
    assert renewed is not None
    assert renewed.lease_expires_at >= first.lease_expires_at

    with closing(connect_database(worker_config.database_url)) as connection:
        connection.execute(
            """UPDATE platform_console_migration_event_outbox SET lease_expires_at=?
               WHERE platform_console_migration_event_outbox_id=?""",
            ("2000-01-01T00:00:00Z", outbox_id),
        )
        connection.commit()

    assert finalize_console_migration_event(
        worker_config,
        first,
        outer_fence=outer_fence,
    ) is False
    delivered: list[tuple[str, str, dict[str, object]]] = []
    assert dispatch_console_migration_event_once(
        worker_config,
        worker_code="prd147-outbox-worker-two",
        outer_fence=outer_fence,
        sink=lambda *event: delivered.append(event),
    )
    assert delivered == [(event_id, "console_domain.backfilled", payload)]
    assert not dispatch_console_migration_event_once(
        worker_config,
        worker_code="prd147-outbox-worker-two",
        outer_fence=outer_fence,
        sink=lambda *event: delivered.append(event),
    )
    with closing(connect_database(worker_config.database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,lease_generation,lease_owner_id,
                      lease_token_hmac,lease_expires_at,dispatched_at
               FROM platform_console_migration_event_outbox
               WHERE platform_console_migration_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
        assert tuple(row[:6]) == ("sent", 2, 2, None, None, None)
        assert row["dispatched_at"] is not None

    assert release_scheduler_lease(
        worker_config,
        registry_lease,
        replica_code="prd147-outbox-replica",
    )
    with pytest.raises(ApiError, match="lease") as lost:
        claim_console_migration_event(
            worker_config,
            worker_code="prd147-outbox-worker-three",
            outer_fence=outer_fence,
        )
    assert lost.value.code == "platform_job.lease_lost"


def _seed_outbox_event(runtime, config: AppConfig, marker: str):
    event_id = "cme_" + marker * 32
    outbox_id = "cmo_" + marker * 32
    payload = {
        "aggregate_ref": marker * 64,
        "contract_generation": 1,
        "domain_code": "workspace_navigation",
        "event_id": event_id,
        "event_type": "console_domain.backfilled",
        "occurred_at": STAMP,
        "operation_generation": 1,
        "reconciliation_digest": marker * 64,
        "schema_version": 1,
    }
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """INSERT INTO platform_console_migration_events(
               platform_console_migration_event_id,workspace_id,tenant_id,aggregate_ref,
               event_type,schema_version,domain_code,operation_generation,
               contract_generation,reconciliation_digest,payload_json,occurred_at)
               VALUES(?,?,?,?,?,1,?,1,1,?,?,?)""",
            (
                event_id,
                runtime["owner"]["workspace_id"],
                runtime["owner"]["tenant_id"],
                marker * 64,
                "console_domain.backfilled",
                "workspace_navigation",
                marker * 64,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                STAMP,
            ),
        )
        connection.execute(
            """INSERT INTO platform_console_migration_event_outbox(
               platform_console_migration_event_outbox_id,
               platform_console_migration_event_id,status,attempt_count,next_attempt_at,
               lease_owner_id,lease_generation,lease_token_hmac,lease_expires_at,
               dispatched_at,created_at,updated_at)
               VALUES(?,?,'pending',0,?,NULL,0,NULL,NULL,NULL,?,?)""",
            (outbox_id, event_id, STAMP, STAMP, STAMP),
        )
        connection.commit()
    return event_id, outbox_id, payload


def _outer_fence(lease) -> PlatformJobOuterFence:
    return PlatformJobOuterFence(
        scheduler_code=lease.scheduler_code,
        partition_code=lease.partition_code,
        lease_token=lease.token,
        fencing_generation=lease.generation,
        registry_generation=lease.registry_generation,
    )


def test_event_outbox_retries_sink_failure_after_backoff(runtime, monkeypatch) -> None:
    clock = _outbox_clock(monkeypatch)
    config = runtime["config"].model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    replica = "prd147-outbox-retry-replica"
    registry_lease = claim_scheduler_lease(
        config,
        scheduler_code="platform-job-runtime",
        replica_code=replica,
        lease_seconds=60,
    )
    assert registry_lease is not None
    outer_fence = _outer_fence(registry_lease)
    event_id, outbox_id, payload = _seed_outbox_event(runtime, config, "a")
    failed_deliveries: list[str] = []

    def failing_sink(delivery_event_id, _event_type, _payload):
        failed_deliveries.append(delivery_event_id)
        raise RuntimeError("injected sink failure")

    assert dispatch_console_migration_event_once(
        config,
        worker_code="prd147-outbox-retry-worker",
        outer_fence=outer_fence,
        sink=failing_sink,
    )
    assert failed_deliveries == [event_id]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,lease_owner_id,lease_token_hmac,
                      lease_expires_at,dispatched_at,next_attempt_at,updated_at
               FROM platform_console_migration_event_outbox
               WHERE platform_console_migration_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
        assert tuple(row[:6]) == ("failed", 1, None, None, None, None)
        assert row["next_attempt_at"] > row["updated_at"]

    assert not dispatch_console_migration_event_once(
        config,
        worker_code="prd147-outbox-retry-worker",
        outer_fence=outer_fence,
        sink=lambda *_event: pytest.fail("backoff was bypassed"),
    )
    clock[0] += timedelta(seconds=2)

    delivered: list[tuple[str, str, dict[str, object]]] = []
    assert dispatch_console_migration_event_once(
        config,
        worker_code="prd147-outbox-retry-worker",
        outer_fence=outer_fence,
        sink=lambda *event: delivered.append(event),
    )
    assert delivered == [(event_id, "console_domain.backfilled", payload)]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,dispatched_at
               FROM platform_console_migration_event_outbox
               WHERE platform_console_migration_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
        assert tuple(row[:2]) == ("sent", 2)
        assert row["dispatched_at"] is not None
    assert release_scheduler_lease(config, registry_lease, replica_code=replica)


def test_event_outbox_does_not_finalize_after_outer_fence_loss(runtime, monkeypatch) -> None:
    _outbox_clock(monkeypatch)
    config = runtime["config"].model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    first_replica = "prd147-outbox-fence-replica-one"
    first_lease = claim_scheduler_lease(
        config,
        scheduler_code="platform-job-runtime",
        replica_code=first_replica,
        lease_seconds=60,
    )
    assert first_lease is not None
    first_fence = _outer_fence(first_lease)
    event_id, outbox_id, payload = _seed_outbox_event(runtime, config, "b")
    delivered: list[tuple[str, str, dict[str, object]]] = []

    def deliver_then_lose_fence(*event):
        delivered.append(event)
        assert release_scheduler_lease(config, first_lease, replica_code=first_replica)

    with pytest.raises(ApiError) as lost:
        dispatch_console_migration_event_once(
            config,
            worker_code="prd147-outbox-fence-worker-one",
            outer_fence=first_fence,
            sink=deliver_then_lose_fence,
        )
    assert lost.value.code == "platform_job.lease_lost"
    assert delivered == [(event_id, "console_domain.backfilled", payload)]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,lease_owner_id,dispatched_at
               FROM platform_console_migration_event_outbox
               WHERE platform_console_migration_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
        assert tuple(row) == (
            "processing",
            1,
            "prd147-outbox-fence-worker-one",
            None,
        )

    second_replica = "prd147-outbox-fence-replica-two"
    second_lease = claim_scheduler_lease(
        config,
        scheduler_code="platform-job-runtime",
        replica_code=second_replica,
        lease_seconds=60,
    )
    assert second_lease is not None
    second_fence = _outer_fence(second_lease)
    assert not dispatch_console_migration_event_once(
        config,
        worker_code="prd147-outbox-fence-worker-two",
        outer_fence=second_fence,
        sink=lambda *_event: pytest.fail("live inner lease was stolen"),
    )
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """UPDATE platform_console_migration_event_outbox SET lease_expires_at=?
               WHERE platform_console_migration_event_outbox_id=?""",
            ("2000-01-01T00:00:00Z", outbox_id),
        )
        connection.commit()

    assert dispatch_console_migration_event_once(
        config,
        worker_code="prd147-outbox-fence-worker-two",
        outer_fence=second_fence,
        sink=lambda *event: delivered.append(event),
    )
    assert delivered == [
        (event_id, "console_domain.backfilled", payload),
        (event_id, "console_domain.backfilled", payload),
    ]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            """SELECT status,attempt_count,lease_generation,dispatched_at
               FROM platform_console_migration_event_outbox
               WHERE platform_console_migration_event_outbox_id=?""",
            (outbox_id,),
        ).fetchone()
        assert tuple(row[:3]) == ("sent", 2, 2)
        assert row["dispatched_at"] is not None
    assert release_scheduler_lease(config, second_lease, replica_code=second_replica)


def test_platform_navigation_projects_console_migration_permission(runtime) -> None:
    permitted = platform_access_service._navigation_items(
        set(runtime["actors"]["preparer"].permissions)
    )
    assert [item.id for item in permitted].count("console-migration") == 1
    item = next(item for item in permitted if item.id == "console-migration")
    assert item.href == "/platform.html#console-migration"

    revoked_permissions = set(runtime["actors"]["preparer"].permissions) - {
        "platform.console_migration.read"
    }
    revoked = platform_access_service._navigation_items(revoked_permissions)
    assert "console-migration" not in {item.id for item in revoked}
