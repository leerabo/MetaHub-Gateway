"""PRD-082 MCP Control API security, persistence, and approval contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response
from metahub_control.auth.routes import router as auth_router
from metahub_control.mcp_governance.routes import router as mcp_router
from metahub_control.mcp_governance.service import _SYNC_FINALIZE_RESERVE_SECONDS
from metahub_shared.db import apply_migrations, apply_seed_data, connect_database
from metahub_shared.db.security import hash_password
from metahub_shared.runtime.fastapi_app import create_service_app

ADMIN_PASSWORD = "Prd082AdminOnly!2026"
ROLE_PASSWORD = "Prd082RoleOnly!2026"
OTHER_PASSWORD = "Prd082OtherOnly!2026"
AUTH_SECRET = "prd082-auth-domain-" + "a" * 48
APPROVAL_SECRET = "prd082-approval-domain-" + "p" * 48
MCP_ENCRYPTION_SECRET = "prd082-mcp-encryption-domain-" + "e" * 48
MCP_HMAC_SECRET = "prd082-mcp-hmac-domain-" + "h" * 48
NOW = "2026-08-15T08:00:00Z"
NOW_1 = "2026-08-15T08:00:01Z"

DEVELOPER_ID = "usr_prd082_api_developer"
SECURITY_ID = "usr_prd082_api_security"
VIEWER_ID = "usr_prd082_api_viewer"
OTHER_TENANT_ID = "tenant_prd082_api_other"
OTHER_TENANT_SLUG = "prd082-api-other"
OTHER_ADMIN_ID = "usr_prd082_api_other_admin"


@dataclass(frozen=True, slots=True)
class McpApiTemplate:
    database: Path
    tenant_id: str
    developer_role_id: str
    server_id: str
    policy_tool_id: str
    standard_tool_id: str
    high_tool_id: str


@dataclass(frozen=True, slots=True)
class McpApiFixture:
    database_url: str
    tenant_id: str
    developer_role_id: str
    server_id: str
    policy_tool_id: str
    standard_tool_id: str
    high_tool_id: str


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _digest(value: str | bytes) -> str:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def _id(prefix: str, marker: str) -> str:
    return prefix + _digest(marker)[:32]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _schema() -> dict[str, object]:
    return {
        "additionalProperties": False,
        "properties": {"payload": {"maxLength": 128, "type": "string"}},
        "required": ["payload"],
        "type": "object",
    }


def _insert_tool(
    connection,
    *,
    tenant_id: str,
    server_id: str,
    actor_id: str,
    developer_role_id: str,
    marker: str,
    risk_level: str,
    active: bool,
) -> str:
    tool_id = _id("mcpt_", f"api-tool-{marker}")
    schema_json = _canonical(_schema())
    annotations_json = _canonical({})
    approval_required = int(risk_level == "high")
    connection.execute(
        """
        INSERT INTO mcp_tools(
          mcp_tool_id,tenant_id,mcp_server_id,remote_name,title,description,
          input_schema_original_json,input_schema_original_sha256,
          input_schema_effective_json,input_schema_effective_sha256,
          output_schema_original_json,output_schema_original_sha256,
          output_schema_effective_json,output_schema_effective_sha256,
          annotations_json,annotations_sha256,schema_revision,policy_revision,
          risk_level,approval_required,approval_expires_seconds,
          required_permission_codes_json,result_redaction_pointers_json,status,
          created_by,updated_by,created_at,updated_at
        ) VALUES(?,?,?,?,?,?, ?,?,?,?, NULL,NULL,NULL,NULL, ?,?,1,1,?,?,900,
                 '[]','[]','review_required',?,?,?,?)
        """,
        (
            tool_id,
            tenant_id,
            server_id,
            f"controlled_{marker}",
            f"Controlled {marker}",
            "Controlled schema for API verification",
            schema_json,
            _digest(schema_json),
            schema_json,
            _digest(schema_json),
            annotations_json,
            _digest(annotations_json),
            risk_level,
            approval_required,
            actor_id,
            actor_id,
            NOW,
            NOW,
        ),
    )
    if active:
        connection.execute(
            """
            INSERT INTO mcp_tool_role_grants(
              mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,created_by,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                _id("mcprg_", f"api-grant-{marker}"),
                tenant_id,
                tool_id,
                developer_role_id,
                actor_id,
                NOW,
            ),
        )
        connection.execute(
            """
            UPDATE mcp_tools
            SET status='active',policy_revision=2,updated_at=?
            WHERE tenant_id=? AND mcp_tool_id=?
            """,
            (NOW_1, tenant_id, tool_id),
        )
    return tool_id


