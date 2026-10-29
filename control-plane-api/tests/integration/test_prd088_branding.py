"""PRD-088 tenant branding API, RBAC, audit, and outbox recovery tests."""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.branding import router as branding_router
from metahub_control.branding import service as branding_service
from metahub_control.branding.outbox import drain_branding_event_once
from metahub_control.console.service import EVENT_TYPE_BY_NAVIGATION, _visible_navigation_items
from metahub_control.users.routes import router as users_router
from metahub_control.webhooks import service as webhook_service
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.rbac import (
    ROLE_CODES,
    ROLE_GRANTABLE_PERMISSIONS,
    canonical_permission_codes,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

STAMP = "2030-01-01T00:00:00Z"
OTHER_TENANT_ID = "tenant_prd088_other"
OTHER_USER_ID = "user_prd088_other"
PRD142_SUPERSEDED_REASON = (
    "PRD-142 retires tenant-scoped branding runtime routes; "
    "owner-separated active contracts are covered by test_prd142_brand_domains.py"
)


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from metahub_control.app import response_cache_private_cache_boundary

    database_url = _database_url(tmp_path / "branding.sqlite3")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "LocalAdmin!2026")
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd088-test-auth-secret")
    assert apply_migrations(database_url).applied[-1] == "0081"
    seed = apply_seed_data(database_url)
    app = create_service_app("control-plane-api", "PRD-088 Branding Test API")
    app.middleware("http")(response_cache_private_cache_boundary)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(branding_router)
    return app, database_url, seed.tenant_id


async def _login(
    client: AsyncClient,
    email: str,
    password: str,
    *,
    tenant_slug: str = "default",
) -> str:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant_slug, "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_branding_backend_navigation_and_permissions_are_retired() -> None:
    with pytest.raises(ApiError) as removed_permission:
        _visible_navigation_items(("branding.read",))
    assert removed_permission.value.code == "rbac.matrix_integrity_failed"
    assert "branding" not in EVENT_TYPE_BY_NAVIGATION
    for role in ROLE_CODES:
        items = _visible_navigation_items(
            canonical_permission_codes(ROLE_GRANTABLE_PERMISSIONS[role])
        )
        assert "branding" not in {item.id for item in items}
        assert "branding.read" not in ROLE_GRANTABLE_PERMISSIONS[role]
        assert "branding.manage" not in ROLE_GRANTABLE_PERMISSIONS[role]


def _headers(token: str, request_id: str, **extra: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trace_{request_id}",
        **extra,
    }


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    checksum = binascii.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)


