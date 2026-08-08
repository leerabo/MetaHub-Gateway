"""PRD-074 public Batch API authorization, tenancy, idempotency, and lifecycle coverage.

The tests use real migrations, Session authentication, PRD-072 upload, PRD-073 admission, and
the ASGI middleware. Test-only secrets and file text stay inside pytest's workspace temp tree;
responses and assertions use only public identifiers and fixed error codes.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import metahub_control.file_security.service as file_security_service
import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.batch_jobs.routes import router as batch_router
from metahub_control.file_security.routes import router as file_security_router
from metahub_control.files.routes import router as files_router
from metahub_gateway.api.internal_batch import _validated_route_plan
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ADMIN_PASSWORD = "Prd074AdminTestOnly!2026"
VIEWER_PASSWORD = "Prd074ViewerTestOnly!2026"
OTHER_PASSWORD = "Prd074OtherTestOnly!2026"
AUTH_SECRET = "prd074-auth-api-domain-secret-0123456789abcdef"
FILE_SECRET = "prd074-file-api-domain-secret-0123456789abcdef"
BATCH_SECRET = "prd074-batch-api-domain-secret-0123456789abcdef"
PROVIDER_SECRET = "prd074-provider-api-domain-secret-0123456789abcdef"
INTERNAL_BATCH_TOKEN = "prd074-internal-batch-api-token-0123456789abcdef"
OTHER_TENANT_ID = "tenant_prd074_api_other"
OTHER_TENANT_SLUG = "prd074-api-other"
PROJECT_ID = "prj_prd074_api"
MODEL_ALIAS_ID = "mal_prd074_api"
VIRTUAL_KEY_ID = "vkey_prd074_api"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def prd074_api_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one closed revision-10 template with two tenants and a Batch binding."""

    root = tmp_path_factory.mktemp("prd074-api-template")
    database = root / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        assert apply_migrations(database_url).applied[-1] == "0067"
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous

    connection = connect_database(database_url)
    now = "2026-08-08T08:00:00Z"
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd074_api_viewer',?,'viewer@prd074.local','PRD074 Viewer',?,
                 'viewer','active',?,?)
        """,
        (seed.tenant_id, hash_password(VIEWER_PASSWORD), now, now),
    )
    # The tenant trigger materializes revision 10 permissions and canonical role matrices.
    connection.execute(
        """
        INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
        VALUES(?,?,'PRD074 Other','internal_only','active',?,?)
        """,
        (OTHER_TENANT_ID, OTHER_TENANT_SLUG, now, now),
    )
    connection.execute(
        """
        INSERT INTO users(
          user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
        ) VALUES('user_prd074_api_other',?,'admin@prd074-other.local','Other Admin',?,
                 'admin','active',?,?)
        """,
        (OTHER_TENANT_ID, hash_password(OTHER_PASSWORD), now, now),
    )
    identity = connection.execute(
        """
        SELECT environment.environment_id,environment.code,model.model_id
        FROM environments environment
        JOIN models model ON model.tenant_id=environment.tenant_id
        WHERE environment.tenant_id=? AND environment.code='dev'
          AND environment.status='active' AND model.status='active'
        ORDER BY model.model_id LIMIT 1
        """,
        (seed.tenant_id,),
    ).fetchone()
    assert identity is not None
    environment_id, environment_code, model_id = map(str, identity)
    admin_id = str(
        connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? AND role='admin'",
            (seed.tenant_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        UPDATE models
        SET input_price_per_1k=0.01,output_price_per_1k=0.02,
            price_schema_version='prd074-api-v1',updated_at=?
        WHERE tenant_id=? AND model_id=?
        """,
        (now, seed.tenant_id, model_id),
    )
    connection.execute(
        """
        INSERT INTO projects(
          project_id,tenant_id,project_code,name,description,owner_user_id,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,'PRD074 API project',NULL,?,'active',?,?,?,?)
        """,
        (
            PROJECT_ID,
            seed.tenant_id,
            "prd074-api",
            admin_id,
            admin_id,
            admin_id,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id,tenant_id,alias,environment,default_model_id,fallback_model_id,
          status,description,created_by,disabled_at,disabled_by,created_at,updated_at,
          environment_id
        ) VALUES(?,?,?,?,?,NULL,'active',NULL,?,NULL,NULL,?,?,?)
        """,
        (
            MODEL_ALIAS_ID,
            seed.tenant_id,
            "prd074-api-alias",
            environment_code,
            model_id,
            admin_id,
            now,
            now,
            environment_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id,tenant_id,name,key_prefix,key_hash,status,created_at,updated_at,
          environment,description,last_used_at,created_by,disabled_at,disabled_by,
          project_id,application_id,environment_id
        ) VALUES(?,?,?,'mhv_prd074',?,'active',?,?,?,NULL,NULL,?,NULL,NULL,?,NULL,?)
        """,
        (
            VIRTUAL_KEY_ID,
            seed.tenant_id,
            "PRD074 API key",
            "7" * 64,
            now,
            now,
            environment_code,
            admin_id,
            PROJECT_ID,
            environment_id,
        ),
    )
    connection.commit()
    connection.close()
    return database


