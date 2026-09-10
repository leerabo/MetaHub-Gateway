"""Bounded signed exchanges with a deployment-owned dependency fault proxy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from .contracts import CapacityPermitClaims, encode_signed, execution_digest
from .fault_contracts import FaultLeaseCommand, FaultLeaseInfo
from .settings import CapacityFaultBinding

FINAL_SEQUENCE = 2_147_483_647


class FaultLeaseClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        binding: CapacityFaultBinding,
        execution: CapacityPermitClaims,
    ):
        self.client, self.binding, self.execution = client, binding, execution

    async def command(self, action: str, sequence: int = 1) -> FaultLeaseInfo | None:
        now = datetime.now(UTC)
        command = FaultLeaseCommand(
            execution=self.execution,
            binding_code=self.binding.binding_code,
            action=action,
            sequence=sequence,
            issued_at=now,
            expires_at=now + timedelta(seconds=10),
        )
        token, secret = self.binding.credentials()
        permit = encode_signed(command, secret, domain="fault-lease")
        try:
            async with (
                asyncio.timeout(3),
                self.client.stream(
                    "POST",
                    self.binding.origin + "/internal/capacity/faults",
                    headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
                    json={"permit": permit},
                    timeout=3,
                    follow_redirects=False,
                ) as response,
            ):
                if response.status_code == 404 and action == "status":
                    return None
                if (
                    response.status_code != 200
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                ):
                    raise ValueError("runner.fault_unavailable")
                payload = bytearray()
                async for chunk in response.aiter_raw():
                    if len(payload) + len(chunk) > 16384:
                        raise ValueError("runner.fault_unavailable")
                    payload.extend(chunk)
            info = FaultLeaseInfo.model_validate_json(payload)
            if (
                info.object_id != self.execution.object_id
                or info.execution_sha256 != execution_digest(self.execution)
                or info.binding_code != self.binding.binding_code
                or (
                    action in {"apply", "renew"}
                    and (
                        info.state != "active"
                        or info.sequence != sequence
                        or info.lease_until <= datetime.now(UTC)
                        or info.lease_until > command.expires_at
                    )
                )
                or (action == "release" and info.state == "active")
            ):
                raise ValueError("runner.fault_unavailable")
            return info
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise ValueError("runner.fault_unavailable") from None

    async def release(self) -> FaultLeaseInfo | None:
        # Status first makes a lost final-sequence reply recoverable with a fresh signature.
        try:
            async with asyncio.timeout(8):
                status = await self.command("status")
                if status is None or status.state == "active":
                    status = await self.command("release", FINAL_SEQUENCE)
                while status is not None and status.state != "active":
                    if status.active_connections == 0:
                        return status
                    await asyncio.sleep(0.05)
                    status = await self.command("status")
                return None
        except (ValueError, TimeoutError):
            return None


def fault_http_client():
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=3,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
    )
