"""Employees own usage keys without getting login accounts or management privileges."""

import asyncio
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.consumers import service as consumer_service
from metahub_control.consumers.routes import router as consumers_router
from metahub_control.virtual_keys.routes import router as keys_router
from metahub_gateway.auth.virtual_key import authenticate_virtual_key
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from migration_lifecycle_support import rollback_later_migrations
from test_local_workspace_creation import BODY, _dev_app, _signup
from test_prd095_platform_iam import (
    ADMIN_PASSWORD,
    GLOBAL_CSRF_COOKIE,
    WORKSPACE_CSRF_COOKIE,
    _csrf,
)
from test_prd095_platform_iam import migrated_database as migrated_database


def _prepare(monkeypatch, tmp_path, migrated_database):
    database, app = _dev_app(monkeypatch, tmp_path, migrated_database)
    app.include_router(consumers_router)
    app.include_router(keys_router)
    return database, app


async def _open_organization(client, monkeypatch, *, slug="consumer-organization", signup=True):
    if signup:
        await _signup(client, monkeypatch)
    created = await client.post(
        "/workspaces",
        json={**BODY, "slug": slug},
        headers={**_csrf(client, GLOBAL_CSRF_COOKIE), "Idempotency-Key": slug + "-create"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace"]["workspace_id"]
    switched = await client.post(
        "/identity/workspace-switches",
        json={"workspace_id": workspace_id},
        headers=_csrf(client, GLOBAL_CSRF_COOKIE),
    )
    assert switched.status_code == 200, switched.text
    return workspace_id


async def _consumer(client, name="Employee Canary", department="Private Department Canary"):
    result = await client.post(
        "/consumers",
        json={"display_name": name, "department": department},
        headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
    )
    assert result.status_code == 201, result.text
    return result.json()["consumer"]


async def _key(client, consumer):
    response = await client.post(
        "/virtual-keys",
        json={"name": "Employee Tool", "consumer_id": consumer["consumer_id"]},
        headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.anyio
async def test_consumer_without_account_multiple_keys_rotation_and_disable(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        with connect_database(database) as connection:
            counts = [
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("users", "global_accounts", "workspace_memberships")
            ]
        employee = await _consumer(client)
        other = await _consumer(client)
        assert employee["consumer_id"] != other["consumer_id"]
        first, second = await _key(client, employee), await _key(client, employee)
        key_id, old_revision = (
            first["virtual_key"]["virtual_key_id"],
            first["virtual_key"]["credential_revision"],
        )
        authorization = "Bearer " + first["plain_value"]
        authenticated = authenticate_virtual_key(app.state.config, authorization)
        assert authenticated.consumer_id == employee["consumer_id"]
        assert (await client.get("/consumers?limit=1")).json()["total"] == 2
        directory = (await client.get("/consumers")).json()["consumers"]
        assert (
            next(item for item in directory if item["consumer_id"] == employee["consumer_id"])[
                "key_count"
            ]
            == 2
        )
        with connect_database(database) as connection:
            assert counts == [
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("users", "global_accounts", "workspace_memberships")
            ]
        rotated = await client.post(
            f"/virtual-keys/{key_id}/rotate",
            json={"expected_revision": old_revision},
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert rotated.status_code == 200, rotated.text
        replacement = rotated.json()
        assert replacement["virtual_key"]["consumer_id"] == employee["consumer_id"]
        assert replacement["virtual_key"]["virtual_key_id"] == key_id
        with pytest.raises(ApiError) as invalid:
            authenticate_virtual_key(app.state.config, authorization)
        assert invalid.value.code == "gateway.auth_invalid"
        assert (
            authenticate_virtual_key(
                app.state.config, "Bearer " + replacement["plain_value"]
            ).consumer_id
            == employee["consumer_id"]
        )
        stale = await client.post(
            f"/virtual-keys/{key_id}/rotate",
            json={"expected_revision": old_revision},
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert stale.status_code == 409
        update = {
            "display_name": "Renamed Employee Canary",
            "department": "Other Department Canary",
            "status": "disabled",
            "revision": employee["revision"],
        }
        disabled = await client.patch(
            f"/consumers/{employee['consumer_id']}",
            json=update,
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert disabled.status_code == 200, disabled.text
        assert (
            await client.patch(
                f"/consumers/{employee['consumer_id']}",
                json=update,
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 409
        for value in (second["plain_value"], replacement["plain_value"]):
            with pytest.raises(ApiError) as denied:
                authenticate_virtual_key(app.state.config, "Bearer " + value)
            assert denied.value.code == "gateway.auth_invalid"
        assert (
            await client.post(
                "/virtual-keys",
                json={"name": "Denied", "consumer_id": employee["consumer_id"]},
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 409
        # Reenabling the key alone does not bypass a disabled usage identity.
        assert (
            await client.patch(
                f"/virtual-keys/{key_id}",
                json={"status": "active"},
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 200
        with pytest.raises(ApiError):
            authenticate_virtual_key(app.state.config, "Bearer " + replacement["plain_value"])
        assert (
            await client.patch(
                f"/consumers/{employee['consumer_id']}",
                json={**update, "status": "active", "revision": 2},
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 200
        assert authenticate_virtual_key(app.state.config, "Bearer " + replacement["plain_value"])
        with connect_database(database) as connection:
            audit = str(
                [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM audit_logs WHERE tenant_id=?",
                        (authenticated.tenant_id,),
                    ).fetchall()
                ]
            )
            for sensitive in (
                "Employee Canary",
                "Private Department Canary",
                "Other Department Canary",
                first["plain_value"],
                second["plain_value"],
                replacement["plain_value"],
            ):
                assert sensitive not in audit
            for statement in (
                "UPDATE virtual_key_consumers SET consumer_id=consumer_id",
                "DELETE FROM virtual_key_consumers",
                "DELETE FROM workspace_consumers",
            ):
                with pytest.raises(sqlite3.Error):
                    connection.execute(statement)
                connection.rollback()


@pytest.mark.anyio
async def test_consumer_credential_boundaries_cross_tenant_and_strict_input(
    monkeypatch,
    tmp_path,
    migrated_database,
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        assert (await client.get("/consumers")).status_code == 401
        await _open_organization(client, monkeypatch)
        assert (
            await client.post("/consumers", json={"display_name": "No CSRF"})
        ).status_code == 403
        for extra in (
            {"password": "NotAllowed123!"},
            {"email": "not-an-account@example.test"},
            {"role": "admin"},
            {"tenant_id": "other"},
        ):
            assert (
                await client.post(
                    "/consumers",
                    json={"display_name": "No login", **extra},
                    headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
                )
            ).status_code == 422
        employee = await _consumer(client)
        await _open_organization(
            client, monkeypatch, slug="other-consumer-organization", signup=False
        )
        assert (await client.get("/consumers")).json()["total"] == 0
        assert (
            await client.post(
                "/virtual-keys",
                json={"name": "Cross tenant", "consumer_id": employee["consumer_id"]},
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 404
        assert (
            await client.patch(
                f"/consumers/{employee['consumer_id']}",
                json={"display_name": "Cross tenant", "status": "disabled", "revision": 1},
                headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            )
        ).status_code == 404
        second = await _consumer(client)
        key = await _key(client, second)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as anonymous:
        assert (
            await anonymous.get(
                "/consumers",
                headers={
                    "Authorization": "Bearer " + key["plain_value"],
                },
            )
        ).status_code == 401
    with connect_database(database) as connection:
        with pytest.raises(sqlite3.Error):
            connection.execute(
                "UPDATE workspace_consumers SET tenant_id='other',revision=revision+1"
            )
        connection.rollback()


@pytest.mark.anyio
async def test_consumer_audit_failure_rolls_back(monkeypatch, tmp_path, migrated_database):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)

        def reject_audit(*args, **kwargs):
            raise ApiError("infra.service_unavailable")

        monkeypatch.setattr(consumer_service, "record_audit_log", reject_audit)
        failed = await client.post(
            "/consumers",
            json={"display_name": "Rollback"},
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert failed.status_code == 503
        assert (await client.get("/consumers")).json()["total"] == 0


def test_consumer_pristine_rollback_and_manifest(monkeypatch, tmp_path, migrated_database):
    database, _ = _prepare(monkeypatch, tmp_path, migrated_database)
    later = rollback_later_migrations(database, "0148")
    with connect_database(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tenant_deletion_table_manifest "
                "WHERE source_table IN('workspace_consumers','virtual_key_consumers')"
            ).fetchone()[0]
            == 2
        )
    assert rollback_last_migration(database, expected_version="0148").rolled_back == "0148"
    assert apply_migrations(database).applied == ["0148", *later]


@pytest.mark.anyio
async def test_legacy_developer_cannot_manage_consumers_or_issue_employee_keys(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    with connect_database(database) as connection:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants WHERE slug='default'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,
               role,status,created_at,updated_at)
               VALUES('consumer-test-developer',?,'consumer-developer@example.test','Developer',?,'developer','active',datetime('now'),datetime('now'))""",
            (tenant_id, hash_password("ConsumerDeveloper!2026")),
        )
        connection.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:

        async def login(email, password):
            response = await client.post(
                "/auth/login", json={"tenant_slug": "default", "email": email, "password": password}
            )
            assert response.status_code == 200
            return {"Authorization": "Bearer " + response.json()["access_token"]}

        admin = await login("admin@metahub.local", ADMIN_PASSWORD)
        developer = await login("consumer-developer@example.test", "ConsumerDeveloper!2026")
        employee = await client.post(
            "/consumers", headers=admin, json={"display_name": "Admin-owned Consumer"}
        )
        assert employee.status_code == 201, employee.text
        consumer_id = employee.json()["consumer"]["consumer_id"]
        denied = await client.get("/consumers", headers=developer)
        assert denied.status_code == 403 and "no-store" in denied.headers["Cache-Control"]
        assert (
            await client.post("/consumers", headers=developer, json={"display_name": "Denied"})
        ).status_code == 403
        assert (
            await client.post(
                "/virtual-keys",
                headers=developer,
                json={"name": "Denied", "consumer_id": consumer_id},
            )
        ).status_code == 403
        technical = await client.post(
            "/virtual-keys", headers=developer, json={"name": "Developer Tool"}
        )
        assert technical.status_code == 201, technical.text
        assert "no-store" in technical.headers["Cache-Control"]
        key = technical.json()["virtual_key"]
        assert key["consumer_id"] is None
        results = await asyncio.gather(
            *[
                client.post(
                    f"/virtual-keys/{key['virtual_key_id']}/rotate",
                    headers=developer,
                    json={"expected_revision": 1},
                )
                for _ in range(2)
            ]
        )
        assert sorted(result.status_code for result in results) == [200, 409]
        assert (
            await client.patch(
                f"/virtual-keys/{key['virtual_key_id']}",
                headers=admin,
                json={"consumer_id": consumer_id},
            )
        ).status_code == 422


@pytest.mark.anyio
async def test_audit_rows_sequences_and_triggers_survive_migration_roundtrip(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        result = await client.post(
            "/virtual-keys",
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
            json={"name": "Preserved Technical Key"},
        )
        assert result.status_code == 201, result.text
    # The current WorkspaceSession/RBAC contract requires the full migration
    # chain. Generate immutable audit facts first, then exercise the 0148
    # down/up round-trip without operating on the older RBAC baseline.
    later = rollback_later_migrations(database, "0148")
    with connect_database(database) as connection:
        records = [
            tuple(row)
            for row in connection.execute("SELECT rowid,* FROM audit_logs ORDER BY rowid")
        ]
        sequences = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM audit_log_export_sequences ORDER BY audit_log_id"
            )
        ]
        triggers = [
            tuple(row)
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE tbl_name='audit_logs' "
                "AND type='trigger' ORDER BY name"
            )
        ]
    assert records and sequences
    assert rollback_last_migration(database, expected_version="0148").rolled_back == "0148"
    assert apply_migrations(database).applied == ["0148", *later]
    with connect_database(database) as connection:
        assert records == [
            tuple(row)
            for row in connection.execute("SELECT rowid,* FROM audit_logs ORDER BY rowid")
        ]
        assert sequences == [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM audit_log_export_sequences ORDER BY audit_log_id"
            )
        ]
        assert triggers == [
            tuple(row)
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE tbl_name='audit_logs' "
                "AND type='trigger' ORDER BY name"
            )
        ]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
