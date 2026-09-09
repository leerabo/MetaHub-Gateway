"""An organization can revoke its membership, not the person's global identity."""

from contextlib import aclosing
from uuid import uuid4

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError
from test_prd095_platform_iam import migrated_database as migrated_database
from test_prd147_workspace_files import _headers, workspace_http_subjects
from test_workspace_consumers import _prepare


async def verify_membership_scope(database, tmp_path, monkeypatch):
    async with aclosing(workspace_http_subjects(database, tmp_path, monkeypatch)) as subjects:
        async for _app, actors in subjects:
            path = f"/workspaces/{actors.source_workspace_id}"
            original = (await actors.source.get(path)).json()["workspace"]
            assert (await actors.member.get(path)).status_code == 200
            member_session = await actors.member.get("/workspace/session")
            assert member_session.json()["context"]["role"] == "admin"
            denied = await actors.member.patch(
                path,
                json={"name": "Admin Cannot Rename"},
                headers={
                    **_headers(actors.member, global_session=True),
                    "If-Match": f'"{original["revision"]}"',
                },
            )
            assert denied.status_code == 404, denied.json().get("code")
            assert (await actors.target.get(path)).status_code == 404
            created = await actors.member.post(
                "/workspaces",
                json={
                    "kind": "organization",
                    "name": "Member independent organization",
                    "slug": "scope-" + uuid4().hex,
                    "region": "cn-north-1",
                    "service_mode": "internal_only",
                },
                headers={
                    **_headers(actors.member, global_session=True),
                    "Idempotency-Key": uuid4().hex,
                },
            )
            assert created.status_code == 201, created.json().get("code")
            own_id = created.json()["workspace"]["workspace_id"]
            entered = await actors.member.post(
                "/identity/workspace-switches",
                json={"workspace_id": own_id},
                headers=_headers(actors.member, global_session=True),
            )
            assert entered.status_code == 200
            identity = (await actors.member.get("/identity/me")).json()["account"]
            revoked = await actors.source.patch(
                f"/memberships/{actors.member_membership_id}",
                json={"status": "disabled"},
                headers={**_headers(actors.source), "If-Match": f'"{actors.member_revision}"'},
            )
            assert revoked.status_code == 200, revoked.json().get("code")
            denied = await actors.member.get(path)
            assert denied.status_code == 404, denied.json().get("code")
            assert denied.json()["code"] == "workspace.not_found"
            assert (await actors.source.get(path)).json()["workspace"] == original
            current = await actors.member.get("/identity/me")
            assert current.status_code == 200, current.json().get("code")
            assert current.json()["account"] == identity
            assert (await actors.member.get(f"/workspaces/{own_id}")).status_code == 200
            current = await actors.member.get("/workspace/session")
            assert current.status_code == 200, current.json().get("code")
            assert current.json()["context"]["workspace_id"] == own_id
            denied = await actors.member.post(
                "/identity/workspace-switches",
                json={"workspace_id": actors.source_workspace_id},
                headers=_headers(actors.member, global_session=True),
            )
            assert denied.status_code == 404, denied.json().get("code")
            with connect_database(database) as connection:
                state = connection.execute(
                    "SELECT u.status,m.status FROM users u JOIN workspace_memberships m "
                    "ON m.tenant_id=u.tenant_id AND m.legacy_user_id=u.user_id "
                    "WHERE m.workspace_membership_id=?",
                    (actors.member_membership_id,),
                ).fetchone()
                assert tuple(state) == ("disabled", "disabled")
            version = "0157" if database.startswith("postgres") else "0150"
            with pytest.raises(DbError) as rejected:
                rollback_last_migration(database, expected_version=version)
            assert rejected.value.code == "db.migration_failed"
            assert "Rollback failed for migration " + version in rejected.value.message
            if version == "0150":
                assert "membership_identity_rollback_blocked" in rejected.value.message
            assert (await actors.member.get("/identity/me")).status_code == 200
            assert (await actors.member.get(path)).status_code == 404
            with connect_database(database) as connection:
                account_id = connection.execute(
                    "SELECT account_id FROM workspace_memberships WHERE workspace_membership_id=?",
                    (actors.member_membership_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE global_accounts SET status='disabled',"
                    "session_revision=session_revision+1 "
                    "WHERE account_id=?",
                    (account_id,),
                )
                connection.commit()
            restored = await actors.source.patch(
                f"/memberships/{actors.member_membership_id}",
                json={"status": "active"},
                headers={
                    **_headers(actors.source),
                    "If-Match": f'"{revoked.json()["membership"]["revision"]}"',
                },
            )
            assert restored.status_code == 200, restored.json().get("code")
            assert (await actors.member.get("/identity/me")).status_code == 401
            with connect_database(database) as connection:
                assert (
                    connection.execute(
                        "SELECT status FROM global_accounts WHERE account_id=?",
                        (account_id,),
                    ).fetchone()[0]
                    == "disabled"
                )


@pytest.mark.anyio
async def test_member_disable_preserves_global_identity_and_other_organization(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    await verify_membership_scope(database, tmp_path, monkeypatch)


def test_membership_identity_pristine_migration_roundtrip(monkeypatch, tmp_path, migrated_database):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    assert rollback_last_migration(database, expected_version="0150").rolled_back == "0150"
    assert apply_migrations(database).applied == ["0150"]
    with connect_database(database) as connection:
        row = connection.execute(
            "SELECT s.sql=m.trigger_sql AND metahub_sha256(s.sql)=m.trigger_sha256 "
            "FROM sqlite_schema s JOIN tenant_deletion_trigger_manifest m ON m.trigger_name=s.name "
            "WHERE s.name='trg_users_identity_status_update_prd091'",
        ).fetchone()
        assert row[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