def _clone_environment(
    template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> str:
    database = tmp_path / "prd074-api.sqlite3"
    storage = tmp_path / "file-storage"
    shutil.copy2(template, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_FILE_ENCRYPTION_SECRET", FILE_SECRET)
    monkeypatch.setenv("METAHUB_BATCH_ENCRYPTION_SECRET", BATCH_SECRET)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", PROVIDER_SECRET)
    monkeypatch.setenv("METAHUB_INTERNAL_BATCH_TOKEN", INTERNAL_BATCH_TOKEN)
    monkeypatch.setenv("METAHUB_FILE_STORAGE_ROOT", str(storage))
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_BATCH_WORKER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    return database_url


def _build_app():
    app = create_service_app("control-plane-api", "PRD-074 Batch API Test")
    app.include_router(auth_router)
    app.include_router(files_router)
    app.include_router(file_security_router)
    app.include_router(batch_router)
    return app


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
    tenant_slug: str = "default",
) -> str:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _headers(token: str, request_id: str, *, idempotency_key: str | None = None):
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": request_id}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _ready_safe_file(client: AsyncClient, token: str, marker: str) -> str:
    upload = await client.post(
        "/files",
        files={
            "file": (
                "batch-input.txt",
                f"PRD074 safe operational report {marker}".encode(),
                "text/plain",
            )
        },
        headers=_headers(token, f"prd074-upload-{marker}"),
    )
    assert upload.status_code == 201, upload.text
    file_id = str(upload.json()["file"]["file_id"])
    scan = await client.post(
        f"/files/{file_id}/security-scan",
        headers=_headers(token, f"prd074-scan-{marker}"),
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["report"]["model_access_status"] == "allowed"
    return file_id


def _create_payload(file_id: str, *, name: str = "PRD074 API lifecycle") -> dict[str, object]:
    return {
        "name": name,
        "project_id": PROJECT_ID,
        "environment_id": "env_development",
        "model_alias_id": MODEL_ALIAS_ID,
        "operation": "summarize",
        "file_ids": [file_id],
        "priority": "normal",
        "max_duration_seconds": 300,
        "max_attempts": 2,
        "max_output_tokens": 128,
        "output_language": "zh-CN",
    }


def _actual_environment_id(database_url: str) -> str:
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT environment.environment_id
            FROM environments environment
            JOIN tenants tenant ON tenant.tenant_id=environment.tenant_id
            WHERE tenant.slug='default' AND environment.code='dev'
              AND environment.status='active'
            ORDER BY environment.environment_id LIMIT 1
            """
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


@pytest.mark.anyio
async def test_batch_api_create_idempotency_list_tenant_and_cancel(
    prd074_api_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the public lifecycle and prove tenant-qualified event/outbox persistence."""

    database_url = _clone_environment(prd074_api_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin_token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        file_id = await _ready_safe_file(client, admin_token, "lifecycle")
        payload = _create_payload(file_id)
        payload["environment_id"] = _actual_environment_id(database_url)
        idem = "74000000-0000-4000-8000-000000000001"
        created = await client.post(
            "/batch-jobs",
            json=payload,
            headers=_headers(admin_token, "prd074-create", idempotency_key=idem),
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store, private"
        body = created.json()
        assert body["request_id"] == "prd074-create"
        assert body["job"]["status"] == "queued"
        assert body["job"]["progress"] == {
            "total": 1,
            "queued": 1,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "expired": 0,
            "outcome_unknown": 0,
        }
        job_id = str(body["job"]["batch_job_id"])

        repeated = await client.post(
            "/batch-jobs",
            json=payload,
            headers=_headers(admin_token, "prd074-repeat", idempotency_key=idem),
        )
        assert repeated.status_code == 201
        assert repeated.json()["job"]["batch_job_id"] == job_id
        assert repeated.json()["request_id"] == "prd074-repeat"

        conflict_payload = dict(payload)
        conflict_payload["name"] = "Different request"
        conflict = await client.post(
            "/batch-jobs",
            json=conflict_payload,
            headers=_headers(admin_token, "prd074-conflict", idempotency_key=idem),
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "batch.idempotency_conflict"
        assert conflict.json()["request_id"] == "prd074-conflict"
        assert conflict.headers["cache-control"] == "no-store, private"

        listing = await client.get(
            "/batch-jobs?status=queued&limit=1",
            headers=_headers(admin_token, "prd074-list"),
        )
        assert listing.status_code == 200
        assert [job["batch_job_id"] for job in listing.json()["jobs"]] == [job_id]
        tasks = await client.get(
            f"/batch-jobs/{job_id}/tasks?limit=1",
            headers=_headers(admin_token, "prd074-tasks"),
        )
        assert tasks.status_code == 200
        assert len(tasks.json()["tasks"]) == 1
        assert tasks.json()["tasks"][0]["file_id"] == file_id
        task_id = str(tasks.json()["tasks"][0]["batch_task_id"])
        connection = connect_database(database_url)
        try:
            tenant_id = str(
                connection.execute(
                    "SELECT tenant_id FROM tenants WHERE slug='default'"
                ).fetchone()[0]
            )
            frozen_route = connection.execute(
                """
                SELECT route_plan_json,model_alias_id,environment_id,virtual_key_id
                FROM batch_jobs WHERE tenant_id=? AND batch_job_id=?
                """,
                (tenant_id, job_id),
            ).fetchone()
            assert frozen_route is not None
            gateway_route, _digest = _validated_route_plan(
                frozen_route["route_plan_json"],
                model_alias_id=str(frozen_route["model_alias_id"]),
                environment_id=str(frozen_route["environment_id"]),
                virtual_key_id=str(frozen_route["virtual_key_id"]),
                application_id=None,
            )
            assert gateway_route.default.input_price_per_1k == "0.010000000000"
            assert gateway_route.default.output_price_per_1k == "0.020000000000"
        finally:
            connection.close()

        original_blob_reader = file_security_service._read_extracted_blob_text

        def revoke_during_unlocked_blob_read(config, owner, *, secret):
            text = original_blob_reader(config, owner, secret=secret)
            connection = connect_database(database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE users SET role='viewer',updated_at='2026-08-08T08:03:00Z'
                    WHERE tenant_id=? AND email='admin@metahub.local'
                    """,
                    (owner.tenant_id,),
                )
                connection.commit()
            finally:
                connection.close()
            return text

        monkeypatch.setattr(
            file_security_service,
            "_read_extracted_blob_text",
            revoke_during_unlocked_blob_read,
        )
        with pytest.raises(ApiError) as revoked_context:
            file_security_service.build_file_model_context_for_batch(
                app.state.config,
                tenant_id=tenant_id,
                batch_job_id=job_id,
                batch_task_id=task_id,
            )
        assert revoked_context.value.code == "batch.authorization_revoked"
        monkeypatch.setattr(
            file_security_service,
            "_read_extracted_blob_text",
            original_blob_reader,
        )
        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                UPDATE users SET role='admin',updated_at='2026-08-08T08:04:00Z'
                WHERE email='admin@metahub.local'
                """
            )
            connection.commit()
        finally:
            connection.close()

        other_token = await _login(
            client,
            tenant_slug=OTHER_TENANT_SLUG,
            email="admin@prd074-other.local",
            password=OTHER_PASSWORD,
        )
        cross = await client.get(
            f"/batch-jobs/{job_id}",
            headers=_headers(other_token, "prd074-cross-tenant"),
        )
        missing = await client.get(
            "/batch-jobs/bjob_ffffffffffffffffffffffffffffffff",
            headers=_headers(other_token, "prd074-missing"),
        )
        for response in (cross, missing):
            assert response.status_code == 404
            assert response.json()["code"] == "batch.not_found"
            assert response.headers["cache-control"] == "no-store, private"

        cancelled = await client.post(
            f"/batch-jobs/{job_id}/cancel",
            content=b"",
            headers=_headers(admin_token, "prd074-cancel"),
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["job"]["status"] == "cancelled"
        assert cancelled.json()["job"]["progress"]["cancelled"] == 1
        repeated_cancel = await client.post(
            f"/batch-jobs/{job_id}/cancel",
            content=b"",
            headers=_headers(admin_token, "prd074-cancel-repeat"),
        )
        assert repeated_cancel.status_code == 200
        assert repeated_cancel.json()["job"]["status"] == "cancelled"

    connection = connect_database(database_url)
    try:
        tenant_id = str(
            connection.execute(
                "SELECT tenant_id FROM tenants WHERE slug='default'"
            ).fetchone()[0]
        )
        events = connection.execute(
            """
            SELECT event_type FROM batch_job_events
            WHERE tenant_id=? AND batch_job_id=? ORDER BY occurred_at,event_type
            """,
            (tenant_id, job_id),
        ).fetchall()
        assert {str(row[0]) for row in events} == {
            "batch_job.created",
            "batch_job.completed",
        }
        assert connection.execute(
            """
                SELECT COUNT(*)
                FROM batch_job_event_outbox outbox
                JOIN batch_job_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.batch_job_event_id=outbox.batch_job_event_id
                WHERE event.tenant_id=? AND event.batch_job_id=?
            """,
            (tenant_id, job_id),
        ).fetchone()[0] == 2
        assert connection.execute(
            """
            SELECT COUNT(*) FROM batch_tasks
            WHERE tenant_id=? AND batch_job_id=? AND status='cancelled'
            """,
            (tenant_id, job_id),
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.anyio
async def test_batch_api_backend_permission_and_strict_input_gates(
    prd074_api_template: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove backend RBAC runs before body parsing and strict inputs never echo content."""

    database_url = _clone_environment(prd074_api_template, monkeypatch, tmp_path)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        viewer_token = await _login(
            client,
            email="viewer@prd074.local",
            password=VIEWER_PASSWORD,
        )
        denied = await client.post(
            "/batch-jobs",
            content=b'{"private_input":"must-not-echo"}',
            headers={
                **_headers(
                    viewer_token,
                    "prd074-viewer-denied",
                    idempotency_key="74000000-0000-4000-8000-000000000002",
                ),
                "Content-Type": "application/json",
            },
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "auth.permission_denied"
        assert denied.json()["request_id"] == "prd074-viewer-denied"
        assert denied.headers["cache-control"] == "no-store, private"
        assert "must-not-echo" not in denied.text

        admin_token = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        duplicate_json = await client.post(
            "/batch-jobs",
            content=b'{"name":"one","name":"two"}',
            headers={
                **_headers(
                    admin_token,
                    "prd074-duplicate-json",
                    idempotency_key="74000000-0000-4000-8000-000000000003",
                ),
                "Content-Type": "application/json",
            },
        )
        assert duplicate_json.status_code == 422
        assert duplicate_json.json()["code"] == "batch.request_invalid"
        assert duplicate_json.headers["cache-control"] == "no-store, private"
        assert "one" not in duplicate_json.text
        assert "two" not in duplicate_json.text

        connection = connect_database(database_url)
        try:
            tenant_id = str(
                connection.execute(
                    "SELECT tenant_id FROM tenants WHERE slug='default'"
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE users SET role='viewer',updated_at='2026-08-08T08:05:00Z'
                WHERE tenant_id=? AND email='admin@metahub.local'
                """,
                (tenant_id,),
            )
            connection.commit()
        finally:
            connection.close()
        revoked = await client.post(
            "/batch-jobs",
            content=b'{"private_input":"revoked-must-not-echo"}',
            headers={
                **_headers(
                    admin_token,
                    "prd074-revoked",
                    idempotency_key="74000000-0000-4000-8000-000000000004",
                ),
                "Content-Type": "application/json",
            },
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "auth.permission_denied"
        assert revoked.json()["request_id"] == "prd074-revoked"
        assert "revoked-must-not-echo" not in revoked.text

    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM batch_jobs").fetchone()[0] == 0
    finally:
        connection.close()