def _png_bytes() -> bytes:
    width = height = 16
    scanlines = b"".join(b"\x00" + b"\x17\x6b\x87" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _insert_other_tenant(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,'prd088-other','PRD088 Other','internal_only','active',?,?)
            """,
            (OTHER_TENANT_ID, STAMP, STAMP),
        )
        connection.execute(
            """
            INSERT INTO users(user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at)
            VALUES(?,?,'other-admin@metahub.local','Other Admin',?,'admin','active',?,?)
            """,
            (
                OTHER_USER_ID,
                OTHER_TENANT_ID,
                hash_password("OtherAdmin!2026", salt=b"prd088-other-salt"),
                STAMP,
                STAMP,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _revoke_role_permission(database_url: str, tenant_id: str, role_code: str, code: str) -> None:
    connection = connect_database(database_url)
    try:
        role = connection.execute(
            "SELECT role_id,permission_revision FROM roles WHERE tenant_id=? AND code=?",
            (tenant_id, role_code),
        ).fetchone()
        permission = connection.execute(
            "SELECT permission_id FROM permissions WHERE tenant_id=? AND code=?",
            (tenant_id, code),
        ).fetchone()
        assert role is not None and permission is not None
        connection.execute(
            "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
            (tenant_id, role["role_id"], permission["permission_id"]),
        )
        remaining = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT permission.code FROM role_permissions relation
                JOIN permissions permission
                  ON permission.tenant_id=relation.tenant_id
                 AND permission.permission_id=relation.permission_id
                WHERE relation.tenant_id=? AND relation.role_id=?
                """,
                (tenant_id, role["role_id"]),
            )
        }
        connection.execute(
            """
            UPDATE roles SET permission_revision=?,matrix_sha256=?,updated_at=?
            WHERE tenant_id=? AND role_id=?
            """,
            (
                int(role["permission_revision"]) + 1,
                permission_matrix_sha256(remaining),
                STAMP,
                tenant_id,
                role["role_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.skip(reason=PRD142_SUPERSEDED_REASON)
async def test_branding_http_logo_public_audit_and_no_subscription(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, database_url, tenant_id = _prepare_app(monkeypatch, tmp_path)
    logo = _png_bytes()
    logo_base64 = base64.b64encode(logo).decode("ascii")
    secret_values = (
        "PRIVATE-BRAND-SENTINEL",
        "PRIVATE-LOGIN-SENTINEL",
        "PRIVATE-MESSAGE-SENTINEL",
        "private-brand.customer.example.org",
        logo_base64,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        unauthenticated = await client.get("/branding")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["code"] == "auth.token_missing"

        missing_public = await client.get(
            "/branding/public", params={"tenant_slug": "missing-tenant"}
        )
        invalid_public = await client.get(
            "/branding/public", params={"tenant_slug": "bad slug"}
        )
        assert missing_public.status_code == invalid_public.status_code == 200
        assert missing_public.json()["branding"] == invalid_public.json()["branding"]
        assert set(missing_public.json()) == {"branding", "request_id", "trace_id"}
        assert set(missing_public.json()["branding"]) == {
            "product_name",
            "primary_color",
            "secondary_color",
            "login_title",
            "login_message",
            "logo_present",
        }
        assert missing_public.headers["cache-control"] == "no-store, private"

        for slug in ("default", "missing-tenant"):
            missing_logo = await client.get(
                "/branding/public/logo", params={"tenant_slug": slug}
            )
            assert missing_logo.status_code == 404
            assert missing_logo.json()["code"] == "branding.logo_not_configured"

        admin_token = await _login(client, "admin@metahub.local", "LocalAdmin!2026")
        current = await client.get(
            "/branding", headers=_headers(admin_token, "req_prd088_get")
        )
        assert current.status_code == 200, current.text
        assert current.json()["branding"]["revision"] == 1
        assert current.json()["branding"]["logo_present"] is False
        assert "tenant_id" not in current.text
        assert current.headers["cache-control"] == "no-store, private"

        missing_match = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_missing_match"),
            json={"product_name": "Rejected"},
        )
        assert missing_match.status_code == 400
        assert missing_match.json()["code"] == "branding.request_invalid"

        invalid_payloads = (
            ({"unknown": "value"}, "branding.request_invalid"),
            ({"product_name": None}, "branding.request_invalid"),
            ({"logo_png_base64": "data:image/png;base64,AAAA"}, "branding.logo_invalid"),
            ({"logo_png_base64": base64.b64encode(b"not-png").decode()}, "branding.logo_invalid"),
            ({"custom_domain": "127.0.0.1"}, "branding.custom_domain_invalid"),
            ({"custom_domain": "console.internal"}, "branding.custom_domain_invalid"),
        )
        for index, (payload, code) in enumerate(invalid_payloads):
            rejected = await client.patch(
                "/branding",
                headers=_headers(
                    admin_token,
                    f"req_prd088_invalid_{index}",
                    **{"If-Match": "1"},
                ),
                json=payload,
            )
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()["code"] == code

        caplog.set_level("DEBUG")
        updated = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_update", **{"If-Match": "1"}),
            json={
                "product_name": secret_values[0],
                "primary_color": "#125E75",
                "secondary_color": "#F4B942",
                "login_title": secret_values[1],
                "login_message": secret_values[2],
                "custom_domain": secret_values[3],
                "logo_png_base64": logo_base64,
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["branding"]["revision"] == 2
        assert updated.json()["branding"]["logo_present"] is True
        assert updated.json()["branding"]["custom_domain_status"] == "reserved"
        assert logo_base64 not in updated.text

        protected_logo = await client.get(
            "/branding/logo", headers=_headers(admin_token, "req_prd088_logo")
        )
        assert protected_logo.status_code == 200
        assert protected_logo.content == logo
        assert protected_logo.headers["content-type"] == "image/png"
        assert protected_logo.headers["cache-control"] == "private, no-store"
        assert protected_logo.headers["x-content-type-options"] == "nosniff"
        assert protected_logo.headers["x-request-id"] == "req_prd088_logo"

        public = await client.get("/branding/public", params={"tenant_slug": "default"})
        public_logo = await client.get(
            "/branding/public/logo", params={"tenant_slug": "default"}
        )
        unknown_after = await client.get(
            "/branding/public", params={"tenant_slug": "missing-tenant"}
        )
        assert public.status_code == public_logo.status_code == 200
        assert public.json()["branding"]["product_name"] == secret_values[0]
        assert public_logo.content == logo
        assert unknown_after.json()["branding"]["product_name"] == "MetaHub"

        stale = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_stale", **{"If-Match": "1"}),
            json={"product_name": "Stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "branding.revision_conflict"
        unchanged = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_unchanged", **{"If-Match": "2"}),
            json={"product_name": secret_values[0]},
        )
        assert unchanged.status_code == 400
        assert unchanged.json()["code"] == "branding.request_invalid"
        query_rejected = await client.get(
            "/branding",
            params={"tenant_id": OTHER_TENANT_ID},
            headers=_headers(admin_token, "req_prd088_query"),
        )
        assert query_rejected.status_code == 400

        cleared = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_clear", **{"If-Match": "2"}),
            json={"custom_domain": None, "logo_png_base64": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["branding"]["revision"] == 3
        assert cleared.json()["branding"]["custom_domain"] is None
        assert cleared.json()["branding"]["logo_present"] is False

    connection = connect_database(database_url)
    try:
        audit_rows = connection.execute(
            """
            SELECT before_summary_json,after_summary_json FROM audit_logs
            WHERE tenant_id=? AND action='branding.updated' ORDER BY created_at,audit_log_id
            """,
            (tenant_id,),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT changed_fields_json,payload_json FROM branding_events
            WHERE tenant_id=? ORDER BY revision
            """,
            (tenant_id,),
        ).fetchall()
        outbox_rows = connection.execute(
            """
            SELECT payload_json,subscription_snapshot_json,status
            FROM branding_event_outbox
            WHERE tenant_id=? ORDER BY created_at,branding_event_outbox_id
            """,
            (tenant_id,),
        ).fetchall()
        assert len(audit_rows) == len(event_rows) == len(outbox_rows) == 2
        assert json.loads(event_rows[0]["changed_fields_json"]) == [
            "custom_domain",
            "login_message",
            "login_title",
            "logo_png",
            "primary_color",
            "product_name",
            "secondary_color",
        ]
        assert json.loads(event_rows[1]["changed_fields_json"]) == [
            "custom_domain",
            "logo_png",
        ]
        assert all(row["subscription_snapshot_json"] == "[]" for row in outbox_rows)
        persisted = json.dumps(
            [
                [dict(row) for row in audit_rows],
                [dict(row) for row in event_rows],
                [dict(row) for row in outbox_rows],
            ],
            ensure_ascii=False,
        )
        for secret in secret_values:
            assert secret not in persisted
            assert secret not in caplog.text
    finally:
        connection.close()

    assert drain_branding_event_once(
        app.state.config,
        worker_id="prd088-no-subscription",
        now=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=2),
    )
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                """
                SELECT status,attempt_count FROM branding_event_outbox
                WHERE tenant_id=? ORDER BY created_at,branding_event_outbox_id LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
        ) == ("no_subscription", 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.skip(reason=PRD142_SUPERSEDED_REASON)
async def test_branding_live_rbac_tenant_isolation_and_domain_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, database_url, tenant_id = _prepare_app(monkeypatch, tmp_path)
    _insert_other_tenant(database_url)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", "LocalAdmin!2026")
        other_token = await _login(
            client,
            "other-admin@metahub.local",
            "OtherAdmin!2026",
            tenant_slug="prd088-other",
        )
        created = await client.post(
            "/users",
            headers=_headers(admin_token, "req_prd088_viewer_create"),
            json={
                "email": "branding-viewer@example.invalid",
                "display_name": "Branding Viewer",
                "password": "BrandingViewer!2026",
                "role": "viewer",
            },
        )
        assert created.status_code == 201, created.text
        viewer_token = await _login(
            client, "branding-viewer@example.invalid", "BrandingViewer!2026"
        )
        viewer_read = await client.get(
            "/branding", headers=_headers(viewer_token, "req_prd088_viewer_read")
        )
        viewer_write = await client.patch(
            "/branding",
            headers=_headers(viewer_token, "req_prd088_viewer_write", **{"If-Match": "1"}),
            json={"product_name": "Forbidden"},
        )
        assert viewer_read.status_code == 200
        assert viewer_write.status_code == 403
        assert viewer_write.json()["code"] == "branding.forbidden"
        assert viewer_write.headers["cache-control"] == "no-store, private"

        other_update = await client.patch(
            "/branding",
            headers=_headers(other_token, "req_prd088_other_update", **{"If-Match": "1"}),
            json={
                "product_name": "Other Tenant Console",
                "custom_domain": "shared.customer.example.org",
            },
        )
        assert other_update.status_code == 200, other_update.text
        default_read = await client.get(
            "/branding", headers=_headers(admin_token, "req_prd088_default_read")
        )
        other_read = await client.get(
            "/branding", headers=_headers(other_token, "req_prd088_other_read")
        )
        assert default_read.json()["branding"]["product_name"] == "MetaHub"
        assert other_read.json()["branding"]["product_name"] == "Other Tenant Console"
        default_id = default_read.json()["branding"]["branding_config_id"]
        other_id = other_read.json()["branding"]["branding_config_id"]
        assert default_id != other_id

        conflict = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_domain_conflict", **{"If-Match": "1"}),
            json={"custom_domain": "shared.customer.example.org"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "branding.custom_domain_conflict"

        _revoke_role_permission(database_url, tenant_id, "viewer", "branding.read")
        revoked = await client.get(
            "/branding", headers=_headers(viewer_token, "req_prd088_viewer_revoked")
        )
        assert revoked.status_code == 403
        assert revoked.json()["code"] == "branding.forbidden"
        assert revoked.headers["cache-control"] == "no-store, private"

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM branding_events WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM branding_events WHERE tenant_id=?", (OTHER_TENANT_ID,)
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.anyio
@pytest.mark.skip(reason=PRD142_SUPERSEDED_REASON)
async def test_branding_transaction_rollback_and_worker_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, database_url, tenant_id = _prepare_app(monkeypatch, tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://control") as client:
        admin_token = await _login(client, "admin@metahub.local", "LocalAdmin!2026")
        original_record = branding_service.record_audit_log
        monkeypatch.setattr(branding_service, "record_audit_log", lambda *args, **kwargs: None)
        rolled_back = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_atomic", **{"If-Match": "1"}),
            json={"product_name": "Atomic Rollback"},
        )
        assert rolled_back.status_code == 503
        assert rolled_back.json()["code"] == "branding.event_unavailable"
        monkeypatch.setattr(branding_service, "record_audit_log", original_record)

        successful = await client.patch(
            "/branding",
            headers=_headers(admin_token, "req_prd088_retry", **{"If-Match": "1"}),
            json={"product_name": "Retry Test"},
        )
        assert successful.status_code == 200, successful.text

    connection = connect_database(database_url)
    try:
        assert connection.execute(
            "SELECT revision FROM branding_configs WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM branding_events WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE tenant_id=? AND action='branding.updated'",
            (tenant_id,),
        ).fetchone()[0] == 1
    finally:
        connection.close()

    now = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=2)

    def fail_dispatch(*args, **kwargs):
        raise RuntimeError("test-only fixed failure")

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", fail_dispatch)
    assert drain_branding_event_once(app.state.config, worker_id="prd088-retry-a", now=now)
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                """
                SELECT status,attempt_count,generation,last_error_code
                FROM branding_event_outbox WHERE tenant_id=?
                """,
                (tenant_id,),
            ).fetchone()
        ) == ("pending", 1, 2, "webhook.delivery_unavailable")
    finally:
        connection.close()

    monkeypatch.setattr(webhook_service, "dispatch_webhook_event", lambda *args, **kwargs: None)
    assert not drain_branding_event_once(
        app.state.config, worker_id="prd088-retry-early", now=now + timedelta(seconds=4)
    )
    assert drain_branding_event_once(
        app.state.config, worker_id="prd088-retry-b", now=now + timedelta(seconds=5)
    )
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                """
                SELECT status,attempt_count,generation,last_error_code
                FROM branding_event_outbox WHERE tenant_id=?
                """,
                (tenant_id,),
            ).fetchone()
        ) == ("no_subscription", 2, 4, None)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
