"""PRD-127 Gateway upgrade synchronization and Control API contract tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway.config import GatewayUpgradeSynchronizer
from metahub_shared.private_gateway import (
    EdgeUpgradeExecutor,
    GatewayReleaseArtifact,
    GatewayReleaseManifest,
    MaintenanceWindowContract,
    UnsignedUpgradeCommand,
    UpgradeAuthority,
    UpgradePreflightEvidence,
    canonical_edge_json_bytes,
    sign_gateway_release,
    sign_upgrade_command,
    strict_json_object,
)
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo

DEPLOYMENT_ID = "edp_12700000000000000000000000000127"
COMMAND_ID = "ucm_12700000000000000000000000000127"
ROLLOUT_ID = "rol_12700000000000000000000000000127"
TARGET_ID = "rtt_12700000000000000000000000000127"
SESSION_TOKEN = "egs_" + "A" * 43
RELEASE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
COMMAND_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _config(now: datetime) -> AppConfig:
    return AppConfig(
        service_name="gateway-data-plane",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=now,
        ),
        auth_token_secret="a" * 43,
        cache_affinity_hmac_secret="b" * 43,
        platform_session_secret="c" * 43,
        control_api_base_url="https://control.test",
        control_background_schedulers_enabled=False,
    )


def _command(now: datetime):
    artifacts = [
        GatewayReleaseArtifact(
            artifact_id="gar_12700000000000000000000000000127",
            installer_artifact_id="ira_12400000000000000000000000000003",
            kind="container_bundle",
            filename="container-images.json",
            media_type="application/json",
            sha256="1" * 64,
            sbom_sha256="2" * 64,
            size_bytes=128,
        ),
        GatewayReleaseArtifact(
            artifact_id="gar_12700000000000000000000000000128",
            installer_artifact_id="ira_12400000000000000000000000000005",
            kind="sbom",
            filename="sbom.spdx.json",
            media_type="application/spdx+json",
            sha256="2" * 64,
            sbom_sha256="2" * 64,
            size_bytes=256,
        ),
    ]
    artifact_set_sha256 = hashlib.sha256(
        canonical_edge_json_bytes([item.model_dump(mode="json") for item in artifacts])
    ).hexdigest()
    release = sign_gateway_release(
        GatewayReleaseManifest(
            release_id="ger_12700000000000000000000000000127",
            installer_release_id="irl_12400000000000000000000000000000",
            version="1.1.0",
            release_channel="stable",
            artifact_set_sha256=artifact_set_sha256,
            sbom_sha256="2" * 64,
            schema_from=119,
            schema_to=119,
            down_migration_supported=True,
            compatible_from_versions=["1.0.0"],
            agent_min_version="1.0.0",
            agent_max_version="1.2.0",
            control_min_version="1.0.0",
            control_max_version="1.2.0",
            artifacts=artifacts,
        ),
        private_key=RELEASE_KEY,
        key_id="edge-release-sync-v1",
    )
    minute = now.hour * 60 + now.minute
    maintenance = MaintenanceWindowContract(
        maintenance_window_id="mwn_12700000000000000000000000000127",
        policy_generation=7,
        release_channel="stable",
        timezone_name="UTC",
        weekdays=list(range(7)),
        start_minute=max(0, minute - 1),
        duration_minutes=30,
    )
    return sign_upgrade_command(
        UnsignedUpgradeCommand(
            upgrade_command_id=COMMAND_ID,
            deployment_id=DEPLOYMENT_ID,
            rollout_id=ROLLOUT_ID,
            rollout_target_id=TARGET_ID,
            command_kind="upgrade",
            command_generation=7,
            rollout_generation=3,
            cancel_watermark=0,
            expected_current_version="1.0.0",
            target_version="1.1.0",
            release=release,
            maintenance=maintenance,
            preflight=UpgradePreflightEvidence(
                health_snapshot_generation=12,
                backup_evidence_sha256="3" * 64,
                database_schema_from=119,
                database_schema_to=119,
                down_migration_supported=True,
                checked_at=now - timedelta(minutes=1),
            ),
            drain_timeout_ms=60_000,
            overall_timeout_ms=300_000,
            issued_at=now - timedelta(minutes=1),
            deadline_at=now + timedelta(minutes=5),
        ),
        private_key=COMMAND_KEY,
        key_id="edge-command-sync-v1",
    )


@dataclass
class Hooks:
    calls: list[str] = field(default_factory=list)

    def _success(self, name: str) -> bool:
        self.calls.append(name)
        return True

    def verify_artifacts(self, _release) -> bool:
        return self._success("verify_artifacts")

    def drain(self, _timeout_ms: int) -> bool:
        return self._success("drain")

    def stage(self, _release) -> bool:
        return self._success("stage")

    def migrate(self, _schema_from: int, _schema_to: int) -> bool:
        return self._success("migrate")

    def health_check(self, _release) -> bool:
        return self._success("health_check")

    def commit(self, _release) -> bool:
        return self._success("commit")

    def resume(self) -> None:
        self.calls.append("resume")


def _authority(now: datetime) -> UpgradeAuthority:
    return UpgradeAuthority(
        rollout_generation=3,
        cancel_watermark=0,
        maintenance_policy_generation=7,
        observed_at=now,
    )


def _command_response(envelope, revision: int, now: datetime) -> bytes:
    return canonical_edge_json_bytes(
        {
            "deployment_id": DEPLOYMENT_ID,
            "command": envelope.model_dump(mode="json"),
            "command_revision": revision,
            "authority": {
                "rollout_generation": 3,
                "cancel_watermark": 0,
                "maintenance_policy_generation": 7,
                "observed_at": now.isoformat().replace("+00:00", "Z"),
            },
            "request_id": "request-prd127-sync",
            "trace_id": "trace-prd127-sync",
        }
    )


@pytest.mark.anyio
async def test_upgrade_sync_uses_control_paths_executes_and_reports_201(tmp_path) -> None:
    now = datetime.now(UTC)
    envelope = _command(now)
    paths: list[str] = []
    lease_nonce: list[str] = []
    reports: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["X-Edge-Session"] == SESSION_TOKEN
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        if request.method == "GET":
            return httpx.Response(
                200,
                content=_command_response(envelope, 1, now),
                headers={"Content-Type": "application/json"},
            )
        payload = strict_json_object(request.content, canonical=True)
        if request.url.path.endswith("/report"):
            reports.append(payload)
            expected_nonce = hashlib.sha256(
                f"prd127\0{DEPLOYMENT_ID}\0{COMMAND_ID}\0{lease_nonce[0]}".encode(
                    "ascii"
                )
            ).hexdigest()
            assert payload["attempt_nonce"] == expected_nonce
            return httpx.Response(
                201,
                content=canonical_edge_json_bytes(
                    {
                        "deployment_id": DEPLOYMENT_ID,
                        "upgrade_command_id": COMMAND_ID,
                        "outcome": "succeeded",
                        "idempotent_replay": False,
                        "request_id": "request-prd127-receipt",
                        "trace_id": "trace-prd127-receipt",
                    }
                ),
                headers={"Content-Type": "application/json"},
            )
        if payload["action"] == "lease":
            lease_nonce.append(str(payload["lease_nonce"]))
            assert payload["expected_revision"] == 1
            revision = 2
        else:
            assert payload["action"] == "start"
            assert payload["lease_nonce"] == lease_nonce[0]
            assert payload["expected_revision"] == 2
            revision = 3
        return httpx.Response(
            200,
            content=_command_response(envelope, revision, now),
            headers={"Content-Type": "application/json"},
        )

    hooks = Hooks()
    executor = EdgeUpgradeExecutor(
        tmp_path / "upgrade",
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys={
            "edge-release-sync-v1": RELEASE_KEY.public_key().public_bytes_raw()
        },
        command_trusted_keys={
            "edge-command-sync-v1": COMMAND_KEY.public_key().public_bytes_raw()
        },
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = GatewayUpgradeSynchronizer(
            _config(now),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            executor=executor,
            hooks=hooks,
            authority_probe=lambda: _authority(datetime.now(UTC)),
            client=client,
        )
        result = await sync.refresh()

    assert result.outcome == "succeeded"
    assert result.reported is True
    assert paths == [
        f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/pending",
        f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{COMMAND_ID}",
        f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{COMMAND_ID}",
        f"/edge/deployments/{DEPLOYMENT_ID}/upgrades/{COMMAND_ID}/report",
    ]
    assert len(reports) == 1
    assert executor.current_state()["current_version"] == "1.1.0"
    assert hooks.calls == [
        "verify_artifacts",
        "drain",
        "stage",
        "migrate",
        "health_check",
        "commit",
        "resume",
    ]


@pytest.mark.anyio
async def test_upgrade_sync_treats_204_as_idle(tmp_path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(204))
    ) as client:
        sync = GatewayUpgradeSynchronizer(
            _config(datetime.now(UTC)),
            deployment_id=DEPLOYMENT_ID,
            session_token=lambda: SESSION_TOKEN,
            executor=object(),
            hooks=object(),
            authority_probe=lambda: _authority(datetime.now(UTC)),
            client=client,
        )
        result = await sync.refresh()
    assert result.outcome == "idle"
    assert result.command_id is None
    assert result.reported is False