@pytest.fixture(scope="module")
def prd082_mcp_api_template(tmp_path_factory: pytest.TempPathFactory) -> McpApiTemplate:
    database = tmp_path_factory.mktemp("prd082-mcp-api-template") / "template.sqlite3"
    database_url = _url(database)
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    try:
        result = apply_migrations(database_url)
        assert result.applied and result.applied[-1] == "0075"
        seed = apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous

    connection = connect_database(database_url)
    try:
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    DEVELOPER_ID,
                    seed.tenant_id,
                    "developer@prd082.local",
                    "PRD082 Developer",
                    hash_password(ROLE_PASSWORD),
                    "developer",
                    NOW,
                    NOW,
                ),
                (
                    SECURITY_ID,
                    seed.tenant_id,
                    "security@prd082.local",
                    "PRD082 Security",
                    hash_password(ROLE_PASSWORD),
                    "security_admin",
                    NOW,
                    NOW,
                ),
                (
                    VIEWER_ID,
                    seed.tenant_id,
                    "viewer@prd082.local",
                    "PRD082 Viewer",
                    hash_password(ROLE_PASSWORD),
                    "viewer",
                    NOW,
                    NOW,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO tenants(
              tenant_id,slug,name,service_mode,status,created_at,updated_at
            ) VALUES(?,?,'PRD082 API Other','internal_only','active',?,?)
            """,
            (OTHER_TENANT_ID, OTHER_TENANT_SLUG, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at
            ) VALUES(?,?,?,'PRD082 Other Admin',?,'admin','active',?,?)
            """,
            (
                OTHER_ADMIN_ID,
                OTHER_TENANT_ID,
                "admin@prd082-other.local",
                hash_password(OTHER_PASSWORD),
                NOW,
                NOW,
            ),
        )
        identity = connection.execute(
            """
            SELECT user.user_id,role.role_id
            FROM users user
            JOIN roles role ON role.tenant_id=user.tenant_id AND role.code='developer'
            WHERE user.tenant_id=? AND user.role='admin'
            ORDER BY user.user_id LIMIT 1
            """,
            (seed.tenant_id,),
        ).fetchone()
        assert identity is not None
        actor_id = str(identity["user_id"])
        developer_role_id = str(identity["role_id"])
        server_id = _id("mcps_", "api-seeded-server")
        credential_id = _id("mcpc_", "api-seeded-credential")
        connection.execute(
            """
            INSERT INTO mcp_servers(
              mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
              endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
              endpoint_scheme,endpoint_host_hint,endpoint_has_path,
              active_credential_revision,protocol_version,capabilities_sha256,status,
              state_revision,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,'Controlled Server','streamable_http','encrypted-placeholder',
                     1,?,1,'https','m***.invalid',0,NULL,NULL,NULL,'pending_sync',1,
                     ?,?,?,?)
            """,
            (
                server_id,
                seed.tenant_id,
                _digest("api-seeded-owner"),
                actor_id,
                actor_id,
                NOW,
                NOW,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_credentials(
              mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
              auth_type,credential_ciphertext,credential_encryption_key_version,
              credential_hmac,credential_hmac_key_version,status,probe_operation_id,
              created_by,created_at,activated_at,rejected_at
            ) VALUES(?,?,?,1,'none',NULL,NULL,NULL,NULL,'candidate',NULL,?,?,NULL,NULL)
            """,
            (credential_id, seed.tenant_id, server_id, actor_id, NOW),
        )
        connection.execute(
            """
            UPDATE mcp_server_credentials
            SET status='active',activated_at=?
            WHERE tenant_id=? AND mcp_server_credential_id=?
            """,
            (NOW_1, seed.tenant_id, credential_id),
        )
        connection.execute(
            """
            UPDATE mcp_servers
            SET active_credential_revision=1,protocol_version='2025-06-18',
                capabilities_sha256=?,status='active',state_revision=2,updated_at=?
            WHERE tenant_id=? AND mcp_server_id=?
            """,
            (_digest("api-capabilities"), NOW_1, seed.tenant_id, server_id),
        )
        policy_tool_id = _insert_tool(
            connection,
            tenant_id=seed.tenant_id,
            server_id=server_id,
            actor_id=actor_id,
            developer_role_id=developer_role_id,
            marker="policy",
            risk_level="standard",
            active=False,
        )
        standard_tool_id = _insert_tool(
            connection,
            tenant_id=seed.tenant_id,
            server_id=server_id,
            actor_id=actor_id,
            developer_role_id=developer_role_id,
            marker="standard",
            risk_level="standard",
            active=True,
        )
        high_tool_id = _insert_tool(
            connection,
            tenant_id=seed.tenant_id,
            server_id=server_id,
            actor_id=actor_id,
            developer_role_id=developer_role_id,
            marker="high",
            risk_level="high",
            active=True,
        )
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    return McpApiTemplate(
        database=database,
        tenant_id=seed.tenant_id,
        developer_role_id=developer_role_id,
        server_id=server_id,
        policy_tool_id=policy_tool_id,
        standard_tool_id=standard_tool_id,
        high_tool_id=high_tool_id,
    )


@pytest.fixture
def mcp_api_fixture(
    prd082_mcp_api_template: McpApiTemplate,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> McpApiFixture:
    database = tmp_path / "prd082-mcp-control.sqlite3"
    shutil.copy2(prd082_mcp_api_template.database, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_DATABASE_URL", database_url)
    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", AUTH_SECRET)
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_VERSION", "1")
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_SECRET", APPROVAL_SECRET)
    monkeypatch.setenv("METAHUB_APPROVAL_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_APPROVAL_OUTBOX_ENABLED", "false")
    monkeypatch.setenv("METAHUB_APPROVAL_WRITEBACK_ENABLED", "false")
    monkeypatch.setenv("METAHUB_MCP_ENABLED", "true")
    monkeypatch.setenv("METAHUB_MCP_ENCRYPTION_VERSION", "1")
    monkeypatch.setenv("METAHUB_MCP_ENCRYPTION_SECRET", MCP_ENCRYPTION_SECRET)
    monkeypatch.setenv("METAHUB_MCP_HMAC_VERSION", "1")
    monkeypatch.setenv("METAHUB_MCP_HMAC_SECRET", MCP_HMAC_SECRET)
    monkeypatch.setenv("METAHUB_MCP_EXECUTOR_MODE", "disabled")
    monkeypatch.setenv("METAHUB_MCP_TEST_LOOPBACK_ENABLED", "false")
    monkeypatch.setenv("METAHUB_MCP_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("METAHUB_PROVIDER_HEALTH_ENABLED", "false")
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    return McpApiFixture(
        database_url=database_url,
        tenant_id=prd082_mcp_api_template.tenant_id,
        developer_role_id=prd082_mcp_api_template.developer_role_id,
        server_id=prd082_mcp_api_template.server_id,
        policy_tool_id=prd082_mcp_api_template.policy_tool_id,
        standard_tool_id=prd082_mcp_api_template.standard_tool_id,
        high_tool_id=prd082_mcp_api_template.high_tool_id,
    )


def _build_app():
    app = create_service_app(
        "control-plane-api",
        "PRD-082 MCP Control API Test",
        extra_cors_allow_headers=("Idempotency-Key",),
    )
    app.include_router(auth_router)
    app.include_router(mcp_router)
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
    assert response.status_code == 200
    return str(response.json()["access_token"])


def _headers(
    token: str,
    marker: str,
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    values = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": f"client-request-{marker}",
        "X-Trace-ID": f"client-trace-{marker}",
    }
    if idempotency_key is not None:
        values["Idempotency-Key"] = idempotency_key
    return values


def _assert_boundary(response: Response, marker: str) -> dict[str, object]:
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "Authorization"
    body = response.json()
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["trace_id"] == response.headers["x-trace-id"]
    assert body["request_id"].startswith("req_")
    assert body["trace_id"].startswith("trc_")
    assert body["request_id"] != f"client-request-{marker}"
    assert body["trace_id"] != f"client-trace-{marker}"
    return body


def _assert_error(
    response: Response,
    marker: str,
    *,
    status_code: int,
    code: str,
    forbidden: tuple[str, ...] = (),
) -> dict[str, object]:
    assert response.status_code == status_code
    body = _assert_boundary(response, marker)
    assert set(body) == {
        "code",
        "message",
        "request_id",
        "trace_id",
        "suggestion",
        "diagnosis",
    }
    assert body["code"] == code
    serialized = json.dumps(body, ensure_ascii=True, sort_keys=True)
    for value in forbidden:
        assert value not in serialized
    return body


def _registration_payload() -> tuple[str, dict[str, object]]:
    endpoint = "".join(("https", "://", "registered-control", ".invalid", "/mcp"))
    return endpoint, {
        "name": "Registered Control Server",
        "endpoint_url": endpoint,
        "auth": {"type": "none"},
    }


@pytest.mark.anyio
async def test_mcp_auth_rbac_exact_wire_and_private_request_ids(
    mcp_api_fixture: McpApiFixture,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.get(
            "/mcp/tools",
            headers={
                "X-Request-ID": "client-request-anonymous",
                "X-Trace-ID": "client-trace-anonymous",
            },
        )
        _assert_error(
            anonymous,
            "anonymous",
            status_code=401,
            code="auth.token_missing",
        )

        viewer = await _login(
            client,
            email="viewer@prd082.local",
            password=ROLE_PASSWORD,
        )
        denied_read = await client.get(
            "/mcp/tools",
            headers=_headers(viewer, "viewer-read"),
        )
        _assert_error(
            denied_read,
            "viewer-read",
            status_code=403,
            code="auth.permission_denied",
        )

        security = await _login(
            client,
            email="security@prd082.local",
            password=ROLE_PASSWORD,
        )
        body_canary = "body-must-not-be-parsed-before-rbac"
        denied_write = await client.post(
            "/mcp/servers",
            content=("{\"private\":\"" + body_canary + "\"}").encode("ascii"),
            headers={
                **_headers(
                    security,
                    "security-register",
                    idempotency_key="prd082-exact-rbac",
                ),
                "Content-Type": "application/json",
            },
        )
        _assert_error(
            denied_write,
            "security-register",
            status_code=403,
            code="auth.permission_denied",
            forbidden=(body_canary,),
        )

        admin = await _login(
            client,
            email="admin@metahub.local",
            password=ADMIN_PASSWORD,
        )
        duplicate_query = await client.get(
            "/mcp/tools?status=active&status=disabled",
            headers=_headers(admin, "duplicate-query"),
        )
        _assert_error(
            duplicate_query,
            "duplicate-query",
            status_code=422,
            code="mcp.request_invalid",
        )

        get_body = await client.request(
            "GET",
            "/mcp/tools",
            content=b"{}",
            headers={**_headers(admin, "get-body"), "Content-Type": "application/json"},
        )
        _assert_error(
            get_body,
            "get-body",
            status_code=422,
            code="mcp.request_invalid",
        )

        missing_key_canary = "missing-key-body-must-not-echo"
        missing_key = await client.post(
            "/mcp/servers",
            content=("{\"private\":\"" + missing_key_canary + "\"}").encode("ascii"),
            headers={**_headers(admin, "missing-key"), "Content-Type": "application/json"},
        )
        _assert_error(
            missing_key,
            "missing-key",
            status_code=400,
            code="mcp.idempotency_required",
            forbidden=(missing_key_canary,),
        )

        duplicate_key_headers = [
            ("Authorization", f"Bearer {admin}"),
            ("X-Request-ID", "client-request-duplicate-key"),
            ("X-Trace-ID", "client-trace-duplicate-key"),
            ("Idempotency-Key", "prd082-duplicate-key-a"),
            ("Idempotency-Key", "prd082-duplicate-key-b"),
            ("Content-Type", "application/json"),
        ]
        duplicate_key = await client.post(
            "/mcp/servers",
            content=b"{}",
            headers=duplicate_key_headers,
        )
        _assert_error(
            duplicate_key,
            "duplicate-key",
            status_code=400,
            code="mcp.idempotency_required",
        )

        duplicate_json = await client.post(
            "/mcp/servers",
            content=b'{"name":"one","name":"two"}',
            headers={
                **_headers(
                    admin,
                    "duplicate-json",
                    idempotency_key="prd082-duplicate-json",
                ),
                "Content-Type": "application/json",
            },
        )
        _assert_error(
            duplicate_json,
            "duplicate-json",
            status_code=422,
            code="mcp.request_invalid",
            forbidden=("one", "two"),
        )

        duplicate_content_type_headers = [
            ("Authorization", f"Bearer {admin}"),
            ("X-Request-ID", "client-request-duplicate-content-type"),
            ("X-Trace-ID", "client-trace-duplicate-content-type"),
            ("Idempotency-Key", "prd082-duplicate-content-type"),
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ]
        duplicate_content_type = await client.post(
            "/mcp/servers",
            content=b"{}",
            headers=duplicate_content_type_headers,
        )
        _assert_error(
            duplicate_content_type,
            "duplicate-content-type",
            status_code=422,
            code="mcp.request_invalid",
        )

    with connect_database(mcp_api_fixture.database_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM mcp_server_operations").fetchone()[0] == 0


@pytest.mark.anyio
async def test_mcp_register_list_detail_policy_and_tenant_persistence(
    mcp_api_fixture: McpApiFixture,
) -> None:
    app = _build_app()
    endpoint, payload = _registration_payload()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        developer = await _login(
            client,
            email="developer@prd082.local",
            password=ROLE_PASSWORD,
        )
        security = await _login(
            client,
            email="security@prd082.local",
            password=ROLE_PASSWORD,
        )
        created = await client.post(
            "/mcp/servers",
            json=payload,
            headers=_headers(
                developer,
                "register",
                idempotency_key="prd082-register-operation",
            ),
        )
        assert created.status_code == 202
        created_body = _assert_boundary(created, "register")
        assert created_body["idempotent_replay"] is False
        assert created_body["server"]["status"] == "pending_sync"
        assert created_body["operation"]["status"] == "queued"
        registered_server_id = str(created_body["server"]["mcp_server_id"])
        operation_id = str(created_body["operation"]["mcp_server_operation_id"])

        replay = await client.post(
            "/mcp/servers",
            json=payload,
            headers=_headers(
                developer,
                "register-replay",
                idempotency_key="prd082-register-operation",
            ),
        )
        assert replay.status_code == 202
        replay_body = _assert_boundary(replay, "register-replay")
        assert replay_body["idempotent_replay"] is True
        assert replay_body["server"]["mcp_server_id"] == registered_server_id
        assert replay_body["operation"]["mcp_server_operation_id"] == operation_id

        conflicting_payload = dict(payload)
        conflicting_payload["name"] = "Different Registered Control Server"
        conflict = await client.post(
            "/mcp/servers",
            json=conflicting_payload,
            headers=_headers(
                developer,
                "register-conflict",
                idempotency_key="prd082-register-operation",
            ),
        )
        _assert_error(
            conflict,
            "register-conflict",
            status_code=409,
            code="mcp.idempotency_conflict",
            forbidden=(endpoint,),
        )

        servers = await client.get(
            "/mcp/servers?status=pending_sync&limit=10",
            headers=_headers(developer, "server-list"),
        )
        assert servers.status_code == 200
        server_body = _assert_boundary(servers, "server-list")
        assert [item["mcp_server_id"] for item in server_body["servers"]] == [
            registered_server_id
        ]
        assert endpoint not in json.dumps(server_body, ensure_ascii=True, sort_keys=True)

        operation = await client.get(
            f"/mcp/server-operations/{operation_id}",
            headers=_headers(developer, "operation-detail"),
        )
        assert operation.status_code == 200
        operation_body = _assert_boundary(operation, "operation-detail")
        assert operation_body["operation"]["mcp_server_id"] == registered_server_id

        tools = await client.get(
            f"/mcp/tools?server_id={mcp_api_fixture.server_id}&limit=10",
            headers=_headers(developer, "tool-list"),
        )
        assert tools.status_code == 200
        tools_body = _assert_boundary(tools, "tool-list")
        assert {item["mcp_tool_id"] for item in tools_body["tools"]} == {
            mcp_api_fixture.policy_tool_id,
            mcp_api_fixture.standard_tool_id,
            mcp_api_fixture.high_tool_id,
        }
        assert all("input_schema_effective" not in item for item in tools_body["tools"])

        detail = await client.get(
            f"/mcp/tools/{mcp_api_fixture.policy_tool_id}",
            headers=_headers(developer, "tool-detail"),
        )
        assert detail.status_code == 200
        detail_body = _assert_boundary(detail, "tool-detail")
        assert detail_body["tool"]["status"] == "review_required"
        assert detail_body["tool"]["input_schema_effective"] == _schema()

        updated = await client.patch(
            f"/mcp/tools/{mcp_api_fixture.policy_tool_id}",
            json={
                "expected_policy_revision": 1,
                "status": "active",
                "risk_level": "standard",
                "approval_required": False,
                "allowed_role_ids": [mcp_api_fixture.developer_role_id],
                "result_redaction_pointers": ["/private"],
            },
            headers=_headers(security, "tool-policy"),
        )
        assert updated.status_code == 200
        updated_body = _assert_boundary(updated, "tool-policy")
        assert updated_body["tool"]["status"] == "active"
        assert updated_body["tool"]["policy_revision"] == 2
        assert updated_body["tool"]["allowed_role_ids"] == [
            mcp_api_fixture.developer_role_id
        ]
        assert updated_body["tool"]["result_redaction_pointers"] == ["/private"]

        other = await _login(
            client,
            tenant_slug=OTHER_TENANT_SLUG,
            email="admin@prd082-other.local",
            password=OTHER_PASSWORD,
        )
        cross = await client.get(
            f"/mcp/tools/{mcp_api_fixture.policy_tool_id}",
            headers=_headers(other, "cross-tenant"),
        )
        missing = await client.get(
            "/mcp/tools/mcpt_ffffffffffffffffffffffffffffffff",
            headers=_headers(other, "missing-tool"),
        )
        cross_body = _assert_error(
            cross,
            "cross-tenant",
            status_code=404,
            code="mcp.tool_not_found",
        )
        missing_body = _assert_error(
            missing,
            "missing-tool",
            status_code=404,
            code="mcp.tool_not_found",
        )
        for key in ("code", "message", "suggestion", "diagnosis"):
            assert cross_body[key] == missing_body[key]

    with connect_database(mcp_api_fixture.database_url) as connection:
        server = connection.execute(
            """
            SELECT tenant_id,endpoint_ciphertext,status,state_revision
            FROM mcp_servers WHERE mcp_server_id=?
            """,
            (registered_server_id,),
        ).fetchone()
        assert server is not None
        assert server["tenant_id"] == mcp_api_fixture.tenant_id
        assert server["endpoint_ciphertext"] != endpoint
        assert endpoint not in str(server["endpoint_ciphertext"])
        assert tuple(server[key] for key in ("status", "state_revision")) == (
            "pending_sync",
            1,
        )
        assert connection.execute(
            """
            SELECT COUNT(*) FROM mcp_server_credentials
            WHERE tenant_id=? AND mcp_server_id=? AND status='candidate'
            """,
            (mcp_api_fixture.tenant_id, registered_server_id),
        ).fetchone()[0] == 1
        sync_job = connection.execute(
            """
            SELECT operation.status AS operation_status,job.status AS job_status,
              unixepoch(job.deadline_at)-unixepoch(job.created_at) AS deadline_seconds
            FROM mcp_server_operations operation
            JOIN mcp_server_sync_jobs job
              ON job.tenant_id=operation.tenant_id
             AND job.mcp_server_operation_id=operation.mcp_server_operation_id
            WHERE operation.tenant_id=? AND operation.mcp_server_operation_id=?
              AND operation.status='queued' AND job.status='pending'
            """,
            (mcp_api_fixture.tenant_id, operation_id),
        ).fetchone()
        assert sync_job is not None
        assert tuple(sync_job) == (
            "queued",
            "pending",
            10 + _SYNC_FINALIZE_RESERVE_SECONDS,
        )
        audit = connection.execute(
            """
            SELECT action,before_summary_json,after_summary_json
            FROM audit_logs
            WHERE tenant_id=? AND object_type='mcp_server' AND object_id=?
            """,
            (mcp_api_fixture.tenant_id, registered_server_id),
        ).fetchone()
        assert audit is not None and audit["action"] == "mcp.server_registered"
        assert "endpoint" not in str(audit["before_summary_json"]).lower()
        assert "endpoint" not in str(audit["after_summary_json"]).lower()
        policy = connection.execute(
            """
            SELECT status,policy_revision,result_redaction_pointers_json
            FROM mcp_tools WHERE tenant_id=? AND mcp_tool_id=?
            """,
            (mcp_api_fixture.tenant_id, mcp_api_fixture.policy_tool_id),
        ).fetchone()
        assert tuple(policy) == ("active", 2, '["/private"]')
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_mcp_invoke_persists_safe_summary_and_dlp_block_without_call_event(
    mcp_api_fixture: McpApiFixture,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        developer = await _login(
            client,
            email="developer@prd082.local",
            password=ROLE_PASSWORD,
        )
        invoked = await client.post(
            f"/mcp/tools/{mcp_api_fixture.standard_tool_id}/invoke",
            json={"arguments": {"payload": "ok"}},
            headers=_headers(
                developer,
                "invoke",
                idempotency_key="prd082-standard-invoke",
            ),
        )
        assert invoked.status_code == 202, invoked.json().get("code")
        invoked_body = _assert_boundary(invoked, "invoke")
        assert invoked_body["invocation"]["status"] == "queued"
        assert invoked_body["invocation"]["execution_status"] == "not_called"
        assert invoked_body["invocation"]["tool_call_count"] == 0
        assert "payload" not in invoked.text
        invocation_id = str(invoked_body["invocation"]["mcp_invocation_id"])

        replay = await client.post(
            f"/mcp/tools/{mcp_api_fixture.standard_tool_id}/invoke",
            json={"arguments": {"payload": "ok"}},
            headers=_headers(
                developer,
                "invoke-replay",
                idempotency_key="prd082-standard-invoke",
            ),
        )
        assert replay.status_code == 202
        replay_body = _assert_boundary(replay, "invoke-replay")
        assert replay_body["idempotent_replay"] is True
        assert replay_body["invocation"]["mcp_invocation_id"] == invocation_id

        listed = await client.get(
            f"/mcp/invocations?tool_id={mcp_api_fixture.standard_tool_id}&status=queued",
            headers=_headers(developer, "invocation-list"),
        )
        assert listed.status_code == 200
        listed_body = _assert_boundary(listed, "invocation-list")
        assert [item["mcp_invocation_id"] for item in listed_body["invocations"]] == [
            invocation_id
        ]

        with connect_database(mcp_api_fixture.database_url) as connection:
            block_pattern = "dlp-block-marker"
            connection.execute(
                """
                INSERT INTO dlp_rules(
                  dlp_rule_id,tenant_id,name,rule_type,pattern,pattern_hash,action,
                  scan_scope,status,priority,description,created_by,updated_by,
                  created_at,updated_at
                ) VALUES(?,?,'Controlled input block','keyword',?,?,'block','input',
                         'active',1,'',?,?,?,?)
                """,
                (
                    _id("dlpr_", "api-input-block"),
                    mcp_api_fixture.tenant_id,
                    block_pattern,
                    "sha256:" + _digest("keyword:" + block_pattern),
                    DEVELOPER_ID,
                    DEVELOPER_ID,
                    NOW,
                    NOW,
                ),
            )
            connection.commit()

        blocked = await client.post(
            f"/mcp/tools/{mcp_api_fixture.standard_tool_id}/invoke",
            json={"arguments": {"payload": "dlp-block-marker"}},
            headers=_headers(
                developer,
                "invoke-blocked",
                idempotency_key="prd082-blocked-invoke",
            ),
        )
        _assert_error(
            blocked,
            "invoke-blocked",
            status_code=403,
            code="mcp.arguments_blocked",
            forbidden=("dlp-block-marker", "payload"),
        )

    with connect_database(mcp_api_fixture.database_url) as connection:
        queued = connection.execute(
            """
            SELECT tenant_id,requested_by,status,execution_status,tool_call_count,
                   cost_status,arguments_ciphertext
            FROM mcp_invocations WHERE mcp_invocation_id=?
            """,
            (invocation_id,),
        ).fetchone()
        assert queued is not None
        assert tuple(queued[key] for key in (
            "tenant_id",
            "requested_by",
            "status",
            "execution_status",
            "tool_call_count",
            "cost_status",
        )) == (
            mcp_api_fixture.tenant_id,
            DEVELOPER_ID,
            "queued",
            "not_called",
            0,
            "not_applicable",
        )
        assert "payload" not in str(queued["arguments_ciphertext"])
        blocked_row = connection.execute(
            """
            SELECT status,error_code,input_dlp_status,input_dlp_match_count,
                   execution_status,tool_call_count,cost_status,approval_task_id
            FROM mcp_invocations
            WHERE tenant_id=? AND requested_by=? AND error_code='mcp.arguments_blocked'
            """,
            (mcp_api_fixture.tenant_id, DEVELOPER_ID),
        ).fetchone()
        assert tuple(blocked_row) == (
            "blocked",
            "mcp.arguments_blocked",
            "blocked",
            1,
            "not_called",
            0,
            "not_applicable",
            None,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_invocation_events"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM mcp_invocation_event_outbox"
        ).fetchone()[0] == 0


@pytest.mark.anyio
async def test_mcp_high_risk_invocation_atomically_binds_approval(
    mcp_api_fixture: McpApiFixture,
) -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        developer = await _login(
            client,
            email="developer@prd082.local",
            password=ROLE_PASSWORD,
        )
        created = await client.post(
            f"/mcp/tools/{mcp_api_fixture.high_tool_id}/invoke",
            json={"arguments": {"payload": "ok"}},
            headers=_headers(
                developer,
                "high-invoke",
                idempotency_key="prd082-high-invoke",
            ),
        )
        assert created.status_code == 202, created.json().get("code")
        body = _assert_boundary(created, "high-invoke")
        invocation = body["invocation"]
        assert invocation["status"] == "approval_pending"
        assert invocation["risk_level"] == "high"
        assert invocation["approval_required"] is True
        assert invocation["approval_task_id"] is not None
        assert invocation["execution_status"] == "not_called"
        assert invocation["tool_call_count"] == 0
        assert "payload" not in created.text
        invocation_id = str(invocation["mcp_invocation_id"])
        approval_id = str(invocation["approval_task_id"])

    with connect_database(mcp_api_fixture.database_url) as connection:
        binding = connection.execute(
            """
            SELECT invocation.tenant_id,invocation.requested_by,invocation.status,
                   invocation.approval_task_id,invocation.policy_fingerprint,
                   invocation.server_state_revision,invocation.credential_revision,
                   invocation.schema_revision,invocation.policy_revision,
                   task.source_type,task.source_id,task.target_type,task.target_id,
                   task.target_action,task.target_version,task.target_fingerprint,
                   task.risk_level,task.status,task.required_permission_code,
                   task.created_by_type,task.created_by_id,task.mcp_invocation_id,
                   task.source_request_id,task.source_trace_id
            FROM mcp_invocations invocation
            JOIN approval_tasks task
              ON task.tenant_id=invocation.tenant_id
             AND task.approval_task_id=invocation.approval_task_id
            WHERE invocation.tenant_id=? AND invocation.mcp_invocation_id=?
            """,
            (mcp_api_fixture.tenant_id, invocation_id),
        ).fetchone()
        assert binding is not None
        assert binding["tenant_id"] == mcp_api_fixture.tenant_id
        assert binding["requested_by"] == DEVELOPER_ID
        assert binding["status"] == "approval_pending"
        assert binding["approval_task_id"] == approval_id
        assert binding["source_type"] == "mcp"
        assert binding["source_id"] == invocation_id
        assert binding["target_type"] == "mcp_tool_invocation"
        assert binding["target_id"] == invocation_id
        assert binding["target_action"] == "invoke"
        assert binding["target_version"] == "2:1:1:2"
        assert binding["target_fingerprint"] == binding["policy_fingerprint"]
        assert binding["risk_level"] == "high"
        assert binding["required_permission_code"] == "mcp.approve"
        assert binding["created_by_type"] == "user"
        assert binding["created_by_id"] == DEVELOPER_ID
        assert binding["mcp_invocation_id"] == invocation_id
        assert binding["source_request_id"].startswith("req_")
        assert binding["source_trace_id"].startswith("trc_")
        assert connection.execute(
            """
            SELECT COUNT(*) FROM approval_events event
            JOIN approval_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.approval_event_id=event.approval_event_id
            WHERE event.tenant_id=? AND event.approval_task_id=?
              AND event.event_type='approval.created' AND outbox.status='pending'
            """,
            (mcp_api_fixture.tenant_id, approval_id),
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
