"""Minimal authenticated ASGI surface for the isolated MCP Sandbox Worker."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from metahub_shared.mcp import (
    MCP_SANDBOX_RESPONSE_RESERVE_SECONDS,
    McpContractError,
    McpSandboxReplayCache,
    canonical_json_bytes,
    decrypt_mcp_sandbox_response,
    decrypt_mcp_value_from_keyring,
    encrypt_mcp_sandbox_response,
    issue_mcp_sandbox_permit,
    validate_mcp_network_policy,
    verify_mcp_sandbox_envelope,
    verify_mcp_sandbox_permit,
)
from metahub_shared.runtime.mcp_sandbox_config import (
    SandboxConfig,
    load_mcp_sandbox_config,
)

from .acl import AclDecision, NetworkBlocked, NetworkUnavailable, authorize_endpoint
from .registry import SessionRegistry
from .transport import NetworkStageUnavailable, PinnedAsyncTransport

_MAX_BODY = 262_144
_MAX_PAGES = 10
_MAX_TOOLS = 100


class _SandboxAuthenticationError(RuntimeError):
    pass


def create_sandbox_app(config: SandboxConfig | None = None) -> FastAPI:
    active = config or load_mcp_sandbox_config()
    app = FastAPI(title="MetaHub MCP Sandbox", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = active
    app.state.boot_id = f"boot_{uuid4().hex}"
    app.state.replay_cache = McpSandboxReplayCache(maximum_entries=4096)
    app.state.registry = SessionRegistry(maximum=active.max_active_sessions)

    @app.get("/health/live")
    async def live() -> dict[str, object]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, object]:
        return {
            "status": "ready",
            "caps_fingerprint": active.caps_fingerprint,
            **(await app.state.registry.snapshot()),
        }

    for action in ("discover", "probe", "authorize-call", "call"):
        app.add_api_route(
            f"/internal/mcp-sandbox/{action}",
            _handler(action),
            methods=["POST"],
        )
    return app


def _handler(action: str) -> Any:
    async def handle(request: Request) -> JSONResponse:
        config: SandboxConfig = request.app.state.config
        worker_request_id = f"mcpw_{uuid4().hex}"
        try:
            _authenticate(request, config)
            wire = await _read_wire(request)
            payload = verify_mcp_sandbox_envelope(
                wire["payload"],
                signature=wire["signature"],
                secret=config.envelope_secret,
                replay_cache=request.app.state.replay_cache,
                expected_action=action,
            )
            if payload["caps_fingerprint"] != config.caps_fingerprint:
                raise McpContractError("mcp.sandbox_unavailable")
            dispatch_id = str(payload["dispatch_id"])
            registry: SessionRegistry = request.app.state.registry
            await registry.register(dispatch_id)
            transports: list[PinnedAsyncTransport] = []

            async def close_transports() -> None:
                for transport in reversed(transports):
                    try:
                        await transport.aclose()
                    except Exception:
                        continue

            task: asyncio.Task[tuple[dict[str, object], dict[str, object]]] = asyncio.create_task(
                _execute(
                    action,
                    payload,
                    config=config,
                    boot_id=request.app.state.boot_id,
                    replay_cache=request.app.state.replay_cache,
                    transports=transports,
                )
            )
            await registry.attach(
                dispatch_id,
                task=task,  # type: ignore[arg-type]
                graceful_close=close_transports,
                force_close=close_transports,
                connection=action in {"discover", "probe", "call"},
            )
            started = monotonic()
            try:
                result, counters = await task
                status = "succeeded"
                failure_code = None
            except _SandboxAuthenticationError:
                raise
            except NetworkBlocked as exc:
                result = {
                    "reason_code": exc.reason_code,
                    "address_count": exc.address_count,
                }
                counters = {"address_count": exc.address_count}
                status = "blocked"
                failure_code = "mcp.network_blocked"
            except NetworkUnavailable as exc:
                result = {"availability_reason": exc.reason_code}
                counters = {"address_count": 0}
                status = "failed"
                failure_code = "mcp.server_unavailable"
            except NetworkStageUnavailable as exc:
                result = {"availability_reason": exc.reason_code}
                counters = {"address_count": 0}
                status = "failed"
                failure_code = (
                    "mcp.outcome_unknown" if action == "call" else "mcp.server_unavailable"
                )
            except (McpContractError, ValueError, TypeError):
                result = {}
                counters = {"address_count": 0}
                status = "failed"
                failure_code = (
                    "mcp.outcome_unknown" if action == "call" else "mcp.sandbox_unavailable"
                )
            finally:
                await asyncio.shield(
                    registry.cleanup(
                        dispatch_id,
                        deadline_seconds=float(config.cleanup_timeout_seconds),
                    )
                )
            metadata = _response_metadata(
                payload,
                worker_request_id=worker_request_id,
                status=status,
                failure_code=failure_code,
                counters=counters,
                duration_ms=max(0, int((monotonic() - started) * 1000)),
            )
            encrypted = encrypt_mcp_sandbox_response(
                result,
                metadata=metadata,
                secret=config.response_secret_current,
                key_version=config.response_key_version,
            )
            return JSONResponse(encrypted.to_wire(), status_code=200)
        except Exception:
            return JSONResponse(
                {"code": "mcp.sandbox_unavailable", "request_id": worker_request_id},
                status_code=503,
                headers={"Cache-Control": "no-store, private"},
            )

    return handle


async def _execute(
    action: str,
    envelope: Mapping[str, Any],
    *,
    config: SandboxConfig,
    boot_id: str,
    replay_cache: McpSandboxReplayCache,
    transports: list[PinnedAsyncTransport],
) -> tuple[dict[str, object], dict[str, object]]:
    policy = validate_mcp_network_policy(
        envelope.get("network_policy"), caps=config.network_policy_caps
    )
    endpoint, bearer = _connection_material(envelope, config=config)
    if action == "authorize-call":
        decision = await authorize_endpoint(
            endpoint,
            policy_status=policy.status,
            egress_mode=policy.egress_mode,
            config=config,  # type: ignore[arg-type]
        )
        permit_claims = {
            "schema_version": 1,
            "action": "call",
            "dispatch_id": envelope["dispatch_id"],
            "tenant_id": envelope["tenant_id"],
            "mcp_server_id": envelope["mcp_server_id"],
            "mcp_tool_id": envelope["mcp_tool_id"],
            "mcp_invocation_id": envelope["mcp_invocation_id"],
            "mcp_network_decision_id": envelope["mcp_network_decision_id"],
            "mcp_network_call_intent_id": envelope["mcp_network_call_intent_id"],
            "lease_generation": envelope["lease_generation"],
            "lease_token_hmac": envelope["lease_token_hmac"],
            "network_policy_revision": envelope["network_policy_revision"],
            "network_policy_fingerprint": envelope["network_policy_fingerprint"],
            "endpoint_hmac": envelope["endpoint_hmac"],
            "endpoint_hmac_key_version": envelope["endpoint_hmac_key_version"],
            "credential_revision": envelope["credential_revision"],
            "credential_id": envelope.get("credential_id"),
            "credential_ciphertext_sha256": _claim_digest(envelope.get("credential_ciphertext")),
            "auth_type": envelope["auth_type"],
            "schema_revision": envelope["schema_revision"],
            "tool_policy_revision": envelope["tool_policy_revision"],
            "tool_policy_fingerprint": envelope["tool_policy_fingerprint"],
            "tool_name_sha256": _claim_digest(envelope.get("tool_name")),
            "arguments_hmac": envelope["arguments_hmac"],
            "arguments_hmac_key_version": envelope["arguments_hmac_key_version"],
            "expires_at": _epoch(envelope["deadline_at"]),
            "scheme": decision.scheme,
            "hostname": decision.hostname,
            "port": decision.port,
            "pinned_ip": decision.pinned_ip,
            "address_count": decision.address_count,
        }
        permit = issue_mcp_sandbox_permit(
            permit_claims,
            secret=config.permit_secret,
            boot_id=boot_id,
        )
        return (
            {
                "permit": permit.value,
                "permit_digest": permit.digest,
                "permit_expires_at": permit.expires_at,
            },
            {"address_count": decision.address_count},
        )

    if action == "call":
        try:
            permit_value = envelope.get("permit")
            permit_digest = envelope.get("permit_digest")
            if (
                type(permit_value) is not str
                or type(permit_digest) is not str
                or not hmac.compare_digest(
                    hashlib.sha256(permit_value.encode("ascii", errors="strict")).hexdigest(),
                    permit_digest,
                )
            ):
                raise McpContractError("mcp.sandbox_unavailable")
            claims = verify_mcp_sandbox_permit(
                permit_value,
                secret=config.permit_secret,
                boot_id=boot_id,
                replay_cache=replay_cache,
                expected_claims={
                    "action": "call",
                    "tenant_id": envelope["tenant_id"],
                    "mcp_server_id": envelope["mcp_server_id"],
                    "mcp_tool_id": envelope["mcp_tool_id"],
                    "mcp_invocation_id": envelope["mcp_invocation_id"],
                    "mcp_network_decision_id": envelope["mcp_network_decision_id"],
                    "mcp_network_call_intent_id": envelope["mcp_network_call_intent_id"],
                    "lease_generation": envelope["lease_generation"],
                    "lease_token_hmac": envelope["lease_token_hmac"],
                    "network_policy_revision": envelope["network_policy_revision"],
                    "network_policy_fingerprint": envelope["network_policy_fingerprint"],
                    "endpoint_hmac": envelope["endpoint_hmac"],
                    "endpoint_hmac_key_version": envelope["endpoint_hmac_key_version"],
                    "credential_revision": envelope["credential_revision"],
                    "credential_id": envelope.get("credential_id"),
                    "credential_ciphertext_sha256": _claim_digest(
                        envelope.get("credential_ciphertext")
                    ),
                    "auth_type": envelope["auth_type"],
                    "schema_revision": envelope["schema_revision"],
                    "tool_policy_revision": envelope["tool_policy_revision"],
                    "tool_policy_fingerprint": envelope["tool_policy_fingerprint"],
                    "tool_name_sha256": _claim_digest(envelope.get("tool_name")),
                    "arguments_hmac": envelope["arguments_hmac"],
                    "arguments_hmac_key_version": envelope["arguments_hmac_key_version"],
                },
            )
        except McpContractError:
            raise _SandboxAuthenticationError("mcp.sandbox_unavailable") from None
        decision = _decision_from_permit(endpoint, claims)
        arguments = _decrypt_json(
            envelope.get("arguments_ciphertext"),
            config=config,
            tenant_id=str(envelope["tenant_id"]),
            owner_id=str(envelope["mcp_invocation_id"]),
            kind="arguments",
        )
        if type(arguments) is not dict:
            raise McpContractError("mcp.sandbox_unavailable")
        timeout_seconds = _remaining_action_timeout_seconds(
            envelope.get("deadline_at"),
            configured=policy.call_timeout_seconds,
            cleanup_timeout_seconds=config.cleanup_timeout_seconds,
        )
        result = await _mcp_action(
            "call",
            endpoint=endpoint,
            bearer=bearer,
            decision=decision,
            tool_name=str(envelope.get("tool_name", "")),
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=policy.connect_timeout_seconds,
            config=config,
            transports=transports,
        )
        return result, {"address_count": decision.address_count}

    _remaining_action_timeout_seconds(
        envelope.get("deadline_at"),
        configured=policy.probe_timeout_seconds,
        cleanup_timeout_seconds=config.cleanup_timeout_seconds,
    )
    decision = await authorize_endpoint(
        endpoint,
        policy_status=policy.status,
        egress_mode=policy.egress_mode,
        config=config,  # type: ignore[arg-type]
    )
    timeout_seconds = _remaining_action_timeout_seconds(
        envelope.get("deadline_at"),
        configured=policy.probe_timeout_seconds,
        cleanup_timeout_seconds=config.cleanup_timeout_seconds,
    )
    result = await _mcp_action(
        action,
        endpoint=endpoint,
        bearer=bearer,
        decision=decision,
        tool_name=str(envelope.get("tool_name", "")),
        arguments=None,
        timeout_seconds=timeout_seconds,
        connect_timeout_seconds=policy.connect_timeout_seconds,
        config=config,
        transports=transports,
    )
    return result, {"address_count": decision.address_count}


def _claim_digest(value: object) -> str:
    if value is None:
        encoded = b""
    elif type(value) is str:
        encoded = value.encode("utf-8", errors="strict")
    else:
        raise McpContractError("mcp.sandbox_unavailable")
    return hashlib.sha256(encoded).hexdigest()


async def _run_mcp_action(
    action: str,
    *,
    endpoint: str,
    bearer: str | None,
    decision: AclDecision,
    tool_name: str,
    arguments: dict[str, Any] | None,
    timeout_seconds: int,
    connect_timeout_seconds: int,
    config: SandboxConfig,
    transports: list[PinnedAsyncTransport],
) -> dict[str, object]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except (ImportError, AttributeError):
        raise McpContractError("mcp.sandbox_unavailable") from None

    headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else None

    def client_factory(headers: Any = None, timeout: Any = None, auth: Any = None) -> Any:
        del timeout
        transport = PinnedAsyncTransport(
            scheme=decision.scheme,
            hostname=decision.hostname,
            port=decision.port,
            pinned_ip=decision.pinned_ip,
        )
        transports.append(transport)
        return httpx.AsyncClient(
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(
                float(timeout_seconds),
                connect=float(
                    min(
                        connect_timeout_seconds,
                        timeout_seconds,
                        config.network_policy_caps.connect_timeout_max_seconds,
                        config.tls_timeout_seconds,
                    )
                ),
                read=float(
                    min(
                        timeout_seconds,
                        config.headers_timeout_seconds,
                        config.body_idle_timeout_seconds,
                    )
                ),
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    stack = AsyncExitStack()
    completed_result: dict[str, object] | None = None
    action_error: BaseException | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            streams = await stack.enter_async_context(
                streamablehttp_client(
                    endpoint,
                    headers=headers,
                    timeout=float(timeout_seconds),
                    sse_read_timeout=float(min(timeout_seconds, config.body_idle_timeout_seconds)),
                    terminate_on_close=True,
                    httpx_client_factory=client_factory,
                )
            )
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            initialized = await session.initialize()
            if action in {"discover", "probe"}:
                tools: list[object] = []
                cursor: str | None = None
                for _ in range(_MAX_PAGES):
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(_jsonable(item) for item in page.tools)
                    if len(tools) > _MAX_TOOLS:
                        raise McpContractError("mcp.sandbox_unavailable")
                    cursor = getattr(page, "nextCursor", None) or getattr(page, "next_cursor", None)
                    if cursor is None:
                        break
                else:
                    raise McpContractError("mcp.sandbox_unavailable")
                completed_result = {
                    "protocol_version": str(
                        getattr(initialized, "protocolVersion", None)
                        or getattr(initialized, "protocol_version", "")
                    ),
                    "capabilities": _jsonable(initialized.capabilities),
                    "tools": tools,
                    "requested_tool_name": tool_name if action == "probe" else None,
                }
            else:
                if action != "call" or arguments is None or not tool_name:
                    raise McpContractError("mcp.sandbox_unavailable")
                called = await session.call_tool(tool_name, arguments=arguments)
                completed_result = _jsonable(called)
    except BaseException as exc:
        action_error = exc

    cleanup_error: BaseException | None = None
    try:
        async with asyncio.timeout(config.cleanup_timeout_seconds):
            await stack.aclose()
    except BaseException as exc:
        cleanup_error = exc

    if action_error is not None:
        raise action_error
    if cleanup_error is not None:
        if not (
            completed_result is not None
            and _network_stage_reason(cleanup_error) == "overall_timeout"
            and _timeout_only_exception(cleanup_error)
        ):
            raise cleanup_error
    if completed_result is None:
        raise McpContractError("mcp.sandbox_unavailable")
    return completed_result


async def _mcp_action(
    action: str,
    **kwargs: Any,
) -> dict[str, object]:
    try:
        return await _run_mcp_action(action, **kwargs)
    except NetworkStageUnavailable:
        raise
    except Exception as exc:
        reason = _network_stage_reason(exc)
        if reason is not None:
            raise NetworkStageUnavailable(reason) from None
        raise


def _network_stage_reason(error: BaseException) -> str | None:
    pending = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, NetworkStageUnavailable):
            return current.reason_code
        if isinstance(current, TimeoutError):
            return "overall_timeout"
        if isinstance(current, httpx.ConnectTimeout | httpx.ConnectError):
            return "connect_unavailable"
        if isinstance(current, httpx.ReadTimeout):
            return "body_timeout"
        if isinstance(current, BaseExceptionGroup):
            explicit = _explicit_network_stage_reasons(current)
            if len(explicit) == 1:
                return next(iter(explicit))
            if len(explicit) > 1:
                return None
            reasons = tuple(_network_stage_reason(item) for item in current.exceptions)
            if reasons and all(reason == reasons[0] and reason is not None for reason in reasons):
                return reasons[0]
            continue
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


def _explicit_network_stage_reasons(error: BaseException) -> set[str]:
    pending = [error]
    visited: set[int] = set()
    reasons: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, NetworkStageUnavailable):
            reasons.add(current.reason_code)
            continue
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return reasons


def _timeout_only_exception(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(
            _timeout_only_exception(item) for item in error.exceptions
        )
    return False


def _connection_material(
    envelope: Mapping[str, Any], *, config: SandboxConfig
) -> tuple[str, str | None]:
    endpoint_bytes, _ = decrypt_mcp_value_from_keyring(
        envelope.get("endpoint_ciphertext"),
        keyring=config.encryption_keyring,
        tenant_id=str(envelope["tenant_id"]),
        owner_id=str(envelope["mcp_server_id"]),
        kind="endpoint",
    )
    endpoint = endpoint_bytes.decode("utf-8", errors="strict")
    auth_type = envelope.get("auth_type")
    if auth_type == "none":
        return endpoint, None
    if auth_type != "bearer":
        raise McpContractError("mcp.sandbox_unavailable")
    credential_bytes, _ = decrypt_mcp_value_from_keyring(
        envelope.get("credential_ciphertext"),
        keyring=config.encryption_keyring,
        tenant_id=str(envelope["tenant_id"]),
        owner_id=str(envelope.get("credential_id")),
        kind="bearer",
    )
    bearer = credential_bytes.decode("utf-8", errors="strict")
    if not bearer or "\r" in bearer or "\n" in bearer:
        raise McpContractError("mcp.sandbox_unavailable")
    return endpoint, bearer


def _decrypt_json(
    ciphertext: object,
    *,
    config: SandboxConfig,
    tenant_id: str,
    owner_id: str,
    kind: str,
) -> object:
    plaintext, _ = decrypt_mcp_value_from_keyring(
        ciphertext,
        keyring=config.encryption_keyring,
        tenant_id=tenant_id,
        owner_id=owner_id,
        kind=kind,  # type: ignore[arg-type]
    )
    return json.loads(plaintext)


def _decision_from_permit(endpoint: str, claims: Mapping[str, Any]) -> AclDecision:
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    port = parsed.port or 443
    if (
        hostname is None
        or parsed.scheme != claims.get("scheme")
        or hostname != claims.get("hostname")
        or port != claims.get("port")
        or type(claims.get("pinned_ip")) is not str
        or type(claims.get("address_count")) is not int
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    return AclDecision(
        scheme=parsed.scheme,
        hostname=hostname,
        port=port,
        pinned_ip=str(claims["pinned_ip"]),
        address_count=int(claims["address_count"]),
    )


def _response_metadata(
    envelope: Mapping[str, Any],
    *,
    worker_request_id: str,
    status: str,
    failure_code: str | None,
    counters: Mapping[str, object],
    duration_ms: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": str(envelope.get("request_id") or worker_request_id),
        "dispatch_id": envelope["dispatch_id"],
        "action": envelope["action"],
        "tenant_id": envelope["tenant_id"],
        "operation_id": envelope["operation_id"],
        "mcp_invocation_id": envelope["mcp_invocation_id"],
        "lease_generation": envelope["lease_generation"],
        "lease_owner_id": envelope["lease_owner_id"],
        "network_policy_revision": envelope["network_policy_revision"],
        "network_policy_fingerprint": envelope["network_policy_fingerprint"],
        "status": status,
        "failure_code": failure_code,
        "deadline_at": envelope["deadline_at"],
        "address_count": int(counters.get("address_count", 0)),
        "duration_ms": duration_ms,
    }


def _authenticate(request: Request, config: SandboxConfig) -> None:
    value = request.headers.get("authorization", "")
    expected = f"Bearer {config.internal_token}"
    if not hmac.compare_digest(value.encode(), expected.encode()):
        raise McpContractError("mcp.sandbox_unavailable")


async def _read_wire(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise McpContractError("mcp.sandbox_unavailable") from None
        if declared_length < 1 or declared_length > _MAX_BODY:
            raise McpContractError("mcp.sandbox_unavailable")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY:
            raise McpContractError("mcp.sandbox_unavailable")
        body.extend(chunk)
    if not body:
        raise McpContractError("mcp.sandbox_unavailable")
    if content_length is not None and len(body) != declared_length:
        raise McpContractError("mcp.sandbox_unavailable")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(bytes(body), object_pairs_hook=unique)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise McpContractError("mcp.sandbox_unavailable") from None
    if type(value) is not dict or set(value) != {"payload", "signature"}:
        raise McpContractError("mcp.sandbox_unavailable")
    return value


def _jsonable(value: object) -> dict[str, object]:
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise McpContractError("mcp.sandbox_unavailable")
    encoded = canonical_json_bytes(raw, maximum=1_048_576, code="mcp.sandbox_unavailable")
    decoded = json.loads(encoded)
    if type(decoded) is not dict:
        raise McpContractError("mcp.sandbox_unavailable")
    return decoded


def _epoch(value: object) -> int:
    if type(value) is not str:
        raise McpContractError("mcp.sandbox_unavailable")
    try:
        return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
    except ValueError:
        raise McpContractError("mcp.sandbox_unavailable") from None


def _remaining_action_timeout_seconds(
    deadline_at: object,
    *,
    configured: object,
    cleanup_timeout_seconds: object,
    now: datetime | None = None,
) -> int:
    if type(configured) is not int or configured < 1:
        raise McpContractError("mcp.sandbox_unavailable")
    if (
        type(cleanup_timeout_seconds) is not int
        or not 1 <= cleanup_timeout_seconds < MCP_SANDBOX_RESPONSE_RESERVE_SECONDS
    ):
        raise McpContractError("mcp.sandbox_unavailable")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    remaining = (
        int(_epoch(deadline_at) - current.timestamp()) - MCP_SANDBOX_RESPONSE_RESERVE_SECONDS
    )
    if remaining < 1:
        raise NetworkStageUnavailable("overall_timeout")
    return min(configured, remaining)


__all__ = ["create_sandbox_app", "decrypt_mcp_sandbox_response"]
