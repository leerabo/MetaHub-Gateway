"""mTLS Fleet upgrade lease, execution, and receipt synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from metahub_shared.private_gateway import (
    EdgeUpgradeExecutor,
    UpgradeAuthority,
    UpgradeContractError,
    UpgradeExecutionReport,
    UpgradeHooks,
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
_COMMAND_ID = re.compile(r"^ucm_[0-9a-f]{32}$")
_MAX_COMMAND_BYTES = 98_304
_MAX_RECEIPT_BYTES = 32_768


@dataclass(frozen=True, slots=True)
class UpgradeSyncResult:
    deployment_id: str
    command_id: str | None
    outcome: Literal["idle", "succeeded", "failed", "cancelled", "unreachable"]
    reported: bool


class GatewayUpgradeSynchronizer:
    """Execute upgrade work outside the request path through fixed runtime hooks."""

    def __init__(
        self,
        config: AppConfig,
        *,
        deployment_id: str,
        session_token: Callable[[], str],
        executor: EdgeUpgradeExecutor,
        hooks: UpgradeHooks,
        authority_probe: Callable[[], UpgradeAuthority],
        client: httpx.AsyncClient | None = None,
        certificate_file: Path | None = None,
        private_key_file: Path | None = None,
        ca_certificate_file: Path | None = None,
    ) -> None:
        if (
            _DEPLOYMENT_ID.fullmatch(deployment_id) is None
            or not callable(session_token)
            or not callable(authority_probe)
        ):
            raise UpgradeContractError("edge_upgrade.owner_invalid")
        self._base_url = _control_base_url(config.control_api_base_url)
        self._deployment_id = deployment_id
        self._session_token = session_token
        self._executor = executor
        self._hooks = hooks
        self._authority_probe = authority_probe
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

    async def refresh(self) -> UpgradeSyncResult:
        async with self._lock:
            pending = await self._pull()
            if pending is None:
                return UpgradeSyncResult(self._deployment_id, None, "idle", False)
            if pending == "unreachable":
                return UpgradeSyncResult(self._deployment_id, None, "unreachable", False)
            command_id = str(pending["command"]["command"]["upgrade_command_id"])
            nonce = secrets.token_urlsafe(32)
            leased = await self._mutate(
                command_id,
                action="lease",
                lease_nonce=nonce,
                expected_revision=int(pending["command_revision"]),
            )
            if leased == "unreachable":
                return UpgradeSyncResult(
                    self._deployment_id, command_id, "unreachable", False
                )
            running = await self._mutate(
                command_id,
                action="start",
                lease_nonce=nonce,
                expected_revision=int(leased["command_revision"]),
            )
            if running == "unreachable":
                return UpgradeSyncResult(
                    self._deployment_id, command_id, "unreachable", False
                )
            report = await asyncio.to_thread(
                self._executor.execute,
                running["command"],
                authority_probe=self._authority_probe,
                hooks=self._hooks,
            )
            reported = await self._report(report, nonce)
            return UpgradeSyncResult(
                deployment_id=self._deployment_id,
                command_id=command_id,
                outcome=report.outcome,
                reported=reported,
            )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _pull(self) -> dict[str, object] | Literal["unreachable"] | None:
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/edge/deployments/{self._deployment_id}/upgrades/pending",
                headers=self._headers(),
            ) as response:
                body = await _bounded_body(response, _MAX_COMMAND_BYTES)
                if response.status_code in {502, 503, 504}:
                    return "unreachable"
                if response.status_code == 204:
                    if body:
                        self._failure("edge_upgrade.payload_invalid")
                    return None
                if response.status_code != 200 or _media_type(response) != "application/json":
                    self._failure("edge_upgrade.transport_rejected")
                value = strict_json_object(
                    body, canonical=True, maximum=_MAX_COMMAND_BYTES
                )
                self._validate_command_response(value)
                return value
        except httpx.TransportError as exc:
            if _transport_unreachable(exc):
                return "unreachable"
            self._failure("edge_upgrade.transport_rejected")

    async def _mutate(
        self,
        command_id: str,
        *,
        action: Literal["lease", "start"],
        lease_nonce: str,
        expected_revision: int,
    ) -> dict[str, object] | Literal["unreachable"]:
        payload = {
            "action": action,
            "lease_nonce": lease_nonce,
            "expected_revision": expected_revision,
        }
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/edge/deployments/{self._deployment_id}/upgrades/{command_id}",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_edge_json_bytes(payload),
            ) as response:
                body = await _bounded_body(response, _MAX_COMMAND_BYTES)
                if response.status_code in {502, 503, 504}:
                    return "unreachable"
                if response.status_code != 200 or _media_type(response) != "application/json":
                    self._failure("edge_upgrade.lease_rejected")
                value = strict_json_object(
                    body, canonical=True, maximum=_MAX_COMMAND_BYTES
                )
                self._validate_command_response(value, expected_command_id=command_id)
                return value
        except httpx.TransportError as exc:
            if _transport_unreachable(exc):
                return "unreachable"
            self._failure("edge_upgrade.lease_rejected")

    async def _report(self, report: UpgradeExecutionReport, nonce: str) -> bool:
        payload = {
            "report": report.model_dump(mode="json"),
            "attempt_nonce": hashlib.sha256(
                f"prd127\0{self._deployment_id}\0{report.upgrade_command_id}\0{nonce}".encode(
                    "ascii"
                )
            ).hexdigest(),
        }
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/edge/deployments/{self._deployment_id}/upgrades/"
                f"{report.upgrade_command_id}/report",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_edge_json_bytes(payload),
            ) as response:
                body = await _bounded_body(response, _MAX_RECEIPT_BYTES)
                if response.status_code in {502, 503, 504}:
                    return False
                if (
                    response.status_code not in {200, 201}
                    or _media_type(response) != "application/json"
                ):
                    self._failure("edge_upgrade.receipt_rejected")
                value = strict_json_object(
                    body, canonical=False, maximum=_MAX_RECEIPT_BYTES
                )
                if (
                    set(value)
                    != {
                        "deployment_id",
                        "upgrade_command_id",
                        "outcome",
                        "idempotent_replay",
                        "request_id",
                        "trace_id",
                    }
                    or value.get("deployment_id") != self._deployment_id
                    or value.get("upgrade_command_id") != report.upgrade_command_id
                    or value.get("outcome") != report.outcome
                    or type(value.get("idempotent_replay")) is not bool
                ):
                    self._failure("edge_upgrade.receipt_invalid")
                return True
        except httpx.TransportError as exc:
            if _transport_unreachable(exc):
                return False
            self._failure("edge_upgrade.receipt_rejected")

    def _validate_command_response(
        self, value: dict[str, object], *, expected_command_id: str | None = None
    ) -> None:
        if (
            set(value)
            != {
                "deployment_id",
                "command",
                "command_revision",
                "authority",
                "request_id",
                "trace_id",
            }
            or value.get("deployment_id") != self._deployment_id
            or type(value.get("command")) is not dict
            or type(value.get("command_revision")) is not int
            or value["command_revision"] < 1
            or type(value.get("authority")) is not dict
        ):
            self._failure("edge_upgrade.payload_invalid")
        inner = value["command"].get("command")
        command_id = inner.get("upgrade_command_id") if type(inner) is dict else None
        if (
            type(command_id) is not str
            or _COMMAND_ID.fullmatch(command_id) is None
            or (expected_command_id is not None and command_id != expected_command_id)
        ):
            self._failure("edge_upgrade.payload_invalid")

    def _headers(self) -> dict[str, str]:
        token = self._session_token()
        if type(token) is not str or _EDGE_SESSION.fullmatch(token) is None:
            self._failure("edge_upgrade.session_invalid")
        return {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Edge-Session": token,
        }

    @staticmethod
    def _failure(code: str):
        raise UpgradeContractError(code)


__all__ = ("GatewayUpgradeSynchronizer", "UpgradeSyncResult")
