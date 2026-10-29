"""Product-level backend QA checks for tenancy, authorization, and privacy.

The suite deliberately boots a minimal real Control API with a fresh SQLite
database for every test. It is safe to run alongside a local development
server because it never reads or writes the configured development database.
"""

from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.tenants.routes import router as tenant_router
from metahub_control.usage.export_service import _safe_text_cell
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.fastapi_app import create_service_app


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _build_app():
    app = create_service_app("control-plane-api", "Product QA Backend API")
    app.include_router(auth_router)
    app.include_router(tenant_router)
    return app


@pytest.fixture(scope="module")
def qa_database(tmp_path_factory: pytest.TempPathFactory):
    """Create the isolated full-migration product QA database once per run."""

    database_url = _sqlite_url(tmp_path_factory.mktemp("product-qa") / "product-qa.db")
    names = (
        "METAHUB_DATABASE_URL",
        "METAHUB_SEED_ADMIN_PASSWORD",
        "METAHUB_AUTH_TOKEN_SECRET",
    )
    previous = {name: os.environ.get(name) for name in names}
    password = secrets.token_urlsafe(32)
    os.environ["METAHUB_DATABASE_URL"] = database_url
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = password
    os.environ["METAHUB_AUTH_TOKEN_SECRET"] = secrets.token_urlsafe(48)
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    yield database_url, seed.tenant_id, password
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


