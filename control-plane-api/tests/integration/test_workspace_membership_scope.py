"""An organization can revoke its membership, not the person's global identity."""

from contextlib import aclosing
from uuid import uuid4

import pytest
from metahub_control.consumers.routes import router as consumers_router
from metahub_control.virtual_keys.routes import router as virtual_keys_router
from metahub_gateway.auth.virtual_key import authenticate_virtual_key
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
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
            # This is deliberately a stale owner-specific rollback request. The
            # fixture is migrated through 0156, so the runner must reject it
            # before executing any rollback SQL.
            version = "0150"
            with pytest.raises(DbError) as rejected:
                rollback_last_migration(database, expected_version=version)
            assert rejected.value.code == "db.migration_version_mismatch"
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


@pytest.mark.anyio
async def test_member_remove_revokes_personal_and_assigned_keys(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    """DELETE membership must invalidate every key issued for that member."""
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    async with aclosing(workspace_http_subjects(database, tmp_path, monkeypatch)) as subjects:
        async for app, actors in subjects:
            app.include_router(consumers_router)
            app.include_router(virtual_keys_router)
            with connect_database(database) as connection:
                target = connection.execute(
                    """SELECT account.display_name,account.email_normalized
                       FROM workspace_memberships member
                       JOIN global_accounts account ON account.account_id=member.account_id
                       WHERE member.workspace_membership_id=?""",
                    (actors.member_membership_id,),
                ).fetchone()
            created_consumer = await actors.source.post(
                "/consumers",
                json={
                    "display_name": target["display_name"],
                    "employee_code": target["email_normalized"],
                },
                headers=_headers(actors.source),
            )
            assert created_consumer.status_code == 201, created_consumer.text
            created_key = await actors.source.post(
                "/virtual-keys",
                json={
                    "name": "Member removal regression key",
                    "consumer_id": created_consumer.json()["consumer"]["consumer_id"],
                },
                headers=_headers(actors.source),
            )
            assert created_key.status_code == 201, created_key.text
            plain_value = created_key.json()["plain_value"]
            assert authenticate_virtual_key(app.state.config, f"Bearer {plain_value}")
            personal_key = await actors.member.post(
                "/virtual-keys",
                json={"name": "Member personal removal regression key"},
                headers=_headers(actors.member),
            )
            assert personal_key.status_code == 201, personal_key.text
            personal_plain_value = personal_key.json()["plain_value"]
            assert authenticate_virtual_key(app.state.config, f"Bearer {personal_plain_value}")

            memberships = await actors.source.get("/memberships", headers=_headers(actors.source))
            assert memberships.status_code == 200, memberships.text
            member = next(
                item for item in memberships.json()["memberships"]
                if item["membership_id"] == actors.member_membership_id
            )
            removed = await actors.source.delete(
                f"/memberships/{actors.member_membership_id}",
                headers={**_headers(actors.source), "If-Match": f'"{member["revision"]}"'},
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["membership"]["status"] == "disabled"
            with pytest.raises(ApiError) as denied:
                authenticate_virtual_key(app.state.config, f"Bearer {plain_value}")
            assert denied.value.code == "gateway.auth_invalid"
            with pytest.raises(ApiError) as personal_denied:
                authenticate_virtual_key(app.state.config, f"Bearer {personal_plain_value}")
            assert personal_denied.value.code == "gateway.auth_invalid"
            listed = await actors.source.get("/virtual-keys", headers=_headers(actors.source))
            assert listed.status_code == 200, listed.text
            assert all(item["virtual_key_id"] != created_key.json()["virtual_key"]["virtual_key_id"]
                       for item in listed.json()["virtual_keys"])
            consumers = await actors.source.get("/consumers", headers=_headers(actors.source))
            assert consumers.status_code == 200, consumers.text
            assert consumers.json()["total"] == 0


def test_membership_identity_pristine_migration_roundtrip(monkeypatch, tmp_path, migrated_database):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    assert rollback_last_migration(database, expected_version="0156").rolled_back == "0156"
    assert apply_migrations(database).applied == ["0156"]
    with connect_database(database) as connection:
        row = connection.execute(
            "SELECT s.sql=m.trigger_sql AND metahub_sha256(s.sql)=m.trigger_sha256 "
            "FROM sqlite_schema s JOIN tenant_deletion_trigger_manifest m ON m.trigger_name=s.name "
            "WHERE s.name='trg_users_identity_status_update_prd091'",
        ).fetchone()
        assert row[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
