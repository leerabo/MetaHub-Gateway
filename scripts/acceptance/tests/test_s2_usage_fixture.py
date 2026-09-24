"""Test-only seed isolation and atomicity without rebuilding the migration baseline."""

import asyncio
import importlib
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "fixture-only-credential-" + "a" * 32
WORKSPACE = "wsp_" + "a" * 32


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fixture_module(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/acceptance"))
    return importlib.import_module("s2_usage_fixture")


@pytest.fixture
def fixture_database(fixture_module):
    with tempfile.TemporaryDirectory(
        prefix="s1-browser-fixture-test-", dir=ROOT / ".tmp"
    ) as folder:
        database = Path(folder) / "acceptance.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            # Minimal columns from migrations 0085/0148 and the existing usage helper.
            connection.executescript("""
                CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY, kind TEXT,
                    creation_source TEXT);
                CREATE TABLE organization_profiles(workspace_id TEXT, slug_normalized TEXT);
                CREATE TABLE workspace_tenant_bindings(workspace_id TEXT, tenant_id TEXT);
                CREATE TABLE virtual_keys(virtual_key_id TEXT PRIMARY KEY,
                    tenant_id TEXT, name TEXT);
                CREATE TABLE virtual_key_consumers(virtual_key_id TEXT, tenant_id TEXT,
                    consumer_id TEXT);
                CREATE TABLE gateway_request_claims(tenant_id TEXT, request_id TEXT, trace_id TEXT,
                    virtual_key_id TEXT, snapshot_version TEXT, policy_snapshot_status TEXT,
                    claimed_at TEXT);
                CREATE TABLE request_logs(request_id TEXT, tenant_id TEXT, trace_id TEXT,
                    virtual_key_id TEXT, endpoint TEXT, request_model TEXT, resolved_model TEXT,
                    status TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE usage_ledger(usage_ledger_id TEXT, tenant_id TEXT, request_id TEXT,
                    virtual_key_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
                    total_tokens INTEGER, cost_amount REAL, cost_cny TEXT, cost_usd TEXT,
                    currency TEXT, estimated INTEGER, usage_source TEXT, created_at TEXT,
                    updated_at TEXT);
            """)
            connection.execute(
                "INSERT INTO workspaces VALUES(?,'organization','user_created')", (WORKSPACE,)
            )
            connection.execute(
                "INSERT INTO organization_profiles VALUES(?,'org-current-organization')", (WORKSPACE,)
            )
            connection.execute(
                "INSERT INTO workspace_tenant_bindings VALUES(?,'test-tenant')", (WORKSPACE,)
            )
            for number in (1, 2):
                key = "key-" + str(number)
                connection.execute("INSERT INTO virtual_keys VALUES(?,'test-tenant',?)", (key, key))
                connection.execute(
                    "INSERT INTO virtual_key_consumers VALUES(?,'test-tenant',?)",
                    (key, "consumer-" + str(number)),
                )
            connection.commit()
        yield database


def counts(database):
    with closing(sqlite3.connect(database)) as connection:
        return [
            connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("gateway_request_claims", "request_logs", "usage_ledger")
        ]


def app_for(module, database):
    return Starlette(
        routes=[
            Route(
                "/__acceptance/usage",
                module.create_usage_fixture(database, ROOT, TOKEN),
                methods=["POST"],
            )
        ]
    )


def client_for(app, host="127.0.0.1", *, raise_errors=True):
    return AsyncClient(
        transport=ASGITransport(app=app, client=(host, 1234), raise_app_exceptions=raise_errors),
        base_url="http://testserver",
    )


@pytest.mark.anyio
async def test_seed_is_atomic_and_concurrently_idempotent(fixture_module, fixture_database):
    async with client_for(app_for(fixture_module, fixture_database)) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/__acceptance/usage",
                    headers={"X-Acceptance-Fixture": TOKEN},
                    json={"workspace_id": WORKSPACE},
                )
                for _ in range(4)
            ]
        )
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert responses[0].headers["cache-control"] == "no-store"
    assert responses[0].json()["source"] == "isolated_usage_fixture"
    assert responses[0].json()["consumer_id"] == "consumer-1"
    assert len(responses[0].json()["requests"]) == 2
    assert counts(fixture_database) == [3, 3, 3]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("host", "token"), [("127.0.0.1", ""), ("127.0.0.1", "wrong"), ("192.0.2.10", TOKEN)]
)
async def test_fixture_requires_loopback_and_credential(
    fixture_module, fixture_database, host, token
):
    async with client_for(app_for(fixture_module, fixture_database), host) as client:
        response = await client.post(
            "/__acceptance/usage",
            headers={"X-Acceptance-Fixture": token},
            json={"workspace_id": WORKSPACE},
        )
    assert response.status_code == 403
    assert counts(fixture_database) == [0, 0, 0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"workspace_id": WORKSPACE, "tenant_id": "other"},
        {"workspace_id": 123},
        {"workspace_id": "x" * 36},
    ],
)
async def test_fixture_rejects_unexpected_body(fixture_module, fixture_database, body):
    async with client_for(app_for(fixture_module, fixture_database)) as client:
        response = await client.post(
            "/__acceptance/usage", headers={"X-Acceptance-Fixture": TOKEN}, json=body
        )
    assert response.status_code == 400
    assert counts(fixture_database) == [0, 0, 0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE workspaces SET kind='personal'",
        "UPDATE workspaces SET creation_source='legacy_backfill'",
        "DELETE FROM virtual_key_consumers WHERE virtual_key_id='key-2'",
        "UPDATE virtual_key_consumers SET consumer_id='same-person'",
    ],
)
async def test_fixture_requires_new_organization_and_distinct_consumers(
    fixture_module, fixture_database, mutation
):
    with closing(sqlite3.connect(fixture_database)) as connection:
        connection.execute(mutation)
        connection.commit()
    async with client_for(app_for(fixture_module, fixture_database)) as client:
        response = await client.post(
            "/__acceptance/usage",
            headers={"X-Acceptance-Fixture": TOKEN},
            json={"workspace_id": WORKSPACE},
        )
    assert response.status_code == 400
    assert counts(fixture_database) == [0, 0, 0]


