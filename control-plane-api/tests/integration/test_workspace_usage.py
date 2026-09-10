"""Workspace usage transport, immutable consumer attribution and export ownership."""

import csv
import io
from contextlib import nullcontext
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.usage import export_service
from metahub_control.usage import service as usage_service
from metahub_control.usage.routes import router as usage_router
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from test_prd095_platform_iam import WORKSPACE_CSRF_COOKIE, _csrf
from test_prd095_platform_iam import migrated_database as migrated_database
from test_workspace_consumers import _consumer, _key, _open_organization, _prepare


def _app(monkeypatch, tmp_path, migrated_database):
    database, app = _prepare(monkeypatch, tmp_path, migrated_database)
    app.include_router(usage_router)
    return database, app


def _usage(database, key, *, currency="CNY", estimated=False, connection=None):
    key_id = key["virtual_key"]["virtual_key_id"]
    request_id = "req_usage_" + uuid4().hex
    owns_connection = connection is None
    with (connect_database(database) if owns_connection else nullcontext(connection)) as connection:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM virtual_keys WHERE virtual_key_id=?", (key_id,)
        ).fetchone()[0]
        now = "2026-09-01T01:00:00Z"
        connection.execute(
            """INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id,
                virtual_key_id,snapshot_version,policy_snapshot_status,claimed_at)
                VALUES(?,?,?,?,NULL,'disabled',?)""",
            (tenant_id, request_id, request_id.replace("req_", "trc_"), key_id, now),
        )
        connection.execute(
            """INSERT INTO request_logs(request_id,tenant_id,trace_id,virtual_key_id,
                endpoint,request_model,resolved_model,status,created_at,updated_at)
                VALUES(?,?,?,?, '/v1/chat/completions','test-alias','test-model',
                'success',?,?)""",
            (request_id, tenant_id, request_id.replace("req_", "trc_"), key_id, now, now),
        )
        connection.execute(
            """INSERT INTO usage_ledger(usage_ledger_id,tenant_id,request_id,virtual_key_id,
                input_tokens,output_tokens,total_tokens,cost_amount,cost_cny,cost_usd,
                currency,estimated,usage_source,created_at,updated_at)
                VALUES(?,?,?,?,10,5,15,0.25,?,?,?,?,?,?,?)""",
            (
                "usg_" + uuid4().hex,
                tenant_id,
                request_id,
                key_id,
                "0.25" if currency == "CNY" else "0",
                "0.25" if currency == "USD" else "0",
                currency,
                int(estimated),
                "estimated" if estimated else "upstream",
                now,
                now,
            ),
        )
        if owns_connection:
            connection.commit()
    return tenant_id, request_id


