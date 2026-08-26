"""mTLS Edge desired-state pull, verification, atomic activation, and ACK."""

from __future__ import annotations

import asyncio
import hashlib
import re
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from metahub_shared.private_gateway import (
    EdgeDesiredStateDecision,
    EdgeDesiredStateError,
    EdgeDesiredStateStore,
    VerifiedDesiredState,
    canonical_edge_json_bytes,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig

_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_EDGE_SESSION = re.compile(r"^egs_[A-Za-z0-9_-]{43}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ACK_ID = re.compile(r"^eak_[0-9a-f]{32}$")
_PULL_KEYS = frozenset(
    {
        "deployment_id",
        "generation",
        "envelope_sha256",
        "snapshot",
        "trust_manifest",
        "request_id",
        "trace_id",
    }
)
_ACK_KEYS = frozenset(
    {
        "ack_id",
        "disposition",
        "status",
        "desired_generation",
        "applied_generation",
        "lkg_generation",
        "idempotent_replay",
        "request_id",
        "trace_id",
    }
)
_MAX_PULL_BYTES = 786_432
_MAX_ACK_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class DesiredStateSyncResult:
    deployment_id: str
    generation: int | None
    status: Literal["applied", "not_modified", "unreachable"]
    acknowledged: bool


class GatewayDesiredStateSynchronizer:
    """Keep Control HTTP and disk writes away from the Gateway request hot path."""

    def __init__(
        self,
        config: AppConfig,
        *,
        deployment_id: str,
        session_token: Callable[[], str],
        store: EdgeDesiredStateStore,
        client: httpx.AsyncClient | None = None,
        certificate_file: Path | None = None,
        private_key_file: Path | None = None,
        ca_certificate_file: Path | None = None,
    ) -> None:
        if _DEPLOYMENT_ID.fullmatch(deployment_id) is None or not callable(session_token):
            raise EdgeDesiredStateError("edge_desired_state.owner_invalid")
        self._base_url = _control_base_url(config.control_api_base_url)
        self._deployment_id = deployment_id
        self._session_token = session_token
        self._store = store
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

    def decision(self) -> EdgeDesiredStateDecision:
        return self._store.decision()

    def content(self) -> Mapping[str, object] | None:
        return self._store.content()

    async def refresh(self) -> DesiredStateSyncResult:
        async with self._lock:
            previous = self._store.snapshot()
            headers = self._headers()
            if previous is not None:
                headers["If-None-Match"] = f'"{previous.envelope_sha256}"'
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/edge/deployments/{self._deployment_id}/desired-state",
                    headers=headers,
                ) as response:
                    body = await _bounded_body(response, _MAX_PULL_BYTES)
                    if response.status_code in {502, 503, 504}:
                        self._store.mark_unreachable()
                        return self._unreachable(previous)
                    if response.status_code != 200:
                        self._protocol_failure("edge_desired_state.transport_rejected")
                    if _media_type(response) != "application/json":
                        self._protocol_failure("edge_desired_state.payload_invalid")
                    outer = strict_json_object(
                        body,
                        canonical=True,
                        maximum=_MAX_PULL_BYTES,
                    )
                    wire = self._validated_wire(outer, response.headers.get("ETag"))
            except httpx.TransportError as exc:
                if _transport_unreachable(exc):
                    self._store.mark_unreachable()
                    return self._unreachable(previous)
                self._protocol_failure("edge_desired_state.transport_rejected")
            except EdgeDesiredStateError:
                self._store.mark_integrity_failed()
                raise

            try:
                changed = self._store.apply(wire)
            except EdgeDesiredStateError as exc:
                if exc.code == "edge_desired_state.storage_unavailable":
                    await self._ack_storage_failure(outer, previous)
                raise
            current = self._store.snapshot()
            if current is None:
                self._protocol_failure("edge_desired_state.unavailable")
            acknowledged = await self._ack_applied(current)
            return DesiredStateSyncResult(
                deployment_id=self._deployment_id,
                generation=current.generation,
                status="applied" if changed else "not_modified",
                acknowledged=acknowledged,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validated_wire(
        self, outer: dict[str, object], etag: str | None
    ) -> dict[str, object]:
        if (
            set(outer) != _PULL_KEYS
            or outer.get("deployment_id") != self._deployment_id
            or type(outer.get("generation")) is not int
            or outer["generation"] < 1
            or _HASH.fullmatch(str(outer.get("envelope_sha256") or "")) is None
            or type(outer.get("snapshot")) is not dict
            or type(outer.get("trust_manifest")) is not dict
        ):
            self._protocol_failure("edge_desired_state.payload_invalid")
        envelope_hash = hashlib.sha256(
            canonical_edge_json_bytes(outer["snapshot"])
        ).hexdigest()
        if (
            envelope_hash != outer["envelope_sha256"]
            or etag != f'"{envelope_hash}"'
            or outer["snapshot"].get("deployment_id") != self._deployment_id
            or outer["snapshot"].get("generation") != outer["generation"]
        ):
            self._protocol_failure("edge_desired_state.hash_invalid")
        return {
            "trust_manifest": outer["trust_manifest"],
            "snapshot": outer["snapshot"],
        }

    async def _ack_applied(self, snapshot: VerifiedDesiredState) -> bool:
        return await self._ack(
            snapshot=snapshot,
            status="applied",
            reason_code="applied",
            lkg_generation=snapshot.generation,
            lkg_envelope_sha256=snapshot.envelope_sha256,
        )

    async def _ack_storage_failure(
        self,
        outer: Mapping[str, object],
        previous: VerifiedDesiredState | None,
    ) -> None:
        snapshot = outer.get("snapshot")
        if type(snapshot) is not dict:
            return
        candidate = _snapshot_reference(snapshot, str(outer.get("envelope_sha256") or ""))
        if candidate is None:
            return
        await self._ack(
            snapshot=candidate,
            status="failed",
            reason_code="storage_failed",
            lkg_generation=previous.generation if previous is not None else 0,
            lkg_envelope_sha256=(
                previous.envelope_sha256 if previous is not None else None
            ),
        )

    async def _ack(
        self,
        *,
        snapshot,
        status: Literal["applied", "failed"],
        reason_code: Literal["applied", "storage_failed"],
        lkg_generation: int,
        lkg_envelope_sha256: str | None,
    ) -> bool:
        payload = {
            "signed_snapshot_id": snapshot.snapshot_id,
            "generation": snapshot.generation,
            "envelope_sha256": snapshot.envelope_sha256,
            "status": status,
            "reason_code": reason_code,
            "apply_duration_ms": 0,
            "lkg_generation": lkg_generation,
            "lkg_envelope_sha256": lkg_envelope_sha256,
            "attempt_nonce": _attempt_nonce(
                self._deployment_id,
                snapshot.envelope_sha256,
                status,
            ),
        }
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/edge/deployments/{self._deployment_id}/acks",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_edge_json_bytes(payload),
            ) as response:
                body = await _bounded_body(response, _MAX_ACK_BYTES)
                if response.status_code in {502, 503, 504}:
                    return False
                if response.status_code != 200 or _media_type(response) != "application/json":
                    self._protocol_failure("edge_desired_state.ack_rejected")
                result = strict_json_object(body, canonical=False, maximum=_MAX_ACK_BYTES)
                if not _valid_ack(result, snapshot=snapshot, status=status):
                    self._protocol_failure("edge_desired_state.ack_invalid")
                return True
        except httpx.TransportError as exc:
            if _transport_unreachable(exc):
                return False
            self._protocol_failure("edge_desired_state.ack_rejected")
        except EdgeDesiredStateError:
            self._store.mark_integrity_failed()
            raise

    def _headers(self) -> dict[str, str]:
        token = self._session_token()
        if not isinstance(token, str) or _EDGE_SESSION.fullmatch(token) is None:
            self._protocol_failure("edge_desired_state.session_invalid")
        return {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Edge-Session": token,
        }

    def _unreachable(
        self, snapshot: VerifiedDesiredState | None
    ) -> DesiredStateSyncResult:
        return DesiredStateSyncResult(
            deployment_id=self._deployment_id,
            generation=snapshot.generation if snapshot is not None else None,
            status="unreachable",
            acknowledged=False,
        )

    def _protocol_failure(self, code: str):
        self._store.mark_integrity_failed()
        raise EdgeDesiredStateError(code)


@dataclass(frozen=True, slots=True)
class _SnapshotReference:
    snapshot_id: str
    generation: int
    envelope_sha256: str


def _snapshot_reference(
    snapshot: Mapping[str, object], envelope_sha256: str
) -> _SnapshotReference | None:
    snapshot_id = snapshot.get("snapshot_id")
    generation = snapshot.get("generation")
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r"edsn_[0-9a-f]{32}", snapshot_id) is None
        or type(generation) is not int
        or generation < 1
        or _HASH.fullmatch(envelope_sha256) is None
    ):
        return None
    return _SnapshotReference(snapshot_id, generation, envelope_sha256)


