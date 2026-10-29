"""PRD-084 Control API integration coverage for local model Endpoints."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.auth.routes import router as auth_router
from metahub_control.config_transfer.routes import router as config_transfer_router
from metahub_control.local_endpoints import routes as local_endpoint_routes
from metahub_control.local_endpoints import service as local_endpoint_service
from metahub_control.local_endpoints.routes import router as local_endpoints_router
from metahub_control.provider_health.routes import router as provider_health_router
from metahub_control.providers.routes import (
    provider_keys_router,
)
from metahub_control.providers.routes import (
    router as providers_router,
)
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app

ADMIN_PASSWORD = "Prd084AdminTestOnly!2026"
DEVELOPER_PASSWORD = "Prd084DeveloperTestOnly!2026"


@pytest.fixture()
def local_models_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1/models":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"object": "list", "data": [{"id": "served-chat"}, {"id": "embed-v1"}]}
                    ).encode("utf-8")
                )
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", server.server_address[1]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    base_url: str,
    port: int,
) -> str:
    database_url = _database_url(tmp_path / "prd084-control.sqlite3")
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", "prd084-auth-test-" + "a" * 48)
    monkeypatch.setenv("METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET", "prd084-provider-test-" + "b" * 48)
    monkeypatch.setenv("METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS", str(port))
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK", "true")
    assert "0089" in apply_migrations(database_url).applied
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES('user_prd084_developer',?,'developer@prd084.local','PRD084 Developer',?,
                     'developer','active','2026-08-16T00:00:00Z','2026-08-16T00:00:00Z')
            """,
            (seed.tenant_id, hash_password(DEVELOPER_PASSWORD)),
        )
        connection.commit()
    finally:
        connection.close()
    return database_url


def _app():
    app = create_service_app("control-plane-api", "PRD084 Control Test")
    app.include_router(auth_router)
    app.include_router(local_endpoints_router)
    app.include_router(providers_router)
    app.include_router(provider_keys_router)
    app.include_router(provider_health_router)
    app.include_router(config_transfer_router)
    return app


async def _login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": "default", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _create_body(base_url: str, *, auth_type: str = "none") -> dict[str, object]:
    body: dict[str, object] = {
        "provider_code": "local_inference",
        "name": "Local inference",
        "engine_kind": "vllm",
        "base_url": base_url,
        "auth_type": auth_type,
        "initial_model": {
            "internal_model_id": "local-chat",
            "provider_model_id": "served-chat",
            "display_name": "Local chat",
            "capabilities": {
                "chat": True,
                "embeddings": True,
                "stream": True,
                "tools": True,
                "json_mode": True,
                "vision": True,
            },
            "context_window": 32768,
            "input_price_per_1k": 0,
            "output_price_per_1k": 0,
            "currency": "CNY",
        },
    }
    if auth_type == "bearer":
        body["bearer_token"] = "token_" + "x" * 40
    return body


def _assert_private_error(response, status_code: int, code: str | None = None) -> None:
    assert response.status_code == status_code, response.text
    payload = response.json()
    if code is not None:
        assert payload["code"] == code
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert response.headers["Cache-Control"] == "no-store, private"


def test_local_endpoint_capability_contract_matches_the_model_catalog() -> None:
    capabilities = {
        "chat": True,
        "embeddings": True,
        "stream": True,
        "tools": True,
        "json_mode": True,
        "vision": True,
    }
    assert local_endpoint_service._capabilities(capabilities) == capabilities
    assert local_endpoint_service._parse_capabilities(json.dumps(capabilities)) == capabilities
    for invalid in ({"vision": False}, {"audio": True}):
        with pytest.raises(ApiError) as captured:
            local_endpoint_service._capabilities(invalid)
        assert captured.value.code == "local_endpoint.catalog_invalid"


