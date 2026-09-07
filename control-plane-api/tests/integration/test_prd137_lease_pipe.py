"""Real local process pipes; no Docker, deployed guest or isolation proof."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metahub_assurance_runner.lease_channel import (
    LeaseBinding,
    StreamLeaseTransport,
    SupervisorLeaseSession,
)
from metahub_control.security_assurance.leases import AssuranceLease

ROOT = Path(__file__).resolve().parents[3]
PEER = ROOT / "control-plane-api/tests/fixtures/prd137_lease_peer.py"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["normal", "revoked", "disconnect", "invalid", "replay"])
async def test_real_subprocess_lease_channel(mode):
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if key in os.environ
    }
    environment.update(
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        PYTHONPATH=os.pathsep.join(
            [
                str(ROOT / "control-plane-api/src"),
                str(ROOT / "shared-sdk/python"),
            ]
        ),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PEER),
        mode,
        cwd=ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    calls = 0

    def renew(generation):
        nonlocal calls
        calls += 1
        if mode == "revoked" and calls >= 3:
            raise RuntimeError("private-authority-secret")
        return generation + 1

    authority = AssuranceLease(renew)
    binding = LeaseBinding(
        request_sha256="1" * 64,
        environment_id="aenv_" + "2" * 32,
        assessment_generation=1,
        deadline_at=datetime.now(UTC) + timedelta(seconds=20),
        channel_nonce="3" * 64,
    )
    session = SupervisorLeaseSession(
        StreamLeaseTransport(process.stdout, process.stdin),
        binding,
        authority.guard,
    )
    try:
        result = await asyncio.wait_for(session.serve(), 15)
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == 0
        assert not await process.stderr.read()
        assert "private-authority-secret" not in repr(result)
        if mode == "normal":
            assert result.reason_code == "report_announced"
            assert result.checks_permitted == 3 and calls == 5
            assert result.report_sha256 == "4" * 64
        else:
            assert result.report_sha256 is None
            assert result.reason_code == (
                "lease_revoked" if mode == "revoked" else "channel_unavailable"
            )
    finally:
        authority.close()
        session.close()
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)
