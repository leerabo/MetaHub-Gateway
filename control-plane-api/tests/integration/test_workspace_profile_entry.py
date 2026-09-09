"""Real profile saves preserve authority, CAS and organization session renewal."""

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_shared.db import connect_database
from test_prd095_platform_iam import GLOBAL_CSRF_COOKIE, _csrf
from test_prd095_platform_iam import migrated_database as migrated_database
from test_workspace_consumers import _open_organization, _prepare


@pytest.mark.anyio
async def test_local_profile_save_renews_session_and_preserves_other_organization(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        first_id = await _open_organization(client, monkeypatch)
        second_id = await _open_organization(
            client, monkeypatch, slug="profile-other", signup=False
        )
        before = (await client.get(f"/workspaces/{first_id}")).json()["workspace"]
        other = (await client.get(f"/workspaces/{second_id}")).json()["workspace"]
        await client.post(
            "/identity/workspace-switches",
            json={"workspace_id": first_id},
            headers=_csrf(client, GLOBAL_CSRF_COOKIE),
        )
        headers = {**_csrf(client, GLOBAL_CSRF_COOKIE), "If-Match": f'"{before["revision"]}"'}
        denied = await client.patch(
            f"/workspaces/{first_id}",
            json={"name": "No CSRF"},
            headers={"Origin": "http://localhost:5173", "If-Match": headers["If-Match"]},
        )
        assert denied.status_code == 403
        for changes in ({"service_mode": "public_service"}, {"tenant_id": "other"}):
            denied = await client.patch(f"/workspaces/{first_id}", json=changes, headers=headers)
            assert denied.status_code == 422
            assert denied.json()["code"] == "workspace.update_invalid"
        updated = await client.patch(
            f"/workspaces/{first_id}", json={"name": "Profile Rename Canary"}, headers=headers
        )
        assert updated.status_code == 200, updated.text
        result = updated.json()["workspace"]
        assert result["name"] == "Profile Rename Canary"
        assert result["revision"] == before["revision"] + 1
        assert result["service_mode"] == before["service_mode"]
        stale = await client.patch(
            f"/workspaces/{first_id}", json={"name": "Stale Canary"}, headers=headers
        )
        assert stale.status_code == 412, stale.text
        assert (await client.get("/workspace/session")).status_code == 401
        reentered = await client.post(
            "/identity/workspace-switches",
            json={"workspace_id": first_id},
            headers=_csrf(client, GLOBAL_CSRF_COOKIE),
        )
        assert reentered.status_code == 200, reentered.text
        current = await client.get("/workspace/session")
        assert current.status_code == 200
        assert current.json()["context"]["name"] == result["name"]
        assert (await client.get(f"/workspaces/{second_id}")).json()["workspace"] == other
        assert (await client.get(f"/workspaces/{first_id}")).json()["workspace"] == result
        with connect_database(database) as connection:
            assert (
                connection.execute(
                    "SELECT name FROM organization_profiles WHERE workspace_id=?", (first_id,)
                ).fetchone()[0]
                == result["name"]
            )
            payloads = connection.execute(
                "SELECT payload_json FROM workspace_events WHERE workspace_id=?", (first_id,)
            ).fetchall()
            assert all("Canary" not in row[0] for row in payloads)
