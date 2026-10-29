"""Authenticated worker envelope and permit acceptance without remote response bodies."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_mcp_sandbox import app as sandbox_app
from metahub_mcp_sandbox.acl import AclDecision
from metahub_mcp_sandbox.app import create_sandbox_app
from metahub_shared.mcp import (
    McpCryptoOwner,
    McpNetworkPolicyCaps,
    McpSandboxReplayCache,
    McpVersionedSecret,
    compile_mcp_network_acl,
    decrypt_mcp_sandbox_response,
    encrypt_mcp_value,
    sign_mcp_sandbox_envelope,
    validate_mcp_network_policy,
)
from metahub_shared.runtime.mcp_sandbox_config import SandboxConfig

ENCRYPTION_SECRET = "prd083-worker-encryption-" + "e" * 48
INTERNAL_TOKEN = "prd083-worker-internal-" + "i" * 48
ENVELOPE_SECRET = "prd083-worker-envelope-" + "v" * 48
PERMIT_SECRET = "prd083-worker-permit-" + "p" * 48
RESPONSE_SECRET = "prd083-worker-response-" + "r" * 48
TENANT_ID = "tenant_prd083_worker"
SERVER_ID = "mcps_" + "a" * 32
INVOCATION_ID = "mcpi_" + "b" * 32
TOOL_ID = "mcpt_" + "c" * 32
CREDENTIAL_ID = "mcpc_" + "d" * 32
DECISION_ID = "mcpnd_" + "e" * 32
CALL_INTENT_ID = "mcpni_" + "f" * 32


def _config() -> SandboxConfig:
    return SandboxConfig(
        worker_enabled=True,
        encryption_version=1,
        encryption_secret=ENCRYPTION_SECRET,
        internal_token=INTERNAL_TOKEN,
        envelope_secret=ENVELOPE_SECRET,
        permit_secret=PERMIT_SECRET,
        response_key_version=1,
        response_secret_current=RESPONSE_SECRET,
        network_policy_caps=McpNetworkPolicyCaps(),
        network_acl=compile_mcp_network_acl(private_cidrs=("10.24.8.9/32",), allowed_ports=(443,)),
    )


def _envelope(config: SandboxConfig) -> dict[str, object]:
    current = datetime.now(UTC).replace(microsecond=0)
    policy = validate_mcp_network_policy(
        {
            "expected_revision": 2,
            "status": "active",
            "egress_mode": "registered_endpoint_only",
            "connect_timeout_seconds": 5,
            "probe_timeout_seconds": 10,
            "call_timeout_seconds": 30,
            "failure_threshold": 3,
            "open_seconds": 60,
        },
        caps=config.network_policy_caps,
    )
    endpoint = encrypt_mcp_value(
        "https://10.24.8.9/mcp",
        secret=ENCRYPTION_SECRET,
        owner=McpCryptoOwner(TENANT_ID, SERVER_ID, "endpoint", 1),
    )
    credential = encrypt_mcp_value(
        "opaque-test-credential",
        secret=ENCRYPTION_SECRET,
        owner=McpCryptoOwner(TENANT_ID, CREDENTIAL_ID, "bearer", 1),
    )
    return {
        "schema_version": 1,
        "action": "authorize-call",
        "dispatch_id": "mcpd_" + "c" * 32,
        "issued_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (current + timedelta(seconds=9)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant_id": TENANT_ID,
        "mcp_server_id": SERVER_ID,
        "mcp_tool_id": TOOL_ID,
        "operation_id": None,
        "mcp_invocation_id": INVOCATION_ID,
        "mcp_network_decision_id": DECISION_ID,
        "mcp_network_call_intent_id": CALL_INTENT_ID,
        "lease_generation": 1,
        "lease_owner_id": "scheduler-test",
        "lease_token_hmac": "d" * 64,
        "network_policy_revision": 2,
        "network_policy_fingerprint": policy.fingerprint,
        "caps_fingerprint": config.caps_fingerprint,
        "deadline_at": (current + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_id": "req_prd083_worker",
        "endpoint_ciphertext": endpoint,
        "endpoint_hmac_key_version": 1,
        "credential_ciphertext": credential,
        "credential_id": CREDENTIAL_ID,
        "credential_revision": 1,
        "auth_type": "bearer",
        "endpoint_hmac": "e" * 64,
        "tool_name": "safe_tool",
        "schema_revision": 1,
        "tool_policy_revision": 1,
        "tool_policy_fingerprint": "3" * 64,
        "arguments_hmac": "f" * 64,
        "arguments_hmac_key_version": 1,
        "network_policy": policy.to_dict(),
    }


def _expected_response_metadata(envelope: dict[str, object], **extra: object) -> dict[str, object]:
    expected = {
        key: envelope[key]
        for key in (
            "request_id",
            "dispatch_id",
            "action",
            "tenant_id",
            "operation_id",
            "mcp_invocation_id",
            "lease_generation",
            "lease_owner_id",
            "network_policy_revision",
            "network_policy_fingerprint",
            "deadline_at",
        )
    }
    expected.update(extra)
    return expected


def test_authorize_call_returns_encrypted_permit_and_rejects_replay() -> None:
    async def run() -> None:
        config = _config()
        app = create_sandbox_app(config)
        envelope = _envelope(config)
        signed = sign_mcp_sandbox_envelope(envelope, secret=ENVELOPE_SECRET)
        wire = {"payload": signed.payload, "signature": signed.signature}
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            response = await client.post(
                "/internal/mcp-sandbox/authorize-call",
                json=wire,
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert response.status_code == 200
            payload = decrypt_mcp_sandbox_response(
                response.json(),
                keyring=(McpVersionedSecret(1, RESPONSE_SECRET),),
                current_version=1,
                expected_metadata=_expected_response_metadata(
                    envelope, status="succeeded", failure_code=None
                ),
            )
            permit = json.loads(payload)
            assert set(permit) == {"permit", "permit_digest", "permit_expires_at"}
            assert len(permit["permit_digest"]) == 64
            assert "10.24.8.9" not in response.text

            replay = await client.post(
                "/internal/mcp-sandbox/authorize-call",
                json=wire,
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert replay.status_code == 503
            assert replay.json()["code"] == "mcp.sandbox_unavailable"

            ready = await client.get("/health/ready")
            assert ready.json()["active_sessions"] == 0

    asyncio.run(run())


def test_authenticated_network_stage_failure_returns_only_fixed_encrypted_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_with_fixed_stage(*_args: object, **_kwargs: object) -> Any:
        raise sandbox_app.NetworkStageUnavailable("headers_timeout")

    monkeypatch.setattr(sandbox_app, "_execute", fail_with_fixed_stage)

    async def run() -> None:
        config = _config()
        app = create_sandbox_app(config)
        envelope = _envelope(config)
        signed = sign_mcp_sandbox_envelope(envelope, secret=ENVELOPE_SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            response = await client.post(
                "/internal/mcp-sandbox/authorize-call",
                json={"payload": signed.payload, "signature": signed.signature},
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert response.status_code == 200
            plaintext = decrypt_mcp_sandbox_response(
                response.json(),
                keyring=(McpVersionedSecret(1, RESPONSE_SECRET),),
                current_version=1,
                expected_metadata=_expected_response_metadata(
                    envelope,
                    status="failed",
                    failure_code="mcp.server_unavailable",
                    address_count=0,
                ),
            )
            assert json.loads(plaintext) == {"availability_reason": "headers_timeout"}
            ready = await client.get("/health/ready")
            assert ready.json()["active_sessions"] == 0
            assert ready.json()["active_connections"] == 0
            assert ready.json()["owned_tasks"] == 0

    asyncio.run(run())


def test_internal_auth_failure_has_fixed_non_sensitive_response() -> None:
    async def run() -> None:
        config = _config()
        app = create_sandbox_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            response = await client.post(
                "/internal/mcp-sandbox/discover",
                json={"unsafe": "value"},
            )
        assert response.status_code == 503
        assert set(response.json()) == {"code", "request_id"}
        assert "unsafe" not in response.text

    asyncio.run(run())


@pytest.mark.parametrize(
    "reason",
    (
        "connect_unavailable",
        "tls_unavailable",
        "headers_timeout",
        "body_timeout",
        "overall_timeout",
    ),
)
def test_network_stage_classification_is_fixed_and_exception_group_safe(
    reason: str,
) -> None:
    failure = sandbox_app.NetworkStageUnavailable(reason)
    grouped = ExceptionGroup("untrusted-detail", [RuntimeError("hidden"), failure])
    assert sandbox_app._network_stage_reason(grouped) == reason
    assert repr(failure) == "NetworkStageUnavailable('mcp.server_unavailable')"
    assert sandbox_app._network_stage_reason(TimeoutError("hidden")) == "overall_timeout"


def test_oversized_streaming_body_is_rejected_without_parsing() -> None:
    async def run() -> None:
        config = _config()
        app = create_sandbox_app(config)

        async def chunks() -> Any:
            yield b"{" + (b"x" * 200_000)
            yield b"y" * 100_000

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            response = await client.post(
                "/internal/mcp-sandbox/discover",
                content=chunks(),
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
        assert response.status_code == 503
        assert set(response.json()) == {"code", "request_id"}

    asyncio.run(run())


def test_discover_and_call_use_bounded_mock_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {"calls": []}

    @asynccontextmanager
    async def fake_streamable_client(
        url: str,
        *,
        headers: dict[str, str] | None,
        timeout: float,
        sse_read_timeout: float,
        terminate_on_close: bool,
        httpx_client_factory: Any,
    ) -> Any:
        observed["url"] = url
        observed["headers"] = headers
        observed["timeout"] = timeout
        observed["sse_read_timeout"] = sse_read_timeout
        observed["terminate_on_close"] = terminate_on_close
        client = httpx_client_factory(headers=headers)
        observed["transport"] = client._transport
        observed["httpx_timeout"] = client.timeout
        await client.aclose()
        yield object(), object(), lambda: None

    class FakeSession:
        def __init__(self, *_streams: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> SimpleNamespace:
            return SimpleNamespace(
                protocolVersion="2025-06-18",
                capabilities={"tools": {}},
            )

        async def list_tools(self, *, cursor: str | None) -> SimpleNamespace:
            observed["cursor"] = cursor
            return SimpleNamespace(
                tools=[{"name": "safe_tool", "inputSchema": {"type": "object"}}],
                nextCursor=None,
            )

        async def call_tool(
            self, tool_name: str, *, arguments: dict[str, Any]
        ) -> dict[str, object]:
            calls = observed["calls"]
            assert isinstance(calls, list)
            calls.append((tool_name, arguments))
            return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

    import mcp
    from mcp.client import streamable_http

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", fake_streamable_client)

    async def run() -> None:
        config = _config()
        decision = AclDecision(
            scheme="https",
            hostname="mcp.example.test",
            port=443,
            pinned_ip="93.184.216.34",
            address_count=1,
        )
        transports: list[object] = []
        discovered = await sandbox_app._mcp_action(
            "discover",
            endpoint="https://mcp.example.test/rpc",
            bearer=None,
            decision=decision,
            tool_name="",
            arguments=None,
            timeout_seconds=10,
            connect_timeout_seconds=4,
            config=config,
            transports=transports,  # type: ignore[arg-type]
        )
        called = await sandbox_app._mcp_action(
            "call",
            endpoint="https://mcp.example.test/rpc",
            bearer="opaque-test-credential",
            decision=decision,
            tool_name="safe_tool",
            arguments={"value": "opaque-test-argument"},
            timeout_seconds=30,
            connect_timeout_seconds=4,
            config=config,
            transports=transports,  # type: ignore[arg-type]
        )
        assert discovered["protocol_version"] == "2025-06-18"
        assert discovered["tools"] == [{"name": "safe_tool", "inputSchema": {"type": "object"}}]
        assert called["isError"] is False
        assert observed["calls"] == [("safe_tool", {"value": "opaque-test-argument"})]
        assert observed["timeout"] == 30.0
        assert observed["sse_read_timeout"] == 10.0
        assert observed["terminate_on_close"] is True
        assert observed["transport"].__class__.__name__ == "PinnedAsyncTransport"
        assert observed["httpx_timeout"].connect == 4.0
        assert observed["httpx_timeout"].read == 10.0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("close_error", "preserve_result"),
    (
        (TimeoutError(), True),
        (ExceptionGroup("close timeout", [TimeoutError()]), True),
        (ExceptionGroup("mixed close", [TimeoutError(), RuntimeError()]), False),
        (
            BaseExceptionGroup("cancelled close", [TimeoutError(), asyncio.CancelledError()]),
            False,
        ),
    ),
    ids=("direct-timeout", "grouped-timeout", "mixed-error", "mixed-cancellation"),
)
def test_completed_discover_handles_stream_close_timeout_groups(
    monkeypatch: pytest.MonkeyPatch,
    close_error: BaseException,
    preserve_result: bool,
) -> None:
    observed = {"session_closed": False, "stream_closed": False, "client_closed": False}

    @asynccontextmanager
    async def fake_streamable_client(*_args: object, **kwargs: Any) -> Any:
        client = kwargs["httpx_client_factory"]()
        try:
            yield object(), object(), lambda: None
        finally:
            observed["stream_closed"] = True
            await client.aclose()
            observed["client_closed"] = client.is_closed
            raise close_error

    class FakeSession:
        def __init__(self, *_streams: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            observed["session_closed"] = True

        async def initialize(self) -> SimpleNamespace:
            return SimpleNamespace(protocolVersion="2025-06-18", capabilities={"tools": {}})

        async def list_tools(self, *, cursor: str | None) -> SimpleNamespace:
            assert cursor is None
            return SimpleNamespace(tools=[{"name": "safe_tool"}], nextCursor=None)

    import mcp
    from mcp.client import streamable_http

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", fake_streamable_client)

    async def run() -> None:
        async def discover() -> dict[str, object]:
            return await sandbox_app._mcp_action(
                "discover",
                endpoint="https://mcp.example.test/rpc",
                bearer=None,
                decision=AclDecision(
                    scheme="https",
                    hostname="mcp.example.test",
                    port=443,
                    pinned_ip="93.184.216.34",
                    address_count=1,
                ),
                tool_name="",
                arguments=None,
                timeout_seconds=10,
                connect_timeout_seconds=4,
                config=_config(),
                transports=[],
            )

        if not preserve_result:
            with pytest.raises(BaseExceptionGroup):
                await discover()
            return
        result = await discover()
        assert result["protocol_version"] == "2025-06-18"
        assert result["tools"] == [{"name": "safe_tool"}]

    asyncio.run(run())
    assert observed == {
        "session_closed": True,
        "stream_closed": True,
        "client_closed": True,
    }


def test_incomplete_discover_timeout_remains_overall_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {"session_closed": False, "stream_closed": False}

    @asynccontextmanager
    async def fake_streamable_client(*_args: object, **_kwargs: Any) -> Any:
        try:
            yield object(), object(), lambda: None
        finally:
            observed["stream_closed"] = True

    class FakeSession:
        def __init__(self, *_streams: object) -> None:
            pass

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            observed["session_closed"] = True

        async def initialize(self) -> SimpleNamespace:
            raise TimeoutError

    import mcp
    from mcp.client import streamable_http

    monkeypatch.setattr(mcp, "ClientSession", FakeSession)
    monkeypatch.setattr(streamable_http, "streamablehttp_client", fake_streamable_client)

    async def run() -> None:
        with pytest.raises(sandbox_app.NetworkStageUnavailable) as raised:
            await sandbox_app._mcp_action(
                "discover",
                endpoint="https://mcp.example.test/rpc",
                bearer=None,
                decision=AclDecision(
                    scheme="https",
                    hostname="mcp.example.test",
                    port=443,
                    pinned_ip="93.184.216.34",
                    address_count=1,
                ),
                tool_name="",
                arguments=None,
                timeout_seconds=10,
                connect_timeout_seconds=4,
                config=_config(),
                transports=[],
            )
        assert raised.value.reason_code == "overall_timeout"

    asyncio.run(run())
    assert observed == {"session_closed": True, "stream_closed": True}


def test_call_uses_permit_destination_without_second_acl_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acl_calls = 0
    tool_calls = 0

    async def fake_authorize(*_args: object, **_kwargs: object) -> AclDecision:
        nonlocal acl_calls
        acl_calls += 1
        return AclDecision(
            scheme="https",
            hostname="10.24.8.9",
            port=443,
            pinned_ip="10.24.8.9",
            address_count=1,
        )

    async def fake_mcp_action(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal tool_calls
        tool_calls += 1
        return {"isError": False, "content": []}

    monkeypatch.setattr(sandbox_app, "authorize_endpoint", fake_authorize)
    monkeypatch.setattr(sandbox_app, "_mcp_action", fake_mcp_action)

    async def run() -> None:
        config = _config()
        app = create_sandbox_app(config)
        authorize_envelope = _envelope(config)
        authorize_signed = sign_mcp_sandbox_envelope(authorize_envelope, secret=ENVELOPE_SECRET)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            authorize_response = await client.post(
                "/internal/mcp-sandbox/authorize-call",
                json={
                    "payload": authorize_signed.payload,
                    "signature": authorize_signed.signature,
                },
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert authorize_response.status_code == 200
            permit_payload = json.loads(
                decrypt_mcp_sandbox_response(
                    authorize_response.json(),
                    keyring=(McpVersionedSecret(1, RESPONSE_SECRET),),
                    current_version=1,
                    expected_metadata=_expected_response_metadata(
                        authorize_envelope,
                        status="succeeded",
                        failure_code=None,
                    ),
                )
            )

            call_envelope = dict(authorize_envelope)
            call_envelope.update(
                {
                    "action": "call",
                    "dispatch_id": "mcpd_" + "d" * 32,
                    "permit": permit_payload["permit"],
                    "permit_digest": permit_payload["permit_digest"],
                    "arguments_ciphertext": encrypt_mcp_value(
                        json.dumps({"value": "opaque"}),
                        secret=ENCRYPTION_SECRET,
                        owner=McpCryptoOwner(TENANT_ID, INVOCATION_ID, "arguments", 1),
                    ),
                }
            )
            replacement_credential = encrypt_mcp_value(
                "replacement-test-credential",
                secret=ENCRYPTION_SECRET,
                owner=McpCryptoOwner(TENANT_ID, CREDENTIAL_ID, "bearer", 1),
            )
            alternate_credential_id = "mcpc_" + "4" * 32
            alternate_credential = encrypt_mcp_value(
                "alternate-test-credential",
                secret=ENCRYPTION_SECRET,
                owner=McpCryptoOwner(TENANT_ID, alternate_credential_id, "bearer", 1),
            )
            tamper_cases = (
                ("tool_id", {"mcp_tool_id": "mcpt_" + "5" * 32}),
                ("tool_name", {"tool_name": "other_tool"}),
                ("credential_revision", {"credential_revision": 2}),
                (
                    "credential_ciphertext",
                    {"credential_ciphertext": replacement_credential},
                ),
                (
                    "credential_identity",
                    {
                        "credential_id": alternate_credential_id,
                        "credential_ciphertext": alternate_credential,
                    },
                ),
                ("schema_revision", {"schema_revision": 2}),
                ("tool_policy_revision", {"tool_policy_revision": 2}),
                ("tool_policy_fingerprint", {"tool_policy_fingerprint": "6" * 64}),
                (
                    "network_decision",
                    {"mcp_network_decision_id": "mcpnd_" + "7" * 32},
                ),
                (
                    "call_intent",
                    {"mcp_network_call_intent_id": "mcpni_" + "8" * 32},
                ),
                ("permit_digest", {"permit_digest": "0" * 64}),
            )
            for index, (case_name, updates) in enumerate(tamper_cases):
                tampered_envelope = {
                    **call_envelope,
                    **updates,
                    "dispatch_id": "mcpd_" + f"{index + 1:032x}",
                }
                tampered_signed = sign_mcp_sandbox_envelope(
                    tampered_envelope, secret=ENVELOPE_SECRET
                )
                tampered_response = await client.post(
                    "/internal/mcp-sandbox/call",
                    json={
                        "payload": tampered_signed.payload,
                        "signature": tampered_signed.signature,
                    },
                    headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
                )
                assert tampered_response.status_code == 503, case_name
                assert tampered_response.json()["code"] == "mcp.sandbox_unavailable"
                assert tool_calls == 0

            call_signed = sign_mcp_sandbox_envelope(call_envelope, secret=ENVELOPE_SECRET)
            call_response = await client.post(
                "/internal/mcp-sandbox/call",
                json={
                    "payload": call_signed.payload,
                    "signature": call_signed.signature,
                },
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert call_response.status_code == 200
            decrypted = json.loads(
                decrypt_mcp_sandbox_response(
                    call_response.json(),
                    keyring=(McpVersionedSecret(1, RESPONSE_SECRET),),
                    current_version=1,
                    expected_metadata=_expected_response_metadata(
                        call_envelope, status="succeeded", failure_code=None
                    ),
                )
            )
            assert decrypted["isError"] is False

            replay_envelope = dict(call_envelope)
            replay_envelope["dispatch_id"] = "mcpd_" + "e" * 32
            replay_signed = sign_mcp_sandbox_envelope(replay_envelope, secret=ENVELOPE_SECRET)
            replay_response = await client.post(
                "/internal/mcp-sandbox/call",
                json={
                    "payload": replay_signed.payload,
                    "signature": replay_signed.signature,
                },
                headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            )
            assert replay_response.status_code == 503
            assert replay_response.json()["code"] == "mcp.sandbox_unavailable"
        assert acl_calls == 1
        assert tool_calls == 1

    asyncio.run(run())


def test_worker_caps_probe_timeout_to_signed_dispatch_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_timeout: int | None = None

    async def fake_authorize(*_args: object, **_kwargs: object) -> AclDecision:
        return AclDecision(
            scheme="https",
            hostname="10.24.8.9",
            port=443,
            pinned_ip="10.24.8.9",
            address_count=1,
        )

    async def fake_mcp_action(*_args: object, **kwargs: object) -> dict[str, object]:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout_seconds"])
        return {"protocol_version": "2025-06-18", "capabilities": {}, "tools": []}

    monkeypatch.setattr(sandbox_app, "authorize_endpoint", fake_authorize)
    monkeypatch.setattr(sandbox_app, "_mcp_action", fake_mcp_action)

    async def run() -> None:
        config = _config()
        envelope = _envelope(config)
        envelope["deadline_at"] = (datetime.now(UTC) + timedelta(seconds=16)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        await sandbox_app._execute(
            "probe",
            envelope,
            config=config,
            boot_id="boot_prd083_budget",
            replay_cache=McpSandboxReplayCache(maximum_entries=8),
            transports=[],
        )

    asyncio.run(run())
    assert observed_timeout is not None
    assert 8 <= observed_timeout <= 10
    assert observed_timeout < 16


def test_worker_deadline_budget_floors_the_elapsed_subsecond() -> None:
    now = datetime(2026, 8, 16, 8, 0, 0, 900_000, tzinfo=UTC)
    assert (
        sandbox_app._remaining_action_timeout_seconds(
            "2026-08-16T08:00:09Z",
            configured=10,
            cleanup_timeout_seconds=2,
            now=now,
        )
        == 2
    )


def test_worker_fixed_response_reserve_does_not_expand_configured_budget() -> None:
    now = datetime(2026, 8, 16, 8, 0, 0, tzinfo=UTC)
    assert (
        sandbox_app._remaining_action_timeout_seconds(
            "2026-08-16T08:00:11Z",
            configured=5,
            cleanup_timeout_seconds=1,
            now=now,
        )
        == 5
    )


def test_worker_rejects_expired_response_budget_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize_calls = 0

    async def fake_authorize(*_args: object, **_kwargs: object) -> AclDecision:
        nonlocal authorize_calls
        authorize_calls += 1
        raise AssertionError("network must not start")

    monkeypatch.setattr(sandbox_app, "authorize_endpoint", fake_authorize)

    async def run() -> None:
        config = _config()
        envelope = _envelope(config)
        envelope["deadline_at"] = (datetime.now(UTC) + timedelta(seconds=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with pytest.raises(sandbox_app.NetworkStageUnavailable) as raised:
            await sandbox_app._execute(
                "probe",
                envelope,
                config=config,
                boot_id="boot_prd083_expired_budget",
                replay_cache=McpSandboxReplayCache(maximum_entries=8),
                transports=[],
            )
        assert raised.value.reason_code == "overall_timeout"

    asyncio.run(run())
    assert authorize_calls == 0
