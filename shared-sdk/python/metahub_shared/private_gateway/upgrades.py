"""Signed Fleet upgrade contracts and crash-safe Edge execution for PRD-127."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .desired_state import canonical_json_bytes, strict_json_object

UPGRADE_RELEASE_SCHEMA_VERSION = "metahub.edge-upgrade-release/v1"
UPGRADE_COMMAND_SCHEMA_VERSION = "metahub.edge-upgrade-command/v1"
UPGRADE_REPORT_SCHEMA_VERSION = "metahub.edge-upgrade-report/v1"
UPGRADE_RELEASE_SIGNATURE_PURPOSE = "metahub.edge-upgrade.release/v1"
UPGRADE_COMMAND_SIGNATURE_PURPOSE = "metahub.edge-upgrade.command/v1"
MAX_UPGRADE_DOCUMENT_BYTES = 65_536
MAX_UPGRADE_MILLISECONDS = 600_000
MAX_DRAIN_MILLISECONDS = 300_000
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")


class UpgradeContractError(ValueError):
    """A stable, secret-safe upgrade contract failure."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class UpgradeSignature(_StrictModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._:-]+$")
    public_key: str = Field(min_length=43, max_length=43, repr=False)
    value: str = Field(min_length=86, max_length=86, repr=False)


class GatewayReleaseArtifact(_StrictModel):
    artifact_id: str = Field(pattern=r"^gar_[0-9a-f]{32}$")
    installer_artifact_id: str = Field(pattern=r"^ira_[0-9a-f]{32}$")
    kind: Literal["cli", "compose", "container_bundle", "profile", "sbom"]
    filename: str = Field(min_length=1, max_length=120, pattern=r"^[^/\\]+$")
    media_type: str = Field(min_length=3, max_length=96)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=8_589_934_592)


