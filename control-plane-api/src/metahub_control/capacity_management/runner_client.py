"""Control-side signed permit client for the isolated capacity runner."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import httpx
from metahub_capacity_runner.contracts import (
    CapacityExecutionInfo,
    CapacityKillClaims,
    CapacityKillRequest,
    CapacityKillResponse,
    CapacityPermitClaims,
    CapacityRunnerRequest,
    CapacityRunnerResult,
    encode_signed,
    execution_digest,
)
from metahub_capacity_runner.engine import simulate_capacity_run
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


def execute_capacity_run(config: AppConfig, claims: CapacityPermitClaims) -> CapacityRunnerResult:
    mode = str(getattr(config, "capacity_runner_mode", "disabled"))
    if mode == "test_loopback" and config.environment == "test":
        return simulate_capacity_run(claims)
    if mode != "isolated_http":
        raise ApiError("capacity.runner_unavailable")
    response = capacity_execution(config, claims, action="submit")
    if isinstance(response, CapacityRunnerResult):
        return response
    if response.result is not None:
        return response.result
    # This synchronous adapter cannot own an external execution's polling lifecycle.
    raise ApiError("capacity.runner_unavailable")


def capacity_execution(
    config: AppConfig,
    claims: CapacityPermitClaims,
    *,
    action: Literal["submit", "status", "cancel"],
) -> CapacityExecutionInfo | CapacityRunnerResult:
    """One signed exchange; the caller durably owns scheduling and terminal state."""
    if getattr(config, "capacity_runner_mode", "disabled") != "isolated_http" or action not in {
        "submit",
        "status",
        "cancel",
    }:
        raise ApiError("capacity.runner_unavailable")
    base_url, token, secret = _settings(config)
    permit = encode_signed(claims, secret, domain="run-permit")
    path = "/internal/capacity/runs"
    if action != "submit":
        path += f"/{claims.object_id}/{action}"
    try:
        response = httpx.post(
            base_url + path,
            headers={"Authorization": f"Bearer {token}"},
            json=CapacityRunnerRequest(permit=permit).model_dump(mode="json"),
            timeout=float(getattr(config, "capacity_runner_http_timeout_seconds", 30)),
            trust_env=False,
            follow_redirects=False,
        )
        if response.status_code not in ({200, 202} if action == "submit" else {200}):
            raise ApiError("capacity.runner_unavailable")
        payload = response.json()
        if isinstance(payload, dict) and "state" in payload:
            info = CapacityExecutionInfo.model_validate(payload)
            if (
                not hmac.compare_digest(info.object_id, claims.object_id)
                or not hmac.compare_digest(info.execution_sha256, execution_digest(claims))
                or (
                    action == "submit"
                    and (response.status_code == 202)
                    != (info.state in {"queued", "running", "stopping"})
                )
                or (action == "cancel" and info.state in {"queued", "running"})
            ):
                raise ApiError("capacity.runner_unavailable")
            if info.result is not None:
                _validate_result(info.result, claims)
            return info
        if action != "submit" or response.status_code != 200:
            raise ApiError("capacity.runner_unavailable")
        return _validate_result(CapacityRunnerResult.model_validate(payload), claims)
    except ApiError:
        raise
    except (httpx.HTTPError, TypeError, ValueError):
        raise ApiError("capacity.runner_unavailable") from None


def _validate_result(result: CapacityRunnerResult, claims: CapacityPermitClaims):
    if (
        result.evidence_kind != "measured"
        or result.operation != claims.operation
        or not hmac.compare_digest(result.object_id, claims.object_id)
        or not hmac.compare_digest(result.manifest_sha256, claims.manifest_sha256)
        or not hmac.compare_digest(result.execution_sha256, execution_digest(claims))
    ):
        raise ApiError("capacity.runner_unavailable")
    return result


def signal_capacity_kill(config: AppConfig, *, experiment_id: str, generation: int) -> None:
    mode = str(getattr(config, "capacity_runner_mode", "disabled"))
    if mode != "isolated_http":
        return
    base_url, token, secret = _settings(config)
    now = datetime.now(UTC)
    claims = CapacityKillClaims(
        experiment_id=experiment_id,
        kill_generation=generation,
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
    )
    permit = encode_signed(claims, secret, domain="kill-permit")
    try:
        response = httpx.post(
            base_url + f"/internal/capacity/runs/{experiment_id}/kill",
            headers={"Authorization": f"Bearer {token}"},
            json=CapacityKillRequest(permit=permit).model_dump(mode="json"),
            timeout=min(5.0, float(getattr(config, "capacity_runner_http_timeout_seconds", 30))),
            trust_env=False,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise ApiError("capacity.runner_unavailable")
        accepted = CapacityKillResponse.model_validate(response.json())
        if accepted.experiment_id != experiment_id or accepted.kill_generation < generation:
            raise ApiError("capacity.runner_unavailable")
    except ApiError:
        raise
    except (httpx.HTTPError, TypeError, ValueError):
        raise ApiError("capacity.runner_unavailable") from None


def permit_id(object_id: str) -> str:
    return "crp_" + hashlib.sha256(object_id.encode("ascii")).hexdigest()[:32]


def _settings(config: AppConfig) -> tuple[str, str, str]:
    raw_url = getattr(config, "capacity_runner_base_url", None)
    token = getattr(config, "capacity_runner_internal_token", None)
    secret = getattr(config, "capacity_runner_permit_secret", None)
    if not raw_url or not token or not secret or len(token) < 32 or len(secret) < 32:
        raise ApiError("capacity.runner_unavailable")
    parsed = urlsplit(str(raw_url))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ApiError("capacity.runner_unavailable")
    return str(raw_url).rstrip("/"), str(token), str(secret)


__all__ = ["capacity_execution", "execute_capacity_run", "permit_id", "signal_capacity_kill"]
