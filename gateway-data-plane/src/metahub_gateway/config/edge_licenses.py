"""Background license pull, local activation, capacity decision, and ACK sync."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from metahub_shared.private_gateway import (
    EdgeLicenseStore,
    LicenseContractError,
    LicenseDecision,
    SignedEntitlement,
    canonical_edge_json_bytes,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig

from .edge_desired_state import (
    _bounded_body,
    _control_base_url,
    _media_type,
    _mtls_context,
    _transport_unreachable,
)

_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_EDGE_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_ENTITLEMENT_ID = re.compile(r"^let_[0-9a-f]{32}$")
_ACK_ID = re.compile(r"^lak_[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PULL_KEYS = frozenset(
    {"deployment_id", "entitlement", "server_time", "request_id", "trace_id"}
)
_ACK_KEYS = frozenset(
    {
        "deployment_id",
        "acknowledgement",
        "idempotent_replay",
        "request_id",
        "trace_id",
    }
)
_MAX_LICENSE_BYTES = 65_536
_MAX_ACK_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class LicenseUsageSnapshot:
    active_node_units: int
    active_request_units: int
    denied_count: int
    bucket_started_at: datetime
    bucket_ended_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.active_node_units) is not int
            or not 0 <= self.active_node_units <= 100_000
            or type(self.active_request_units) is not int
            or not 0 <= self.active_request_units <= 10_000_000
            or type(self.denied_count) is not int
            or not 0 <= self.denied_count <= 1_000_000_000
            or _aware(self.bucket_ended_at) <= _aware(self.bucket_started_at)
            or _aware(self.bucket_ended_at) - _aware(self.bucket_started_at)
            > timedelta(days=1)
        ):
            raise LicenseContractError("edge_license.usage_invalid")


@dataclass(frozen=True, slots=True)
class LicenseSyncResult:
    deployment_id: str
    generation: int | None
    status: Literal["applied", "not_modified", "unavailable", "unreachable"]
    acknowledged: bool


@dataclass(frozen=True, slots=True)
class _EntitlementReference:
    entitlement_id: str
    generation: int
    manifest_sha256: str


class GatewayLicenseSynchronizer:
    """Keep Control HTTP and persistence outside the Gateway request hot path."""

    def __init__(
        self,
        config: AppConfig,
        *,
        deployment_id: str,
        session_token: Callable[[], str],
        store: EdgeLicenseStore,
        usage_probe: Callable[[], LicenseUsageSnapshot] | None = None,
        client: httpx.AsyncClient | None = None,
        certificate_file: Path | None = None,
        private_key_file: Path | None = None,
        ca_certificate_file: Path | None = None,
    ) -> None:
        if _DEPLOYMENT_ID.fullmatch(deployment_id) is None or not callable(session_token):
            raise LicenseContractError("edge_license.owner_invalid")
        self._base_url = _control_base_url(config.control_api_base_url)
        self._deployment_id = deployment_id
        self._session_token = session_token
        self._store = store
        self._usage_probe = usage_probe or self._default_usage
        self._lock = asyncio.Lock()
        if client is None:
            context = _mtls_context(
                certificate_file=certificate_file,
                private_key_file=private_key_file,
                ca_certificate_file=ca_certificate_file,
            )
            self._client = httpx.AsyncClient(
                verify=context,
                timeout=httpx.Timeout(float(config.infrastructure_probe_timeout_seconds)),
                follow_redirects=False,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    def decision(self, *, now: datetime | None = None) -> LicenseDecision:
        """Request-path capacity checks are local and never call Control or disk."""

        return self._store.decision(now=now)

    async def refresh(self) -> LicenseSyncResult:
        async with self._lock:
            previous = self._store.snapshot()
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/edge/deployments/{self._deployment_id}/license",
                    headers=self._headers(),
                ) as response:
                    body = await _bounded_body(response, _MAX_LICENSE_BYTES)
                    if response.status_code in {502, 503, 504}:
                        return self._result(previous, "unreachable", False)
                    if response.status_code == 204:
                        if body:
                            self._protocol_failure("edge_license.payload_invalid")
                        return self._result(previous, "unavailable", False)
                    if response.status_code != 200:
                        self._protocol_failure("edge_license.transport_rejected")
                    if _media_type(response) != "application/json":
                        self._protocol_failure("edge_license.payload_invalid")
                    outer = strict_json_object(
                        body, canonical=False, maximum=_MAX_LICENSE_BYTES
                    )
                    signed, server_time = self._validated_wire(
                        outer, response.headers.get("ETag")
                    )
            except httpx.TransportError as exc:
                if _transport_unreachable(exc):
                    return self._result(previous, "unreachable", False)
                self._protocol_failure("edge_license.transport_rejected")

            reference = _reference(signed)
            try:
                changed = self._store.apply(signed, now=server_time)
            except LicenseContractError as exc:
                if reference is not None:
                    await self._ack(
                        reference,
                        disposition="rejected",
                        decision_status=_ack_decision(self._store.decision(now=server_time)),
                        reason_code=exc.code,
                        trusted_wall_time=server_time,
                    )
                raise
            current = self._store.snapshot()
            if current is None:
                self._protocol_failure("edge_license.unavailable")
            decision = self._store.decision(now=server_time)
            acknowledged = await self._ack(
                _EntitlementReference(
                    current.signed.entitlement.entitlement_id,
                    current.signed.entitlement.generation,
                    current.signed.manifest_sha256,
                ),
                disposition="applied",
                decision_status=_ack_decision(decision),
                reason_code=decision.reason_code,
                trusted_wall_time=server_time,
            )
            return LicenseSyncResult(
                deployment_id=self._deployment_id,
                generation=current.signed.entitlement.generation,
                status="applied" if changed else "not_modified",
                acknowledged=acknowledged,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validated_wire(
        self, outer: dict[str, object], etag: str | None
    ) -> tuple[dict[str, object], datetime]:
        if (
            set(outer) != _PULL_KEYS
            or outer.get("deployment_id") != self._deployment_id
            or type(outer.get("entitlement")) is not dict
            or not isinstance(outer.get("server_time"), str)
            or not isinstance(outer.get("request_id"), str)
            or not isinstance(outer.get("trace_id"), str)
        ):
            self._protocol_failure("edge_license.payload_invalid")
        signed = outer["entitlement"]
        manifest_sha256 = signed.get("manifest_sha256")
        entitlement = signed.get("entitlement")
        if (
            _HASH.fullmatch(str(manifest_sha256 or "")) is None
            or etag != f'"{manifest_sha256}"'
            or type(entitlement) is not dict
            or entitlement.get("deployment_id") != self._deployment_id
        ):
            self._protocol_failure("edge_license.hash_invalid")
        try:
            server_time = _parse_time(outer["server_time"])
        except (TypeError, ValueError):
            self._protocol_failure("edge_license.time_invalid")
        return signed, server_time

    async def _ack(
        self,
        reference: _EntitlementReference,
        *,
        disposition: Literal["applied", "rejected"],
        decision_status: str,
        reason_code: str,
        trusted_wall_time: datetime,
    ) -> bool:
        usage = self._usage_probe()
        if not isinstance(usage, LicenseUsageSnapshot):
            raise LicenseContractError("edge_license.usage_invalid")
        payload = {
            "entitlement_id": reference.entitlement_id,
            "generation": reference.generation,
            "disposition": disposition,
            "decision_status": decision_status,
            "reason_code": _safe_reason(reason_code),
            "trusted_wall_time": _time(trusted_wall_time),
            "attempt_nonce": _attempt_nonce(reference, disposition),
            "active_node_units": usage.active_node_units,
            "active_request_units": usage.active_request_units,
            "denied_count": usage.denied_count,
            "bucket_started_at": _time(usage.bucket_started_at),
            "bucket_ended_at": _time(usage.bucket_ended_at),
        }
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/edge/deployments/{self._deployment_id}/license/acks",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_edge_json_bytes(payload),
            ) as response:
                body = await _bounded_body(response, _MAX_ACK_BYTES)
                if response.status_code in {502, 503, 504}:
                    return False
                if response.status_code not in {200, 201}:
                    self._protocol_failure("edge_license.ack_rejected")
                if _media_type(response) != "application/json":
                    self._protocol_failure("edge_license.ack_invalid")
                result = strict_json_object(
                    body, canonical=False, maximum=_MAX_ACK_BYTES
                )
                if not _valid_ack(
                    result, self._deployment_id, reference, disposition
                ):
                    self._protocol_failure("edge_license.ack_invalid")
                return True
        except httpx.TransportError as exc:
            if _transport_unreachable(exc):
                return False
            self._protocol_failure("edge_license.ack_rejected")

    def _headers(self) -> dict[str, str]:
        token = self._session_token()
        if not isinstance(token, str) or _EDGE_SESSION.fullmatch(token) is None:
            self._protocol_failure("edge_license.session_invalid")
        return {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Edge-Session": token,
        }

    def _default_usage(self) -> LicenseUsageSnapshot:
        now = datetime.now(UTC)
        return LicenseUsageSnapshot(
            active_node_units=self._store.active_units("node", now=now),
            active_request_units=self._store.active_units(
                "concurrent_request", now=now
            ),
            denied_count=0,
            bucket_started_at=now - timedelta(minutes=5),
            bucket_ended_at=now,
        )

    def _result(
        self,
        snapshot,
        status: Literal["unavailable", "unreachable"],
        acknowledged: bool,
    ) -> LicenseSyncResult:
        return LicenseSyncResult(
            deployment_id=self._deployment_id,
            generation=(
                snapshot.signed.entitlement.generation if snapshot is not None else None
            ),
            status=status,
            acknowledged=acknowledged,
        )

    @staticmethod
    def _protocol_failure(code: str):
        raise LicenseContractError(code)


def _reference(value: object) -> _EntitlementReference | None:
    try:
        signed = SignedEntitlement.model_validate_json(canonical_edge_json_bytes(value))
    except ValueError:
        return None
    entitlement = signed.entitlement
    return _EntitlementReference(
        entitlement.entitlement_id,
        entitlement.generation,
        signed.manifest_sha256,
    )


def _valid_ack(
    value: dict[str, object],
    deployment_id: str,
    reference: _EntitlementReference,
    disposition: str,
) -> bool:
    acknowledgement = value.get("acknowledgement")
    return bool(
        set(value) == _ACK_KEYS
        and value.get("deployment_id") == deployment_id
        and type(acknowledgement) is dict
        and set(acknowledgement)
        == {
            "acknowledgement_id",
            "generation",
            "disposition",
            "decision_status",
            "reason_code",
            "acknowledged_at",
        }
        and _ACK_ID.fullmatch(str(acknowledgement.get("acknowledgement_id") or ""))
        and acknowledgement.get("generation") == reference.generation
        and acknowledgement.get("disposition") == disposition
        and isinstance(acknowledgement.get("acknowledged_at"), str)
        and type(value.get("idempotent_replay")) is bool
        and isinstance(value.get("request_id"), str)
        and isinstance(value.get("trace_id"), str)
    )


def _ack_decision(decision: LicenseDecision) -> str:
    if decision.status in {
        "current",
        "grace",
        "restricted",
        "revoked",
        "not_yet_valid",
        "clock_rollback",
    }:
        return decision.status
    return "restricted"


def _attempt_nonce(
    reference: _EntitlementReference, disposition: Literal["applied", "rejected"]
) -> str:
    return "ack_" + hashlib.sha256(
        b"metahub.edge-license.ack/v1\0"
        + reference.entitlement_id.encode("ascii")
        + b"\0"
        + str(reference.generation).encode("ascii")
        + b"\0"
        + reference.manifest_sha256.encode("ascii")
        + b"\0"
        + disposition.encode("ascii")
    ).hexdigest()


def _safe_reason(value: str) -> str:
    return value if re.fullmatch(r"^[a-z][a-z0-9_.-]{1,79}$", value) else "edge_license.rejected"


def _parse_time(value: str) -> datetime:
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _time(value: datetime) -> str:
    return _aware(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC)


__all__ = (
    "GatewayLicenseSynchronizer",
    "LicenseSyncResult",
    "LicenseUsageSnapshot",
)
