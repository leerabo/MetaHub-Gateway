"""Real Cookie sessions against local HTTP routes; fixture data, not GA evidence."""

from functools import partial

import pytest
from metahub_assurance_runner.probes import WORKSPACE_SESSION
from metahub_control.audit import AuditRequestMeta
from metahub_control.cost_anomalies import service as anomaly_service
from metahub_control.cost_anomalies.models import CostAnomalyActionRequest
from metahub_control.cost_anomalies.routes import router as anomaly_router
from metahub_control.permissions import service as permission_service
from metahub_control.permissions.models import ReplaceRolePermissionsRequest
from metahub_control.permissions.routes import router as permission_router
from metahub_control.policies.models import CreatePolicyRequest
from metahub_control.policies.service import create_policy
from metahub_control.policy_snapshots import service as snapshot_service
from metahub_control.policy_snapshots.models import PublishPolicySnapshotRequest
from metahub_control.policy_snapshots.routes import router as snapshot_router
from metahub_control.workspace_access.service import authenticate_workspace_session_as_auth_context
from metahub_shared.runtime.errors import ApiError
from test_prd147_workspace_files import (  # noqa: F401
    _headers,
    file_subjects,
    migrated_baseline,
)


@pytest.mark.anyio
async def test_workspace_governance_scope_csrf_cas_replay_and_transaction_revocation(
    file_subjects,  # noqa: F811
):
    app, _database, f = file_subjects
    for router in (permission_router, anomaly_router, snapshot_router):
        app.include_router(router)
    assert not app.dependency_overrides
    source, other, member = f.source, f.target, f.member
    config = app.state.config
    meta = AuditRequestMeta(client_ip=None, user_agent=None)
    correlation = {"request_id": "req_governance_fixture", "trace_id": "trc_governance_fixture"}
    owner_context = authenticate_workspace_session_as_auth_context(
        config, source.cookies.get(WORKSPACE_SESSION)
    )
    stale = authenticate_workspace_session_as_auth_context(
        config, member.cookies.get(WORKSPACE_SESSION)
    )
    matrix = await source.get("/permissions")
    assert matrix.status_code == 200, matrix.json().get("code")
    scope = matrix.json()
    assert scope["workspace_id"] == f.source_workspace_id
    assert scope["tenant_id"] == owner_context.user.tenant_id
    assert scope["workspace_id"] != scope["tenant_id"]
    assert {row["tenant_id"] for row in scope["roles"] + scope["permissions"]} == {
        scope["tenant_id"]
    }
    role = next(row for row in scope["roles"] if row["code"] == "developer")
    role_path = f"/roles/{role['role_id']}/permissions"
    replace_body = {
        "expected_revision": role["permission_revision"],
        "permission_codes": role["permission_codes"],
    }
    noop = await source.patch(role_path, json=replace_body, headers=_headers(source))
    assert noop.status_code == 200, noop.json().get("code")
    assert noop.json()["changed"] is False
    assert noop.json()["workspace_id"] == scope["workspace_id"]
    foreign = await other.patch(role_path, json=replace_body, headers=_headers(other))
    assert foreign.status_code == 404
    assert "request_log.read" in role["permission_codes"]
    replace_body["permission_codes"] = [
        code for code in role["permission_codes"] if code != "request_log.read"
    ]
    changed = await source.patch(role_path, json=replace_body, headers=_headers(source))
    assert changed.status_code == 200, changed.json().get("code")
    assert changed.json()["role"]["permission_revision"] == role["permission_revision"] + 1
    conflict = await source.patch(role_path, json=replace_body, headers=_headers(source))
    assert conflict.status_code == 409

    anomaly_id = "can_" + "a" * 32
    writes = (
        ("PATCH", role_path),
        ("PATCH", f"/cost-anomalies/{anomaly_id}/action"),
        ("POST", "/policy-snapshots/publish"),
        ("POST", "/policy-snapshots/psn_missing/rollback"),
    )
    cookie_headers = _headers(source, attack=True)
    for method, path in writes:
        for attack, expected in (
            ({}, 401),
            ({**cookie_headers, "X-CSRF-Token": "invalid"}, 403),
            ({**cookie_headers, "Origin": "https://foreign.test"}, 403),
            ({**cookie_headers, "Authorization": "Bearer invalid"}, 400),
        ):
            denied = await f.anonymous.request(
                method, path, content=b"invalid-json", headers=attack
            )
            assert denied.status_code == expected, (path, denied.json().get("code"))
    anomalies = await source.get("/cost-anomalies")
    assert anomalies.status_code == 200, anomalies.json().get("code")
    assert anomalies.json()["cost_anomalies"] == []

    for body in (
        b"invalid-json",
        b'{"reason":"first","reason":"second"}',
        b'{"reason":NaN}',
        b'[]',
        b'{"reason":"' + b"x" * 4096 + b'"}',
    ):
        for path in ("/policy-snapshots/publish", "/policy-snapshots/psn_missing/rollback"):
            invalid = await source.post(
                path, content=body, headers={**_headers(source), "Content-Type": "application/json"}
            )
            assert invalid.status_code == 422, invalid.json().get("code")
            assert invalid.json()["code"] == "policy_snapshot.payload_invalid"

    async def publish(request_id):
        current = await source.get("/policy-snapshots/current")
        assert current.status_code == 200, current.json().get("code")
        headers = {
            **_headers(source),
            "X-Request-ID": request_id,
            "If-Match": current.headers["ETag"],
            "If-Policy-Candidate-Match": current.headers["X-Policy-Candidate-ETag"],
        }
        body = {"reason": "Governance fixture publication"}
        response = await source.post("/policy-snapshots/publish", json=body, headers=headers)
        assert response.status_code == 201, response.json().get("code")
        replay = await source.post("/policy-snapshots/publish", json=body, headers=headers)
        assert replay.status_code == 201, replay.json().get("code")
        assert replay.json()["idempotent_replay"] is True
        return response.json()["policy_snapshot"], headers

    first, _first_headers = await publish("req_governance_publish_first")
    # Candidate setup uses the service and a real session; this is fixture data.
    create_policy(
        config,
        owner_context,
        CreatePolicyRequest(
            name="Governance test candidate",
            status="active",
            priority=100,
            rule={"condition_expr": 'env == "production"'},
            action={"type": "deny"},
        ),
        audit_request_meta=meta,
        **correlation,
    )
    second, second_headers = await publish("req_governance_publish_second")
    assert second["snapshot_version"] == first["snapshot_version"] + 1
    conflict = await source.post(
        "/policy-snapshots/publish",
        json={"reason": "Stale head fixture"},
        headers={**second_headers, "X-Request-ID": "req_governance_conflict"},
    )
    assert conflict.status_code == 409
    rollback_path = f"/policy-snapshots/{first['policy_snapshot_id']}/rollback"
    foreign = await other.post(
        rollback_path,
        json={"reason": "Foreign target fixture"},
        headers={**_headers(other), "If-Match": '"0"', "X-Request-ID": "req_foreign_rollback"},
    )
    assert foreign.status_code == 404, foreign.json().get("code")
    rollback_headers = {
        **_headers(source),
        "If-Match": f'"{second["snapshot_version"]}"',
        "X-Request-ID": "req_governance_rollback",
    }
    for replayed in (False, True):
        rolled = await source.post(
            rollback_path, json={"reason": "Governance rollback fixture"}, headers=rollback_headers
        )
        assert rolled.status_code == 201, rolled.json().get("code")
        assert rolled.json()["idempotent_replay"] is replayed
        assert rolled.json()["policy_snapshot"]["content_hash"] == first["content_hash"]
    history = await source.get("/policy-snapshots")
    foreign_history = await other.get("/policy-snapshots")
    assert history.status_code == foreign_history.status_code == 200
    assert history.json()["total"] == 3 and foreign_history.json()["total"] == 0

    changed = await source.patch(
        f"/memberships/{f.member_membership_id}",
        json={"role": "finance_admin"},
        headers={**_headers(source), "If-Match": f'"{f.member_revision}"'},
    )
    assert changed.status_code == 200, changed.json().get("code")
    for path in ("/permissions", "/cost-anomalies", "/policy-snapshots/current"):
        denied = await member.get(path)
        assert denied.status_code == 401, (path, denied.json().get("code"))
    stale_calls = (
        partial(permission_service.list_permission_matrix, config, stale, **correlation),
        partial(
            permission_service.replace_role_permissions,
            config,
            stale,
            role["role_id"],
            ReplaceRolePermissionsRequest(**replace_body),
            request_meta=meta,
            **correlation,
        ),
        partial(
            anomaly_service.apply_cost_anomaly_action,
            config,
            stale,
            anomaly_id,
            CostAnomalyActionRequest(action="acknowledge"),
            idempotency_key="governance_stale_action",
            audit_request_meta=meta,
            **correlation,
        ),
        partial(
            snapshot_service.get_current_policy_snapshot,
            config,
            stale,
            query_keys=(),
            **correlation,
        ),
        partial(
            snapshot_service.publish_policy_snapshot,
            config,
            stale,
            PublishPolicySnapshotRequest(reason="Revoked publisher fixture"),
            if_match=second_headers["If-Match"],
            candidate_match=second_headers["If-Policy-Candidate-Match"],
            audit_request_meta=meta,
            **correlation,
        ),
    )
    for call in stale_calls:
        with pytest.raises(ApiError) as failure:
            call()
        assert failure.value.code.startswith("workspace_session."), failure.value.code
    switched = await member.post(
        "/identity/workspace-switches",
        json={"workspace_id": f.source_workspace_id},
        headers=_headers(member, global_session=True),
    )
    assert switched.status_code == 200, switched.json().get("code")
    finance_read = await member.get("/cost-anomalies")
    assert finance_read.status_code == 200, finance_read.json().get("code")
    for method, path in writes:
        denied = await member.request(
            method, path, content=b"invalid-json", headers=_headers(member)
        )
        if path.startswith("/cost-anomalies/"):
            # Finance admins can act on anomalies; authorized requests reach wire validation.
            assert denied.status_code == 415, denied.json().get("code")
            assert denied.json()["code"] == "cost_anomaly.media_type_invalid"
        else:
            assert denied.status_code == 403, (path, denied.json().get("code"))

    finance_role = next(row for row in scope["roles"] if row["code"] == "finance_admin")
    assert "billing.anomaly.write" in finance_role["permission_codes"]
    read_only = await source.patch(
        f"/roles/{finance_role['role_id']}/permissions",
        json={
            "expected_revision": finance_role["permission_revision"],
            "permission_codes": [
                code for code in finance_role["permission_codes"] if code != "billing.anomaly.write"
            ],
        },
        headers=_headers(source),
    )
    assert read_only.status_code == 200, read_only.json().get("code")
    revoked = await member.get("/cost-anomalies")
    assert revoked.status_code == 401, revoked.json().get("code")
    switched = await member.post(
        "/identity/workspace-switches",
        json={"workspace_id": f.source_workspace_id},
        headers=_headers(member, global_session=True),
    )
    assert switched.status_code == 200, switched.json().get("code")
    assert (await member.get("/cost-anomalies")).status_code == 200
    denied = await member.patch(
        f"/cost-anomalies/{anomaly_id}/action",
        content=b"invalid-json",
        headers=_headers(member),
    )
    assert denied.status_code == 403, denied.json().get("code")
