"""Workspace DLP transport and revoked transaction ownership; isolated QA only."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from metahub_control.dlp import routes, service
from metahub_control.dlp.models import CreateDlpRuleRequest, UpdateDlpRuleRequest
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_as_auth_context,
)
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    WORKSPACE_SESSION,
    _headers,
    file_subjects,
    migrated_baseline,
)


@pytest.mark.anyio
async def test_workspace_dlp_cookie_csrf_scope_and_revoked_transaction(file_subjects):  # noqa: F811
    app, database, f = file_subjects
    app.include_router(routes.router)
    payload = {
        "name": "Workspace protection",
        "rule_type": "keyword",
        "pattern": "isolated-dlp-marker",
        "action": "log_only",
    }
    for headers, expected in (
        ({}, 403),
        ({**_headers(f.source), "X-CSRF-Token": "invalid"}, 403),
        ({**_headers(f.source), "Origin": "https://foreign.test"}, 403),
        ({**_headers(f.source), "Authorization": "Bearer invalid"}, 400),
    ):
        response = await f.source.post("/dlp/rules", json=payload, headers=headers)
        assert response.status_code == expected, response.json().get("code")
    created = await f.source.post("/dlp/rules", json=payload, headers=_headers(f.source))
    assert created.status_code == 200, created.json().get("code")
    rule_id = created.json()["dlp_rule"]["dlp_rule_id"]
    response = await f.source.get("/dlp/rules")
    assert response.status_code == 200, response.json().get("code")
    assert [r["dlp_rule_id"] for r in response.json()["dlp_rules"]] == [rule_id]
    foreign = await f.target.get("/dlp/rules")
    assert foreign.status_code == 200 and foreign.json()["dlp_rules"] == []
    foreign = await f.target.patch(
        f"/dlp/rules/{rule_id}",
        json={"status": "disabled"},
        headers=_headers(f.target),
    )
    assert foreign.status_code == 404 and foreign.json()["code"] == "dlp.rule_not_found"
    changed = await f.source.patch(
        f"/dlp/rules/{rule_id}",
        json={"scan_scope": "both"},
        headers=_headers(f.source),
    )
    assert changed.status_code == 200 and changed.json()["dlp_rule"]["scan_scope"] == "both"
    context = authenticate_workspace_session_as_auth_context(
        app.state.config,
        f.source.cookies.get(WORKSPACE_SESSION),
    )
    with closing(sqlite3.connect(database)) as connection:
        revoked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        connection.execute(
            "UPDATE workspace_sessions SET status='revoked', revoked_at=?, "
            "revoked_reason='test.revoke', updated_at=? WHERE workspace_session_id=?",
            (revoked_at, revoked_at, context.session.session_id),
        )
        connection.commit()
        before = connection.execute("SELECT * FROM dlp_rules").fetchall()
        audit_before = connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    operations = (
        lambda: service.list_dlp_rules(
            app.state.config,
            context,
            status=None,
            rule_type=None,
            action=None,
            scan_scope=None,
            request_id="dlp-stale-read",
            trace_id="dlp-stale-read",
        ),
        lambda: service.create_dlp_rule(
            app.state.config,
            context,
            CreateDlpRuleRequest(**{**payload, "pattern": "second"}),
            request_id="dlp-stale-create",
            trace_id="dlp-stale-create",
        ),
        lambda: service.update_dlp_rule(
            app.state.config,
            context,
            rule_id,
            UpdateDlpRuleRequest(status="disabled"),
            request_id="dlp-stale-update",
            trace_id="dlp-stale-update",
        ),
    )
    for operation in operations:
        with pytest.raises(ApiError) as denied:
            operation()
        assert denied.value.code.startswith("workspace_session.")
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT * FROM dlp_rules").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0] == audit_before
