"""Full-schema DR approval smoke; authentication is synthetic, no restoration is claimed."""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import test_prd111_disaster_recovery as prior
from metahub_control.disaster_recovery import (
    runtime,  # noqa: F401
    service,
)
from metahub_control.disaster_recovery.models import DRCommandRequest
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.errors import ApiError

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    directory = tmp_path_factory.mktemp("dr-approval-full-schema")
    path = directory / "template.sqlite3"
    source = os.environ.get("METAHUB_TEST_DR_SQLITE_TEMPLATE")
    with pytest.MonkeyPatch.context() as patch:
        for key in os.environ:
            if key.startswith("METAHUB_"):
                patch.delenv(key)
        patch.chdir(directory)
        if source:
            source_path = Path(source).resolve()
            assert source_path.is_file() and source_path.is_relative_to(ROOT / ".tmp")
            assert any(part.startswith("s3-dr-binding-") for part in source_path.parts)
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as origin:
                with closing(sqlite3.connect(path)) as destination:
                    origin.backup(destination)
        url = prior._url(path)
        apply_migrations(url, migrations_dir=ROOT / "infra/migrations")
        for name in ("ADMIN_PASSWORD", "VIRTUAL_KEY", "PROVIDER_API_KEY"):
            patch.setenv("METAHUB_SEED_" + name, secrets.token_urlsafe(48))
        apply_seed_data(url, seed_file=ROOT / "infra/seeds/default_seed.json")
        with closing(connect_database(url)) as connection:
            account = dict(connection.execute("SELECT * FROM global_accounts LIMIT 1").fetchone())
            for actor in (prior.OPS, prior.SECURITY):
                value = {
                    **account,
                    "account_id": actor.account_id,
                    "email_normalized": actor.operator_id + "@example.invalid",
                }
                connection.execute(
                    f"INSERT INTO global_accounts({','.join(value)}) "
                    f"VALUES({','.join('?' for _ in value)})",
                    tuple(value.values()),
                )
                connection.execute(
                    """INSERT INTO platform_operators(operator_id,account_id,status,revision,
                         created_by_operator_id,created_at,updated_at)
                       VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))""",
                    (actor.operator_id, actor.account_id),
                )
                connection.execute(
                    """INSERT INTO platform_operator_role_bindings(operator_id,role_code,status,
                         granted_by_operator_id,granted_at,revoked_at)
                       VALUES(?,?,'active',NULL,datetime('now'),NULL)""",
                    (actor.operator_id, actor.roles[0]),
                )
            connection.commit()
    return path


