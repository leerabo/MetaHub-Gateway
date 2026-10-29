"""Strict delivery-validation and redacted support-bundle contracts for PRD-125."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import rfc8785
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VALIDATION_SCHEMA_VERSION = "metahub.delivery-validation/v1"
VALIDATION_PLAN_VERSION = "edge-delivery-v1"
SUPPORT_BUNDLE_SCHEMA_VERSION = "metahub.support-bundle/v1"
SUPPORT_BUNDLE_SCAN_VERSION = "support-bundle-scan-v1"
SUPPORT_BUNDLE_UPLOAD_SCHEMA_VERSION = "metahub.support-bundle-upload/v1"
MAX_VALIDATION_MILLISECONDS = 900_000
MAX_SUPPORT_BUNDLE_BYTES = 100 * 1024 * 1024
MAX_SUPPORT_BUNDLE_ENTRY_BYTES = 8 * 1024 * 1024
MAX_SUPPORT_BUNDLE_ENTRIES = 64
MAX_SUPPORT_BUNDLE_MILLISECONDS = 300_000

VALIDATION_CHECK_CODES = (
    "device_certificate",
    "signed_config_lkg",
    "provider_connectivity",
    "minimal_gateway_request",
    "usage_cost_failure_sync",
    "backup_readiness",
    "capacity_readiness",
    "performance_baseline",
)
SUPPORT_BUNDLE_COLLECTOR_CODES = (
    "runtime.version",
    "runtime.watermarks",
    "runtime.health",
    "runtime.errors",
    "runtime.logs",
)

type ValidationCheckCode = Literal[
    "device_certificate",
    "signed_config_lkg",
    "provider_connectivity",
    "minimal_gateway_request",
    "usage_cost_failure_sync",
    "backup_readiness",
    "capacity_readiness",
    "performance_baseline",
]
type SupportBundleCollectorCode = Literal[
    "runtime.version",
    "runtime.watermarks",
    "runtime.health",
    "runtime.errors",
    "runtime.logs",
]

_UTC_TIME = re.compile(
    r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?Z$"
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_ID = re.compile(r"^esb_[0-9a-f]{32}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_VALIDATION_ID = re.compile(r"^dvr_[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{2,159}$")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_TOKEN_PREFIX = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk|rk|pk|ghp|glpat|xox[baprs])-?[A-Za-z0-9_-]{20,}"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?key|client[_-]?secret|password|"
    r"passphrase|private[_-]?key|secret|token)\s*[:=]\s*[^\s,;]{6,}"
)
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9_+./=-]{20,512}")
_FORBIDDEN_KEYS = re.compile(
    r"(?:authorization|cookie|credential|secret|token|password|passphrase|private[_-]?key|"
    r"access[_-]?key|api[_-]?key|client[_-]?secret|prompt|response|request[_-]?body)",
    re.IGNORECASE,
)
_COLLECTOR_SPEC = {
    "runtime.version": ("meta/version.json", "version", "application/json"),
    "runtime.watermarks": ("state/watermarks.json", "watermark", "application/json"),
    "runtime.health": ("health/summary.json", "health", "application/json"),
    "runtime.errors": ("errors/codes.json", "error_code", "application/json"),
    "runtime.logs": ("logs/events.ndjson", "log_summary", "application/x-ndjson"),
}
_ENCRYPTION_MAGIC = b"MHSB1"
_ENCRYPTION_DOMAIN = b"metahub/edge-support-bundle/aes-gcm/v1\0"


class ValidationContractError(ValueError):
    """A stable failure that never echoes submitted evidence or collected content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ValidationCheckEvidence(_StrictModel):
    code: ValidationCheckCode
    outcome: Literal["passed", "failed"]
    reason_code: str = Field(pattern=_SAFE_CODE.pattern)
    recovery_action_code: str | None = Field(default=None, pattern=_SAFE_CODE.pattern)
    evidence_sha256: str = Field(pattern=_HEX_64.pattern)
    duration_ms: int = Field(ge=0, le=MAX_VALIDATION_MILLISECONDS)

    @model_validator(mode="after")
    def _recovery_matches_outcome(self) -> ValidationCheckEvidence:
        if (self.outcome == "failed") != (self.recovery_action_code is not None):
            raise ValueError("edge_validation.check_invalid")
        return self


