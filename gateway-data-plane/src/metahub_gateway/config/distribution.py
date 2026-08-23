"""Signed Control-to-Gateway configuration synchronization with bounded restart LKG."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from metahub_shared.config_distribution import (
    ConfigDecision,
    ConfigDistributionError,
    ConfigSnapshotOwner,
    Verifier,
    canonical_json_bytes,
)
from metahub_shared.runtime.config import AppConfig

_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_GATEWAY_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_STRONG_ETAG = re.compile(r'^"[0-9a-f]{64}"$')
_ACK_KEYS = frozenset(
    {
        "ack_id",
        "disposition",
        "observed_generation",
        "applied_generation",
        "lkg_generation",
        "idempotent_replay",
        "request_id",
        "trace_id",
    }
)
_MAX_WIRE_BYTES = 262_144
_MAX_ACK_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class ConfigSyncResult:
    workspace_id: str
    generation: int | None
    status: Literal["applied", "not_modified", "unreachable"]
    acknowledged: bool


@dataclass(slots=True)
class _WorkspaceState:
    owner: ConfigSnapshotOwner
    lock: asyncio.Lock
    lkg_path: Path | None
    wire: dict[str, object] | None


class GatewayConfigSynchronizer:
    """Own per-workspace signed snapshots without putting HTTP on the request hot path."""

    def __init__(
        self,
        config: AppConfig,
        *,
        gateway_identity: str,
        verifier: Verifier,
        client: httpx.AsyncClient | None = None,
        lkg_root: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if _GATEWAY_IDENTITY.fullmatch(gateway_identity) is None:
            raise ConfigDistributionError("config.owner_invalid")
        if not config.internal_config_token:
            raise ConfigDistributionError("config.transport_unavailable")
        self._base_url = _control_base_url(config.control_api_base_url)
        self._gateway_identity = gateway_identity
        self._internal_token = config.internal_config_token
        self._verifier = verifier
        self._now = now or (lambda: datetime.now(UTC))
        self._lkg_root = lkg_root.resolve() if lkg_root is not None else None
        self._states: dict[str, _WorkspaceState] = {}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(float(config.infrastructure_probe_timeout_seconds)),
            follow_redirects=False,
        )
        self._owns_client = client is None

    def register_workspace(
        self, *, workspace_id: str, tenant_id: str, cell_code: str
    ) -> ConfigSnapshotOwner:
        if workspace_id in self._states:
            raise ConfigDistributionError("config.owner_invalid")
        owner = ConfigSnapshotOwner(
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            cell_code=cell_code,
            verifier=self._verifier,
            now=self._now,
        )
        state = _WorkspaceState(
            owner=owner,
            lock=asyncio.Lock(),
            lkg_path=self._lkg_path(workspace_id),
            wire=None,
        )
        self._states[workspace_id] = state
        if state.lkg_path is not None and state.lkg_path.exists():
            self._restore_lkg(state)
        return owner

    def decision(
        self,
        workspace_id: str,
        *,
        required_revocation_watermark: int = 0,
        security_sensitive: bool = True,
    ) -> ConfigDecision:
        return self._state(workspace_id).owner.decision(
            required_revocation_watermark=required_revocation_watermark,
            security_sensitive=security_sensitive,
        )

    async def refresh(self, workspace_id: str) -> ConfigSyncResult:
        state = self._state(workspace_id)
        async with state.lock:
            snapshot = state.owner.snapshot()
            headers = self._headers()
            params: dict[str, str] = {}
            if snapshot is not None:
                params["after_generation"] = str(snapshot.generation)
                headers["If-None-Match"] = f'"{snapshot.manifest_sha256}"'
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base_url}/internal/config-snapshots/{workspace_id}",
                    headers=headers,
                    params=params,
                ) as response:
                    body = await _bounded_body(response, _MAX_WIRE_BYTES)
                    etag = response.headers.get("ETag")
                    if response.status_code == 304:
                        if (
                            body
                            or snapshot is None
                            or state.wire is None
                            or etag != f'"{snapshot.manifest_sha256}"'
                        ):
                            return self._protocol_failure(state, "config.transport_rejected")
                        state.owner.mark_current()
                        self._persist_lkg(state, state.wire, self._now())
                        return ConfigSyncResult(
                            workspace_id, snapshot.generation, "not_modified", True
                        )
                    if response.status_code in {502, 503, 504}:
                        state.owner.mark_unreachable()
                        return ConfigSyncResult(
                            workspace_id,
                            snapshot.generation if snapshot is not None else None,
                            "unreachable",
                            False,
                        )
                    if response.status_code != 200:
                        return self._protocol_failure(state, "config.transport_rejected")
                    if (
                        response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        != "application/json"
                        or etag is None
                        or _STRONG_ETAG.fullmatch(etag) is None
                    ):
                        return self._protocol_failure(state, "config.payload_invalid")
                    wire = _json_object(body, canonical=True)
            except httpx.TransportError:
                state.owner.mark_unreachable()
                return ConfigSyncResult(
                    workspace_id,
                    snapshot.generation if snapshot is not None else None,
                    "unreachable",
                    False,
                )
            except ConfigDistributionError:
                state.owner.mark_integrity_failed()
                self._discard_lkg(state)
                raise

            try:
                changed = state.owner.apply(wire)
                current = state.owner.snapshot()
                if current is None or etag != f'"{current.manifest_sha256}"':
                    raise ConfigDistributionError("config.hash_invalid")
            except ConfigDistributionError:
                self._discard_lkg(state)
                raise
            state.wire = _full_wire(wire, current.manifest)
            self._persist_lkg(state, state.wire, self._now())
            acknowledged = await self._ack(state, current)
            if not acknowledged and state.owner.decision().status == "unavailable":
                self._discard_lkg(state)
                raise ConfigDistributionError("config.ack_invalid")
            return ConfigSyncResult(
                workspace_id,
                current.generation,
                "applied" if changed else "not_modified",
                acknowledged,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _ack(self, state: _WorkspaceState, snapshot) -> bool:
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/internal/config-snapshots/{snapshot.snapshot_id}/acks",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_json_bytes(
                    {
                        "generation": snapshot.generation,
                        "manifest_sha256": snapshot.manifest_sha256,
                        "status": "applied",
                    }
                ),
            ) as response:
                body = await _bounded_body(response, _MAX_ACK_BYTES)
                if response.status_code in {502, 503, 504}:
                    return False
                if response.status_code != 200:
                    state.owner.mark_integrity_failed()
                    return False
                value = _json_object(body, canonical=False)
                if (
                    set(value) != _ACK_KEYS
                    or value.get("disposition") != "accepted"
                    or type(value.get("applied_generation")) is not int
                    or value["applied_generation"] < snapshot.generation
                ):
                    state.owner.mark_integrity_failed()
                    return False
                return True
        except httpx.TransportError:
            return False
        except ConfigDistributionError:
            state.owner.mark_integrity_failed()
            return False

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Gateway-Identity": self._gateway_identity,
            "X-Internal-Token": self._internal_token,
        }

    def _protocol_failure(self, state: _WorkspaceState, code: str):
        state.owner.mark_integrity_failed()
        self._discard_lkg(state)
        raise ConfigDistributionError(code)

    def _lkg_path(self, workspace_id: str) -> Path | None:
        if self._lkg_root is None:
            return None
        if _WORKSPACE_ID.fullmatch(workspace_id) is None:
            raise ConfigDistributionError("config.owner_invalid")
        return self._lkg_root / f"{workspace_id}.json"

    def _persist_lkg(
        self, state: _WorkspaceState, wire: dict[str, object], observed_at: datetime
    ) -> None:
        path = state.lkg_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = canonical_json_bytes(
            {
                "saved_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "version": 1,
                "wire": wire,
            }
        )
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".config-lkg-", dir=path.parent)
            with os.fdopen(descriptor, "wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _restore_lkg(self, state: _WorkspaceState) -> None:
        try:
            raw = state.lkg_path.read_bytes() if state.lkg_path is not None else b""
            if len(raw) > _MAX_WIRE_BYTES:
                raise ValueError
            document = _json_object(raw, canonical=True)
            if set(document) != {"saved_at", "version", "wire"} or document["version"] != 1:
                raise ValueError
            wire = document["wire"]
            state.owner.restore(wire, last_success_at=_time(document["saved_at"]))
            state.wire = dict(wire)
        except Exception:
            state.owner.mark_integrity_failed()
            self._discard_lkg(state)

    @staticmethod
    def _discard_lkg(state: _WorkspaceState) -> None:
        if state.lkg_path is None:
            return
        try:
            state.lkg_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _state(self, workspace_id: str) -> _WorkspaceState:
        try:
            return self._states[workspace_id]
        except KeyError:
            raise ConfigDistributionError("config.owner_invalid") from None


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
        raise ConfigDistributionError("config.payload_invalid")
    declared = response.headers.get("Content-Length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise ConfigDistributionError("config.payload_invalid")
    if response.is_stream_consumed:
        if len(response.content) > limit:
            raise ConfigDistributionError("config.payload_invalid")
        return response.content
    body = bytearray()
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body) > limit:
            raise ConfigDistributionError("config.payload_invalid")
    return bytes(body)


def _json_object(body: bytes, *, canonical: bool) -> dict[str, object]:
    try:
        value = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigDistributionError("config.payload_invalid") from None
    if type(value) is not dict or (canonical and canonical_json_bytes(value) != body):
        raise ConfigDistributionError("config.payload_invalid")
    return value


def _full_wire(wire: dict[str, object], manifest: Mapping[str, object]) -> dict[str, object]:
    if wire.get("kind") == "full":
        return wire
    return {
        key: value
        for key, value in wire.items()
        if key not in {"delta", "from_generation", "from_manifest_sha256"}
    } | {"kind": "full", "manifest": dict(manifest)}


def _control_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigDistributionError("config.transport_unavailable")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


__all__ = ["ConfigSyncResult", "GatewayConfigSynchronizer"]
