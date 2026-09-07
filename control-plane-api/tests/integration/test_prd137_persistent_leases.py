"""Persistent coordinator state with controlled runners; not real isolation evidence."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_prd137_security_assurance as api
from httpx import ASGITransport, AsyncClient
from metahub_control.security_assurance import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

base_database = api.base_database
runtime = api.runtime


def _lease(runtime, assessment_id):
    with closing(connect_database(runtime.config.database_url)) as connection:
        return connection.execute(
            "SELECT * FROM assurance_runner_leases WHERE security_assessment_id=?",
            (assessment_id,),
        ).fetchone()


async def _case(runtime, monkeypatch, name, runner):
    app = api._app(monkeypatch, runtime, runner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await api._create(
            client,
            runtime,
            token=api.PLATFORM_A_TOKEN,
            key=f"prd137-short-lease-{name}",
        )


def _assert_failed(response):
    assert response.status_code == 201, response.text
    assessment = response.json()["assessment"]
    assert assessment["status"] == "failed"
    assert {item["outcome"] for item in assessment["runs"]} == {"error"}
    assert len(assessment["runs"]) == len(assessment["findings"]) == 10
    assert assessment["environment_receipt_sha256"] is None


@pytest.mark.anyio
async def test_persistent_short_lease_controls(runtime, monkeypatch, record_property):
    clock = [datetime.now(UTC)]
    completed = []
    observed = set()
    with monkeypatch.context() as patch:
        patch.setattr(service, "_utcnow", lambda: clock[0])

        def renewing(request, lease):
            first = _lease(runtime, request.security_assessment_id)
            assert (
                timedelta(seconds=29)
                < (service._parse_time(first["lease_expires_at"]) - request.started_at)
                <= timedelta(seconds=30)
            )
            assert lease.check()
            clock[0] += timedelta(seconds=20)
            assert lease.check()
            assert lease.fencing_generation == 2
            renewed = _lease(runtime, request.security_assessment_id)
            assert renewed["lease_expires_at"] > first["lease_expires_at"]
            with pytest.raises(ApiError) as stale:
                service._renew_assessment_lease(
                    runtime.config,
                    request,
                    expected_fencing_generation=1,
                )
            assert stale.value.code == "security_assurance.runner_fenced"
            assert lease.check()
            for change in (
                {"artifact_sha256": "f" * 64},
                {"workspace_id": "wsp_" + "f" * 32},
                {"generation": request.generation + 1},
                {"scenarios": request.scenarios[:-1]},
                {"deadline_at": request.deadline_at + timedelta(seconds=1)},
                {"started_at": request.started_at + timedelta(seconds=1)},
            ):
                with pytest.raises(ApiError):
                    service._renew_assessment_lease(
                        runtime.config,
                        replace(request, **change),
                        expected_fencing_generation=2,
                    )
            return api._passed_runner(request)

        response = await _case(runtime, monkeypatch, "renew", renewing)
        assert response.status_code == 201, response.text
        body = response.json()["assessment"]
        assert body["status"] == "passed"
        with closing(connect_database(runtime.config.database_url)) as connection:
            generations = connection.execute(
                "SELECT DISTINCT fencing_generation FROM isolation_test_runs "
                "WHERE security_assessment_id=?",
                (body["security_assessment_id"],),
            ).fetchall()
            assert [row[0] for row in generations] == [2]
        completed.append("renewal_binding_and_final_generation")

    with monkeypatch.context() as patch:
        clock[0] = datetime.now(UTC)
        patch.setattr(service, "_utcnow", lambda: clock[0])

        def deadline_capped(request, lease):
            for _ in range(89):
                clock[0] += timedelta(seconds=20)
                assert lease.check()
            clock[0] = request.deadline_at - timedelta(seconds=5)
            assert lease.check()
            generation = lease.fencing_generation
            for _ in range(5):
                assert lease.check()
                assert lease.fencing_generation == generation
            row = _lease(runtime, request.security_assessment_id)
            assert service._parse_time(row["lease_expires_at"]) <= request.deadline_at
            return api._passed_runner(request)

        response = await _case(runtime, monkeypatch, "deadline-cap", deadline_capped)
        assert response.status_code == 201, response.text
        assert response.json()["assessment"]["status"] == "passed"
        completed.append("overall_deadline_cap_without_spurious_generation_churn")

    for mode in ("operator_revision", "operator_disabled", "role_revoked", "account_disabled"):

        def revoking(request, lease, mode=mode):
            assert lease.check()
            with closing(connect_database(runtime.config.database_url)) as connection:
                if mode == "operator_revision":
                    connection.execute(
                        "UPDATE platform_operators SET revision=revision+1,updated_at=? "
                        "WHERE operator_id=?",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                elif mode == "operator_disabled":
                    connection.execute(
                        "UPDATE platform_operators SET status='disabled',revision=revision+1,"
                        "updated_at=? WHERE operator_id=?",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                elif mode == "role_revoked":
                    connection.execute(
                        "UPDATE platform_operator_role_bindings SET status='revoked',revoked_at=? "
                        "WHERE operator_id=? AND role_code='platform_security'",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                else:
                    connection.execute(
                        "UPDATE global_accounts SET status='disabled',"
                        "session_revision=session_revision+1,"
                        "updated_at=? WHERE account_id=(SELECT account_id FROM platform_operators "
                        "WHERE operator_id=?)",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                connection.commit()
            assert not lease.check()
            with closing(connect_database(runtime.config.database_url)) as connection:
                if mode == "operator_disabled":
                    connection.execute(
                        "UPDATE platform_operators SET status='active',"
                        "revision=revision+1,updated_at=? "
                        "WHERE operator_id=?",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                elif mode == "role_revoked":
                    connection.execute(
                        "UPDATE platform_operator_role_bindings "
                        "SET status='active',revoked_at=NULL "
                        "WHERE operator_id=? AND role_code='platform_security'",
                        (api.OPERATOR_A_ID,),
                    )
                elif mode == "account_disabled":
                    connection.execute(
                        "UPDATE global_accounts SET status='active',"
                        "session_revision=session_revision+1,"
                        "updated_at=? WHERE account_id=(SELECT account_id FROM platform_operators "
                        "WHERE operator_id=?)",
                        (service._db_time(datetime.now(UTC)), api.OPERATOR_A_ID),
                    )
                connection.commit()
            assert not lease.check()
            observed.add(mode)
            return api._passed_runner(request)

        _assert_failed(await _case(runtime, monkeypatch, mode, revoking))
        assert mode in observed, "revocation runner did not complete all checks"
        completed.append(mode)

    with monkeypatch.context() as patch:
        clock[0] = datetime.now(UTC)
        patch.setattr(service, "_utcnow", lambda: clock[0])

        def expired(request, lease):
            clock[0] = request.started_at + timedelta(seconds=31)
            assert not lease.check()
            observed.add("expired")
            return api._passed_runner(request)

        _assert_failed(await _case(runtime, monkeypatch, "expired-return", expired))
        assert "expired" in observed
        completed.append("expired_result_cannot_pass")

    captured = {}
    with monkeypatch.context() as patch:
        clock[0] = datetime.now(UTC)
        patch.setattr(service, "_utcnow", lambda: clock[0])

        def stalled(request, lease):
            captured["request"] = request
            clock[0] = request.started_at + timedelta(seconds=31)
            assert (
                service.process_security_assurance_once(
                    runtime.config,
                    worker_id="lease-expiry-test-worker",
                )
                >= 1
            )
            assert not lease.check()
            return api._passed_runner(request)

        response = await _case(runtime, monkeypatch, "sweeper-race", stalled)
        assert response.status_code == 409, response.text
        clock[0] += timedelta(milliseconds=2)
        assert (
            service.process_security_assurance_once(
                runtime.config,
                worker_id="lease-expiry-test-worker",
            )
            == 0
        )
        assessment_id = captured["request"].security_assessment_id
        with closing(connect_database(runtime.config.database_url)) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM security_assessments WHERE security_assessment_id=?",
                    (assessment_id,),
                ).fetchone()[0]
                == "failed"
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM isolation_test_runs WHERE security_assessment_id=?",
                    (assessment_id,),
                ).fetchone()[0]
                == 10
            )
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM assurance_cleanup_journal journal "
                    "JOIN isolation_test_runs run "
                    "ON run.isolation_test_run_id=journal.isolation_test_run_id "
                    "WHERE run.security_assessment_id=?",
                    (assessment_id,),
                ).fetchone()[0]
                == 0
            )
        completed.append("expired_worker_sweep_single_terminal_no_cleanup")
    record_property("controlled_lease_cases", ",".join(completed))


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["revoked_before_start", "abandoned_queue", "late_start"])
async def test_queued_assessment_recovery(runtime, monkeypatch, mode):
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    execute = service.execute_security_assessment
    captured = {}
    runner_calls = []

    def runner(request, lease):
        runner_calls.append(request.security_assessment_id)
        return api._passed_runner(request)

    def before_start(config, assessment_id, **kwargs):
        captured["id"] = assessment_id
        captured["kwargs"] = kwargs
        if mode == "abandoned_queue":
            return
        if mode == "revoked_before_start":
            with closing(connect_database(config.database_url)) as connection:
                connection.execute(
                    "UPDATE platform_operators SET status='disabled',revision=revision+1,"
                    "updated_at=? WHERE operator_id=?",
                    (service._db_time(clock[0]), api.OPERATOR_A_ID),
                )
                connection.commit()
        else:
            clock[0] += timedelta(seconds=31)
        execute(config, assessment_id, **kwargs)

    monkeypatch.setattr(service, "execute_security_assessment", before_start)
    response = await _case(runtime, monkeypatch, mode, runner)
    assert "id" in captured, response.text
    monkeypatch.setattr(service, "execute_security_assessment", execute)
    if mode == "abandoned_queue":
        assert response.status_code == 201, response.text
        assert response.json()["assessment"]["status"] == "queued"
        assert service.process_security_assurance_once(
            runtime.config, worker_id="queued-recovery-test-worker",
        ) == 0
        clock[0] += timedelta(seconds=31)
        assert service.process_security_assurance_once(
            runtime.config, worker_id="queued-recovery-test-worker",
        ) >= 1
    assert not runner_calls
    with closing(connect_database(runtime.config.database_url)) as connection:
        assessment = connection.execute(
            "SELECT * FROM security_assessments WHERE security_assessment_id=?",
            (captured["id"],),
        ).fetchone()
        assert assessment["status"] == "failed"
        assert assessment["environment_receipt_sha256"] is None
        assert connection.execute(
            "SELECT COUNT(*) FROM isolation_test_runs WHERE security_assessment_id=? "
            "AND outcome='error'", (captured["id"],),
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM assurance_cleanup_journal",
        ).fetchone()[0] == 0
    with pytest.raises(ApiError) as stale:
        execute(runtime.config, captured["id"], **captured["kwargs"])
    assert stale.value.code == "security_assurance.assessment_conflict"
    service.process_security_assurance_once(
        runtime.config, worker_id="queued-recovery-test-worker",
    )
    clock[0] += timedelta(milliseconds=2)
    assert service.process_security_assurance_once(
        runtime.config, worker_id="queued-recovery-test-worker",
    ) == 0
    assert not runner_calls


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["tenant_disabled", "workspace_inactive", "login_disabled"])
async def test_owner_and_login_revocation(runtime, monkeypatch, mode):
    completed = []

    def revoking(request, lease):
        assert lease.check()
        with closing(connect_database(runtime.config.database_url)) as connection:
            if mode == "tenant_disabled":
                statement = "UPDATE tenants SET status=? WHERE tenant_id=?"
                identity, inactive, active = runtime.tenant_id, "disabled", "active"
            elif mode == "workspace_inactive":
                statement = "UPDATE workspaces SET status=? WHERE workspace_id=?"
                identity, inactive, active = runtime.workspace_id, "migration_conflict", "active"
            else:
                statement = (
                    "UPDATE global_accounts SET login_enabled=?,"
                    "session_revision=session_revision+1 WHERE account_id="
                    "(SELECT account_id FROM platform_operators WHERE operator_id=?)"
                )
                identity, inactive, active = api.OPERATOR_A_ID, 0, 1
            connection.execute(statement, (inactive, identity))
            connection.commit()
        assert not lease.check()
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(statement, (active, identity))
            connection.commit()
        assert not lease.check()
        completed.append(mode)
        return api._passed_runner(request)

    _assert_failed(await _case(runtime, monkeypatch, mode, revoking))
    assert completed == [mode]


@pytest.mark.anyio
async def test_generation_only_takeover(runtime, monkeypatch):
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    completed = []
    captured = {}

    def takeover(request, lease):
        captured["request"] = request
        assert lease.check()
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                "UPDATE assurance_runner_leases SET fencing_generation=fencing_generation+1,"
                "updated_at=? WHERE security_assessment_id=?",
                (service._db_time(clock[0] + timedelta(milliseconds=1)),
                 request.security_assessment_id),
            )
            connection.commit()
        clock[0] += timedelta(milliseconds=2)
        assert not lease.check()
        completed.append(True)
        return api._passed_runner(request)

    response = await _case(runtime, monkeypatch, "generation-only", takeover)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "security_assurance.runner_fenced"
    assert completed == [True]
    request = captured["request"]
    assert _lease(runtime, request.security_assessment_id)["lease_token_sha256"] == (
        request.lease_challenge_sha256
    )
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM isolation_test_runs").fetchone()[0] == 0
    clock[0] = request.started_at + timedelta(seconds=31)
    assert service.process_security_assurance_once(
        runtime.config, worker_id="generation-recovery-test-worker",
    ) >= 1
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM isolation_test_runs WHERE outcome='error' "
            "AND fencing_generation=2",
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM assurance_cleanup_journal",
        ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_queued_recovery_competing_schedulers(runtime, monkeypatch):
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    execute = service.execute_security_assessment
    monkeypatch.setattr(service, "execute_security_assessment", lambda *args, **kwargs: None)
    response = await _case(runtime, monkeypatch, "competing-schedulers", None)
    assert response.status_code == 201, response.text
    assessment_id = response.json()["assessment"]["security_assessment_id"]
    barrier = threading.Barrier(2)

    def competing_execute(*args, **kwargs):
        barrier.wait(timeout=10)
        return execute(*args, **kwargs)

    monkeypatch.setattr(service, "execute_security_assessment", competing_execute)
    clock[0] += timedelta(seconds=31)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(
            service.process_security_assurance_once, runtime.config,
            worker_id=f"queued-competing-worker-{index}",
        ) for index in range(2)]
        assert sum(future.result(timeout=30) for future in futures) >= 1
    monkeypatch.setattr(service, "execute_security_assessment", execute)
    clock[0] += timedelta(milliseconds=2)
    assert service.process_security_assurance_once(
        runtime.config, worker_id="queued-competing-final-check",
    ) == 0
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute(
            "SELECT status FROM security_assessments WHERE security_assessment_id=?",
            (assessment_id,),
        ).fetchone()[0] == "failed"
        for table, count in (
            ("assurance_runner_leases", 1), ("isolation_test_runs", 10),
            ("pen_test_findings", 10), ("assurance_cleanup_journal", 0),
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
