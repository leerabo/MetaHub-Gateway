"""PRD-118 Service Account, OAuth, route allowlist, and revocation acceptance."""

from __future__ import annotations

import base64
import math
import secrets
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.machine_identity import service as machine_service
from metahub_control.machine_identity.routes import router as machine_router
from metahub_control.machine_identity.transport import (
    direct_peer_ip,
    verified_mtls_fingerprint,
)
from metahub_control.providers.routes import router as provider_router
from metahub_control.request_logs.routes import router as request_log_router
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    workspace_actor_permissions_in_transaction,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
)
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from starlette.requests import Request

ORIGIN = "http://localhost:5173"
WORKSPACE_TOKEN = "w" * 43
WORKSPACE_CSRF = "c" * 43


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        workspace_session_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        tenant_data_encryption_secret=secrets.token_urlsafe(32),
        database_url=database_url,
        allowed_origins=[ORIGIN],
        control_background_schedulers_enabled=False,
    )


@pytest.fixture(scope="module")
def base_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    database = tmp_path_factory.mktemp("prd118-base") / "base.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url).applied[-1] == "0111"
    apply_seed_data(database_url)
    return database


@pytest.fixture
def runtime(
    base_database: Path,
    tmp_path: Path,
) -> tuple[AppConfig, WorkspaceSessionContext]:
    database = tmp_path / "prd118.sqlite3"
    shutil.copy2(base_database, database)
    config = _config(_url(database))
    return config, _owner_actor(config)


