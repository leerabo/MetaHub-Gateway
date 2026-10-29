"""Real Workspace SDK metadata, ZIP downloads, credential domains and revocation."""

import io
import zipfile
from contextlib import aclosing

import pytest
from metahub_control.developer_portal import service as sdk_service
from metahub_control.developer_portal.routes import router
from metahub_shared.db import connect_database
from test_prd095_platform_iam import migrated_database as migrated_database
from test_prd147_workspace_files import _headers, workspace_http_subjects
from test_workspace_consumers import _prepare


@pytest.mark.anyio
async def test_workspace_sdk_read_download_and_permission_boundary(
    migrated_database, tmp_path, monkeypatch,
):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    await verify_workspace_sdk_boundary(database, tmp_path, monkeypatch)


async def verify_workspace_sdk_boundary(database, tmp_path, monkeypatch):
    async with aclosing(workspace_http_subjects(database, tmp_path, monkeypatch)) as subjects:
        async for app, actors in subjects:
            app.include_router(router)
            for language, files, archive_root in (
                ("python", sdk_service.PYTHON_EXAMPLE_FILE_ALLOWLIST,
                 sdk_service.PYTHON_EXAMPLE_ARCHIVE_ROOT),
                ("javascript", sdk_service.JAVASCRIPT_EXAMPLE_FILE_ALLOWLIST,
                 sdk_service.JAVASCRIPT_EXAMPLE_ARCHIVE_ROOT),
            ):
                path = "/developer/sdk-examples/" + language
                source = await actors.source.get(path)
                target = await actors.target.get(path)
                assert source.status_code == target.status_code == 200
                assert source.json()["tenant_id"] != target.json()["tenant_id"]
                injected = await actors.source.get(
                    path, params={"tenant_id": target.json()["tenant_id"]},
                )
                assert injected.json()["tenant_id"] == source.json()["tenant_id"]
                for suffix in ("", "/download"):
                    assert (await actors.anonymous.get(path + suffix)).status_code == 401
                    mixed = await actors.source.get(
                        path + suffix, headers={"Authorization": "Bearer test"},
                    )
                    assert mixed.status_code == 400
                    assert mixed.json()["code"] == "workspace_session.credential_conflict"
                downloaded = await actors.source.get(path + "/download")
                assert downloaded.status_code == 200
                assert downloaded.headers["content-type"] == "application/zip"
                assert "no-store" in downloaded.headers["cache-control"]
                with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                    assert archive.testzip() is None
                    assert set(archive.namelist()) == {archive_root + "/" + file for file in files}
                    assert b"your-virtual-key" in archive.read(archive_root + "/.env.example")
                assert (await actors.member.get(path)).status_code == 200
            with connect_database(database) as connection:
                events = connection.execute(
                    "SELECT session_id,payload_json FROM developer_portal_events "
                    "WHERE tenant_id=? AND event_type='sdk.example_downloaded'",
                    (source.json()["tenant_id"],),
                ).fetchall()
                assert len(events) == 2
                assert all(row[0] is None for row in events)
                assert all("Bearer" not in row[1] and "wss_" not in row[1] for row in events)
            revoked = await actors.source.patch(
                f"/memberships/{actors.member_membership_id}", json={"role": "viewer"},
                headers={**_headers(actors.source), "If-Match": f'"{actors.member_revision}"'},
            )
            assert revoked.status_code == 200
            for language in ("python", "javascript"):
                for suffix in ("", "/download"):
                    denied = await actors.member.get("/developer/sdk-examples/" + language + suffix)
                    assert denied.status_code in (401, 403)
            assert (await actors.source.get("/developer/quickstart-data")).status_code == 401


@pytest.mark.anyio
async def test_sdk_download_rechecks_membership_after_archive_generation(
    migrated_database, tmp_path, monkeypatch,
):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    await verify_sdk_download_revocation(database, tmp_path, monkeypatch)


async def verify_sdk_download_revocation(database, tmp_path, monkeypatch):
    async with aclosing(workspace_http_subjects(database, tmp_path, monkeypatch)) as subjects:
        async for app, actors in subjects:
            app.include_router(router)
            build = sdk_service._build_sdk_example_archive

            def revoke_during_archive(spec, build=build, member_id=actors.member_membership_id):
                archive = build(spec)
                with connect_database(database) as connection:
                    connection.execute(
                        "UPDATE workspace_memberships SET status='disabled',revision=revision+1 "
                        "WHERE workspace_membership_id=?", (member_id,),
                    )
                    connection.commit()
                return archive

            monkeypatch.setattr(sdk_service, "_build_sdk_example_archive", revoke_during_archive)
            response = await actors.member.get("/developer/sdk-examples/python/download")
            assert response.status_code in (401, 403)
            assert response.headers["content-type"].startswith("application/json")
            with connect_database(database) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM developer_portal_events "
                    "WHERE event_type='sdk.example_downloaded'",
                ).fetchone()[0] == 0