class GatewayReleaseManifest(_StrictModel):
    schema_version: Literal["metahub.edge-upgrade-release/v1"] = (
        UPGRADE_RELEASE_SCHEMA_VERSION
    )
    release_id: str = Field(pattern=r"^ger_[0-9a-f]{32}$")
    installer_release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    version: str = Field(min_length=5, max_length=32, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    release_channel: Literal["preview", "stable", "extended"]
    signature_purpose: Literal["metahub.edge-upgrade.release/v1"] = (
        UPGRADE_RELEASE_SIGNATURE_PURPOSE
    )
    artifact_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_from: int = Field(ge=1, le=9999)
    schema_to: int = Field(ge=1, le=9999)
    down_migration_supported: bool
    backup_required: Literal[True] = True
    compatible_from_versions: list[str] = Field(min_length=1, max_length=8)
    agent_min_version: str = Field(min_length=5, max_length=32)
    agent_max_version: str = Field(min_length=5, max_length=32)
    control_min_version: str = Field(min_length=5, max_length=32)
    control_max_version: str = Field(min_length=5, max_length=32)
    artifacts: list[GatewayReleaseArtifact] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _consistent(self) -> GatewayReleaseManifest:
        versions = self.compatible_from_versions
        artifacts = self.artifacts
        if (
            self.schema_from > self.schema_to
            or (self.schema_from != self.schema_to and not self.down_migration_supported)
            or len(set(versions)) != len(versions)
            or any(_VERSION_RE.fullmatch(item) is None for item in versions)
            or len({item.artifact_id for item in artifacts}) != len(artifacts)
            or len({item.installer_artifact_id for item in artifacts}) != len(artifacts)
            or len({item.filename for item in artifacts}) != len(artifacts)
            or sum(item.kind == "sbom" for item in artifacts) != 1
            or any(item.sbom_sha256 != self.sbom_sha256 for item in artifacts)
            or _artifact_set_sha256(artifacts) != self.artifact_set_sha256
            or not _version_between(
                self.version, self.agent_min_version, self.agent_max_version
            )
        ):
            raise ValueError("edge_upgrade.release_invalid")
        return self


class GatewayRelease(_StrictModel):
    manifest: GatewayReleaseManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: UpgradeSignature


class MaintenanceWindowContract(_StrictModel):
    maintenance_window_id: str = Field(pattern=r"^mwn_[0-9a-f]{32}$")
    policy_generation: int = Field(ge=1)
    release_channel: Literal["preview", "stable", "extended"]
    timezone_name: str = Field(min_length=3, max_length=64)
    weekdays: list[int] = Field(min_length=1, max_length=7)
    start_minute: int = Field(ge=0, le=1439)
    duration_minutes: int = Field(ge=30, le=720)
    freeze_starts_at: datetime | None = None
    freeze_ends_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> MaintenanceWindowContract:
        if (
            len(set(self.weekdays)) != len(self.weekdays)
            or any(type(item) is not int or not 0 <= item <= 6 for item in self.weekdays)
            or (self.freeze_starts_at is None) != (self.freeze_ends_at is None)
            or (
                self.freeze_starts_at is not None
                and _aware(self.freeze_ends_at) <= _aware(self.freeze_starts_at)
            )
        ):
            raise ValueError("edge_upgrade.maintenance_invalid")
        return self


class UpgradePreflightEvidence(_StrictModel):
    health_snapshot_generation: int = Field(ge=1)
    health_code: Literal["healthy"] = "healthy"
    capacity_ready: Literal[True] = True
    backup_verified: Literal[True] = True
    backup_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_schema_from: int = Field(ge=1, le=9999)
    database_schema_to: int = Field(ge=1, le=9999)
    down_migration_supported: bool
    checked_at: datetime


class UnsignedUpgradeCommand(_StrictModel):
    schema_version: Literal["metahub.edge-upgrade-command/v1"] = (
        UPGRADE_COMMAND_SCHEMA_VERSION
    )
    upgrade_command_id: str = Field(pattern=r"^ucm_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    rollout_id: str | None = Field(default=None, pattern=r"^rol_[0-9a-f]{32}$")
    rollout_target_id: str | None = Field(default=None, pattern=r"^rtt_[0-9a-f]{32}$")
    rollback_attempt_id: str | None = Field(default=None, pattern=r"^rba_[0-9a-f]{32}$")
    command_kind: Literal["upgrade", "rollback"]
    command_generation: int = Field(ge=1)
    rollout_generation: int = Field(ge=1)
    cancel_watermark: int = Field(ge=0)
    expected_current_version: str = Field(min_length=1, max_length=64)
    target_version: str = Field(min_length=1, max_length=64)
    release: GatewayRelease = Field(repr=False)
    maintenance: MaintenanceWindowContract
    preflight: UpgradePreflightEvidence
    drain_timeout_ms: int = Field(ge=1, le=MAX_DRAIN_MILLISECONDS)
    overall_timeout_ms: int = Field(ge=1, le=MAX_UPGRADE_MILLISECONDS)
    issued_at: datetime
    deadline_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> UnsignedUpgradeCommand:
        manifest = self.release.manifest
        upgrade_compatible = (
            self.expected_current_version in manifest.compatible_from_versions
            and self.preflight.database_schema_from == manifest.schema_from
            and self.preflight.database_schema_to == manifest.schema_to
            and self.preflight.down_migration_supported
            == manifest.down_migration_supported
        )
        rollback_compatible = (
            self.expected_current_version != self.target_version
            and self.preflight.database_schema_to == manifest.schema_to
            and self.preflight.down_migration_supported
        )
        if (
            (self.command_kind == "upgrade")
            != (self.rollout_id is not None and self.rollout_target_id is not None)
            or (self.command_kind == "rollback") != (self.rollback_attempt_id is not None)
            or self.target_version != manifest.version
            or self.maintenance.release_channel != manifest.release_channel
            or (self.command_kind == "upgrade" and not upgrade_compatible)
            or (self.command_kind == "rollback" and not rollback_compatible)
            or _aware(self.deadline_at) <= _aware(self.issued_at)
            or _aware(self.deadline_at) - _aware(self.issued_at) > timedelta(minutes=10)
        ):
            raise ValueError("edge_upgrade.command_invalid")
        return self


class UpgradeCommandEnvelope(_StrictModel):
    command: UnsignedUpgradeCommand = Field(repr=False)
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_purpose: Literal["metahub.edge-upgrade.command/v1"] = (
        UPGRADE_COMMAND_SIGNATURE_PURPOSE
    )
    signature: UpgradeSignature = Field(repr=False)


class UpgradeExecutionReport(_StrictModel):
    schema_version: Literal["metahub.edge-upgrade-report/v1"] = (
        UPGRADE_REPORT_SCHEMA_VERSION
    )
    upgrade_command_id: str = Field(pattern=r"^ucm_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    command_generation: int = Field(ge=1)
    command_kind: Literal["upgrade", "rollback"]
    outcome: Literal["succeeded", "failed", "cancelled"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    from_version: str = Field(min_length=1, max_length=64)
    target_version: str = Field(min_length=1, max_length=64)
    applied_version: str = Field(min_length=1, max_length=64)
    lkg_version: str = Field(min_length=1, max_length=64)
    drain_duration_ms: int = Field(ge=0, le=MAX_UPGRADE_MILLISECONDS)
    stage_duration_ms: int = Field(ge=0, le=MAX_UPGRADE_MILLISECONDS)
    migration_duration_ms: int = Field(ge=0, le=MAX_UPGRADE_MILLISECONDS)
    health_duration_ms: int = Field(ge=0, le=MAX_UPGRADE_MILLISECONDS)
    total_duration_ms: int = Field(ge=0, le=MAX_UPGRADE_MILLISECONDS)
    release_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedGatewayRelease:
    release: GatewayRelease
    canonical_manifest: bytes


@dataclass(frozen=True, slots=True)
class VerifiedUpgradeCommand:
    envelope: UpgradeCommandEnvelope
    release: VerifiedGatewayRelease
    canonical_command: bytes


@dataclass(frozen=True, slots=True)
class UpgradeAuthority:
    rollout_generation: int
    cancel_watermark: int
    maintenance_policy_generation: int
    observed_at: datetime


class UpgradeHooks(Protocol):
    def verify_artifacts(self, release: GatewayReleaseManifest) -> bool: ...

    def drain(self, timeout_ms: int) -> bool: ...

    def stage(self, release: GatewayReleaseManifest) -> bool: ...

    def migrate(self, schema_from: int, schema_to: int) -> bool: ...

    def health_check(self, release: GatewayReleaseManifest) -> bool: ...

    def commit(self, release: GatewayReleaseManifest) -> bool: ...

    def resume(self) -> None: ...


def sign_gateway_release(
    manifest: GatewayReleaseManifest,
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> GatewayRelease:
    canonical = canonical_json_bytes(
        manifest.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
    )
    return GatewayRelease(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical).hexdigest(),
        signature=UpgradeSignature(
            key_id=key_id,
            public_key=_b64(private_key.public_key().public_bytes_raw()),
            value=_b64(
                private_key.sign(
                    UPGRADE_RELEASE_SIGNATURE_PURPOSE.encode("ascii")
                    + b"\0"
                    + canonical
                )
            ),
        ),
    )


def verify_gateway_release(
    value: object, *, trusted_keys: Mapping[str, bytes]
) -> VerifiedGatewayRelease:
    try:
        _reject_sensitive_fields(value)
        release = GatewayRelease.model_validate_json(
            canonical_json_bytes(value, maximum=MAX_UPGRADE_DOCUMENT_BYTES)
        )
        canonical = canonical_json_bytes(
            release.manifest.model_dump(mode="json"),
            maximum=MAX_UPGRADE_DOCUMENT_BYTES,
        )
        if not _same_digest(hashlib.sha256(canonical).hexdigest(), release.manifest_sha256):
            raise UpgradeContractError("edge_upgrade.release_digest_invalid")
        _verify_signature(
            release.signature,
            trusted_keys,
            UPGRADE_RELEASE_SIGNATURE_PURPOSE,
            canonical,
            untrusted_code="edge_upgrade.release_key_untrusted",
            invalid_code="edge_upgrade.release_signature_invalid",
        )
        return VerifiedGatewayRelease(release=release, canonical_manifest=canonical)
    except UpgradeContractError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise UpgradeContractError("edge_upgrade.release_invalid") from None


def sign_upgrade_command(
    command: UnsignedUpgradeCommand,
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> UpgradeCommandEnvelope:
    canonical = canonical_json_bytes(
        command.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
    )
    return UpgradeCommandEnvelope(
        command=command,
        command_sha256=hashlib.sha256(canonical).hexdigest(),
        signature=UpgradeSignature(
            key_id=key_id,
            public_key=_b64(private_key.public_key().public_bytes_raw()),
            value=_b64(
                private_key.sign(
                    UPGRADE_COMMAND_SIGNATURE_PURPOSE.encode("ascii")
                    + b"\0"
                    + canonical
                )
            ),
        ),
    )


def verify_upgrade_command(
    value: object,
    *,
    release_trusted_keys: Mapping[str, bytes],
    command_trusted_keys: Mapping[str, bytes],
    deployment_id: str,
    current_version: str,
    minimum_generation: int,
    authority: UpgradeAuthority,
    now: datetime,
) -> VerifiedUpgradeCommand:
    try:
        _reject_sensitive_fields(value)
        envelope = UpgradeCommandEnvelope.model_validate_json(
            canonical_json_bytes(value, maximum=MAX_UPGRADE_DOCUMENT_BYTES)
        )
        command = envelope.command
        canonical = canonical_json_bytes(
            command.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
        )
        if not _same_digest(hashlib.sha256(canonical).hexdigest(), envelope.command_sha256):
            raise UpgradeContractError("edge_upgrade.command_digest_invalid")
        _verify_signature(
            envelope.signature,
            command_trusted_keys,
            UPGRADE_COMMAND_SIGNATURE_PURPOSE,
            canonical,
            untrusted_code="edge_upgrade.command_key_untrusted",
            invalid_code="edge_upgrade.command_signature_invalid",
        )
        release = verify_gateway_release(
            command.release.model_dump(mode="json"), trusted_keys=release_trusted_keys
        )
        instant = _aware(now)
        if command.deployment_id != deployment_id:
            raise UpgradeContractError("edge_upgrade.deployment_mismatch")
        if command.expected_current_version != current_version:
            raise UpgradeContractError("edge_upgrade.version_conflict")
        if command.command_generation <= minimum_generation:
            raise UpgradeContractError("edge_upgrade.generation_stale")
        _verify_authority(command, authority, instant)
        if instant < _aware(command.issued_at) - timedelta(minutes=1):
            raise UpgradeContractError("edge_upgrade.command_not_yet_valid")
        if instant >= _aware(command.deadline_at):
            raise UpgradeContractError("edge_upgrade.command_expired")
        return VerifiedUpgradeCommand(
            envelope=envelope, release=release, canonical_command=canonical
        )
    except UpgradeContractError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise UpgradeContractError("edge_upgrade.command_invalid") from None


class EdgeUpgradeExecutor:
    """Run a bounded upgrade through injected, shell-free platform hooks."""

    def __init__(
        self,
        root: Path,
        *,
        deployment_id: str,
        initial_version: str,
        release_trusted_keys: Mapping[str, bytes],
        command_trusted_keys: Mapping[str, bytes],
    ) -> None:
        self._root = root
        self._deployment_id = deployment_id
        self._initial_version = initial_version
        self._release_keys = dict(release_trusted_keys)
        self._command_keys = dict(command_trusted_keys)
        self._lock = threading.Lock()

    def execute(
        self,
        value: object,
        *,
        authority_probe: Callable[[], UpgradeAuthority],
        hooks: UpgradeHooks,
        now: datetime | None = None,
    ) -> UpgradeExecutionReport:
        with self._lock:
            return self._execute(
                value, authority_probe=authority_probe, hooks=hooks, now=now
            )

    def current_state(self) -> dict[str, object]:
        with self._lock:
            return self._head()

    def _execute(
        self,
        value: object,
        *,
        authority_probe: Callable[[], UpgradeAuthority],
        hooks: UpgradeHooks,
        now: datetime | None,
    ) -> UpgradeExecutionReport:
        self._root.mkdir(parents=True, exist_ok=True)
        instant = _aware(now or datetime.now(UTC))
        head = self._head()
        try:
            basic = UpgradeCommandEnvelope.model_validate_json(
                canonical_json_bytes(value, maximum=MAX_UPGRADE_DOCUMENT_BYTES)
            )
        except ValueError:
            raise UpgradeContractError("edge_upgrade.command_invalid") from None
        command = basic.command
        receipt_path = self._receipt_path(command.upgrade_command_id)
        if receipt_path.exists():
            report = self._load_report(receipt_path)
            if report.command_sha256 != basic.command_sha256:
                raise UpgradeContractError("edge_upgrade.command_conflict")
            return report
        journal = self._load_journal()
        if journal is not None:
            if journal.get("upgrade_command_id") != command.upgrade_command_id:
                raise UpgradeContractError("edge_upgrade.in_progress")
            if journal.get("phase") == "committed":
                report = UpgradeExecutionReport.model_validate_json(
                    canonical_json_bytes(
                        journal.get("report"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
                    )
                )
                if report.command_sha256 != basic.command_sha256:
                    raise UpgradeContractError("edge_upgrade.command_conflict")
                self._store_receipt(receipt_path, report)
                self._clear_journal()
                return report

        authority = authority_probe()
        recovery_after_commit = (
            journal is not None
            and journal.get("phase") == "committing"
            and int(head["command_generation"]) == command.command_generation
            and head["current_version"] == command.target_version
        )
        verified = verify_upgrade_command(
            value,
            release_trusted_keys=self._release_keys,
            command_trusted_keys=self._command_keys,
            deployment_id=self._deployment_id,
            current_version=(
                command.expected_current_version
                if recovery_after_commit
                else str(head["current_version"])
            ),
            minimum_generation=(
                command.command_generation - 1
                if recovery_after_commit
                else int(head["command_generation"])
            ),
            authority=authority,
            now=instant,
        )
        command = verified.envelope.command
        if recovery_after_commit:
            recovered_lkg_version = (
                command.target_version
                if command.command_kind == "rollback"
                else command.expected_current_version
            )
            report = _report(
                verified,
                outcome="succeeded",
                reason="edge_upgrade.applied",
                from_version=command.expected_current_version,
                applied_version=command.target_version,
                lkg_version=recovered_lkg_version,
                durations={"drain": 0, "stage": 0, "migration": 0, "health": 0},
                total_ms=0,
                completed_at=instant,
            )
            self._store_journal(command, "committed", report=report)
            self._store_receipt(receipt_path, report)
            self._clear_journal()
            return report

        started = time.monotonic()
        durations = {"drain": 0, "stage": 0, "migration": 0, "health": 0}
        self._store_journal(command, "verified")
        outcome: Literal["succeeded", "failed", "cancelled"] = "failed"
        reason = "edge_upgrade.internal_failure"
        applied_version = str(head["current_version"])
        lkg_version = str(head["lkg_version"])
        try:
            if not hooks.verify_artifacts(command.release.manifest):
                reason = "edge_upgrade.artifact_verification_failed"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            self._store_journal(command, "draining")
            phase = time.monotonic()
            drained = hooks.drain(command.drain_timeout_ms)
            durations["drain"] = _elapsed_ms(phase)
            if not drained:
                reason = "edge_upgrade.drain_timeout"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            self._store_journal(command, "staging")
            phase = time.monotonic()
            if not hooks.stage(command.release.manifest):
                reason = "edge_upgrade.stage_failed"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            durations["stage"] = _elapsed_ms(phase)
            self._store_journal(command, "migrating")
            phase = time.monotonic()
            if not hooks.migrate(
                command.preflight.database_schema_from,
                command.preflight.database_schema_to,
            ):
                reason = "edge_upgrade.migration_failed"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            durations["migration"] = _elapsed_ms(phase)
            self._store_journal(command, "health_check")
            phase = time.monotonic()
            if not hooks.health_check(command.release.manifest):
                reason = "edge_upgrade.health_failed"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            durations["health"] = _elapsed_ms(phase)
            commit_authority = authority_probe()
            _verify_authority(command, commit_authority, _aware(commit_authority.observed_at))
            if _elapsed_ms(started) > command.overall_timeout_ms:
                reason = "edge_upgrade.deadline_exceeded"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            self._store_journal(command, "committing")
            if not hooks.commit(command.release.manifest):
                reason = "edge_upgrade.commit_failed"
                return self._finish_failure(
                    verified, head, durations, started, instant, reason, hooks
                )
            outcome = "succeeded"
            reason = "edge_upgrade.applied"
            applied_version = command.target_version
            lkg_version = (
                command.target_version
                if command.command_kind == "rollback"
                else str(head["current_version"])
            )
            next_head = {
                "command_generation": command.command_generation,
                "current_version": applied_version,
                "lkg_version": lkg_version,
                "release_id": command.release.manifest.release_id,
                "version": 1,
            }
            _atomic_write(
                self._root / "head.json",
                canonical_json_bytes(next_head, maximum=4096),
            )
        except UpgradeContractError as exc:
            reason = str(exc)
            outcome = "cancelled" if reason == "edge_upgrade.cancelled" else "failed"
            return self._finish_failure(
                verified, head, durations, started, instant, reason, hooks, outcome=outcome
            )
        except Exception:
            return self._finish_failure(
                verified,
                head,
                durations,
                started,
                instant,
                "edge_upgrade.internal_failure",
                hooks,
            )
        finally:
            try:
                hooks.resume()
            except Exception:
                pass
        report = _report(
            verified,
            outcome=outcome,
            reason=reason,
            from_version=str(head["current_version"]),
            applied_version=applied_version,
            lkg_version=lkg_version,
            durations=durations,
            total_ms=_elapsed_ms(started),
            completed_at=instant,
        )
        self._store_journal(command, "committed", report=report)
        self._store_receipt(receipt_path, report)
        self._clear_journal()
        return report

    def _finish_failure(
        self,
        verified: VerifiedUpgradeCommand,
        head: dict[str, object],
        durations: dict[str, int],
        started: float,
        completed_at: datetime,
        reason: str,
        hooks: UpgradeHooks,
        *,
        outcome: Literal["failed", "cancelled"] = "failed",
    ) -> UpgradeExecutionReport:
        del hooks
        report = _report(
            verified,
            outcome=outcome,
            reason=reason if _SAFE_CODE_RE.fullmatch(reason) else "edge_upgrade.internal_failure",
            from_version=str(head["current_version"]),
            applied_version=str(head["current_version"]),
            lkg_version=str(head["lkg_version"]),
            durations=durations,
            total_ms=_elapsed_ms(started),
            completed_at=completed_at,
        )
        self._store_receipt(
            self._receipt_path(verified.envelope.command.upgrade_command_id), report
        )
        self._clear_journal()
        return report

    def _head(self) -> dict[str, object]:
        path = self._root / "head.json"
        if not path.exists():
            return {
                "command_generation": 0,
                "current_version": self._initial_version,
                "lkg_version": self._initial_version,
                "release_id": None,
                "version": 1,
            }
        value = strict_json_object(path.read_bytes(), canonical=True, maximum=4096)
        expected = {
            "command_generation",
            "current_version",
            "lkg_version",
            "release_id",
            "version",
        }
        if (
            set(value) != expected
            or value.get("version") != 1
            or type(value.get("command_generation")) is not int
            or value["command_generation"] < 0
            or type(value.get("current_version")) is not str
            or type(value.get("lkg_version")) is not str
        ):
            raise UpgradeContractError("edge_upgrade.storage_invalid")
        return value

    def _store_journal(
        self,
        command: UnsignedUpgradeCommand,
        phase: str,
        *,
        report: UpgradeExecutionReport | None = None,
    ) -> None:
        value: dict[str, object] = {
            "phase": phase,
            "upgrade_command_id": command.upgrade_command_id,
            "command_generation": command.command_generation,
            "version": 1,
        }
        if report is not None:
            value["report"] = report.model_dump(mode="json")
        _atomic_write(
            self._root / "journal.json",
            canonical_json_bytes(value, maximum=MAX_UPGRADE_DOCUMENT_BYTES),
        )

    def _load_journal(self) -> dict[str, object] | None:
        path = self._root / "journal.json"
        if not path.exists():
            return None
        return strict_json_object(
            path.read_bytes(), canonical=True, maximum=MAX_UPGRADE_DOCUMENT_BYTES
        )

    def _clear_journal(self) -> None:
        path = self._root / "journal.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise UpgradeContractError("edge_upgrade.storage_failed") from exc

    def _receipt_path(self, command_id: str) -> Path:
        directory = self._root / "receipts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{command_id}.json"

    def _store_receipt(self, path: Path, report: UpgradeExecutionReport) -> None:
        _atomic_write(
            path,
            canonical_json_bytes(
                report.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
            ),
        )

    def _load_report(self, path: Path) -> UpgradeExecutionReport:
        try:
            raw = path.read_bytes()
            strict_json_object(
                raw, canonical=True, maximum=MAX_UPGRADE_DOCUMENT_BYTES
            )
            return UpgradeExecutionReport.model_validate_json(raw)
        except (OSError, ValueError):
            raise UpgradeContractError("edge_upgrade.storage_invalid") from None


def maintenance_window_open(window: MaintenanceWindowContract, now: datetime) -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    instant = _aware(now)
    if window.freeze_starts_at is not None and (
        _aware(window.freeze_starts_at) <= instant < _aware(window.freeze_ends_at)
    ):
        return False
    try:
        local = instant.astimezone(ZoneInfo(window.timezone_name))
    except (ValueError, ZoneInfoNotFoundError):
        raise UpgradeContractError("edge_upgrade.maintenance_invalid") from None
    minute = local.hour * 60 + local.minute
    if local.weekday() in window.weekdays and (
        window.start_minute <= minute < window.start_minute + window.duration_minutes
    ):
        return True
    if window.start_minute + window.duration_minutes > 1440:
        prior = local - timedelta(days=1)
        return (
            prior.weekday() in window.weekdays
            and minute < window.start_minute + window.duration_minutes - 1440
        )
    return False


def gateway_release_manifest_sha256(manifest: GatewayReleaseManifest) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            manifest.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
        )
    ).hexdigest()


def upgrade_report_sha256(report: UpgradeExecutionReport) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            report.model_dump(mode="json"), maximum=MAX_UPGRADE_DOCUMENT_BYTES
        )
    ).hexdigest()


def _verify_authority(
    command: UnsignedUpgradeCommand, authority: UpgradeAuthority, now: datetime
) -> None:
    if (
        authority.rollout_generation != command.rollout_generation
        or authority.cancel_watermark != command.cancel_watermark
        or authority.maintenance_policy_generation
        != command.maintenance.policy_generation
    ):
        raise UpgradeContractError("edge_upgrade.cancelled")
    if not maintenance_window_open(command.maintenance, now):
        raise UpgradeContractError("edge_upgrade.maintenance_closed")


def _report(
    verified: VerifiedUpgradeCommand,
    *,
    outcome: Literal["succeeded", "failed", "cancelled"],
    reason: str,
    from_version: str,
    applied_version: str,
    lkg_version: str,
    durations: dict[str, int],
    total_ms: int,
    completed_at: datetime,
) -> UpgradeExecutionReport:
    command = verified.envelope.command
    return UpgradeExecutionReport(
        upgrade_command_id=command.upgrade_command_id,
        deployment_id=command.deployment_id,
        command_generation=command.command_generation,
        command_kind=command.command_kind,
        outcome=outcome,
        reason_code=reason,
        from_version=from_version,
        target_version=command.target_version,
        applied_version=applied_version,
        lkg_version=lkg_version,
        drain_duration_ms=durations["drain"],
        stage_duration_ms=durations["stage"],
        migration_duration_ms=durations["migration"],
        health_duration_ms=durations["health"],
        total_duration_ms=min(MAX_UPGRADE_MILLISECONDS, total_ms),
        release_manifest_sha256=command.release.manifest_sha256,
        command_sha256=verified.envelope.command_sha256,
        completed_at=_aware(completed_at),
    )


def _verify_signature(
    signature: UpgradeSignature,
    trusted_keys: Mapping[str, bytes],
    purpose: str,
    canonical: bytes,
    *,
    untrusted_code: str,
    invalid_code: str,
) -> None:
    trusted = trusted_keys.get(signature.key_id)
    if type(trusted) is not bytes or len(trusted) != 32:
        raise UpgradeContractError(untrusted_code)
    try:
        declared = _unb64(signature.public_key, 32)
        signed = _unb64(signature.value, 64)
        if not _same_bytes(declared, trusted):
            raise UpgradeContractError(untrusted_code)
        Ed25519PublicKey.from_public_bytes(trusted).verify(
            signed, purpose.encode("ascii") + b"\0" + canonical
        )
    except UpgradeContractError:
        raise
    except (InvalidSignature, TypeError, ValueError):
        raise UpgradeContractError(invalid_code) from None


def _artifact_set_sha256(artifacts: list[GatewayReleaseArtifact]) -> str:
    value = [item.model_dump(mode="json") for item in artifacts]
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _version_between(version: str, minimum: str, maximum: str) -> bool:
    try:
        current = tuple(int(item) for item in version.split("."))
        low = tuple(int(item) for item in minimum.split("."))
        high = tuple(int(item) for item in maximum.split("."))
        return len(current) == len(low) == len(high) == 3 and low <= current <= high
    except ValueError:
        return False


def _reject_sensitive_fields(value: object, depth: int = 0) -> None:
    if depth > 16:
        raise UpgradeContractError("edge_upgrade.document_invalid")
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or re.search(
                r"(?:secret|password|credential|api[_-]?key|access[_-]?token|prompt|response)",
                key,
                re.IGNORECASE,
            ):
                raise UpgradeContractError("edge_upgrade.sensitive_field_rejected")
            _reject_sensitive_fields(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_fields(item, depth + 1)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as writer:
            descriptor = -1
            writer.write(value)
            writer.flush()
            os.fsync(writer.fileno())
        _durable_replace(Path(temporary), path)
        temporary = None
    except OSError as exc:
        raise UpgradeContractError("edge_upgrade.storage_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _durable_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        move_file = ctypes.windll.kernel32.MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        if not move_file(str(source), str(target), 0x1 | 0x8):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        return
    os.replace(source, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        raise UpgradeContractError("edge_upgrade.time_invalid")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _elapsed_ms(started: float) -> int:
    return min(MAX_UPGRADE_MILLISECONDS, max(0, int((time.monotonic() - started) * 1000)))


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str, expected: int) -> bytes:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != expected or _b64(decoded) != value:
        raise ValueError
    return decoded


def _same_digest(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _same_bytes(left: bytes, right: bytes) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


__all__ = (
    "MAX_DRAIN_MILLISECONDS",
    "MAX_UPGRADE_DOCUMENT_BYTES",
    "MAX_UPGRADE_MILLISECONDS",
    "UPGRADE_COMMAND_SCHEMA_VERSION",
    "UPGRADE_COMMAND_SIGNATURE_PURPOSE",
    "UPGRADE_RELEASE_SCHEMA_VERSION",
    "UPGRADE_RELEASE_SIGNATURE_PURPOSE",
    "UPGRADE_REPORT_SCHEMA_VERSION",
    "EdgeUpgradeExecutor",
    "GatewayRelease",
    "GatewayReleaseArtifact",
    "GatewayReleaseManifest",
    "MaintenanceWindowContract",
    "UnsignedUpgradeCommand",
    "UpgradeAuthority",
    "UpgradeCommandEnvelope",
    "UpgradeContractError",
    "UpgradeExecutionReport",
    "UpgradeHooks",
    "UpgradePreflightEvidence",
    "UpgradeSignature",
    "VerifiedGatewayRelease",
    "VerifiedUpgradeCommand",
    "gateway_release_manifest_sha256",
    "maintenance_window_open",
    "sign_gateway_release",
    "sign_upgrade_command",
    "upgrade_report_sha256",
    "verify_gateway_release",
    "verify_upgrade_command",
)