@pytest.mark.anyio
async def test_local_endpoint_create_replay_permissions_and_generic_bypass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    database_url = _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@prd084.local", DEVELOPER_PASSWORD)
        request_id = "req_prd084_create_replay"
        body = _create_body(base_url, auth_type="bearer")
        created = await client.post(
            "/local-endpoints",
            headers={"Authorization": f"Bearer {admin}", "X-Request-ID": request_id},
            json=body,
        )
        replay_body = _create_body(base_url, auth_type="bearer")
        replay_body["bearer_token"] = "token_" + "y" * 40
        replayed = await client.post(
            "/local-endpoints",
            headers={"Authorization": f"Bearer {admin}", "X-Request-ID": request_id},
            json=replay_body,
        )
        developer_list = await client.get(
            "/local-endpoints",
            headers={"Authorization": f"Bearer {developer}"},
        )
        developer_create = await client.post(
            "/local-endpoints",
            headers={"Authorization": f"Bearer {developer}"},
            json=_create_body(base_url),
        )
        generic = await client.post(
            "/providers",
            headers={"Authorization": f"Bearer {admin}"},
            json={
                "provider_code": "local_bypass",
                "name": "Local bypass",
                "provider_type": "local_endpoint",
                "base_url": base_url,
                "auth_type": "none",
            },
        )

    assert created.status_code == 201, created.text
    assert replayed.status_code == 200, replayed.text
    assert created.headers["Cache-Control"] == "no-store, private"
    assert created.json()["created"] is True
    assert replayed.json()["created"] is False
    assert created.json()["local_endpoint"] == replayed.json()["local_endpoint"]
    serialized = json.dumps(created.json(), sort_keys=True)
    assert base_url not in serialized and "bearer_token" not in serialized
    assert developer_list.status_code == 200
    assert developer_create.status_code == 403
    assert generic.status_code == 422
    assert generic.json()["code"] == "provider.local_endpoint_requires_local_api"

    connection = connect_database(database_url)
    try:
        credential = connection.execute(
            "SELECT encrypted_key FROM provider_credentials WHERE provider_id=?",
            (created.json()["local_endpoint"]["provider_id"],),
        ).fetchone()
        assert credential is not None and not credential["encrypted_key"].startswith("token_")
        assert connection.execute(
            "SELECT COUNT(*) FROM provider_credentials WHERE provider_id=?",
            (created.json()["local_endpoint"]["provider_id"],),
        ).fetchone()[0] == 1
        audit = connection.execute(
            """
            SELECT object_name,after_summary_json FROM audit_logs
            WHERE object_id=? AND action='provider.created'
            """,
            (created.json()["local_endpoint"]["provider_id"],),
        ).fetchone()
        assert audit is not None
        audit_summary = json.loads(audit["after_summary_json"])
        assert audit["object_name"] == body["provider_code"]
        assert audit_summary["name_hash"] == hashlib.sha256(
            str(body["name"]).encode("utf-8")
        ).hexdigest()
        assert "name" not in audit_summary
        assert base_url not in audit["after_summary_json"]
        assert body["name"] not in audit["after_summary_json"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_local_endpoint_probe_updates_bounded_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    database_url = _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        created = await client.post(
            "/local-endpoints",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd084_probe_create",
            },
            json=_create_body(base_url),
        )
        endpoint_id = created.json()["local_endpoint"]["local_endpoint_id"]
        tested = await client.post(
            f"/local-endpoints/{endpoint_id}/test",
            headers={"Authorization": f"Bearer {admin}"},
            json={},
        )

    assert tested.status_code == 200, tested.text
    assert tested.headers["Cache-Control"] == "no-store, private"
    payload = tested.json()
    assert payload["test_result"]["status"] == "success"
    assert payload["test_result"]["discovered_model_count"] == 2
    assert payload["local_endpoint"]["catalog_revision"] == 1
    assert payload["local_endpoint"]["catalog_status"] == "ready"
    assert base_url not in json.dumps(payload, sort_keys=True)
    connection = connect_database(database_url)
    try:
        rows = connection.execute(
            """
            SELECT provider_model_id,availability_status,linked_model_id
            FROM local_model_catalog_entries
            WHERE local_endpoint_id=? ORDER BY provider_model_id
            """,
            (endpoint_id,),
        ).fetchall()
        assert [(row["provider_model_id"], row["availability_status"]) for row in rows] == [
            ("embed-v1", "available"),
            ("served-chat", "available"),
        ]
        assert rows[1]["linked_model_id"] == payload["local_endpoint"]["initial_model"]["model_id"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_backing_provider_is_closed_from_all_generic_control_apis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    database_url = _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    token_canary = "token_" + "z" * 40
    body = _create_body(base_url, auth_type="bearer")
    body["bearer_token"] = token_canary
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        created = await client.post(
            "/local-endpoints",
            headers={**headers, "X-Request-ID": "req_prd084_generic_create"},
            json=body,
        )
        assert created.status_code == 201, created.text
        provider_id = created.json()["local_endpoint"]["provider_id"]

        connection = connect_database(database_url)
        try:
            credential = connection.execute(
                "SELECT provider_credential_id FROM provider_credentials WHERE provider_id=?",
                (provider_id,),
            ).fetchone()
            assert credential is not None
            credential_id = credential["provider_credential_id"]
        finally:
            connection.close()

        providers = await client.get("/providers", headers=headers)
        provider_filter = await client.get(
            "/providers?provider_type=local_endpoint", headers=headers
        )
        provider_patch = await client.patch(
            f"/providers/{provider_id}", headers=headers, json={"name": "Blocked update"}
        )
        provider_test = await client.post(
            f"/providers/{provider_id}/test", headers=headers, json={}
        )
        key_list = await client.get(f"/providers/{provider_id}/keys", headers=headers)
        key_create = await client.post(
            f"/providers/{provider_id}/keys",
            headers=headers,
            json={"plain_value": token_canary},
        )
        key_patch = await client.patch(
            f"/provider-keys/{credential_id}", headers=headers, json={"weight": 200}
        )
        health = await client.get("/provider-health", headers=headers)
        health_filter = await client.get(
            "/provider-health?provider_type=local_endpoint", headers=headers
        )
        probe = await client.post(f"/providers/{provider_id}/probe", headers=headers, json={})
        config_export = await client.post("/config/export", headers=headers, json={})

    assert providers.status_code == 200, providers.text
    assert provider_id not in {item["provider_id"] for item in providers.json()["providers"]}
    assert health.status_code == 200, health.text
    assert provider_id not in {
        item["provider_id"] for item in health.json()["provider_health"]
    }
    blocked = [
        provider_filter,
        provider_patch,
        provider_test,
        key_list,
        key_create,
        key_patch,
        health_filter,
        probe,
        config_export,
    ]
    for response in blocked:
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "provider.local_endpoint_requires_local_api"
    blocked_serialized = "\n".join(response.text for response in blocked)
    assert base_url not in blocked_serialized
    assert token_canary not in blocked_serialized


@pytest.mark.anyio
async def test_local_endpoint_private_errors_cover_401_403_422_and_unhandled_500(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    private_canary = "PRD084_LOCAL_ENDPOINT_PRIVATE_CANARY"

    def fail_after_authentication(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(private_canary)

    app = _app()
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://control",
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        developer = await _login(client, "developer@prd084.local", DEVELOPER_PASSWORD)
        unauthorized = await client.get(
            "/local-endpoints", headers={"X-Request-ID": "req_prd084_private_401"}
        )
        forbidden = await client.post(
            "/local-endpoints",
            headers={
                "Authorization": f"Bearer {developer}",
                "X-Request-ID": "req_prd084_private_403",
            },
            json=_create_body(base_url),
        )
        invalid = await client.post(
            "/local-endpoints",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd084_private_422",
            },
            json={},
        )
        monkeypatch.setattr(
            local_endpoint_routes, "list_local_endpoints", fail_after_authentication
        )
        unhandled = await client.get(
            "/local-endpoints",
            headers={
                "Authorization": f"Bearer {admin}",
                "X-Request-ID": "req_prd084_private_500",
            },
        )

    _assert_private_error(unauthorized, 401)
    _assert_private_error(forbidden, 403)
    _assert_private_error(invalid, 422)
    _assert_private_error(unhandled, 500, "infra.unhandled")
    assert private_canary not in unhandled.text


@pytest.mark.anyio
async def test_idempotent_replay_precedes_new_token_validation_and_encryption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    request_id = "req_prd084_replay_before_encrypt"
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        created = await client.post(
            "/local-endpoints",
            headers={**headers, "X-Request-ID": request_id},
            json=_create_body(base_url, auth_type="bearer"),
        )
        assert created.status_code == 201, created.text

        def encryption_failure(_value: str, *, encryption_secret: str | None = None) -> str:
            raise ValueError("test-only encryption failure")

        monkeypatch.setattr(local_endpoint_service, "encrypt_provider_key", encryption_failure)
        replay_body = _create_body(base_url, auth_type="bearer")
        replay_body["bearer_token"] = "token_" + "r" * 40
        replayed = await client.post(
            "/local-endpoints",
            headers={**headers, "X-Request-ID": request_id},
            json=replay_body,
        )
        new_request = await client.post(
            "/local-endpoints",
            headers={**headers, "X-Request-ID": "req_prd084_new_encrypt_failure"},
            json=replay_body,
        )

    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["created"] is False
    assert replayed.json()["local_endpoint"] == created.json()["local_endpoint"]
    _assert_private_error(new_request, 503, "local_endpoint.unavailable")


@pytest.mark.anyio
async def test_local_probe_reports_the_successful_attempt_after_a_broken_higher_weight_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_models_server: tuple[str, int],
) -> None:
    base_url, port = local_models_server
    database_url = _prepare(monkeypatch, tmp_path, base_url=base_url, port=port)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://control"
    ) as client:
        admin = await _login(client, "admin@metahub.local", ADMIN_PASSWORD)
        headers = {"Authorization": f"Bearer {admin}"}
        created = await client.post(
            "/local-endpoints",
            headers={**headers, "X-Request-ID": "req_prd084_multi_key_create"},
            json=_create_body(base_url, auth_type="bearer"),
        )
        assert created.status_code == 201, created.text
        endpoint = created.json()["local_endpoint"]

        connection = connect_database(database_url)
        try:
            connection.execute(
                """
                INSERT INTO provider_credentials(
                  provider_credential_id,tenant_id,provider_id,key_fingerprint,encrypted_key,
                  name,key_prefix,weight,status,created_by,created_at,updated_at
                )
                SELECT 'credential_prd084_corrupt',tenant_id,provider_id,?,
                       'not-a-valid-ciphertext','Broken credential','broken',1000,'active',
                       created_by,created_at,updated_at
                FROM provider_credentials WHERE provider_id=? LIMIT 1
                """,
                ("f" * 64, endpoint["provider_id"]),
            )
            connection.commit()
        finally:
            connection.close()

        tested = await client.post(
            f"/local-endpoints/{endpoint['local_endpoint_id']}/test",
            headers=headers,
            json={},
        )

    assert tested.status_code == 200, tested.text
    result = tested.json()["test_result"]
    assert result["status"] == "success"
    assert result["http_status"] == 200
    assert result["error_code"] is None
    assert result["discovered_model_count"] == 2