@pytest.mark.anyio
async def test_partial_seed_failure_rolls_back_and_can_retry(fixture_module, fixture_database):
    with closing(sqlite3.connect(fixture_database)) as connection:
        connection.executescript("""CREATE TRIGGER fail_second BEFORE INSERT ON usage_ledger
            WHEN NEW.currency='USD' BEGIN SELECT RAISE(ABORT,'test fixture rollback'); END;""")
    async with client_for(app_for(fixture_module, fixture_database), raise_errors=False) as client:
        response = await client.post(
            "/__acceptance/usage",
            headers={"X-Acceptance-Fixture": TOKEN},
            json={"workspace_id": WORKSPACE},
        )
        assert response.status_code == 500
        assert counts(fixture_database) == [0, 0, 0]
        with closing(sqlite3.connect(fixture_database)) as connection:
            connection.execute("DROP TRIGGER fail_second")
            connection.commit()
        response = await client.post(
            "/__acceptance/usage",
            headers={"X-Acceptance-Fixture": TOKEN},
            json={"workspace_id": WORKSPACE},
        )
        assert response.status_code == 200
    assert counts(fixture_database) == [3, 3, 3]


def test_fixture_rejects_wrong_database_and_short_credential(fixture_module, fixture_database):
    with pytest.raises(ValueError, match="disposable"):
        fixture_module.create_usage_fixture(ROOT / "pyproject.toml", ROOT, TOKEN)
    with pytest.raises(ValueError, match="credential"):
        fixture_module.create_usage_fixture(fixture_database, ROOT, "short")