def _valid_ack(
    value: Mapping[str, object],
    *,
    snapshot,
    status: Literal["applied", "failed"],
) -> bool:
    disposition = value.get("disposition")
    accepted = disposition == "accepted"
    applied_floor = snapshot.generation if accepted and status == "applied" else 0
    lkg_floor = snapshot.generation if accepted and status == "applied" else 0
    return bool(
        set(value) == _ACK_KEYS
        and _ACK_ID.fullmatch(str(value.get("ack_id") or ""))
        and disposition in {"accepted", "late"}
        and value.get("status") == status
        and type(value.get("desired_generation")) is int
        and value["desired_generation"] >= snapshot.generation
        and type(value.get("applied_generation")) is int
        and value["applied_generation"] >= applied_floor
        and type(value.get("lkg_generation")) is int
        and value["lkg_generation"] >= lkg_floor
        and type(value.get("idempotent_replay")) is bool
        and isinstance(value.get("request_id"), str)
        and isinstance(value.get("trace_id"), str)
    )


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    declared = response.headers.get("Content-Length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
        return response.content
    body = bytearray()
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body) > limit:
            raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    return bytes(body)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def _attempt_nonce(deployment_id: str, envelope_sha256: str, status: str) -> str:
    digest = hashlib.sha256(
        b"metahub.edge-ack.v1\0"
        + deployment_id.encode("ascii")
        + b"\0"
        + envelope_sha256.encode("ascii")
        + b"\0"
        + status.encode("ascii")
    ).hexdigest()
    return "ack_" + digest[:32]


def _transport_unreachable(exc: httpx.TransportError) -> bool:
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException))


def _control_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EdgeDesiredStateError("edge_desired_state.transport_unavailable")
    path = parsed.path.rstrip("/")
    return f"https://{parsed.netloc}{path}"


def _mtls_context(
    *,
    certificate_file: Path | None,
    private_key_file: Path | None,
    ca_certificate_file: Path | None,
) -> ssl.SSLContext:
    paths = (certificate_file, private_key_file, ca_certificate_file)
    if any(path is None for path in paths):
        raise EdgeDesiredStateError("edge_desired_state.transport_unavailable")
    certificate = Path(certificate_file).resolve()
    private_key = Path(private_key_file).resolve()
    ca_certificate = Path(ca_certificate_file).resolve()
    if not all(path.is_file() for path in (certificate, private_key, ca_certificate)):
        raise EdgeDesiredStateError("edge_desired_state.transport_unavailable")
    try:
        context = ssl.create_default_context(cafile=str(ca_certificate))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
        return context
    except (OSError, ssl.SSLError):
        raise EdgeDesiredStateError("edge_desired_state.transport_unavailable") from None


__all__ = ["DesiredStateSyncResult", "GatewayDesiredStateSynchronizer"]