@pytest.fixture
def configured(template, tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("METAHUB_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "approval.sqlite3"
    shutil.copy2(template, path)
    config = prior._config(prior._url(path))
    monkeypatch.setattr(
        service,
        "authenticate_platform_session_in_transaction",
        lambda *a, session_token, **k: prior.OPS if session_token == "ops" else prior.SECURITY,
    )
    with closing(connect_database(config.database_url)) as connection:
        connection.execute(
            """INSERT INTO backup_sets(
                 backup_id,backup_type,scope_kind,status,source_backend,source_watermark,object_key,
                 size_bytes,content_sha256,manifest_sha256,kms_key_version,schema_head,
                 requested_by_operator_id,idempotency_sha256,request_sha256,request_id,trace_id,
                 retention_until,created_at,updated_at,completed_at)
               VALUES(?,'full','platform','ready','sqlite','snapshot:1','backups/isolated-dr',1024,
                 ?,?,'synthetic-kms','0153',?,?,?,'request-dr-backup','trace-dr-backup',
                 datetime('now','+30 days'),datetime('now','-30 seconds'),datetime('now'),
                 datetime('now'))""",
            (prior.BACKUP_ID, "c" * 64, prior.MANIFEST, prior.OPS_ID, "d" * 64, "e" * 64),
        )
        connection.execute(
            """INSERT INTO backup_restore_verifications(backup_restore_verification_id,backup_id,
                 status,schema_head,integrity_status,manifest_sha256,tenant_owner_sample_count,
                 started_at,completed_at,created_at,updated_at)
               VALUES(?,?,'passed','0153','ok',?,1,datetime('now','-1 second'),datetime('now'),
                 datetime('now','-1 second'),datetime('now'))""",
            ("brv_" + "1" * 32, prior.BACKUP_ID, prior.MANIFEST),
        )
        connection.commit()
    return config


def vote(config, actor):
    return service.cast_dr_vote(
        config,
        session_token=actor,
        csrf_token="synthetic-csrf",
        run_kind="drill",
        region_id=None,
        payload=DRCommandRequest(
            failover_plan_id=prior.NORTH_TO_EAST_PLAN,
            plan_version=1,
            reason_code="quarterly_drill",
            expected_epoch=0,
            confirmed=True,
        ),
        idempotency_key="dr-bound-approval-test-" + actor,
        request_id="request-dr-bound-approval",
        trace_id="trace-dr-bound-approval",
    )


def test_full_schema_votes_bind_enqueue_and_replay(configured):
    planned = vote(configured, "ops")
    approved = vote(configured, "security")
    replay = vote(configured, "security")
    assert planned.run.disaster_recovery_run_id == approved.run.disaster_recovery_run_id
    assert planned.run.status == "planned" and approved.run.status == "approved"
    assert replay.idempotent_replay and replay.run.platform_job_id == approved.run.platform_job_id
    with closing(connect_database(configured.database_url)) as connection:
        fact = connection.execute("SELECT * FROM dr_run_recovery_bindings").fetchone()
        assert fact["platform_job_id"] == approved.run.platform_job_id
        assert fact["manifest_sha256"] == prior.MANIFEST
        assert connection.execute("SELECT COUNT(*) FROM platform_jobs").fetchone()[0] == 1
        connection.execute("BEGIN IMMEDIATE")
        service.precheck_run_in_transaction(
            connection,
            run_id=approved.run.disaster_recovery_run_id,
            expected_epoch=0,
            plan_manifest_sha256="1" * 64,
        )
        connection.commit()
        assert (
            connection.execute("SELECT status FROM disaster_recovery_runs").fetchone()[0]
            == "prechecking"
        )


def test_binding_failure_rolls_back_review_job_and_point(configured, monkeypatch):
    vote(configured, "ops")
    original = service.bind_recovery_in_transaction

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ApiError("dr.recovery_point_unavailable")

    monkeypatch.setattr(service, "bind_recovery_in_transaction", fail)
    with pytest.raises(ApiError) as error:
        vote(configured, "security")
    assert error.value.code == "dr.recovery_point_unavailable"
    with closing(connect_database(configured.database_url)) as connection:
        for table in ("dr_run_recovery_bindings", "platform_jobs", "recovery_points"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM failover_approvals").fetchone()[0] == 1
        assert (
            connection.execute("SELECT status FROM disaster_recovery_runs").fetchone()[0]
            == "planned"
        )


def test_full_schema_status_reads_actual_current_backup(configured):
    status = service.get_dr_status(
        configured, session_token="ops", request_id="request-dr-status", trace_id="trace-dr-status"
    )
    assert status.objectives.recovery_ready and status.objectives.latest_recovery_age_seconds >= 30
    points = service.list_recovery_points(
        configured, session_token="ops", request_id="request-dr-list", trace_id="trace-dr-list"
    )
    assert len(points.items) == 1 and points.items[0].schema_head == "0153"


def test_delayed_execution_refuses_stale_bound_backup(configured, monkeypatch):
    vote(configured, "ops")
    approved = vote(configured, "security")
    with closing(connect_database(configured.database_url)) as connection:
        now = service._database_now(connection) + timedelta(seconds=901)
        monkeypatch.setattr(service, "_database_now", lambda _: now)
        with pytest.raises(ApiError) as error:
            service.precheck_run_in_transaction(
                connection,
                run_id=approved.run.disaster_recovery_run_id,
                expected_epoch=0,
                plan_manifest_sha256="1" * 64,
            )
        assert error.value.code == "dr.recovery_point_unavailable"
        assert (
            connection.execute("SELECT status FROM disaster_recovery_runs").fetchone()[0]
            == "approved"
        )