async def _login(client: AsyncClient, password: str) -> str:
    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "default",
            "email": "admin@metahub.local",
            "password": password,
        },
        headers={"X-Request-ID": "qa-login-request"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == "qa-login-request"
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_api_unified_error_contract_preserves_client_request_id(
    qa_database: tuple[str, str, str],
) -> None:
    """API-01/SEC-01: public error paths always expose code, request, and trace IDs."""

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        missing_auth = await client.get("/tenant/current", headers={"X-Request-ID": "qa-missing"})
        invalid_payload = await client.patch(
            "/tenant/current",
            json={"name": 99},
            headers={"X-Request-ID": "qa-validation"},
        )
        absent_resource = await client.get(
            "/not-a-product-endpoint", headers={"X-Request-ID": "qa-404"}
        )

    for response, expected_code, expected_request_id in [
        (missing_auth, "auth.token_missing", "qa-missing"),
        (invalid_payload, "auth.token_missing", "qa-validation"),
        (absent_resource, "infra.not_found", "qa-404"),
    ]:
        payload = response.json()
        assert payload["code"] == expected_code
        assert payload["request_id"] == expected_request_id
        assert payload["trace_id"]
        assert "detail" not in payload


@pytest.mark.anyio
async def test_black_box_tenant_update_uses_authenticated_tenant_not_request_payload(
    qa_database: tuple[str, str, str],
) -> None:
    """BB-03/SEC-01: an authenticated admin cannot target another tenant by payload."""

    database_url, tenant_id, password = qa_database
    connection = connect_database(database_url)
    connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES('product-qa-other', 'product-qa-other', 'Other tenant', 'internal_only',
               'active', datetime('now'), datetime('now'))
        """
    )
    connection.commit()
    connection.close()

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = await _login(client, password)
        response = await client.patch(
            "/tenant/current",
            json={"tenant_id": "product-qa-other", "name": "Unauthorized target"},
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": "qa-cross-tenant"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == "infra.validation_failed"
    assert response.json()["request_id"] == "qa-cross-tenant"
    connection = connect_database(database_url)
    current = connection.execute(
        "SELECT name FROM tenants WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    other = connection.execute(
        "SELECT name FROM tenants WHERE tenant_id='product-qa-other'"
    ).fetchone()
    connection.close()
    assert current["name"] != "Unauthorized target"
    assert other["name"] == "Other tenant"


@pytest.mark.anyio
async def test_rbac_drift_fails_closed_before_session_issuance(
    qa_database: tuple[str, str, str],
) -> None:
    """BB-03/GR-03: partial legacy role drift cannot mint an inconsistent session."""

    database_url, _, password = qa_database
    connection = connect_database(database_url)
    connection.execute("UPDATE users SET role='viewer' WHERE email='admin@metahub.local'")
    connection.commit()
    connection.close()

    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/auth/login",
            json={
                "tenant_slug": "default",
                "email": "admin@metahub.local",
                "password": password,
            },
            headers={"X-Request-ID": "qa-rbac-drift"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "rbac.unavailable"
    assert response.json()["request_id"] == "qa-rbac-drift"
    assert response.json()["trace_id"]


@pytest.mark.anyio
async def test_grey_box_login_failure_does_not_echo_sensitive_input(
    qa_database: tuple[str, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """GR-08/SEC-06: failed authentication keeps private input out of response and logs."""

    private_email = "product-qa-private@example.invalid"
    private_password = secrets.token_urlsafe(32)
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/auth/login",
            json={"tenant_slug": "default", "email": private_email, "password": private_password},
            headers={"X-Request-ID": "qa-private-login"},
        )

    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == "auth.login_invalid"
    rendered_evidence = "\n".join(
        [response.text, *(repr(record.__dict__) for record in caplog.records)]
    )
    assert private_email not in rendered_evidence
    assert private_password not in rendered_evidence


def test_white_box_all_core_business_tables_carry_tenant_id(tmp_path: Path) -> None:
    """WB-02: the tenant boundary is explicit for representative business tables."""

    database_url = _sqlite_url(tmp_path / "tenant-schema.db")
    apply_migrations(database_url)
    connection = connect_database(database_url)
    tables = {
        "tenants",
        "roles",
        "users",
        "sessions",
        "providers",
        "provider_credentials",
        "models",
        "virtual_keys",
        "request_logs",
        "usage_ledger",
    }
    for table in tables:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        assert "tenant_id" in columns, table
    connection.close()


def test_white_box_sqlite_request_scope_restores_connection_local_safety_state(
    tmp_path: Path,
) -> None:
    """WB-06: one request reuses a handle and the next request gets clean state."""

    database_url = _sqlite_url(tmp_path / "request-scope-state.db")
    with database_request_scope():
        authentication = connect_database(database_url)
        authentication.execute("CREATE TABLE writable_probe(value TEXT NOT NULL)")
        authentication.commit()
        authentication.close()

        business = connect_database(database_url)
        assert business is authentication
        business.execute("PRAGMA query_only=ON")

    with pytest.raises(sqlite3.ProgrammingError):
        business.execute("SELECT 1")

    with database_request_scope():
        next_request = connect_database(database_url)
        assert next_request.execute("PRAGMA query_only").fetchone()[0] == 0
        assert next_request.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        next_request.execute("INSERT INTO writable_probe(value) VALUES ('ok')")
        next_request.commit()
        assert next_request.execute("SELECT value FROM writable_probe").fetchone()[0] == "ok"


def test_white_box_production_csv_cells_are_excel_safe_and_utf8() -> None:
    """WB-08/SEC-07/COMP-07: production CSV cells are safe for Excel import."""

    raw_cells = ["中文企业", "=2+2", "+SUM(A1)", "-1+2", "@cmd", "  =hidden"]
    safe_cells = [_safe_text_cell(value) for value in raw_cells]
    assert safe_cells[0] == "中文企业"
    assert all(value.startswith("'") for value in safe_cells[1:])
    assert all(not value.lstrip().startswith(("=", "+", "-", "@")) for value in safe_cells)

    binary = io.BytesIO()
    binary.write(b"\xef\xbb\xbf")
    text = io.TextIOWrapper(binary, encoding="utf-8", newline="", write_through=True)
    writer = csv.writer(text, lineterminator="\r\n")
    writer.writerow(
        ["tenant_label", "formula_a", "formula_b", "formula_c", "formula_d", "formula_e"]
    )
    writer.writerow(safe_cells)
    text.flush()
    content = binary.getvalue()
    assert content.startswith(b"\xef\xbb\xbf")
    assert content.endswith(b"\r\n")
    decoded = content.decode("utf-8-sig")
    assert "中文企业" in decoded
    for line in decoded.split("\r\n"):
        for cell in next(csv.reader([line]), []):
            assert not cell.lstrip().startswith(("=", "+", "-", "@"))
