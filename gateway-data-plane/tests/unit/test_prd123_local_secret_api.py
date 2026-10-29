from __future__ import annotations

from secrets import token_urlsafe

import httpx
import pytest
from fastapi import FastAPI
from metahub_gateway.local_secrets import (
    LocalAdminAuthError,
    LocalAdminClaims,
    LocalAdminHandoffVerifier,
    LocalAdminSessionSigner,
    LocalManagementPolicy,
    LocalSecretStore,
    ProviderTestResult,
    csrf_sha256,
)
from metahub_gateway.local_secrets.routes import install_local_api_error_handler, router
from metahub_shared.private_gateway import LocalKek, LocalKekKeyring

DEPLOYMENT_ID = "edp_" + "1" * 32
NOW_SECONDS = 1_777_777_777
ORIGIN = "https://console.example.test"
HANDOFF_SECRET = b"local-handoff-test-credential-1234567890"


def _app(tmp_path) -> tuple[FastAPI, LocalAdminSessionSigner]:
    app = FastAPI()
    app.state.local_secret_store = LocalSecretStore(
        tmp_path / "local-secrets.sqlite3",
        deployment_id=DEPLOYMENT_ID,
        keyring=LocalKekKeyring(LocalKek(1, bytes(range(32)))),
    )
    signer = LocalAdminSessionSigner(bytes(reversed(range(32))), now=lambda: NOW_SECONDS)
    app.state.local_admin_session_signer = signer
    app.state.local_admin_handoff_verifier = LocalAdminHandoffVerifier(
        HANDOFF_SECRET, now=lambda: NOW_SECONDS
    )
    app.state.local_management_policy = LocalManagementPolicy.build(
        allowed_origins=(ORIGIN,)
    )
    app.state.local_provider_tester = lambda _reference_id, secret: ProviderTestResult(
        "succeeded",
        ("chat_completions", "models") if len(secret) else (),
        "lt_100ms",
    )
    install_local_api_error_handler(app)
    app.include_router(router)
    return app, signer


def _auth(
    signer: LocalAdminSessionSigner,
    *,
    tenant_id: str = "ten_private_001",
    step_up_expires_at: int = NOW_SECONDS + 300,
) -> tuple[dict[str, str], dict[str, str]]:
    csrf = token_urlsafe(32)
    claims = LocalAdminClaims.model_validate(
        {
            "schema_version": "1",
            "session_id": "las_" + "2" * 32,
            "account_id": "acc_private_001",
            "tenant_id": tenant_id,
            "workspace_id": "wsp_private_001",
            "deployment_id": DEPLOYMENT_ID,
            "role": "tenant_security_admin",
            "csrf_sha256": csrf_sha256(csrf),
            "issued_at": NOW_SECONDS - 30,
            "expires_at": NOW_SECONDS + 600,
            "step_up_expires_at": step_up_expires_at,
        }
    )
    headers = {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-CSRF-Token": csrf,
    }
    cookies = {
        "__Host-metahub-local-session": signer.issue(claims),
        "__Host-metahub-local-csrf": csrf,
    }
    return headers, cookies


def _create_headers(auth_headers: dict[str, str]) -> dict[str, str]:
    return {
        **auth_headers,
        "Content-Type": "application/octet-stream",
        "X-Provider-Connection-ID": "pcn_private_001",
        "X-Local-Secret-Label": "Primary provider credential",
        "X-Local-Secret-Kind": "api_key",
    }


def _handoff_headers(**overrides: str) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-site",
        "Content-Type": "application/octet-stream",
        "X-Local-Account-ID": "acc_private_001",
        "X-Local-Tenant-ID": "ten_private_001",
        "X-Local-Workspace-ID": "wsp_private_001",
        "X-Local-Deployment-ID": DEPLOYMENT_ID,
        **overrides,
    }


@pytest.mark.parametrize(
    "origin",
    (
        "http://localhost.attacker.example",
        "https://user@localhost",
        "https://localhost/path",
        "https://localhost:99999",
    ),
)
def test_local_management_policy_rejects_ambiguous_origins(origin: str) -> None:
    with pytest.raises(LocalAdminAuthError, match="local_admin.policy_invalid"):
        LocalManagementPolicy.build(allowed_origins=(origin,))


