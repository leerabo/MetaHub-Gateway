"""PRD-089 tenant export/deletion API and worker integration tests."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.tenant_data import router as tenant_data_router
from metahub_control.tenant_data.service import run_tenant_data_once
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.runtime.fastapi_app import create_service_app


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    database_url = _url(tmp_path / "prd089.sqlite3")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_MIGRATIONS_DIR", str(Path("infra/migrations").resolve()))
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "LocalAdmin!2026")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd089-test-auth-secret")
    monkeypatch.setenv(
        "METAHUB_TENANT_DATA_ENCRYPTION_SECRET",
        "prd089-independent-tenant-data-encryption-secret-2026",
    )
    apply_migrations(database_url, migrations_dir=Path("infra/migrations"))
    seed = apply_seed_data(database_url)
    app = create_service_app("control-plane-api", "PRD-089 Test API")
    app.state.config.tenant_data_scheduler_enabled = False
    app.include_router(auth_router)
    app.include_router(tenant_data_router)
    return app, database_url, seed


async def _login(client: AsyncClient) -> str:
    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": "default",
            "email": "admin@metahub.local",
            "password": "LocalAdmin!2026",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str, request_id: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace_{request_id}",
        **extra,
    }


@pytest.mark.anyio
async def test_export_is_tenant_scoped_encrypted_and_downloadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app, database_url, seed = _prepare(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        response = await client.post(
            "/tenant/export",
            json={},
            headers=_headers(token, "req_prd089_create", **{"Idempotency-Key": "export-1"}),
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["export_job"]["tenant_export_job_id"]
        assert response.headers["cache-control"] == "no-store, private"

        replay = await client.post(
            "/tenant/export",
            json={},
            headers=_headers(token, "req_prd089_replay", **{"Idempotency-Key": "export-1"}),
        )
        assert replay.status_code == 202
        assert replay.json()["export_job"]["tenant_export_job_id"] == job_id
        assert run_tenant_data_once(app.state.config) == 1

        status = await client.get(
            f"/tenant/exports/{job_id}", headers=_headers(token, "req_prd089_status")
        )
        assert status.status_code == 200, status.text
        assert status.json()["export_job"]["status"] == "completed"
        download = await client.get(
            f"/tenant/exports/{job_id}/download",
            headers=_headers(token, "req_prd089_download"),
        )
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        assert download.headers["x-content-type-options"] == "nosniff"
        assert len(download.headers["x-content-sha256"]) == 64
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert set(archive.namelist()) == {
                "manifest.json",
                "tenant.json",
                "usage/summary.json",
                "audit/summary.json",
                "failures/summary.json",
            }
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["schema_version"] == "metahub.tenant-export/v1"
        assert b"replace-with-local-provider-key" not in download.content
        assert b"MetaHubLocal!2026" not in download.content

    connection = connect_database(database_url)
    try:
        row = connection.execute(
            "SELECT content_ciphertext,content_sha256,download_count FROM tenant_export_jobs "
            "WHERE tenant_id=? AND tenant_export_job_id=?",
            (seed.tenant_id, job_id),
        ).fetchone()
        assert row is not None
        assert not bytes(str(row["content_ciphertext"]), "utf-8").startswith(b"PK")
        assert len(row["content_sha256"]) == 64
        assert row["download_count"] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_deletion_requires_exact_confirmation_and_erases_tenant_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app, database_url, seed = _prepare(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _login(client)
        mismatch = await client.post(
            "/tenant/deletion-request",
            json={"confirmation": "wrong", "tenant_revision": 1},
            headers=_headers(token, "req_prd089_wrong", **{"Idempotency-Key": "delete-wrong"}),
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["code"] == "tenant_data.confirmation_mismatch"

        response = await client.post(
            "/tenant/deletion-request",
            json={"confirmation": "default", "tenant_revision": 1},
            headers=_headers(token, "req_prd089_delete", **{"Idempotency-Key": "delete-1"}),
        )
        assert response.status_code == 202, response.text
        assert response.json()["deletion_request"]["status"] == "requested"

    assert run_tenant_data_once(app.state.config) == 1
    assert run_tenant_data_once(app.state.config) == 1
    connection = connect_database(database_url)
    try:
        request = connection.execute(
            "SELECT status,archived_rows,deleted_rows,confirmation_hash "
            "FROM deletion_requests WHERE tenant_id=?",
            (seed.tenant_id,),
        ).fetchone()
        assert request["status"] == "completed"
        assert request["archived_rows"] >= 1
        assert request["deleted_rows"] > 0
        assert request["confirmation_hash"] != "default"
        state = connection.execute(
            "SELECT lifecycle_status,revision,cache_generation FROM tenant_data_states "
            "WHERE tenant_id=?",
            (seed.tenant_id,),
        ).fetchone()
        assert tuple(state) == ("deleted", 3, 1)
        assert (
            connection.execute(
                "SELECT status FROM tenants WHERE tenant_id=?", (seed.tenant_id,)
            ).fetchone()[0]
            == "disabled"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()
