"""Workspace Cookie Webhook transport and tenant boundaries, not GA signoff."""

import hashlib

import pytest
from metahub_control.webhooks.routes import router
from test_prd147_workspace_files import (  # noqa: F401
    _headers,
    file_subjects,
    migrated_baseline,
)


@pytest.mark.anyio
async def test_workspace_webhooks_cookie_csrf_isolation_and_revocation(file_subjects, monkeypatch):
    app, _database, fixtures = file_subjects
    app.include_router(router)
    assert app.state.config.provider_key_encryption_secret
    monkeypatch.delenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", raising=False)
    monkeypatch.delenv("METAHUB_AUTH_TOKEN_SECRET", raising=False)
    source, other = fixtures.source, fixtures.target
    payload = {
        "name": "Workspace webhook fixture",
        "endpoint_url": "https://receiver.example.test/events",
        "event_types": ["budget.threshold_reached"],
        "status": "disabled",
    }
    headers = _headers(source, attack=True)
    for attack, expected in (
        ({}, 401),
        ({**headers, "X-CSRF-Token": "invalid"}, 403),
        ({**headers, "Origin": "https://foreign.test"}, 403),
        ({**headers, "Authorization": "Bearer invalid"}, 400),
    ):
        denied = await fixtures.anonymous.post(
            "/webhooks", content=b"invalid-json", headers=attack
        )
        assert denied.status_code == expected, denied.json().get("code")

    created = await source.post("/webhooks", json=payload, headers=_headers(source))
    assert created.status_code == 200, created.json().get("code")
    endpoint_id = created.json()["webhook"]["webhook_endpoint_id"]
    original_secret_hash = hashlib.sha256(created.json()["signing_secret"].encode()).digest()
    listed = await source.get("/webhooks")
    assert listed.status_code == 200, listed.json().get("code")
    assert endpoint_id in {
        endpoint["webhook_endpoint_id"] for endpoint in listed.json()["webhooks"]
    }
    assert "signing_secret" not in listed.text
    foreign_list = await other.get("/webhooks")
    assert foreign_list.status_code == 200
    assert endpoint_id not in {
        endpoint["webhook_endpoint_id"] for endpoint in foreign_list.json()["webhooks"]
    }
    for client, expected in ((source, 200), (other, 404)):
        detail = await client.get(f"/webhooks/{endpoint_id}/deliveries")
        assert detail.status_code == expected, detail.json().get("code")

    writes = (
        (f"/webhooks/{endpoint_id}/test", {}),
        (f"/webhooks/{endpoint_id}/rotate-secret", {}),
        (f"/webhooks/{endpoint_id}/replay", {}),
        ("/webhook-deliveries/whd_missing/replay", {}),
        ("/webhook-dead-letters/whdl_missing/archive", {}),
    )
    for path, body in writes:
        missing_csrf = await source.post(path, json=body)
        assert missing_csrf.status_code == 403, (path, missing_csrf.json().get("code"))
        mixed = await source.post(
            path, json=body, headers={**_headers(source), "Authorization": "Bearer invalid"}
        )
        assert mixed.status_code == 400, (path, mixed.json().get("code"))

    rotated = await source.post(
        f"/webhooks/{endpoint_id}/rotate-secret", headers=_headers(source)
    )
    assert rotated.status_code == 200, rotated.json().get("code")
    rotated_secret_hash = hashlib.sha256(rotated.json()["signing_secret"].encode()).digest()
    assert rotated_secret_hash != original_secret_hash
    foreign_rotation = await other.post(
        f"/webhooks/{endpoint_id}/rotate-secret", headers=_headers(other)
    )
    assert foreign_rotation.status_code == 404
    dead_letters = await source.get("/webhook-dead-letters")
    assert dead_letters.status_code == 200, dead_letters.json().get("code")

    member = fixtures.member
    member_read = await member.get("/webhooks")
    assert member_read.status_code == 200
    changed = await source.patch(
        f"/memberships/{fixtures.member_membership_id}",
        json={"role": "viewer"},
        headers={**_headers(source), "If-Match": f'"{fixtures.member_revision}"'},
    )
    assert changed.status_code == 200, changed.json().get("code")
    denied = await member.get("/webhooks")
    assert denied.status_code == 401, denied.json().get("code")