@pytest.mark.anyio
async def test_local_handoff_issues_owner_bound_session_and_logout_clears_it(tmp_path) -> None:
    app, _ = _app(tmp_path)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as client:
        handoff = await client.post(
            "/local/admin/session-handoffs",
            content=HANDOFF_SECRET,
            headers=_handoff_headers(),
        )
        assert handoff.status_code == 201
        assert handoff.headers["cache-control"] == "no-store, private"
        assert handoff.json() == {
            "deployment_id": DEPLOYMENT_ID,
            "expires_at": NOW_SECONDS + 600,
        }
        csrf = handoff.headers["x-csrf-token"]
        assert len(csrf) == 43
        set_cookies = handoff.headers.get_list("set-cookie")
        assert len(set_cookies) == 2
        session_cookie = next(
            value for value in set_cookies if value.startswith("__Host-metahub-local-session=")
        )
        csrf_cookie = next(
            value for value in set_cookies if value.startswith("__Host-metahub-local-csrf=")
        )
        assert all(part in session_cookie for part in ("HttpOnly", "Path=/", "Secure"))
        assert "SameSite=strict" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert all(part in csrf_cookie for part in ("Path=/", "Secure", "SameSite=strict"))
        assert HANDOFF_SECRET not in handoff.content

        created = await client.post(
            "/local/secrets",
            content=b"provider-test-key-that-is-never-returned",
            headers=_create_headers(
                {
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-site",
                    "X-CSRF-Token": csrf,
                }
            ),
        )
        assert created.status_code == 201
        references = app.state.local_secret_store.list_references(
            tenant_id="ten_private_001", workspace_id="wsp_private_001"
        )
        assert [item.reference_id for item in references] == [created.json()["reference_id"]]

        logout = await client.delete(
            "/local/admin/session",
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-site"},
        )
        assert logout.status_code == 204
        cleared = logout.headers.get_list("set-cookie")
        assert len(cleared) == 2
        assert all("Max-Age=0" in value for value in cleared)


@pytest.mark.anyio
async def test_local_handoff_rejects_wrong_boundary_and_rate_limits_by_client(tmp_path) -> None:
    app, _ = _app(tmp_path)

    async def handoff(
        *,
        content: bytes = b"wrong-local-handoff-credential-123456789",
        headers: dict[str, str] | None = None,
        client_address: str = "127.0.0.1",
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(client_address, 41000))
        async with httpx.AsyncClient(
            transport=transport, base_url="https://localhost"
        ) as client:
            return await client.post(
                "/local/admin/session-handoffs",
                content=content,
                headers=_handoff_headers() if headers is None else headers,
            )

    remote = await handoff(client_address="10.10.0.7")
    assert remote.status_code == 403
    assert remote.json() == {"code": "local_admin.network_denied"}

    forwarded = await handoff(headers=_handoff_headers(**{"X-Forwarded-For": "127.0.0.1"}))
    assert forwarded.status_code == 400
    assert forwarded.json() == {"code": "local_admin.request_invalid"}

    wrong_origin = await handoff(headers=_handoff_headers(Origin="https://attacker.invalid"))
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"code": "local_admin.origin_invalid"}

    wrong_deployment = await handoff(
        content=HANDOFF_SECRET,
        headers=_handoff_headers(**{"X-Local-Deployment-ID": "edp_" + "2" * 32}),
    )
    assert wrong_deployment.status_code == 401
    assert wrong_deployment.json() == {"code": "local_admin.authentication_failed"}

    for _ in range(4):
        rejected = await handoff()
        assert rejected.status_code == 401
        assert rejected.json() == {"code": "local_admin.authentication_failed"}
    locked = await handoff()
    assert locked.status_code == 429
    assert locked.json() == {"code": "local_admin.rate_limited"}
    still_locked = await handoff(content=HANDOFF_SECRET)
    assert still_locked.status_code == 429
    assert still_locked.json() == {"code": "local_admin.rate_limited"}
    assert HANDOFF_SECRET not in repr(app.state.local_admin_handoff_verifier).encode("ascii")