class DeploymentValidationReport(_StrictModel):
    schema_version: Literal["metahub.delivery-validation/v1"]
    validation_id: str = Field(pattern=_VALIDATION_ID.pattern)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    plan_version: Literal["edge-delivery-v1"]
    attempt: int = Field(ge=1, le=2_147_483_647)
    input_contract_sha256: str = Field(pattern=_HEX_64.pattern)
    outcome: Literal["passed", "failed"]
    checks: tuple[ValidationCheckEvidence, ...] = Field(min_length=1, max_length=8)
    started_at: str = Field(pattern=_UTC_TIME.pattern)
    completed_at: str = Field(pattern=_UTC_TIME.pattern)
    duration_ms: int = Field(ge=0, le=MAX_VALIDATION_MILLISECONDS)

    @field_validator("checks", mode="before")
    @classmethod
    def _json_checks_are_immutable(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def _report_is_canonical(self) -> DeploymentValidationReport:
        codes = [item.code for item in self.checks]
        started = _parse_time(self.started_at)
        completed = _parse_time(self.completed_at)
        elapsed = int((completed - started).total_seconds() * 1000)
        failed = any(item.outcome == "failed" for item in self.checks)
        if (
            codes != sorted(codes, key=VALIDATION_CHECK_CODES.index)
            or len(set(codes)) != len(codes)
            or completed < started
            or elapsed != self.duration_ms
            or self.duration_ms < max(item.duration_ms for item in self.checks)
            or (self.outcome == "failed") != failed
        ):
            raise ValueError("edge_validation.report_invalid")
        return self


class SupportBundleEntry(_StrictModel):
    collector_code: SupportBundleCollectorCode
    path: str = Field(min_length=3, max_length=120)
    classification: Literal["version", "watermark", "health", "error_code", "log_summary"]
    media_type: Literal["application/json", "application/x-ndjson"]
    size_bytes: int = Field(ge=1, le=MAX_SUPPORT_BUNDLE_ENTRY_BYTES)
    sha256: str = Field(pattern=_HEX_64.pattern)

    @model_validator(mode="after")
    def _entry_matches_compiled_collector(self) -> SupportBundleEntry:
        expected = _COLLECTOR_SPEC[self.collector_code]
        if (self.path, self.classification, self.media_type) != expected:
            raise ValueError("support_bundle.manifest_invalid")
        _safe_archive_path(self.path)
        return self


class SupportBundleManifest(_StrictModel):
    schema_version: Literal["metahub.support-bundle/v1"]
    bundle_id: str = Field(pattern=_BUNDLE_ID.pattern)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    scan_version: Literal["support-bundle-scan-v1"]
    scan_outcome: Literal["clean"]
    entries: tuple[SupportBundleEntry, ...] = Field(
        min_length=1, max_length=MAX_SUPPORT_BUNDLE_ENTRIES
    )
    total_uncompressed_bytes: int = Field(ge=1, le=MAX_SUPPORT_BUNDLE_BYTES)
    archive_size_bytes: int = Field(ge=1, le=MAX_SUPPORT_BUNDLE_BYTES)
    archive_sha256: str = Field(pattern=_HEX_64.pattern)
    created_at: str = Field(pattern=_UTC_TIME.pattern)
    expires_at: str = Field(pattern=_UTC_TIME.pattern)

    @field_validator("entries", mode="before")
    @classmethod
    def _json_entries_are_immutable(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def _manifest_is_canonical(self) -> SupportBundleManifest:
        codes = [item.collector_code for item in self.entries]
        paths = [item.path for item in self.entries]
        if (
            codes != sorted(codes, key=SUPPORT_BUNDLE_COLLECTOR_CODES.index)
            or len(set(codes)) != len(codes)
            or len(set(paths)) != len(paths)
            or sum(item.size_bytes for item in self.entries) != self.total_uncompressed_bytes
            or _parse_time(self.expires_at) <= _parse_time(self.created_at)
        ):
            raise ValueError("support_bundle.manifest_invalid")
        return self


class SupportBundleUploadMetadata(_StrictModel):
    schema_version: Literal["metahub.support-bundle-upload/v1"]
    manifest: SupportBundleManifest
    manifest_sha256: str = Field(pattern=_HEX_64.pattern)
    object_sha256: str = Field(pattern=_HEX_64.pattern)
    object_size_bytes: int = Field(ge=34, le=MAX_SUPPORT_BUNDLE_BYTES + 64)
    approved_by_account_id: str = Field(pattern=_OWNER_ID.pattern)
    approval_proof_sha256: str = Field(pattern=_HEX_64.pattern)
    approved_at: str = Field(pattern=_UTC_TIME.pattern)

    @model_validator(mode="after")
    def _metadata_matches_manifest(self) -> SupportBundleUploadMetadata:
        if (
            self.manifest_sha256 != support_bundle_manifest_sha256(self.manifest)
            or self.object_size_bytes != self.manifest.archive_size_bytes + 33
            or _parse_time(self.approved_at) < _parse_time(self.manifest.created_at)
            or _parse_time(self.approved_at) >= _parse_time(self.manifest.expires_at)
        ):
            raise ValueError("support_bundle.upload_invalid")
        return self


class SupportBundleUploadReceipt(_StrictModel):
    bundle_id: str = Field(pattern=_BUNDLE_ID.pattern)
    cloud_reference: str = Field(pattern=_BUNDLE_ID.pattern)
    status: Literal["ready"]
    revision: int = Field(ge=1)
    object_sha256: str = Field(pattern=_HEX_64.pattern)
    idempotent_replay: bool = False
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


@dataclass(frozen=True, slots=True)
class PreparedSupportBundle:
    manifest: SupportBundleManifest
    archive_path: Path


@dataclass(frozen=True, slots=True)
class EncryptedSupportBundle:
    manifest: SupportBundleManifest
    object_path: Path
    object_sha256: str
    object_size_bytes: int


class SupportBundleBuilder:
    """Collect only compiled callbacks and persist no caller-selected paths."""

    def __init__(
        self,
        output_root: str | Path,
        collectors: Mapping[str, Callable[[], object]],
        *,
        scanner_hmac_key: bytes,
        known_secret_hmacs: Sequence[str] = (),
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        if set(collectors) != set(SUPPORT_BUNDLE_COLLECTOR_CODES):
            raise ValidationContractError("support_bundle.collector_invalid")
        if not 32 <= len(scanner_hmac_key) <= 512:
            raise ValidationContractError("support_bundle.scanner_invalid")
        if any(_HEX_64.fullmatch(item) is None for item in known_secret_hmacs):
            raise ValidationContractError("support_bundle.scanner_invalid")
        self._collectors = dict(collectors)
        self._scanner_hmac_key = bytes(scanner_hmac_key)
        self._known_secret_hmacs = frozenset(known_secret_hmacs)
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def build(
        self,
        *,
        bundle_id: str,
        deployment_id: str,
        collector_codes: Sequence[str],
        expires_at: datetime,
    ) -> PreparedSupportBundle:
        started = self._monotonic_value()
        if (
            _BUNDLE_ID.fullmatch(bundle_id) is None
            or _DEPLOYMENT_ID.fullmatch(deployment_id) is None
        ):
            raise ValidationContractError("support_bundle.request_invalid")
        codes = tuple(collector_codes)
        if (
            not codes
            or len(codes) > len(SUPPORT_BUNDLE_COLLECTOR_CODES)
            or len(set(codes)) != len(codes)
            or any(code not in SUPPORT_BUNDLE_COLLECTOR_CODES for code in codes)
        ):
            raise ValidationContractError("support_bundle.collector_invalid")
        ordered = tuple(sorted(codes, key=SUPPORT_BUNDLE_COLLECTOR_CODES.index))
        now = _aware_utc(self._now(), code="support_bundle.generation_failed")
        expires = _aware_utc(expires_at, code="support_bundle.request_invalid")
        if expires <= now:
            raise ValidationContractError("support_bundle.request_invalid")

        self.output_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".support-bundle-", dir=self.output_root))
        archive = stage / "bundle.zip"
        final = self.output_root / f"{bundle_id}.zip"
        try:
            entries: list[SupportBundleEntry] = []
            total = 0
            with zipfile.ZipFile(
                archive,
                mode="x",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=False,
            ) as bundle:
                for code in ordered:
                    self._ensure_generation_deadline(started)
                    path, classification, media_type = _COLLECTOR_SPEC[code]
                    content = _collector_bytes(self._collectors[code](), media_type=media_type)
                    self._ensure_generation_deadline(started)
                    _scan_sensitive(
                        content,
                        scanner_hmac_key=self._scanner_hmac_key,
                        known_secret_hmacs=self._known_secret_hmacs,
                    )
                    if not content or len(content) > MAX_SUPPORT_BUNDLE_ENTRY_BYTES:
                        raise ValidationContractError("support_bundle.size_exceeded")
                    total += len(content)
                    if total > MAX_SUPPORT_BUNDLE_BYTES:
                        raise ValidationContractError("support_bundle.size_exceeded")
                    _safe_archive_path(path)
                    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    bundle.writestr(info, content)
                    entries.append(
                        SupportBundleEntry(
                            collector_code=code,
                            path=path,
                            classification=classification,
                            media_type=media_type,
                            size_bytes=len(content),
                            sha256=hashlib.sha256(content).hexdigest(),
                        )
                    )
            self._ensure_generation_deadline(started)
            archive_size = archive.stat().st_size
            if not 0 < archive_size <= MAX_SUPPORT_BUNDLE_BYTES:
                raise ValidationContractError("support_bundle.size_exceeded")
            archive_sha256 = _file_sha256(archive)
            self._ensure_generation_deadline(started)
            manifest = SupportBundleManifest(
                schema_version=SUPPORT_BUNDLE_SCHEMA_VERSION,
                bundle_id=bundle_id,
                deployment_id=deployment_id,
                scan_version=SUPPORT_BUNDLE_SCAN_VERSION,
                scan_outcome="clean",
                entries=tuple(entries),
                total_uncompressed_bytes=total,
                archive_size_bytes=archive_size,
                archive_sha256=archive_sha256,
                created_at=_utc_text(now),
                expires_at=_utc_text(expires),
            )
            if final.exists() or final.is_symlink():
                raise ValidationContractError("support_bundle.conflict")
            os.replace(archive, final)
            return PreparedSupportBundle(manifest=manifest, archive_path=final)
        except ValidationContractError:
            raise
        except (OSError, TypeError, ValueError, zipfile.BadZipFile):
            raise ValidationContractError("support_bundle.generation_failed") from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _ensure_generation_deadline(self, started: float) -> None:
        current = self._monotonic_value()
        if current < started:
            raise ValidationContractError("support_bundle.generation_failed")
        elapsed_ms = int((current - started) * 1000)
        if elapsed_ms >= MAX_SUPPORT_BUNDLE_MILLISECONDS:
            raise ValidationContractError("support_bundle.generation_timeout")

    def _monotonic_value(self) -> float:
        try:
            value = self._monotonic()
        except Exception:
            raise ValidationContractError("support_bundle.generation_failed") from None
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ValidationContractError("support_bundle.generation_failed")
        return float(value)


def validation_report_sha256(report: DeploymentValidationReport) -> str:
    return hashlib.sha256(_canonical(report.model_dump(mode="json"))).hexdigest()


def support_bundle_manifest_sha256(manifest: SupportBundleManifest) -> str:
    return hashlib.sha256(_canonical(manifest.model_dump(mode="json"))).hexdigest()


def support_bundle_approval_proof_sha256(
    *,
    bundle_id: str,
    deployment_id: str,
    tenant_id: str,
    workspace_id: str,
    manifest_sha256: str,
    approved_by_account_id: str,
    approved_at: str,
    local_revision: int,
) -> str:
    if (
        _BUNDLE_ID.fullmatch(bundle_id) is None
        or _DEPLOYMENT_ID.fullmatch(deployment_id) is None
        or any(
            type(value) is not str or _OWNER_ID.fullmatch(value) is None
            for value in (tenant_id, workspace_id, approved_by_account_id)
        )
        or _HEX_64.fullmatch(manifest_sha256) is None
        or type(local_revision) is not int
        or local_revision < 2
    ):
        raise ValidationContractError("support_bundle.approval_invalid")
    try:
        _parse_time(approved_at)
    except ValueError:
        raise ValidationContractError("support_bundle.approval_invalid") from None
    proof = {
        "approved_at": approved_at,
        "approved_by_account_id": approved_by_account_id,
        "bundle_id": bundle_id,
        "deployment_id": deployment_id,
        "local_revision": local_revision,
        "manifest_sha256": manifest_sha256,
        "schema_version": "metahub.support-bundle-approval/v1",
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
    }
    return hashlib.sha256(_canonical(proof)).hexdigest()


def encrypt_support_bundle(
    prepared: PreparedSupportBundle,
    output_path: str | Path,
    *,
    encryption_secret: bytes,
) -> EncryptedSupportBundle:
    source = prepared.archive_path
    target = Path(output_path)
    if _file_sha256(source) != prepared.manifest.archive_sha256:
        raise ValidationContractError("support_bundle.integrity_failed")
    key = _encryption_key(encryption_secret)
    nonce = os.urandom(12)
    aad = support_bundle_manifest_sha256(prepared.manifest).encode("ascii")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
    try:
        encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        with source.open("rb") as reader, temporary.open("xb") as writer:
            writer.write(_ENCRYPTION_MAGIC)
            writer.write(nonce)
            while chunk := reader.read(1024 * 1024):
                writer.write(encryptor.update(chunk))
            writer.write(encryptor.finalize())
            writer.write(encryptor.tag)
            writer.flush()
            os.fsync(writer.fileno())
        if target.exists() or target.is_symlink():
            raise ValidationContractError("support_bundle.conflict")
        os.replace(temporary, target)
        size = target.stat().st_size
        return EncryptedSupportBundle(
            manifest=prepared.manifest,
            object_path=target,
            object_sha256=_file_sha256(target),
            object_size_bytes=size,
        )
    except ValidationContractError:
        raise
    except (OSError, ValueError):
        raise ValidationContractError("support_bundle.encryption_failed") from None
    finally:
        temporary.unlink(missing_ok=True)


def decrypt_support_bundle(
    source_path: str | Path,
    output_path: str | Path,
    *,
    manifest: SupportBundleManifest,
    encryption_secret: bytes,
    expected_object_sha256: str,
) -> Path:
    source = Path(source_path)
    target = Path(output_path)
    if _HEX_64.fullmatch(expected_object_sha256) is None or (
        _file_sha256(source) != expected_object_sha256
    ):
        raise ValidationContractError("support_bundle.integrity_failed")
    size = source.stat().st_size
    if size <= len(_ENCRYPTION_MAGIC) + 12 + 16 or size > MAX_SUPPORT_BUNDLE_BYTES + 64:
        raise ValidationContractError("support_bundle.integrity_failed")
    if target.exists() or target.is_symlink() or source.resolve() == target.resolve():
        raise ValidationContractError("support_bundle.conflict")
    temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.tmp")
    try:
        with source.open("rb") as reader:
            if reader.read(len(_ENCRYPTION_MAGIC)) != _ENCRYPTION_MAGIC:
                raise ValidationContractError("support_bundle.integrity_failed")
            nonce = reader.read(12)
            reader.seek(-16, os.SEEK_END)
            tag = reader.read(16)
            remaining = size - len(_ENCRYPTION_MAGIC) - 12 - 16
            reader.seek(len(_ENCRYPTION_MAGIC) + 12)
            decryptor = Cipher(
                algorithms.AES(_encryption_key(encryption_secret)), modes.GCM(nonce, tag)
            ).decryptor()
            decryptor.authenticate_additional_data(
                support_bundle_manifest_sha256(manifest).encode("ascii")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as writer:
                while remaining:
                    chunk = reader.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValidationContractError("support_bundle.integrity_failed")
                    remaining -= len(chunk)
                    writer.write(decryptor.update(chunk))
                writer.write(decryptor.finalize())
                writer.flush()
                os.fsync(writer.fileno())
        if _file_sha256(temporary) != manifest.archive_sha256:
            raise ValidationContractError("support_bundle.integrity_failed")
        os.replace(temporary, target)
        return target
    except ValidationContractError:
        raise
    except (InvalidTag, OSError, ValueError):
        raise ValidationContractError("support_bundle.integrity_failed") from None
    finally:
        temporary.unlink(missing_ok=True)


def _collector_bytes(
    value: object, *, media_type: Literal["application/json", "application/x-ndjson"]
) -> bytes:
    try:
        if type(value) is bytes:
            encoded = value
            encoded.decode("utf-8")
        elif type(value) is str:
            encoded = value.encode("utf-8")
        else:
            _reject_forbidden_fields(value)
            encoded = _canonical(value) + b"\n"
        _validate_media_content(encoded, media_type=media_type)
        return encoded
    except ValidationContractError:
        raise
    except (json.JSONDecodeError, TypeError, UnicodeError, ValueError):
        raise ValidationContractError("support_bundle.collector_invalid") from None


def _validate_media_content(
    content: bytes, *, media_type: Literal["application/json", "application/x-ndjson"]
) -> None:
    text = content.decode("utf-8")
    if media_type == "application/json":
        _reject_forbidden_fields(json.loads(text))
        return
    records = text.splitlines()
    if not records:
        raise ValidationContractError("support_bundle.collector_invalid")
    for record in records:
        if not record.strip():
            raise ValidationContractError("support_bundle.collector_invalid")
        _reject_forbidden_fields(json.loads(record))


def _reject_forbidden_fields(value: object) -> None:
    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 4096:
            raise ValidationContractError("support_bundle.collector_invalid")
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str or _FORBIDDEN_KEYS.search(key):
                    raise ValidationContractError("support_bundle.sensitive_detected")
                stack.append(item)
        elif type(current) is list:
            stack.extend(current)
        elif type(current) not in {str, int, bool, type(None)}:
            raise ValidationContractError("support_bundle.collector_invalid")


def _scan_sensitive(
    content: bytes, *, scanner_hmac_key: bytes, known_secret_hmacs: frozenset[str]
) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise ValidationContractError("support_bundle.collector_invalid") from None
    if (
        _PRIVATE_KEY.search(text)
        or _JWT.search(text)
        or _TOKEN_PREFIX.search(text)
        or _ASSIGNED_SECRET.search(text)
    ):
        raise ValidationContractError("support_bundle.sensitive_detected")
    candidates = set(_TOKEN_CANDIDATE.findall(text))
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        stack = [parsed]
        while stack:
            value = stack.pop()
            if type(value) is dict:
                for key, item in value.items():
                    if type(key) is not str or _FORBIDDEN_KEYS.search(key):
                        raise ValidationContractError("support_bundle.sensitive_detected")
                    stack.append(item)
            elif type(value) is list:
                stack.extend(value)
            elif type(value) is str:
                candidates.add(value)
    for candidate in candidates:
        if not candidate:
            continue
        digest = hmac.new(scanner_hmac_key, candidate.encode("utf-8"), hashlib.sha256).hexdigest()
        if digest in known_secret_hmacs or _looks_high_entropy_secret(candidate):
            raise ValidationContractError("support_bundle.sensitive_detected")


def _looks_high_entropy_secret(value: str) -> bool:
    if len(value) < 24 or len(value) > 512:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,128}", value) or re.fullmatch(
        r"[a-z][a-z0-9_:-]{2,79}", value
    ):
        return False
    counts = Counter(value)
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value)) for count in counts.values()
    )
    categories = sum(
        any(predicate(character) for character in value)
        for predicate in (str.islower, str.isupper, str.isdigit, lambda item: not item.isalnum())
    )
    return entropy >= 4.25 and categories >= 3