@pytest.mark.anyio
async def test_workspace_usage_consumer_filter_rotation_and_export(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        assert (await client.get("/usage/summary")).status_code == 401
        await _open_organization(client, monkeypatch)
        employee = await _consumer(client)
        other = await _consumer(client)
        first, second = await _key(client, employee), await _key(client, employee)
        third = await _key(client, other)
        _usage(database, first)
        _usage(database, second, currency="USD", estimated=True)
        _usage(database, third)
        query = {"consumer_id": employee["consumer_id"]}
        summary = await client.get("/usage/summary", params=query)
        assert summary.status_code == 200, summary.text
        totals = summary.json()["totals"]
        assert totals["request_count"] == 2 and totals["total_tokens"] == 30
        assert totals["cost_cny"] == totals["cost_usd"] == 0.25
        assert totals["estimated_records"] == totals["real_usage_records"] == 1
        assert summary.json()["by_consumer"] == [
            {
                **totals,
                **query,
                "consumer_name": employee["display_name"],
                "consumer_department": employee["department"],
            }
        ]
        records = await client.get("/usage/records", params=query)
        assert records.status_code == 200, records.text
        assert records.json()["total"] == 2
        assert all(
            row["consumer_id"] == query["consumer_id"] for row in records.json()["usage_records"]
        )
        assert (await client.get("/usage/export.csv", params=query)).status_code == 403
        headers = {**_csrf(client, WORKSPACE_CSRF_COOKIE), "X-Request-ID": "usage-owner-export"}
        exported = await client.get("/usage/export.csv", params=query, headers=headers)
        assert exported.status_code == 200, exported.text
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert len(rows) == 2
        assert {r["virtual_key_id"] for r in rows} == {
            first["virtual_key"]["virtual_key_id"],
            second["virtual_key"]["virtual_key_id"],
        }
        assert "consumer_id" not in rows[0]  # Existing fixed CSV column contract.
        assert (
            await client.get("/usage/export.csv", params=query, headers=headers)
        ).content == exported.content
        browser_headers = {
            **{name: value for name, value in headers.items() if name.lower() != "origin"},
            "Referer": "http://localhost:5173/console.html",
            "Sec-Fetch-Site": "same-origin",
        }
        browser_export = await client.get(
            "/usage/export.csv", params=query, headers=browser_headers,
        )
        assert browser_export.status_code == 200
        assert browser_export.content == exported.content
        denied_export = await client.get(
            "/usage/export.csv", params=query,
            headers={**browser_headers, "Origin": "https://untrusted.example"},
        )
        assert denied_export.status_code == 403
        rotated = await client.post(
            f"/virtual-keys/{first['virtual_key']['virtual_key_id']}/rotate",
            json={"expected_revision": 1},
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert rotated.status_code == 200, rotated.text
        disabled = await client.patch(
            f"/consumers/{employee['consumer_id']}",
            json={
                "display_name": "Renamed",
                "department": "Changed",
                "status": "disabled",
                "revision": 1,
            },
            headers=_csrf(client, WORKSPACE_CSRF_COOKIE),
        )
        assert disabled.status_code == 200, disabled.text
        assert (await client.get("/usage/summary", params=query)).json()["totals"] == totals
        await _open_organization(client, monkeypatch, slug="usage-other", signup=False)
        empty = await client.get("/usage/records", params=query)
        assert empty.status_code == 200 and empty.json()["total"] == 0


@pytest.mark.anyio
async def test_workspace_async_usage_export_csrf_owner_and_expiry(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    app.state.config = app.state.config.model_copy(
        update={
            "usage_export_sync_row_limit": 1,
            "usage_export_enabled": True,
        }
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        employee = await _consumer(client)
        key = await _key(client, employee)
        tenant_id, _ = _usage(database, key)
        _usage(database, key)
        headers = {**_csrf(client, WORKSPACE_CSRF_COOKIE), "X-Request-ID": "async-owner-export"}
        created = await client.get("/usage/export.csv", headers=headers)
        assert created.status_code == 202, created.text
        job = created.json()["export_job"]["usage_export_job_id"]
        route = f"/usage/exports/{job}"
        assert (await client.get(route)).json()["export_job"]["status"] == "pending"
        claim = export_service.claim_next_usage_export(app.state.config)
        assert claim and claim.usage_export_job_id == job
        assert export_service.process_claimed_usage_export(app.state.config, claim)
        assert (await client.get(route + "/download")).status_code == 403
        downloaded = await client.get(route + "/download", headers=headers)
        assert downloaded.status_code == 200, downloaded.text
        with connect_database(database) as connection:
            slug = connection.execute(
                "SELECT slug FROM tenants WHERE tenant_id=?", (tenant_id,)
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,
                    role,status,created_at,updated_at) VALUES('usage-other-admin',?,
                    'usage-admin@example.test','Other Admin',?,'admin','active',
                    '2026-09-01T01:00:00Z','2026-09-01T01:00:00Z')""",
                (tenant_id, hash_password("UsageOtherAdmin!2026")),
            )
            connection.execute(
                """INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,
                    role,status,created_at,updated_at) VALUES('usage-finance',?,
                    'usage-finance@example.test','Finance',?,'finance_admin','active',
                    '2026-09-01T01:00:00Z','2026-09-01T01:00:00Z')""",
                (tenant_id, hash_password("UsageFinance!2026")),
            )
            connection.commit()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as other_client:
            login = await other_client.post(
                "/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "usage-admin@example.test",
                    "password": "UsageOtherAdmin!2026",
                },
            )
            assert login.status_code == 200, login.text
            other_headers = {
                "Authorization": "Bearer " + login.json()["access_token"],
                "X-Request-ID": "async-owner-export",
            }
            assert (
                await other_client.get("/usage/summary", headers=other_headers)
            ).status_code == 200
            for path in (route, route + "/download", "/usage/export.csv"):
                denied = await other_client.get(path, headers=other_headers)
                assert denied.status_code == 404, denied.text
            finance_login = await other_client.post(
                "/auth/login",
                json={
                    "tenant_slug": slug,
                    "email": "usage-finance@example.test",
                    "password": "UsageFinance!2026",
                },
            )
            assert finance_login.status_code == 200, finance_login.text
            finance_summary = await other_client.get(
                "/usage/summary",
                headers={
                    "Authorization": "Bearer " + finance_login.json()["access_token"],
                },
            )
            assert finance_summary.status_code == 200, finance_summary.text
            assert finance_summary.json()["by_consumer"][0]["consumer_name"] is None
            assert employee["display_name"] not in finance_summary.text
        with connect_database(database) as connection:
            connection.execute(
                "UPDATE usage_export_jobs SET expires_at='2020-01-01T00:00:00Z' "
                "WHERE tenant_id=? AND usage_export_job_id=?",
                (tenant_id, job),
            )
            connection.commit()
        assert (await client.get(route)).json()["export_job"]["status"] == "expired"
        assert (await client.get(route + "/download", headers=headers)).status_code == 410
        assert (await client.get("/usage/export.csv", headers=headers)).status_code == 202
        with connect_database(database) as connection:
            assert (
                connection.execute(
                    "SELECT download_count FROM usage_export_jobs WHERE usage_export_job_id=?",
                    (job,),
                ).fetchone()[0]
                == 1
            )


@pytest.mark.anyio
async def test_workspace_usage_export_audit_failure_is_atomic(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)

        def fail_audit(*args, **kwargs):
            raise ApiError("infra.service_unavailable")

        monkeypatch.setattr(export_service, "record_audit_log", fail_audit)
        failed = await client.get("/usage/export.csv", headers=_csrf(client, WORKSPACE_CSRF_COOKIE))
        assert failed.status_code == 503, failed.text
        with connect_database(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM usage_export_jobs").fetchone()[0] == 0


@pytest.mark.anyio
async def test_usage_export_reauthenticates_after_generation_and_rejects_mixed_credentials(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        mixed = await client.get(
            "/usage/summary", headers={"Authorization": "Bearer invalid-test-token"}
        )
        assert (
            mixed.status_code == 400
            and mixed.json()["code"] == "workspace_session.credential_conflict"
        )
        original = export_service._generate_csv

        def revoke_after_generation(*args, **kwargs):
            generated = original(*args, **kwargs)
            # A separate lease models another request, not the export's own transaction.
            with (
                database_request_scope(reuse_connections=True),
                connect_database(database) as connection,
            ):
                connection.execute(
                    """UPDATE workspace_sessions SET status='revoked',
                        revoked_at='2026-09-09T00:00:00Z',revoked_reason='workspace_switched',
                        updated_at='2026-09-09T00:00:00Z' WHERE status='active'"""
                )
                connection.commit()
            return generated

        monkeypatch.setattr(export_service, "_generate_csv", revoke_after_generation)
        rejected = await client.get(
            "/usage/export.csv", headers=_csrf(client, WORKSPACE_CSRF_COOKIE)
        )
        assert rejected.status_code == 401, rejected.text
        with connect_database(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM usage_export_jobs").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE action='usage_export.requested'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.anyio
async def test_usage_summary_reauthenticates_after_read_snapshot(
    monkeypatch, tmp_path, migrated_database
):
    database, app = _app(monkeypatch, tmp_path, migrated_database)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        await _open_organization(client, monkeypatch)
        original = usage_service._load_by_application

        def revoke_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            with (
                database_request_scope(reuse_connections=True),
                connect_database(database) as connection,
            ):
                connection.execute(
                    """UPDATE workspace_sessions SET status='revoked',
                        revoked_at='2026-09-09T00:00:00Z',revoked_reason='workspace_switched',
                        updated_at='2026-09-09T00:00:00Z' WHERE status='active'"""
                )
                connection.commit()
            return result

        monkeypatch.setattr(usage_service, "_load_by_application", revoke_after_read)
        rejected = await client.get("/usage/summary")
        assert rejected.status_code == 401, rejected.text
        assert "totals" not in rejected.json()
