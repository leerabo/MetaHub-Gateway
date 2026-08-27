"""PRD-127 signed release, maintenance, and crash-safe upgrade contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    EdgeUpgradeExecutor,
    GatewayReleaseArtifact,
    GatewayReleaseManifest,
    MaintenanceWindowContract,
    UnsignedUpgradeCommand,
    UpgradeAuthority,
    UpgradeContractError,
    UpgradePreflightEvidence,
    canonical_edge_json_bytes,
    maintenance_window_open,
    sign_gateway_release,
    sign_upgrade_command,
    verify_gateway_release,
    verify_upgrade_command,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
DEPLOYMENT_ID = "edp_12700000000000000000000000000000"
RELEASE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
COMMAND_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))


def _release(
    *, version: str = "1.1.0", compatible_from_versions: list[str] | None = None
):
    artifacts = [
        GatewayReleaseArtifact(
            artifact_id="gar_12700000000000000000000000000001",
            installer_artifact_id="ira_12400000000000000000000000000003",
            kind="container_bundle",
            filename="container-images.json",
            media_type="application/json",
            sha256="1" * 64,
            sbom_sha256="2" * 64,
            size_bytes=128,
        ),
        GatewayReleaseArtifact(
            artifact_id="gar_12700000000000000000000000000002",
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
    manifest = GatewayReleaseManifest(
        release_id="ger_12700000000000000000000000000000",
        installer_release_id="irl_12400000000000000000000000000000",
        version=version,
        release_channel="stable",
        artifact_set_sha256=artifact_set_sha256,
        sbom_sha256="2" * 64,
        schema_from=119,
        schema_to=119,
        down_migration_supported=True,
        compatible_from_versions=compatible_from_versions or ["1.0.0"],
        agent_min_version="1.0.0",
        agent_max_version="1.2.0",
        control_min_version="1.0.0",
        control_max_version="1.2.0",
        artifacts=artifacts,
    )
    return sign_gateway_release(
        manifest, private_key=RELEASE_KEY, key_id="edge-release-test-v1"
    )


def _window(**overrides):
    value = {
        "maintenance_window_id": "mwn_12700000000000000000000000000000",
        "policy_generation": 4,
        "release_channel": "stable",
        "timezone_name": "UTC",
        "weekdays": [NOW.weekday()],
        "start_minute": 11 * 60,
        "duration_minutes": 180,
    }
    value.update(overrides)
    return MaintenanceWindowContract.model_validate(value)


def _command(*, generation: int = 7, window=None):
    release = _release()
    command = UnsignedUpgradeCommand(
        upgrade_command_id=f"ucm_{generation:032x}",
        deployment_id=DEPLOYMENT_ID,
        rollout_id="rol_12700000000000000000000000000000",
        rollout_target_id="rtt_12700000000000000000000000000000",
        command_kind="upgrade",
        command_generation=generation,
        rollout_generation=3,
        cancel_watermark=0,
        expected_current_version="1.0.0",
        target_version="1.1.0",
        release=release,
        maintenance=window or _window(),
        preflight=UpgradePreflightEvidence(
            health_snapshot_generation=12,
            backup_evidence_sha256="3" * 64,
            database_schema_from=119,
            database_schema_to=119,
            down_migration_supported=True,
            checked_at=NOW - timedelta(minutes=1),
        ),
        drain_timeout_ms=60_000,
        overall_timeout_ms=300_000,
        issued_at=NOW - timedelta(minutes=1),
        deadline_at=NOW + timedelta(minutes=5),
    )
    return sign_upgrade_command(
        command, private_key=COMMAND_KEY, key_id="edge-command-test-v1"
    )


def _keys():
    return (
        {"edge-release-test-v1": RELEASE_KEY.public_key().public_bytes_raw()},
        {"edge-command-test-v1": COMMAND_KEY.public_key().public_bytes_raw()},
    )


def _authority(**overrides):
    value = {
        "rollout_generation": 3,
        "cancel_watermark": 0,
        "maintenance_policy_generation": 4,
        "observed_at": NOW,
    }
    value.update(overrides)
    return UpgradeAuthority(**value)


@dataclass
class Hooks:
    outcomes: dict[str, bool] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def _call(self, name: str) -> bool:
        self.calls.append(name)
        return self.outcomes.get(name, True)

    def verify_artifacts(self, _release) -> bool:
        return self._call("verify_artifacts")

    def drain(self, _timeout_ms: int) -> bool:
        return self._call("drain")

    def stage(self, _release) -> bool:
        return self._call("stage")

    def migrate(self, _schema_from: int, _schema_to: int) -> bool:
        return self._call("migrate")

    def health_check(self, _release) -> bool:
        return self._call("health_check")

    def commit(self, _release) -> bool:
        return self._call("commit")

    def resume(self) -> None:
        self.calls.append("resume")


def test_release_and_command_are_domain_signed_and_exact() -> None:
    release = _release()
    release_keys, command_keys = _keys()
    verified = verify_gateway_release(
        release.model_dump(mode="json"), trusted_keys=release_keys
    )
    assert verified.release.manifest.version == "1.1.0"

    envelope = _command()
    command = verify_upgrade_command(
        envelope.model_dump(mode="json"),
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
        deployment_id=DEPLOYMENT_ID,
        current_version="1.0.0",
        minimum_generation=6,
        authority=_authority(),
        now=NOW,
    )
    assert command.envelope.command.release.manifest_sha256 == release.manifest_sha256

    tampered = envelope.model_dump(mode="json")
    tampered["command"]["target_version"] = "1.1.1"
    with pytest.raises(UpgradeContractError) as invalid:
        verify_upgrade_command(
            tampered,
            release_trusted_keys=release_keys,
            command_trusted_keys=command_keys,
            deployment_id=DEPLOYMENT_ID,
            current_version="1.0.0",
            minimum_generation=6,
            authority=_authority(),
            now=NOW,
        )
    assert invalid.value.args[0] in {
        "edge_upgrade.command_invalid",
        "edge_upgrade.command_digest_invalid",
    }

    sensitive = envelope.model_dump(mode="json")
    sensitive["command"]["api_key"] = "not-a-real-value"
    with pytest.raises(UpgradeContractError) as rejected:
        verify_upgrade_command(
            sensitive,
            release_trusted_keys=release_keys,
            command_trusted_keys=command_keys,
            deployment_id=DEPLOYMENT_ID,
            current_version="1.0.0",
            minimum_generation=6,
            authority=_authority(),
            now=NOW,
        )
    assert rejected.value.args[0] == "edge_upgrade.sensitive_field_rejected"


def test_maintenance_window_and_freeze_fail_closed() -> None:
    assert maintenance_window_open(_window(), NOW)
    frozen = _window(
        freeze_starts_at=NOW - timedelta(minutes=1),
        freeze_ends_at=NOW + timedelta(minutes=1),
    )
    assert not maintenance_window_open(frozen, NOW)
    assert not maintenance_window_open(_window(weekdays=[(NOW.weekday() + 1) % 7]), NOW)


def test_executor_applies_once_and_persists_lkg(tmp_path) -> None:
    release_keys, command_keys = _keys()
    executor = EdgeUpgradeExecutor(
        tmp_path / "upgrade",
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
    )
    hooks = Hooks()
    envelope = _command()
    first = executor.execute(
        envelope.model_dump(mode="json"),
        authority_probe=_authority,
        hooks=hooks,
        now=NOW,
    )
    assert first.outcome == "succeeded"
    assert executor.current_state() == {
        "command_generation": 7,
        "current_version": "1.1.0",
        "lkg_version": "1.0.0",
        "release_id": "ger_12700000000000000000000000000000",
        "version": 1,
    }
    assert hooks.calls == [
        "verify_artifacts",
        "drain",
        "stage",
        "migrate",
        "health_check",
        "commit",
        "resume",
    ]
    second = executor.execute(
        envelope.model_dump(mode="json"),
        authority_probe=_authority,
        hooks=hooks,
        now=NOW,
    )
    assert second == first
    assert hooks.calls.count("commit") == 1


def test_drain_timeout_never_stages_or_switches(tmp_path) -> None:
    release_keys, command_keys = _keys()
    executor = EdgeUpgradeExecutor(
        tmp_path / "upgrade",
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
    )
    hooks = Hooks(outcomes={"drain": False})
    report = executor.execute(
        _command().model_dump(mode="json"),
        authority_probe=_authority,
        hooks=hooks,
        now=NOW,
    )
    assert report.outcome == "failed"
    assert report.reason_code == "edge_upgrade.drain_timeout"
    assert "stage" not in hooks.calls
    assert "commit" not in hooks.calls
    assert executor.current_state()["current_version"] == "1.0.0"


def test_commit_rechecks_cancel_watermark(tmp_path) -> None:
    release_keys, command_keys = _keys()
    executor = EdgeUpgradeExecutor(
        tmp_path / "upgrade",
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
    )
    authorities = iter([_authority(), _authority(cancel_watermark=1)])
    hooks = Hooks()
    report = executor.execute(
        _command().model_dump(mode="json"),
        authority_probe=lambda: next(authorities),
        hooks=hooks,
        now=NOW,
    )
    assert report.outcome == "cancelled"
    assert report.reason_code == "edge_upgrade.cancelled"
    assert "commit" not in hooks.calls
    assert executor.current_state()["current_version"] == "1.0.0"


def test_restart_recovers_commit_before_receipt(tmp_path) -> None:
    release_keys, command_keys = _keys()
    root = tmp_path / "upgrade"
    root.mkdir()
    envelope = _command()
    command = envelope.command
    (root / "head.json").write_bytes(
        canonical_edge_json_bytes(
            {
                "command_generation": command.command_generation,
                "current_version": command.target_version,
                "lkg_version": command.expected_current_version,
                "release_id": command.release.manifest.release_id,
                "version": 1,
            }
        )
    )
    (root / "journal.json").write_bytes(
        canonical_edge_json_bytes(
            {
                "phase": "committing",
                "upgrade_command_id": command.upgrade_command_id,
                "command_generation": command.command_generation,
                "version": 1,
            }
        )
    )
    executor = EdgeUpgradeExecutor(
        root,
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
    )
    hooks = Hooks()
    report = executor.execute(
        json.loads(envelope.model_dump_json()),
        authority_probe=_authority,
        hooks=hooks,
        now=NOW,
    )
    assert report.outcome == "succeeded"
    assert hooks.calls == []
    assert not (root / "journal.json").exists()
    assert (root / "receipts" / f"{command.upgrade_command_id}.json").exists()


def test_rollback_promotes_verified_target_to_lkg(tmp_path) -> None:
    release_keys, command_keys = _keys()
    root = tmp_path / "upgrade"
    root.mkdir()
    (root / "head.json").write_bytes(
        canonical_edge_json_bytes(
            {
                "command_generation": 7,
                "current_version": "1.1.0",
                "lkg_version": "1.0.0",
                "release_id": "ger_12700000000000000000000000000000",
                "version": 1,
            }
        )
    )
    release = _release(version="1.0.0", compatible_from_versions=["0.9.0"])
    unsigned = UnsignedUpgradeCommand(
        upgrade_command_id="ucm_12700000000000000000000000000008",
        deployment_id=DEPLOYMENT_ID,
        rollback_attempt_id="rba_12700000000000000000000000000000",
        command_kind="rollback",
        command_generation=8,
        rollout_generation=8,
        cancel_watermark=0,
        expected_current_version="1.1.0",
        target_version="1.0.0",
        release=release,
        maintenance=_window(),
        preflight=UpgradePreflightEvidence(
            health_snapshot_generation=13,
            backup_evidence_sha256="4" * 64,
            database_schema_from=120,
            database_schema_to=119,
            down_migration_supported=True,
            checked_at=NOW - timedelta(minutes=1),
        ),
        drain_timeout_ms=60_000,
        overall_timeout_ms=300_000,
        issued_at=NOW - timedelta(minutes=1),
        deadline_at=NOW + timedelta(minutes=5),
    )
    envelope = sign_upgrade_command(
        unsigned, private_key=COMMAND_KEY, key_id="edge-command-test-v1"
    )
    executor = EdgeUpgradeExecutor(
        root,
        deployment_id=DEPLOYMENT_ID,
        initial_version="1.0.0",
        release_trusted_keys=release_keys,
        command_trusted_keys=command_keys,
    )
    report = executor.execute(
        envelope.model_dump(mode="json"),
        authority_probe=lambda: _authority(
            rollout_generation=8, maintenance_policy_generation=4
        ),
        hooks=Hooks(),
        now=NOW,
    )
    assert report.outcome == "succeeded"
    assert report.lkg_version == "1.0.0"
    assert executor.current_state()["current_version"] == "1.0.0"
    assert executor.current_state()["lkg_version"] == "1.0.0"