def _safe_archive_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or not 2 <= len(path.parts) <= 4
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationContractError("support_bundle.manifest_invalid")


def _canonical(value: object) -> bytes:
    try:
        encoded = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError, UnicodeError):
        raise ValidationContractError("support_bundle.contract_invalid") from None
    if not encoded or len(encoded) > MAX_SUPPORT_BUNDLE_ENTRY_BYTES:
        raise ValidationContractError("support_bundle.contract_invalid")
    return encoded


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("time") from None
    if parsed.tzinfo is None or _utc_text(parsed) != value:
        raise ValueError("time")
    return parsed.astimezone(UTC)


def _aware_utc(value: datetime, *, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationContractError(code)
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise ValidationContractError("support_bundle.integrity_failed") from None
    return digest.hexdigest()


def _encryption_key(secret: bytes) -> bytes:
    if not 32 <= len(secret) <= 512:
        raise ValidationContractError("support_bundle.encryption_unavailable")
    return hashlib.sha256(_ENCRYPTION_DOMAIN + secret).digest()


__all__ = (
    "MAX_SUPPORT_BUNDLE_BYTES",
    "MAX_SUPPORT_BUNDLE_ENTRIES",
    "MAX_SUPPORT_BUNDLE_MILLISECONDS",
    "MAX_VALIDATION_MILLISECONDS",
    "SUPPORT_BUNDLE_COLLECTOR_CODES",
    "SUPPORT_BUNDLE_SCHEMA_VERSION",
    "SUPPORT_BUNDLE_SCAN_VERSION",
    "SUPPORT_BUNDLE_UPLOAD_SCHEMA_VERSION",
    "VALIDATION_CHECK_CODES",
    "VALIDATION_PLAN_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "DeploymentValidationReport",
    "EncryptedSupportBundle",
    "PreparedSupportBundle",
    "SupportBundleBuilder",
    "SupportBundleCollectorCode",
    "SupportBundleEntry",
    "SupportBundleManifest",
    "SupportBundleUploadMetadata",
    "SupportBundleUploadReceipt",
    "ValidationCheckCode",
    "ValidationCheckEvidence",
    "ValidationContractError",
    "decrypt_support_bundle",
    "encrypt_support_bundle",
    "support_bundle_manifest_sha256",
    "support_bundle_approval_proof_sha256",
    "validation_report_sha256",
)
