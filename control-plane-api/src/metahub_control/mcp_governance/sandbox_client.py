"""Authenticated PRD-083 Scheduler client for the isolated MCP Sandbox Worker.

The client never decrypts tenant endpoints or credentials.  All request secrets,
ciphertexts, permits, and decrypted response payloads are excluded from object
representations and from raised errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import httpx
from metahub_shared.mcp import (
    McpContractError,
    McpNetworkPolicy,
    McpVersionedSecret,
    canonical_json_bytes,
    decrypt_mcp_sandbox_response,
    sign_mcp_sandbox_envelope,
    validate_server_schema_budget,
)
from metahub_shared.runtime.config import AppConfig

from .executor import (
    McpCallResult,
    McpDiscoveryResult,
    McpExecutorError,
    McpProbeResult,
    McpSdkCallResult,
    McpSdkTool,
    _call_result,
    _capabilities,
    _discovered_tool,
)

SandboxAction = Literal["discover", "probe", "authorize-call", "call"]

_PROTOCOL_VERSION = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", re.ASCII)
_MAX_READY_BYTES = 4_096
_MAX_RESPONSE_BYTES = 2_097_152
_FIXED_FAILURE_CODES = frozenset(
    {
        "mcp.network_blocked",
        "mcp.outcome_unknown",
        "mcp.sandbox_unavailable",
        "mcp.server_unavailable",
    }
)


class McpSandboxClientError(RuntimeError):
    """One fixed, non-sensitive Scheduler-to-Worker failure."""

    def __init__(self, code: str = "mcp.sandbox_unavailable") -> None:
        self.code = code if code in _FIXED_FAILURE_CODES else "mcp.sandbox_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class McpSandboxDispatch:
    """Trusted database facts for one short-lived Sandbox request."""

    action: SandboxAction
    tenant_id: str
    server_id: str
    operation_id: str | None
    invocation_id: str | None
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    policy: McpNetworkPolicy = field(repr=False)
    policy_revision: int
    policy_fingerprint: str
    deadline_at: datetime
    request_id: str
    endpoint_ciphertext: str = field(repr=False)
    endpoint_hmac: str
    endpoint_hmac_key_version: int
    auth_type: str
    credential_id: str | None
    credential_ciphertext: str | None = field(default=None, repr=False)
    credential_revision: int | None = None
    tool_name: str | None = None
    arguments_ciphertext: str | None = field(default=None, repr=False)
    arguments_hmac: str | None = None
    arguments_hmac_key_version: int | None = None
    tool_id: str | None = None
    schema_revision: int | None = None
    tool_policy_revision: int | None = None
    tool_policy_fingerprint: str | None = None
    network_decision_id: str | None = None
    call_intent_id: str | None = None
    permit: str | None = field(default=None, repr=False)
    permit_digest: str | None = None


@dataclass(frozen=True, slots=True)
class McpSandboxResponse:
    """Authenticated safe metadata plus an encrypted-response payload."""

    status: Literal["succeeded", "failed", "blocked"]
    failure_code: str | None
    dispatch_id: str
    address_count: int
    duration_ms: int
    request_bytes: int
    response_bytes: int
    payload: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpSandboxPermitResponse:
    """Worker-only opaque permit and persistence-safe digest."""

    permit: str = field(repr=False)
    permit_digest: str
    permit_expires_at: int
    dispatch: McpSandboxResponse


class McpSandboxHttpClient:
    """Bounded synchronous HTTP adapter used only by the Scheduler process."""

    def __init__(
        self,
        config: AppConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if getattr(config, "mcp_executor_mode", None) != "sandbox_http":
            raise McpSandboxClientError()
        self._config = config
        self._base_url = _required_string(config.mcp_sandbox_base_url)
        self._internal_token = _required_string(config.mcp_sandbox_internal_token)
        self._envelope_secret = _required_string(config.mcp_sandbox_envelope_secret)
        current_secret = _required_string(config.mcp_sandbox_response_secret_current)
        current_version = config.mcp_sandbox_response_key_version
        if type(current_version) is not int or current_version < 1:
            raise McpSandboxClientError()
        keyring = [McpVersionedSecret(current_version, current_secret)]
        previous = config.mcp_sandbox_response_secret_previous
        if previous is not None:
            if current_version <= 1:
                raise McpSandboxClientError()
            keyring.append(McpVersionedSecret(current_version - 1, _required_string(previous)))
        self._response_keyring = tuple(keyring)
        self._response_current_version = current_version
        self._transport = transport

    def readiness(self) -> None:
        """Require the Worker to publish the exact shared caps fingerprint."""

        try:
            with self._http_client(timeout=5.0) as client:
                response = client.get("/health/ready")
            if response.status_code != 200 or len(response.content) > _MAX_READY_BYTES:
                raise McpSandboxClientError()
            payload = _json_object(response.content, maximum=_MAX_READY_BYTES)
            if (
                payload.get("status") != "ready"
                or payload.get("caps_fingerprint")
                != self._config.mcp_network_policy_caps().fingerprint
            ):
                raise McpSandboxClientError()
        except McpSandboxClientError:
            raise
        except Exception:
            raise McpSandboxClientError() from None

    def dispatch(self, request: McpSandboxDispatch) -> McpSandboxResponse:
        """Sign, send, authenticate, and decrypt exactly one Worker response."""

        now = datetime.now(UTC).replace(microsecond=0)
        envelope = _envelope(
            request,
            caps_fingerprint=self._config.mcp_network_policy_caps().fingerprint,
            envelope_secret=self._envelope_secret,
            now=now,
        )
        signed = sign_mcp_sandbox_envelope(
            envelope,
            secret=self._envelope_secret,
            now=now,
        )
        wire = {"payload": signed.payload, "signature": signed.signature}
        body = canonical_json_bytes(wire, maximum=262_144, code="mcp.sandbox_unavailable")
        remaining = int((request.deadline_at.astimezone(UTC) - now).total_seconds())
        if remaining < 1:
            raise McpSandboxClientError()
        try:
            with self._http_client(timeout=float(min(remaining + 1, 125))) as client:
                response = client.post(
                    f"/internal/mcp-sandbox/{request.action}",
                    content=body,
                    headers={
                        "Authorization": f"Bearer {self._internal_token}",
                        "Content-Type": "application/json",
                        "Cache-Control": "no-store",
                    },
                )
            if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
                raise McpSandboxClientError()
            response_wire = _json_object(response.content, maximum=_MAX_RESPONSE_BYTES)
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
            plaintext = decrypt_mcp_sandbox_response(
                response_wire,
                keyring=self._response_keyring,
                current_version=self._response_current_version,
                expected_metadata=expected,
            )
            payload = _json_object(plaintext, maximum=self._config.mcp_result_max_bytes)
            status, failure_code, address_count, duration_ms = _safe_response_metadata(
                response_wire
            )
            return McpSandboxResponse(
                status=status,
                failure_code=failure_code,
                dispatch_id=str(envelope["dispatch_id"]),
                address_count=address_count,
                duration_ms=duration_ms,
                request_bytes=min(len(body), 1_048_576),
                response_bytes=min(len(response.content), 1_048_576),
                payload=payload,
            )
        except McpSandboxClientError:
            raise
        except (httpx.HTTPError, McpContractError, UnicodeError, TypeError, ValueError):
            raise McpSandboxClientError() from None
        except Exception:
            raise McpSandboxClientError() from None

    def authorize_call(self, request: McpSandboxDispatch) -> McpSandboxPermitResponse:
        if request.action != "authorize-call":
            raise McpSandboxClientError()
        response = self.dispatch(request)
        if response.status != "succeeded":
            raise McpSandboxClientError(response.failure_code or "mcp.sandbox_unavailable")
        return parse_sandbox_permit(response, deadline_at=request.deadline_at)

    def _http_client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )


def parse_sandbox_discovery(
    response: McpSandboxResponse,
    *,
    config: AppConfig,
) -> McpDiscoveryResult:
    """Convert one authenticated Worker payload into the existing PRD-082 DTO."""

    _require_success(response)
    raw = response.payload
    protocol = raw.get("protocol_version")
    capabilities = raw.get("capabilities")
    tools = raw.get("tools")
    if (
        type(protocol) is not str
        or _PROTOCOL_VERSION.fullmatch(protocol) is None
        or type(capabilities) is not dict
        or type(tools) is not list
        or len(tools) > 100
    ):
        raise McpExecutorError("mcp.sync_failed")
    try:
        discovered = tuple(
            sorted((_parse_tool(item, config=config) for item in tools), key=lambda item: item.name)
        )
        if len({item.name for item in discovered}) != len(discovered):
            raise McpContractError("mcp.schema_invalid")
        validate_server_schema_budget(
            [item.schema_bundle for item in discovered],
            maximum=config.mcp_server_schema_max_bytes,
        )
        capabilities_json, capabilities_sha256 = _capabilities(capabilities)
    except (McpContractError, TypeError, ValueError):
        raise McpExecutorError("mcp.sync_failed") from None
    return McpDiscoveryResult(
        protocol_version=protocol,
        capabilities_json=capabilities_json,
        capabilities_sha256=capabilities_sha256,
        tools=discovered,
        request_bytes=response.request_bytes,
        response_bytes=response.response_bytes,
    )


def parse_sandbox_probe(
    response: McpSandboxResponse,
    *,
    config: AppConfig,
    tool_name: str,
) -> McpProbeResult:
    discovery = parse_sandbox_discovery(response, config=config)
    if response.payload.get("requested_tool_name") != tool_name:
        raise McpExecutorError("mcp.server_unavailable")
    tool = next((item for item in discovery.tools if item.name == tool_name), None)
    if tool is None:
        raise McpExecutorError("mcp.server_unavailable")
    return McpProbeResult(
        protocol_version=discovery.protocol_version,
        capabilities_json=discovery.capabilities_json,
        capabilities_sha256=discovery.capabilities_sha256,
        tool=tool,
        request_bytes=discovery.request_bytes,
        response_bytes=discovery.response_bytes,
    )


def parse_sandbox_call(response: McpSandboxResponse, *, config: AppConfig) -> McpCallResult:
    _require_success(response)
    raw = response.payload
    try:
        normalized = _call_result(
            McpSdkCallResult(
                is_error=raw.get("isError", raw.get("is_error")),
                content=raw.get("content"),
                structured_content=raw.get("structuredContent", raw.get("structured_content")),
            ),
            maximum=config.mcp_result_max_bytes,
        )
    except (McpContractError, TypeError, ValueError):
        raise McpExecutorError("mcp.result_invalid") from None
    return McpCallResult(
        is_error=bool(normalized.is_error),
        content=normalized.content,  # type: ignore[arg-type]
        structured_content=normalized.structured_content,  # type: ignore[arg-type]
        request_bytes=response.request_bytes,
        response_bytes=response.response_bytes,
    )


def _envelope(
    request: McpSandboxDispatch,
    *,
    caps_fingerprint: str,
    envelope_secret: str,
    now: datetime,
) -> dict[str, object]:
    if not isinstance(request, McpSandboxDispatch) or request.deadline_at.tzinfo is None:
        raise McpSandboxClientError()
    deadline = request.deadline_at.astimezone(UTC).replace(microsecond=0)
    expiry = min(now + timedelta(seconds=9), deadline)
    if expiry <= now:
        raise McpSandboxClientError()
    lease_hmac = mcp_sandbox_lease_token_hmac(
        tenant_id=request.tenant_id,
        owner_id=request.operation_id or request.invocation_id or "",
        lease_generation=request.lease_generation,
        lease_token=request.lease_token,
        envelope_secret=envelope_secret,
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "action": request.action,
        "dispatch_id": f"mcpd_{uuid4().hex}",
        "issued_at": _utc(now),
        "expires_at": _utc(expiry),
        "tenant_id": request.tenant_id,
        "mcp_server_id": request.server_id,
        "operation_id": request.operation_id,
        "mcp_invocation_id": request.invocation_id,
        "lease_generation": request.lease_generation,
        "lease_owner_id": request.lease_owner_id,
        "lease_token_hmac": lease_hmac,
        "network_policy_revision": request.policy_revision,
        "network_policy_fingerprint": request.policy_fingerprint,
        "caps_fingerprint": caps_fingerprint,
        "deadline_at": _utc(deadline),
        "request_id": request.request_id,
        "endpoint_ciphertext": request.endpoint_ciphertext,
        "endpoint_hmac": request.endpoint_hmac,
        "endpoint_hmac_key_version": request.endpoint_hmac_key_version,
        "auth_type": request.auth_type,
        "credential_id": request.credential_id,
        "credential_ciphertext": request.credential_ciphertext,
        "credential_revision": request.credential_revision,
        "network_policy": request.policy.to_dict(),
    }
    optional = {
        "tool_name": request.tool_name,
        "arguments_ciphertext": request.arguments_ciphertext,
        "arguments_hmac": request.arguments_hmac,
        "arguments_hmac_key_version": request.arguments_hmac_key_version,
        "mcp_tool_id": request.tool_id,
        "schema_revision": request.schema_revision,
        "tool_policy_revision": request.tool_policy_revision,
        "tool_policy_fingerprint": request.tool_policy_fingerprint,
        "mcp_network_decision_id": request.network_decision_id,
        "mcp_network_call_intent_id": request.call_intent_id,
        "permit": request.permit,
        "permit_digest": request.permit_digest,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _parse_tool(value: object, *, config: AppConfig) -> Any:
    if type(value) is not dict:
        raise McpContractError("mcp.schema_invalid")
    input_schema = value.get("inputSchema", value.get("input_schema"))
    if input_schema is None:
        raise McpContractError("mcp.schema_invalid")
    return _discovered_tool(
        McpSdkTool(
            name=value.get("name"),
            description=value.get("description"),
            input_schema=input_schema,
            output_schema=value.get("outputSchema", value.get("output_schema")),
            annotations=value.get("annotations"),
        ),
        config=config,
    )


def parse_sandbox_permit(
    response: McpSandboxResponse,
    *,
    deadline_at: datetime,
) -> McpSandboxPermitResponse:
    if response.status != "succeeded" or deadline_at.tzinfo is None:
        raise McpSandboxClientError(response.failure_code or "mcp.sandbox_unavailable")
    permit = response.payload.get("permit")
    digest = response.payload.get("permit_digest")
    expires = response.payload.get("permit_expires_at")
    if (
        type(permit) is not str
        or not permit
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not hmac.compare_digest(
            hashlib.sha256(permit.encode("ascii", errors="strict")).hexdigest(), digest
        )
        or type(expires) is not int
        or expires <= int(datetime.now(UTC).timestamp())
        or expires > int(deadline_at.timestamp())
    ):
        raise McpSandboxClientError()
    return McpSandboxPermitResponse(permit, digest, expires, response)


def mcp_sandbox_lease_token_hmac(
    *,
    tenant_id: str,
    owner_id: str,
    lease_generation: int,
    lease_token: str,
    envelope_secret: str,
) -> str:
    try:
        return hmac.new(
            envelope_secret.encode("utf-8", errors="strict"),
            b"metahub/mcp/sandbox/lease/v1\x00"
            + tenant_id.encode("utf-8", errors="strict")
            + b"\x00"
            + owner_id.encode("utf-8", errors="strict")
            + b"\x00"
            + str(lease_generation).encode("ascii")
            + b"\x00"
            + lease_token.encode("ascii", errors="strict"),
            hashlib.sha256,
        ).hexdigest()
    except (UnicodeError, TypeError, ValueError):
        raise McpSandboxClientError() from None


def _require_success(response: McpSandboxResponse) -> None:
    if not isinstance(response, McpSandboxResponse) or response.status != "succeeded":
        raise McpExecutorError(
            response.failure_code
            if isinstance(response, McpSandboxResponse) and response.failure_code is not None
            else "mcp.unavailable"
        )


def _safe_response_metadata(
    value: dict[str, Any],
) -> tuple[Literal["succeeded", "failed", "blocked"], str | None, int, int]:
    status = value.get("status")
    failure_code = value.get("failure_code")
    address_count = value.get("address_count")
    duration_ms = value.get("duration_ms")
    if (
        status not in {"succeeded", "failed", "blocked"}
        or type(status) is not str
        or (failure_code is not None and failure_code not in _FIXED_FAILURE_CODES)
        or (status == "succeeded") != (failure_code is None)
        or (status == "blocked" and failure_code != "mcp.network_blocked")
        or type(address_count) is not int
        or not 0 <= address_count <= 16
        or type(duration_ms) is not int
        or not 0 <= duration_ms <= 300_000
    ):
        raise McpSandboxClientError()
    return status, failure_code, address_count, duration_ms  # type: ignore[return-value]


def _json_object(value: bytes, *, maximum: int) -> dict[str, Any]:
    if not isinstance(value, bytes) or len(value) > maximum:
        raise McpSandboxClientError()

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        decoded = json.loads(value, object_pairs_hook=unique)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise McpSandboxClientError() from None
    if type(decoded) is not dict:
        raise McpSandboxClientError()
    return decoded


def _required_string(value: object) -> str:
    if type(value) is not str or not value:
        raise McpSandboxClientError()
    return value


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "McpSandboxClientError",
    "McpSandboxDispatch",
    "McpSandboxHttpClient",
    "McpSandboxPermitResponse",
    "McpSandboxResponse",
    "parse_sandbox_call",
    "parse_sandbox_discovery",
    "parse_sandbox_permit",
    "parse_sandbox_probe",
    "mcp_sandbox_lease_token_hmac",
]