@pytest.mark.anyio
async def test_local_api_create_test_rotate_delete_never_reads_back_secret(tmp_path) -> None:
    app, signer = _app(tmp_path)
    tested_provider_connection_ids: list[str] = []

    def provider_tester(
        provider_connection_id: str, secret: memoryview
    ) -> ProviderTestResult:
        tested_provider_connection_ids.append(provider_connection_id)
        return ProviderTestResult(
            "succeeded",
            ("chat_completions", "models") if len(secret) else (),
            "lt_100ms",
        )

    app.state.local_provider_tester = provider_tester
    auth_headers, cookies = _auth(signer)
    first = bytearray((index * 13) % 251 for index in range(1024))
    second = bytearray((index * 19) % 251 for index in range(1024))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=cookies
    ) as client:
        created = await client.post(
            "/local/secrets", content=bytes(first), headers=_create_headers(auth_headers)
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store, private"
        reference = created.json()
        assert reference["status"] == "active"
        assert reference["active_version"] == 1
        assert not {
            "value",
            "secret",
            "ciphertext",
            "length",
            "fingerprint",
        }.intersection(reference)
        assert bytes(first) not in created.content

        tested = await client.post(
            "/local/provider-connections/pcn_private_001/test",
            headers=auth_headers,
        )
        assert tested.status_code == 200
        assert tested.json()["outcome"] == "succeeded"
        assert tested.json()["capabilities"] == ["chat_completions", "models"]

        rotated = await client.post(
            f"/local/secrets/{reference['reference_id']}/rotate",
            content=bytes(second),
            headers={
                **auth_headers,
                "Content-Type": "application/octet-stream",
                "If-Match": str(tested.json()["version"] + 1),
            },
        )
        assert rotated.status_code == 200
        assert rotated.json()["active_version"] == 2
        assert bytes(second) not in rotated.content
        assert tested_provider_connection_ids == [
            "pcn_private_001",
            "pcn_private_001",
        ]

        no_readback = await client.get(
            f"/local/secrets/{reference['reference_id']}", headers=auth_headers
        )
        assert no_readback.status_code in {404, 405}

        deleted = await client.delete(
            f"/local/secrets/{reference['reference_id']}",
            headers={**auth_headers, "If-Match": str(rotated.json()["revision"])},
        )
        assert deleted.status_code == 204
        assert deleted.content == b""

    raw = app.state.local_secret_store.path.read_bytes()
    assert bytes(first) not in raw
    assert bytes(second) not in raw


@pytest.mark.anyio
async def test_local_api_requires_loopback_origin_csrf_session_and_step_up(tmp_path) -> None:
    app, signer = _app(tmp_path)
    auth_headers, cookies = _auth(signer)
    body = bytearray(range(1, 65))

    async def post(
        *,
        headers: dict[str, str] | None = None,
        supplied_cookies: dict[str, str] | None = None,
        client_address: str = "127.0.0.1",
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=app, client=(client_address, 41000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://localhost",
            cookies=cookies if supplied_cookies is None else supplied_cookies,
        ) as client:
            return await client.post(
                "/local/secrets",
                content=bytes(body),
                headers=_create_headers(auth_headers) if headers is None else headers,
            )

    remote = await post(client_address="10.10.0.7")
    assert remote.status_code == 403
    assert remote.json() == {"code": "local_admin.network_denied"}

    wrong_origin = await post(
        headers={**_create_headers(auth_headers), "Origin": "https://attacker.invalid"}
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"code": "local_admin.origin_invalid"}

    forged_csrf = await post(
        headers={**_create_headers(auth_headers), "X-CSRF-Token": token_urlsafe(32)}
    )
    assert forged_csrf.status_code == 403
    assert forged_csrf.json() == {"code": "local_admin.csrf_invalid"}

    forwarded = await post(
        headers={**_create_headers(auth_headers), "X-Forwarded-For": "127.0.0.1"}
    )
    assert forwarded.status_code == 400
    assert forwarded.json() == {"code": "local_admin.request_invalid"}

    missing_session = await post(supplied_cookies={})
    assert missing_session.status_code == 401
    assert missing_session.json() == {"code": "local_admin.authentication_failed"}

    expired_headers, expired_cookies = _auth(
        signer, step_up_expires_at=NOW_SECONDS - 1
    )
    expired = await post(
        headers=_create_headers(expired_headers), supplied_cookies=expired_cookies
    )
    assert expired.status_code == 401
    assert expired.json() == {"code": "local_admin.authentication_failed"}


@pytest.mark.anyio
async def test_local_api_owner_is_taken_only_from_signed_session(tmp_path) -> None:
    app, signer = _app(tmp_path)
    first_headers, first_cookies = _auth(signer)
    second_headers, second_cookies = _auth(signer, tenant_id="ten_private_002")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=first_cookies
    ) as first:
        created = await first.post(
            "/local/secrets",
            content=bytes(range(1, 65)),
            headers=_create_headers(first_headers),
        )
        assert created.status_code == 201
        reference = created.json()

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost", cookies=second_cookies
    ) as second:
        denied = await second.post(
            f"/local/secrets/{reference['reference_id']}/rotate",
            content=bytes(range(65, 129)),
            headers={
                **second_headers,
                "Content-Type": "application/octet-stream",
                "If-Match": str(reference["revision"]),
                "X-Tenant-ID": "ten_private_001",
            },
        )
        assert denied.status_code == 404
        assert denied.json() == {"code": "local_secret.not_found"}
