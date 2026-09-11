"""GA state-machine regressions with isolated DBs and explicit simulated RC evidence.

The RC verifier, platform identity and clock are fixtures. These tests prove code
behavior, never real approval, deployment, rollback execution or a 72-hour run.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_prd138_launch_governance as rc
from httpx import ASGITransport, AsyncClient
from metahub_control.launch_governance import ga_service, service
from metahub_shared.db import connect_database
from test_prd138_launch_governance import (
    WAVES,
    _app,
    _approve_all,
    _create_candidate,
    _headers,
    _sha,
    _submit_all_evidence,
)

base_database = rc.base_database
runtime = rc.runtime
ROOT = Path(__file__).resolve().parents[3]
DOWN = ROOT / "infra/migrations/0154_saas_ga_launch.down.sql"
UP = ROOT / "infra/migrations/0154_saas_ga_launch.sql"


@pytest.fixture
def ga_app(runtime, monkeypatch):
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(service, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(ga_service, "_utcnow", lambda: clock[0])
    consumed = []
    monkeypatch.setattr(
        ga_service,
        "consume_global_step_up_grant_in_transaction",
        lambda *args, **kwargs: consumed.append(kwargs),
    )
    return _app(monkeypatch, runtime), clock, consumed


async def _rc(client, runtime, clock):
    candidate = await _approve_all(
        client,
        runtime,
        await _submit_all_evidence(
            client,
            runtime,
            await _create_candidate(client, runtime),
            clock[0],
        ),
    )
    for index, wave in enumerate(WAVES):
        for operation in ("start_wave", "complete_wave"):
            if operation == "complete_wave":
                clock[0] += timedelta(seconds=61)
            body = {
                "operation": operation,
                "launch_candidate_id": candidate["launch_candidate_id"],
                "expected_revision": candidate["revision"],
                "wave_kind": wave,
                "confirmed": True,
            }
            if operation == "complete_wave":
                body["health_watermark_sha256"] = _sha(wave)
            response = await client.post(
                "/platform/launches",
                json=body,
                headers=_headers(
                    runtime.tokens["executor"],
                    f"prd148-{operation}-{index}-0001",
                    candidate["revision"],
                ),
            )
            assert response.status_code == 201, response.text
            candidate = response.json()["candidate"]
    response = await client.post(
        "/platform/launches",
        headers=_headers(
            runtime.tokens["executor"],
            "prd148-rc-review-0001",
            candidate["revision"],
        ),
        json={
            "operation": "review",
            "launch_candidate_id": candidate["launch_candidate_id"],
            "expected_revision": candidate["revision"],
            "summary_sha256": _sha("rc-summary"),
            "action_items_sha256": _sha("rc-actions"),
            "conclusion": "release_candidate",
            "confirmed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["candidate"]


def _source_bodies(runtime, candidate):
    with closing(connect_database(runtime.config.database_url)) as connection:
        rows = connection.execute(
            """SELECT e.*, g.ga_gate_id FROM launch_evidence e
            JOIN ga_gate_policies g ON g.launch_gate_id=e.launch_gate_id
            WHERE e.launch_candidate_id=? ORDER BY g.gate_code""",
            (candidate["launch_candidate_id"],),
        ).fetchall()
    fields = (
        "launch_candidate_id",
        "workspace_id",
        "artifact_sha256",
        "verifier_code",
        "verifier_version",
        "outcome",
        "metric_value",
        "metric_unit",
        "environment_sha256",
        "provenance_sha256",
        "started_at",
        "finished_at",
    )
    return [
        (
            row["ga_gate_id"],
            {
                **{field: row[field] for field in fields},
                "signature": "s" * 64,
                "expected_revision": candidate["revision"],
                "confirmed": True,
            },
        )
        for row in rows
    ]


async def _import_all(client, runtime, candidate):
    for index, (gate_id, body) in enumerate(_source_bodies(runtime, candidate)):
        response = await client.post(
            f"/platform/ga-gates/{gate_id}/evidence",
            json=body,
            headers=_headers(
                runtime.tokens["test"],
                f"prd148-ga-evidence-{index:02d}-0001",
                candidate["revision"],
            ),
        )
        assert response.status_code == 201, response.text


def _launch_body(candidate):
    return {
        "operation": "launch",
        "launch_candidate_id": candidate["launch_candidate_id"],
        "expected_revision": candidate["revision"],
        "confirmed": True,
    }


def _facts(runtime):
    with closing(connect_database(runtime.config.database_url)) as connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "ga_evidence",
                "ga_launches",
                "ga_decisions",
                "ga_rollbacks",
                "post_ga_reviews",
                "ga_events",
                "ga_event_outbox",
                "ga_action_commands",
            )
        }


@pytest.mark.anyio
async def test_ga_evidence_import_rejects_changed_signed_fields_and_cross_gate_replay(
    runtime, ga_app
):
    app, clock, consumed = ga_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _rc(client, runtime, clock)
        (gate_id, body), (other_gate, _) = _source_bodies(runtime, candidate)[:2]
        url = f"/platform/ga-gates/{gate_id}/evidence"
        headers = _headers(
            runtime.tokens["test"], "prd148-bind-evidence-0001", candidate["revision"]
        )
        before = _facts(runtime)
        for field, value in (
            ("metric_value", 0.5),
            ("metric_unit", "hours"),
            ("environment_sha256", _sha("wrong-env")),
            ("provenance_sha256", _sha("wrong-source")),
            ("signature", "x" * 64),
            (
                "started_at",
                (
                    datetime.fromisoformat(body["started_at"].replace("Z", "+00:00"))
                    + timedelta(seconds=1)
                ).isoformat(),
            ),
            ("finished_at", clock[0].isoformat()),
        ):
            response = await client.post(url, headers=headers, json={**body, field: value})
            assert response.status_code == 403, (field, response.text)
            assert response.json()["code"] == "launch.verifier_invalid"
            assert _facts(runtime) == before
            assert not consumed
        accepted = await client.post(url, headers=headers, json=body)
        assert accepted.status_code == 201, accepted.text
        assert body["signature"] not in accepted.text
        after = _facts(runtime)
        replay = await client.post(url, headers=headers, json=body)
        assert replay.status_code in (200, 201), replay.text
        assert replay.json()["evidence"] == accepted.json()["evidence"]
        assert _facts(runtime) == after
        crossed = await client.post(
            f"/platform/ga-gates/{other_gate}/evidence", headers=headers, json=body
        )
        assert crossed.json()["code"] == "launch.idempotency_conflict", crossed.text
        assert len(consumed) == 1
        # Even imported evidence before any launch is an immutable business fact.
        with closing(connect_database(runtime.config.database_url)) as connection:
            with pytest.raises(sqlite3.DatabaseError, match="ga_facts_present"):
                connection.executescript(DOWN.read_text(encoding="utf-8"))
        assert _facts(runtime) == after


@pytest.mark.anyio
async def test_ga_promotion_requires_all_gates_independent_executor_and_current_signers(
    runtime, ga_app
):
    app, clock, consumed = ga_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _rc(client, runtime, clock)
        body = _launch_body(candidate)
        headers = _headers(
            runtime.tokens["executor"], "prd148-launch-guards-0001", candidate["revision"]
        )
        blocked = await client.post("/platform/ga-launches", headers=headers, json=body)
        assert blocked.json()["code"] == "ga.gate_blocked", blocked.text
        await _import_all(client, runtime, candidate)
        before = _facts(runtime)
        calls = len(consumed)
        self_release = await client.post(
            "/platform/ga-launches",
            json=body,
            headers=_headers(
                runtime.tokens["product"], "prd148-self-release-0001", candidate["revision"]
            ),
        )
        assert self_release.json()["code"] == "launch.independence_required", self_release.text
        with closing(connect_database(runtime.config.database_url)) as connection:
            connection.execute(
                "UPDATE platform_operators SET revision=2,updated_at=? WHERE operator_id=?",
                (clock[0].isoformat(), runtime.operator_ids["security"]),
            )
            connection.commit()
        revoked = await client.post("/platform/ga-launches", headers=headers, json=body)
        assert revoked.json()["code"] == "launch.permission_revoked", revoked.text
        assert _facts(runtime) == before
        assert len(consumed) == calls


@pytest.mark.anyio
@pytest.mark.parametrize("rollback", [False, True])
async def test_ga_launch_review_and_independent_rollback_are_durable_and_idempotent(
    runtime, ga_app, rollback
):
    app, clock, consumed = ga_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        candidate = await _rc(client, runtime, clock)
        await _import_all(client, runtime, candidate)
        headers = _headers(
            runtime.tokens["executor"], "prd148-launch-success-0001", candidate["revision"]
        )
        response = await client.post(
            "/platform/ga-launches", headers=headers, json=_launch_body(candidate)
        )
        assert response.status_code == 201, response.text
        launch = response.json()["launch"]
        assert launch["status"] == "launched"
        assert len(launch["evidence"]) == 20
        assert len(launch["decisions"]) == 6
        before = _facts(runtime)
        replay = await client.post(
            "/platform/ga-launches", headers=headers, json=_launch_body(candidate)
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert _facts(runtime) == before
        if rollback:
            clock[0] += timedelta(seconds=1)
            url = f"/platform/ga-launches/{launch['ga_launch_id']}/rollback"
            body = {
                "operation": "request",
                "expected_revision": launch["revision"],
                "trigger_code": "operator_request",
                "trigger_sha256": _sha("trigger"),
                "checkpoint_sha256": _sha("checkpoint"),
                "job_receipt_sha256": _sha("receipt"),
                "confirmed": True,
            }
            headers = _headers(
                runtime.tokens["rollback_requester"],
                "prd148-rollback-request-0001",
                launch["revision"],
            )
            pending = await client.post(url, headers=headers, json=body)
            assert pending.status_code == 201, pending.text
            launch = pending.json()["launch"]
            before = _facts(runtime)
            crossed = await client.post(
                "/platform/ga-launches/gal_" + "f" * 32 + "/rollback", headers=headers, json=body
            )
            assert crossed.json()["code"] == "launch.idempotency_conflict", crossed.text
            assert _facts(runtime) == before
            verify = {
                "operation": "verify",
                "expected_revision": launch["revision"],
                "ga_rollback_id": pending.json()["rollback"]["ga_rollback_id"],
                "outcome": "passed",
                "verification_sha256": _sha("verification"),
                "confirmed": True,
            }
            denied = await client.post(
                url,
                json=verify,
                headers=_headers(
                    runtime.tokens["rollback_requester"],
                    "prd148-self-verify-0001",
                    launch["revision"],
                ),
            )
            assert denied.json()["code"] == "launch.independence_required", denied.text
            assert _facts(runtime) == before
            clock[0] += timedelta(seconds=1)
            verified = await client.post(
                url,
                json=verify,
                headers=_headers(
                    runtime.tokens["rollback_verifier"],
                    "prd148-verify-rollback-0001",
                    launch["revision"],
                ),
            )
            assert verified.status_code == 201, verified.text
            launch = verified.json()["launch"]
            assert launch["status"] == "rolled_back"
        body = {
            "expected_revision": launch["revision"],
            "summary_sha256": _sha("summary"),
            "action_items_sha256": _sha("actions"),
            "conclusion": "ga_rolled_back" if rollback else "ga_launched",
            "confirmed": True,
        }
        headers = _headers(
            runtime.tokens["executor"], "prd148-post-ga-review-0001", launch["revision"]
        )
        url = f"/platform/ga-launches/{launch['ga_launch_id']}/review"
        reviewed = await client.post(url, headers=headers, json=body)
        assert reviewed.status_code == 201, reviewed.text
        before = _facts(runtime)
        replay = await client.post(url, headers=headers, json=body)
        assert replay.status_code == 200, replay.text
        crossed = await client.post(
            "/platform/ga-launches/gal_" + "e" * 32 + "/review", headers=headers, json=body
        )
        assert crossed.json()["code"] == "launch.idempotency_conflict", crossed.text
        assert _facts(runtime) == before
        listed = await client.get("/platform/ga-gates", headers=_headers(runtime.tokens["test"]))
        assert listed.status_code == 200, listed.text
        assert listed.json()["launch"]["review"] == reviewed.json()["review"]
        assert "tenant_id" not in listed.text
        assert len(consumed) == (24 if rollback else 22)
        assert before["ga_events"] == before["ga_event_outbox"] == (3 if rollback else 2)


def test_ga_empty_migration_roundtrip_and_command_only_down_protection(runtime):
    with closing(connect_database(runtime.config.database_url)) as connection:
        policies = [
            dict(row)
            for row in connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id")
        ]
        connection.executescript(DOWN.read_text(encoding="utf-8"))
        connection.executescript(UP.read_text(encoding="utf-8"))
        assert [
            dict(row)
            for row in connection.execute("SELECT * FROM ga_gate_policies ORDER BY ga_gate_id")
        ] == policies
        connection.execute(
            """INSERT INTO ga_action_commands VALUES(?,?,?,?,?,?,?,?)""",
            (
                "gac_" + "f" * 32,
                runtime.operator_ids["test"],
                "ga.evidence",
                _sha("key"),
                _sha("request"),
                "gae_" + "f" * 32,
                1,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.DatabaseError, match="ga_facts_present"):
            connection.executescript(DOWN.read_text(encoding="utf-8"))
        assert connection.execute("SELECT count(*) FROM ga_action_commands").fetchone()[0] == 1


@pytest.mark.parametrize(
    "hours, passed", [(71.99, False), (72, True), (73, True), (96, True), (96.01, False)]
)
def test_ga_stability_requires_signed_elapsed_time(hours, passed):
    started = datetime.now(UTC)
    assert (
        ga_service._stability_passed(
            {
                "started_at": started.isoformat(),
                "finished_at": (started + timedelta(hours=hours)).isoformat(),
            }
        )
        is passed
    )