def _owner_actor(config: AppConfig) -> WorkspaceSessionContext:
    with connect_database(config.database_url) as connection:
        row = connection.execute(
            """SELECT membership.workspace_membership_id,membership.workspace_id,
                      membership.tenant_id,membership.account_id,membership.legacy_user_id,
                      tenant.slug,tenant.name
                 FROM workspace_memberships membership
                 JOIN tenants tenant ON tenant.tenant_id=membership.tenant_id
                WHERE membership.role_code='owner' AND membership.status='active'
                ORDER BY membership.workspace_id LIMIT 1"""
        ).fetchone()
        assert row is not None
        connection.execute("BEGIN")
        snapshot = workspace_actor_permissions_in_transaction(
            connection,
            account_id=str(row["account_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
        )
        connection.rollback()
    return WorkspaceSessionContext(
        workspace_session_id="wss_" + "1" * 32,
        global_session_id="gss_" + "1" * 32,
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_id=str(row["workspace_membership_id"]),
        legacy_user_id=str(row["legacy_user_id"]),
        membership_role="owner",
        legacy_role="admin",
        account_email="owner@example.invalid",
        display_name="Workspace Owner",
        tenant_slug=str(row["slug"]),
        tenant_name=str(row["name"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        token=WORKSPACE_TOKEN,
        legacy_authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def _app(config: AppConfig):
    app = create_service_app(
        service_name="control-plane-api",
        title="PRD-118 test",
        extra_cors_allow_headers=("Idempotency-Key", "If-Match", "X-CSRF-Token"),
        cors_allow_credentials=True,
    )
    app.state.config = config

    @app.middleware("http")
    async def shared_machine_read_connection(request: Request, call_next):
        if request.method in {"GET", "HEAD"}:
            with database_request_scope(
                reuse_connections=request.url.path in {"/providers", "/request-logs"}
            ):
                return await call_next(request)
        return await call_next(request)

    app.include_router(machine_router)
    app.include_router(provider_router)
    app.include_router(request_log_router)
    return app


def _workspace_headers(
    *, revision: int | None = None, idempotency_key: str | None = None
) -> dict[str, str]:
    headers = {
        "Cookie": (
            f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
            f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
        ),
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "X-CSRF-Token": WORKSPACE_CSRF,
        "Idempotency-Key": idempotency_key or f"prd118-command-{secrets.token_hex(12)}",
    }
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def _basic(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {encoded}"


async def _provision(
    client: AsyncClient,
    *,
    name: str,
    scope_template: str = "provider_reader",
    scope: str = "provider.read",
    ip_allowlist: list[str] | None = None,
    mtls_fingerprint_sha256: str | None = None,
) -> tuple[dict, dict]:
    body = {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "scope_template": scope_template,
        "scopes": [scope],
        "token_ttl_seconds": 300,
        "ip_allowlist": ip_allowlist or [],
    }
    if mtls_fingerprint_sha256 is not None:
        body["mtls_fingerprint_sha256"] = mtls_fingerprint_sha256
    created = await client.post(
        "/service-accounts",
        headers=_workspace_headers(),
        json=body,
    )
    assert created.status_code == 201, created.text
    account = created.json()["service_account"]
    credential = await client.post(
        "/oauth/clients",
        headers=_workspace_headers(),
        json={"service_account_id": account["service_account_id"]},
    )
    assert credential.status_code == 201, credential.text
    return account, credential.json()


async def _token(
    client: AsyncClient,
    credential: dict,
    *,
    secret: str | None = None,
    scope: str = "provider.read",
    extra_headers: dict[str, str] | None = None,
):
    headers = {
        "Authorization": _basic(
            credential["oauth_client"]["client_id"],
            credential["client_secret"] if secret is None else secret,
        ),
        "Idempotency-Key": f"prd118-token-{secrets.token_hex(12)}",
        **(extra_headers or {}),
    }
    return await client.post(
        "/oauth/token",
        headers=headers,
        data={
            "grant_type": "client_credentials",
            "audience": "metahub-control",
            "scope": scope,
        },
    )


@pytest.mark.anyio
async def test_machine_identity_full_lifecycle_and_allowlist(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    database_url = config.database_url
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    transport = ASGITransport(app=_app(config))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/service-accounts",
            headers=_workspace_headers(),
            json={
                "name": "provider-sync",
                "display_name": "Provider Sync",
                "scope_template": "provider_reader",
                "scopes": ["provider.read"],
                "token_ttl_seconds": 300,
                "ip_allowlist": [],
            },
        )
        assert created.status_code == 201, created.text
        account = created.json()["service_account"]
        assert created.headers["etag"] == '"1"'
        assert created.headers["cache-control"] == "no-store, private"

        client_created = await client.post(
            "/oauth/clients",
            headers=_workspace_headers(),
            json={
                "service_account_id": account["service_account_id"],
                "audience": "metahub-control",
            },
        )
        assert client_created.status_code == 201, client_created.text
        client_payload = client_created.json()
        oauth_client = client_payload["oauth_client"]
        client_secret = client_payload["client_secret"]
        assert client_secret.startswith("mcs_")

        listed = await client.get(
            "/service-accounts",
            headers={"Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}"},
        )
        assert listed.status_code == 200, listed.text
        assert "client_secret" not in listed.text
        assert listed.json()["service_accounts"][0]["oauth_client"][
            "credential_prefix"
        ] == client_secret[:12]

        token_response = await client.post(
            "/oauth/token",
            headers={
                "Authorization": _basic(oauth_client["client_id"], client_secret),
                "Idempotency-Key": "prd118-lifecycle-token-0001",
            },
            data={
                "grant_type": "client_credentials",
                "audience": "metahub-control",
                "scope": "provider.read",
            },
        )
        assert token_response.status_code == 200, token_response.text
        assert token_response.headers["cache-control"] == "no-store"
        access_token = token_response.json()["access_token"]
        machine_headers = {"Authorization": f"Bearer {access_token}"}

        providers = await client.get("/providers", headers=machine_headers)
        assert providers.status_code == 200, providers.text
        assert providers.json()["providers"]

        forbidden_management = await client.get(
            "/service-accounts",
            headers=machine_headers,
        )
        assert forbidden_management.status_code == 400
        forbidden_detail = await client.get(
            "/providers/not-a-provider/keys",
            headers=machine_headers,
        )
        assert forbidden_detail.status_code == 401
        mixed = await client.get(
            "/providers",
            headers={
                **machine_headers,
                "Cookie": f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}",
            },
        )
        assert mixed.status_code == 401

        rotated = await client.post(
            f"/service-accounts/{account['service_account_id']}/rotate",
            headers=_workspace_headers(revision=oauth_client["revision"]),
        )
        assert rotated.status_code == 200, rotated.text
        rotated_payload = rotated.json()
        rotated_secret = rotated_payload["client_secret"]
        assert rotated_secret != client_secret
        assert rotated_payload["oauth_client"]["credential_generation"] == 2
        assert await _status(client, "/providers", machine_headers) == 200

        rotated_token_response = await client.post(
            "/oauth/token",
            headers={
                "Authorization": _basic(oauth_client["client_id"], rotated_secret),
                "Idempotency-Key": "prd118-lifecycle-token-0002",
            },
            data={
                "grant_type": "client_credentials",
                "audience": "metahub-control",
                "scope": "provider.read",
            },
        )
        assert rotated_token_response.status_code == 200, rotated_token_response.text
        rotated_token = rotated_token_response.json()["access_token"]

        revoke_started = time.perf_counter()
        revoked = await client.delete(
            f"/service-accounts/{account['service_account_id']}",
            headers=_workspace_headers(revision=account["revision"]),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["service_account"]["status"] == "revoked"
        assert await _status(client, "/providers", machine_headers) == 401
        assert await _status(
            client,
            "/providers",
            {"Authorization": f"Bearer {rotated_token}"},
        ) == 401
        assert time.perf_counter() - revoke_started < 2.0

    with connect_database(database_url) as connection:
        dump = "\n".join(connection.iterdump())
        assert client_secret not in dump
        assert rotated_secret not in dump
        assert access_token not in dump
        assert rotated_token not in dump
        assert connection.execute(
            "SELECT COUNT(*) FROM machine_identity_event_outbox"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM machine_identity_audits WHERE subject_type='service_account'"
        ).fetchone()[0] >= 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.anyio
async def test_all_machine_writes_replay_without_persisting_plaintext(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    transport = ASGITransport(app=_app(config))
    keys = {
        "account": "prd118-idempotent-account-0001",
        "client": "prd118-idempotent-client-00001",
        "token": "prd118-idempotent-token-000001",
        "rotate": "prd118-idempotent-rotate-0001",
        "revoke": "prd118-idempotent-revoke-0001",
    }
    account_body = {
        "name": "idempotent-reader",
        "display_name": "Idempotent Reader",
        "scope_template": "provider_reader",
        "scopes": ["provider.read"],
        "token_ttl_seconds": 300,
        "ip_allowlist": [],
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing_headers = _workspace_headers()
        missing_headers.pop("Idempotency-Key")
        missing = await client.post(
            "/service-accounts", headers=missing_headers, json=account_body
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "machine_identity.request_invalid"

        account_first = await client.post(
            "/service-accounts",
            headers=_workspace_headers(idempotency_key=keys["account"]),
            json=account_body,
        )
        account_replay = await client.post(
            "/service-accounts",
            headers=_workspace_headers(idempotency_key=keys["account"]),
            json=account_body,
        )
        assert account_first.status_code == account_replay.status_code == 201
        account = account_first.json()["service_account"]
        assert account_replay.json()["service_account"] == account
        conflict = await client.post(
            "/service-accounts",
            headers=_workspace_headers(idempotency_key=keys["account"]),
            json={**account_body, "display_name": "Different Intent"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "machine_identity.idempotency_conflict"

        client_body = {"service_account_id": account["service_account_id"]}
        credential_first = await client.post(
            "/oauth/clients",
            headers=_workspace_headers(idempotency_key=keys["client"]),
            json=client_body,
        )
        credential_replay = await client.post(
            "/oauth/clients",
            headers=_workspace_headers(idempotency_key=keys["client"]),
            json=client_body,
        )
        assert credential_first.status_code == credential_replay.status_code == 201
        credential = credential_first.json()
        assert credential_replay.json()["client_secret"] == credential["client_secret"]
        assert credential_replay.json()["oauth_client"] == credential["oauth_client"]

        token_headers = {
            "Authorization": _basic(
                credential["oauth_client"]["client_id"], credential["client_secret"]
            ),
            "Idempotency-Key": keys["token"],
        }
        token_first = await client.post(
            "/oauth/token",
            headers=token_headers,
            data={
                "grant_type": "client_credentials",
                "audience": "metahub-control",
                "scope": "provider.read",
            },
        )
        token_replay = await client.post(
            "/oauth/token",
            headers=token_headers,
            data={
                "grant_type": "client_credentials",
                "audience": "metahub-control",
                "scope": "provider.read",
            },
        )
        assert token_first.status_code == token_replay.status_code == 200
        assert token_replay.json()["access_token"] == token_first.json()["access_token"]

        rotated_first = await client.post(
            f"/service-accounts/{account['service_account_id']}/rotate",
            headers=_workspace_headers(
                revision=credential["oauth_client"]["revision"],
                idempotency_key=keys["rotate"],
            ),
        )
        rotated_replay = await client.post(
            f"/service-accounts/{account['service_account_id']}/rotate",
            headers=_workspace_headers(
                revision=credential["oauth_client"]["revision"],
                idempotency_key=keys["rotate"],
            ),
        )
        assert rotated_first.status_code == rotated_replay.status_code == 200
        assert rotated_replay.json()["client_secret"] == rotated_first.json()["client_secret"]

        revoked_first = await client.delete(
            f"/service-accounts/{account['service_account_id']}",
            headers=_workspace_headers(
                revision=account["revision"], idempotency_key=keys["revoke"]
            ),
        )
        revoked_replay = await client.delete(
            f"/service-accounts/{account['service_account_id']}",
            headers=_workspace_headers(
                revision=account["revision"], idempotency_key=keys["revoke"]
            ),
        )
        assert revoked_first.status_code == revoked_replay.status_code == 200
        assert revoked_replay.json()["service_account"] == revoked_first.json()[
            "service_account"
        ]

    with connect_database(config.database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM machine_identity_commands"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM service_accounts"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM access_token_grants"
        ).fetchone()[0] == 1
        dump = "\n".join(connection.iterdump())
        for plaintext in (
            *keys.values(),
            credential["client_secret"],
            rotated_first.json()["client_secret"],
            token_first.json()["access_token"],
        ):
            assert plaintext not in dump


async def _status(
    client: AsyncClient,
    path: str,
    headers: dict[str, str],
) -> int:
    return (await client.get(path, headers=headers)).status_code


@pytest.mark.anyio
async def test_oauth_transport_and_observability_scope_are_fail_closed(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    transport = ASGITransport(app=_app(config))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing_origin = await client.post(
            "/service-accounts",
            headers={
                "Cookie": (
                    f"{WORKSPACE_SESSION_COOKIE}={WORKSPACE_TOKEN}; "
                    f"{WORKSPACE_CSRF_COOKIE}={WORKSPACE_CSRF}"
                ),
                "X-CSRF-Token": WORKSPACE_CSRF,
            },
            json={},
        )
        assert missing_origin.status_code == 422
        malformed_token = await client.post(
            "/oauth/token",
            headers={"Authorization": "Basic invalid***", "Content-Type": "application/json"},
            content=b"{}",
        )
        assert malformed_token.status_code == 401

        created = await client.post(
            "/service-accounts",
            headers=_workspace_headers(),
            json={
                "name": "log-reader",
                "display_name": "Log Reader",
                "scope_template": "observability_reader",
                "scopes": ["request_log.read"],
                "token_ttl_seconds": 120,
            },
        )
        account = created.json()["service_account"]
        credential = await client.post(
            "/oauth/clients",
            headers=_workspace_headers(),
            json={"service_account_id": account["service_account_id"]},
        )
        payload = credential.json()
        token = await client.post(
            "/oauth/token",
            headers={
                "Authorization": _basic(
                    payload["oauth_client"]["client_id"],
                    payload["client_secret"],
                ),
                "Content-Type": "application/x-www-form-urlencoded",
                "Idempotency-Key": "prd118-strict-form-token-0001",
            },
            content=(
                b"grant_type=client_credentials&audience=metahub-control&"
                b"scope=request_log.read"
            ),
        )
        assert token.status_code == 200, token.text
        machine_headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        logs = await client.get("/request-logs", headers=machine_headers)
        assert logs.status_code == 200, logs.text
        assert logs.json()["query_event_id"] is None
        saved_filter = await client.get(
            "/request-logs?saved_filter_id=sf_" + "1" * 32,
            headers=machine_headers,
        )
        assert saved_filter.status_code == 422
        detail = await client.get(
            "/request-logs/req_" + "1" * 32,
            headers=machine_headers,
        )
        assert detail.status_code == 401


@pytest.mark.anyio
async def test_constraints_rate_limit_and_live_operator_revocation(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    fingerprint = "a" * 64
    transport = ASGITransport(app=_app(config))

    trusted_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/oauth/token",
            "headers": [
                (b"x-forwarded-for", b"203.0.113.9"),
                (b"x-client-cert-sha256", b"b" * 64),
            ],
            "client": ("192.0.2.25", 443),
            "extensions": {"tls": {"client_cert_sha256": fingerprint}},
        }
    )
    forged_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/oauth/token",
            "headers": [
                (b"x-forwarded-for", b"192.0.2.25"),
                (b"x-client-cert-sha256", fingerprint.encode()),
            ],
            "client": ("127.0.0.1", 443),
        }
    )
    assert direct_peer_ip(trusted_request) == "192.0.2.25"
    assert verified_mtls_fingerprint(trusted_request) == fingerprint
    assert direct_peer_ip(forged_request) == "127.0.0.1"
    assert verified_mtls_fingerprint(forged_request) is None

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _restricted_account, restricted = await _provision(
            client,
            name="network-bound",
            ip_allowlist=["192.0.2.0/24"],
            mtls_fingerprint_sha256=fingerprint,
        )
        forged = await _token(
            client,
            restricted,
            extra_headers={
                "X-Forwarded-For": "192.0.2.25",
                "X-Client-Cert-Sha256": fingerprint,
            },
        )
        assert forged.status_code == 401

        with database_request_scope(reuse_connections=True):
            constrained_token = machine_service.issue_access_token(
                config,
                client_id=restricted["oauth_client"]["client_id"],
                client_secret=restricted["client_secret"],
                idempotency_key="prd118-constraint-token-0001",
                audience="metahub-control",
                scopes=("provider.read",),
                peer_ip="192.0.2.25",
                verified_mtls_fingerprint=fingerprint,
                request_id="req_constraint_issue",
                trace_id="trace_constraint_issue",
            )
        with pytest.raises(ApiError) as wrong_constraint:
            with database_request_scope(reuse_connections=True):
                machine_service.authenticate_machine_request(
                    config,
                    access_token=constrained_token.access_token,
                    method="GET",
                    path="/providers",
                    peer_ip="192.0.2.25",
                    verified_mtls_fingerprint="b" * 64,
                    request_id="req_constraint_denied",
                    trace_id="trace_constraint_denied",
                )
        assert wrong_constraint.value.code == "machine_auth.invalid"
        with database_request_scope(reuse_connections=True):
            restored = machine_service.authenticate_machine_request(
                config,
                access_token=constrained_token.access_token,
                method="GET",
                path="/providers",
                peer_ip="192.0.2.25",
                verified_mtls_fingerprint=fingerprint,
                request_id="req_constraint_ok",
                trace_id="trace_constraint_ok",
            )
        assert restored.workspace_id == actor.workspace_id

        _open_account, open_credential = await _provision(
            client,
            name="rate-limited",
        )
        valid = await _token(client, open_credential)
        assert valid.status_code == 200, valid.text
        access_token = valid.json()["access_token"]
        machine_headers = {"Authorization": f"Bearer {access_token}"}
        assert await _status(client, "/providers", machine_headers) == 200

        wrong_secret = "mcs_" + "x" * 43
        for _index in range(5):
            rejected = await _token(client, open_credential, secret=wrong_secret)
            assert rejected.status_code == 401
            assert rejected.json()["code"] == "oauth.invalid_client"
        blocked = await _token(client, open_credential)
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "oauth.rate_limited"

        with connect_database(config.database_url) as connection:
            connection.execute(
                "UPDATE global_accounts SET status='disabled',updated_at=datetime('now') "
                "WHERE account_id=?",
                (actor.account_id,),
            )
            connection.commit()
        revoked_operator = await client.get("/providers", headers=machine_headers)
        assert revoked_operator.status_code == 401
        assert revoked_operator.json()["code"] == "machine_auth.invalid"

    with connect_database(config.database_url) as connection:
        dump = "\n".join(connection.iterdump())
        assert wrong_secret not in dump
        assert "203.0.113.9" not in dump
        guard = connection.execute(
            "SELECT failure_count,blocked_until FROM oauth_token_attempt_guards"
        ).fetchone()
        assert guard is not None
        assert guard["failure_count"] == 5
        assert guard["blocked_until"] is not None


@pytest.mark.anyio
async def test_concurrent_revoke_has_one_cas_winner(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(config)),
        base_url="http://test",
    ) as client:
        account, _credential = await _provision(client, name="concurrent-revoke")

    barrier = Barrier(2)

    def revoke(index: int) -> str:
        barrier.wait()
        try:
            machine_service.revoke_service_account(
                config,
                session_token=WORKSPACE_TOKEN,
                csrf_token=WORKSPACE_CSRF,
                service_account_id=account["service_account_id"],
                expected_revision=account["revision"],
                idempotency_key=f"prd118-concurrent-revoke-{index}",
                request_id=f"req_revoke_{index}",
                trace_id=f"trace_revoke_{index}",
            )
            return "success"
        except ApiError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(revoke, range(2)))
    assert sorted(outcomes) == ["machine_identity.revision_conflict", "success"]

    with connect_database(config.database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM machine_identity_event_outbox "
            "WHERE event_type='service_account.revoked'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM machine_identity_audits "
            "WHERE action_code='service_account.revoked' AND outcome='success'"
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_warm_token_issue_and_machine_authorization_meet_p95(
    runtime: tuple[AppConfig, WorkspaceSessionContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, actor = runtime
    monkeypatch.setattr(
        machine_service,
        "authenticate_workspace_session_in_transaction",
        lambda *_args, **_kwargs: actor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app(config)),
        base_url="http://test",
    ) as client:
        _account, credential = await _provision(client, name="performance-reader")
        warm = await _token(client, credential)
        assert warm.status_code == 200, warm.text

        issue_samples: list[float] = []
        token_payload = warm.json()
        for _index in range(20):
            started = time.perf_counter()
            issued = await _token(client, credential)
            issue_samples.append(time.perf_counter() - started)
            assert issued.status_code == 200, issued.text
            token_payload = issued.json()

        machine_headers = {"Authorization": f"Bearer {token_payload['access_token']}"}
        assert await _status(client, "/providers", machine_headers) == 200
        auth_samples: list[float] = []
        for _index in range(40):
            started = time.perf_counter()
            with database_request_scope(reuse_connections=True):
                restored = machine_service.authenticate_machine_request(
                    config,
                    access_token=token_payload["access_token"],
                    method="GET",
                    path="/providers",
                    peer_ip="127.0.0.1",
                    verified_mtls_fingerprint=None,
                    request_id=f"req_perf_auth_{_index}",
                    trace_id=f"trace_perf_auth_{_index}",
                )
            auth_samples.append(time.perf_counter() - started)
            assert restored.workspace_id == actor.workspace_id

    issue_p95 = sorted(issue_samples)[math.ceil(len(issue_samples) * 0.95) - 1]
    auth_p95 = sorted(auth_samples)[math.ceil(len(auth_samples) * 0.95) - 1]
    assert issue_p95 < 0.300, issue_samples
    assert auth_p95 < 0.020, auth_samples
