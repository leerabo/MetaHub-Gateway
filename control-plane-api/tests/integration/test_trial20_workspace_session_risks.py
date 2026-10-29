"""Real Workspace Cookie risk reads/writes, isolation and transaction revocation."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from metahub_control.session_risks import service as risk_service
from metahub_control.session_risks.routes import router
from metahub_control.workspace_access.service import authenticate_workspace_session_as_auth_context
from metahub_gateway.session_risk.store import InMemorySessionRiskStore
from test_prd070_session_risks import SESSION_RISK_SECRET, _insert_risk_score
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)
from test_prd147_workspace_resource_writes import resource_subjects  # noqa: F401

PATH = "/security/session-risks"


@pytest.fixture
async def risk_subjects(resource_subjects):  # noqa: F811
    app, database, f, ids = resource_subjects
    app.state.config = app.state.config.model_copy(update={
        "session_risk_enabled": True,
        "session_risk_hmac_version": 1,
        "session_risk_hmac_secret": SESSION_RISK_SECRET,
    })
    app.include_router(router)
    app.state.session_risk_store = InMemorySessionRiskStore()
    current = authenticate_workspace_session_as_auth_context(
        app.state.config, f.source.cookies.get(WORKSPACE_SESSION)
    )
    risk = _insert_risk_score(
        app.state.config.database_url,
        tenant_id=current.user.tenant_id,
        virtual_key_id=ids["key"],
        ordinal=20001,
        current=datetime.now(UTC).replace(microsecond=0),
    )
    return app, database, f, risk


@pytest.mark.anyio
async def test_workspace_risks_read_scope_and_deny_foreign_or_mixed_credentials(risk_subjects):
    _app, _database, f, risk = risk_subjects
    own = await f.source.get(PATH)
    assert own.status_code == 200, own.json().get("code")
    assert [r["session_risk_score_id"] for r in own.json()["session_risks"]] == [risk.score_id]
    assert own.headers["cache-control"] == "no-store"
    foreign = await f.target.get(PATH)
    assert foreign.status_code == 200, foreign.json().get("code")
    assert foreign.json()["session_risks"] == []
    filter_probe = await f.target.get(PATH, params={"virtual_key_id": risk.virtual_key_id})
    assert filter_probe.status_code == 404
    assert filter_probe.json()["code"] == "session_risk.not_found"
    for extra, expected in (({}, 401), ({"Authorization": "Bearer invalid"}, 400)):
        headers = extra if not extra else {**_headers(f.source, attack=True), **extra}
        result = await f.anonymous.get(PATH, headers=headers)
        assert result.status_code == expected, result.json().get("code")


@pytest.mark.anyio
async def test_workspace_risk_action_csrf_origin_isolation_and_idempotent_commit(risk_subjects):
    _app, database, f, risk = risk_subjects
    path = f"{PATH}/{risk.score_id}/action"
    payload = {"action": "dismiss", "reason_code": "false_positive", "expected_revision": 1}
    headers = {**_headers(f.source), "Idempotency-Key": "workspace-risk-dismiss-0001"}
    for changed in (
        {**headers, "X-CSRF-Token": "invalid"},
        {**headers, "Origin": "https://foreign.test"},
    ):
        denied = await f.source.post(path, json=payload, headers=changed)
        assert denied.status_code == 403, denied.json().get("code")
    foreign = await f.target.post(path, json=payload, headers={
        **_headers(f.target), "Idempotency-Key": "workspace-risk-foreign-0001",
    })
    assert foreign.status_code == 404, foreign.json().get("code")
    accepted = await f.source.post(path, json=payload, headers=headers)
    assert accepted.status_code == 200, accepted.json().get("code")
    assert accepted.json()["session_risk"]["status"] == "resolved"
    replay = await f.source.post(path, json=payload, headers=headers)
    assert replay.status_code == 200, replay.json().get("code")
    assert replay.json()["idempotent_replay"] is True
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_risk_actions WHERE session_risk_score_id=?",
            (risk.score_id,),
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_workspace_risk_revoked_after_guard_cannot_commit(risk_subjects, monkeypatch):
    _app, database, f, risk = risk_subjects
    original = risk_service.apply_session_risk_action

    def revoked_before_transaction(config, context, *args, **kwargs):
        # Revoke only this isolated fixture after route authentication has succeeded.
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "UPDATE workspace_sessions SET status='revoked',"
                "revoked_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                "revoked_reason='authority_changed' WHERE workspace_session_id=?",
                (context.session.session_id,),
            )
            connection.commit()
        return original(config, context, *args, **kwargs)

    monkeypatch.setattr(
        "metahub_control.session_risks.routes.apply_session_risk_action", revoked_before_transaction
    )
    response = await f.source.post(
        f"{PATH}/{risk.score_id}/action",
        json={"action": "dismiss", "reason_code": "false_positive", "expected_revision": 1},
        headers={**_headers(f.source), "Idempotency-Key": "workspace-risk-revoked-0001"},
    )
    assert response.status_code == 401, response.json().get("code")
    assert response.json()["code"] == "workspace_session.revoked"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT status,state_revision FROM session_risk_scores WHERE session_risk_score_id=?",
            (risk.score_id,),
        ).fetchone() == ("high_risk", 1)
        assert connection.execute(
            "SELECT COUNT(*) FROM session_risk_actions WHERE session_risk_score_id=?",
            (risk.score_id,),
        ).fetchone()[0] == 0
