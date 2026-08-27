"""Signed private-Gateway entitlements and an in-memory capacity hot path."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .desired_state import canonical_json_bytes, strict_json_object

LICENSE_SCHEMA_VERSION = "metahub.edge-license-entitlement/v1"
LICENSE_SIGNATURE_PURPOSE = "metahub.edge-license.entitlement/v1"
MAX_LICENSE_DOCUMENT_BYTES = 32_768
MINIMUM_OFFLINE_GRACE = timedelta(days=7)
MAXIMUM_OFFLINE_GRACE = timedelta(days=30)
_CLOCK_SKEW = timedelta(minutes=5)
_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_TENANT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_FEATURE_CODES = frozenset(
    {
        "advanced_routing",
        "audit_export",
        "diagnostic_support",
        "policy_enforcement",
        "telemetry_export",
    }
)


class LicenseContractError(ValueError):
    """固定错误码异常，禁止把签名材料或商业正文带入日志。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LicenseCapacity(_StrictModel):
    node_limit: int = Field(ge=1, le=100_000)
    concurrent_request_limit: int = Field(ge=1, le=10_000_000)
    features: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _canonical_features(self) -> LicenseCapacity:
        if self.features != sorted(set(self.features)) or any(
            item not in _FEATURE_CODES for item in self.features
        ):
            raise ValueError("edge_license.capacity_invalid")
        return self


class LicenseSignature(_StrictModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._:-]+$")
    public_key: str = Field(min_length=43, max_length=43, repr=False)
    value: str = Field(min_length=86, max_length=86, repr=False)


class LicenseEntitlement(_StrictModel):
    schema_version: Literal["metahub.edge-license-entitlement/v1"] = LICENSE_SCHEMA_VERSION
    entitlement_id: str = Field(pattern=r"^let_[0-9a-f]{32}$")
    license_id: str = Field(pattern=r"^lic_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tenant_id: str
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    device_generation: int = Field(ge=1)
    commercial_source_type: Literal["entitlement_snapshot", "subscription_order"]
    commercial_source_id: str = Field(min_length=4, max_length=40)
    commercial_source_revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    refresh_counter: int = Field(ge=1)
    revocation_generation: int = Field(ge=0)
    status: Literal["active", "revoked"]
    capacity: LicenseCapacity
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    grace_ends_at: datetime
    signature_purpose: Literal["metahub.edge-license.entitlement/v1"] = LICENSE_SIGNATURE_PURPOSE

    @model_validator(mode="after")
    def _consistent(self) -> LicenseEntitlement:
        issued = _aware(self.issued_at)
        not_before = _aware(self.not_before)
        expires = _aware(self.expires_at)
        grace_ends = _aware(self.grace_ends_at)
        source_prefix = "ent_" if self.commercial_source_type == "entitlement_snapshot" else "ord_"
        if (
            _TENANT_ID.fullmatch(self.tenant_id) is None
            or not self.commercial_source_id.startswith(source_prefix)
            or re.fullmatch(r"^[a-z]{3}_[0-9a-f]{32}$", self.commercial_source_id) is None
            or issued > not_before + _CLOCK_SKEW
            or expires <= not_before
            or grace_ends - expires < MINIMUM_OFFLINE_GRACE
            or grace_ends - expires > MAXIMUM_OFFLINE_GRACE
            or self.refresh_counter < self.generation
        ):
            raise ValueError("edge_license.entitlement_invalid")
        return self


class SignedEntitlement(_StrictModel):
    entitlement: LicenseEntitlement = Field(repr=False)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_purpose: Literal["metahub.edge-license.entitlement/v1"] = LICENSE_SIGNATURE_PURPOSE
    signature: LicenseSignature = Field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedEntitlement:
    signed: SignedEntitlement
    canonical_manifest: bytes


@dataclass(frozen=True, slots=True)
class LicenseDecision:
    status: Literal[
        "current",
        "grace",
        "restricted",
        "revoked",
        "not_yet_valid",
        "clock_rollback",
        "unavailable",
        "integrity_failed",
    ]
    generation: int | None
    manifest_sha256: str | None
    existing_work_allowed: bool
    new_capacity_allowed: bool
    reason_code: str
    expires_at: datetime | None
    grace_ends_at: datetime | None


@dataclass(frozen=True, slots=True)
class CapacityLease:
    lease_id: str
    dimension: Literal["node", "concurrent_request"]
    units: int
    entitlement_generation: int
    acquired_at: datetime
    expires_at: datetime


def sign_license_entitlement(
    entitlement: LicenseEntitlement,
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> SignedEntitlement:
    """使用独立用途域签名规范 entitlement，避免与配置或升级密钥混用。"""

    manifest = canonical_json_bytes(
        entitlement.model_dump(mode="json"), maximum=MAX_LICENSE_DOCUMENT_BYTES
    )
    return SignedEntitlement(
        entitlement=entitlement,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        signature=LicenseSignature(
            key_id=key_id,
            public_key=_b64(private_key.public_key().public_bytes_raw()),
            value=_b64(
                private_key.sign(LICENSE_SIGNATURE_PURPOSE.encode("ascii") + b"\0" + manifest)
            ),
        ),
    )


def verify_license_entitlement(
    value: object, *, trusted_keys: Mapping[str, bytes]
) -> VerifiedEntitlement:
    """严格验签并拒绝未知字段、hash 漂移、敏感字段和非信任 key。"""

    try:
        _reject_sensitive_fields(value)
        signed = SignedEntitlement.model_validate_json(
            canonical_json_bytes(value, maximum=MAX_LICENSE_DOCUMENT_BYTES)
        )
        manifest = canonical_json_bytes(
            signed.entitlement.model_dump(mode="json"),
            maximum=MAX_LICENSE_DOCUMENT_BYTES,
        )
        if not hmac.compare_digest(hashlib.sha256(manifest).hexdigest(), signed.manifest_sha256):
            raise LicenseContractError("edge_license.manifest_hash_invalid")
        key = trusted_keys.get(signed.signature.key_id)
        if key is None or len(key) != 32:
            raise LicenseContractError("edge_license.key_untrusted")
        public = _unb64(signed.signature.public_key, expected=32)
        if not hmac.compare_digest(public, key):
            raise LicenseContractError("edge_license.key_untrusted")
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(
                _unb64(signed.signature.value, expected=64),
                LICENSE_SIGNATURE_PURPOSE.encode("ascii") + b"\0" + manifest,
            )
        except (InvalidSignature, ValueError):
            raise LicenseContractError("edge_license.signature_invalid") from None
        return VerifiedEntitlement(signed=signed, canonical_manifest=manifest)
    except LicenseContractError:
        raise
    except (RecursionError, TypeError, ValueError):
        raise LicenseContractError("edge_license.entitlement_invalid") from None


class EdgeLicenseStore:
    """原子保存 entitlement，并让请求热路径只读内存快照与 lease 计数。"""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        tenant_id: str,
        deployment_id: str,
        device_generation: int,
        trusted_keys: Mapping[str, bytes],
    ) -> None:
        if (
            _WORKSPACE_ID.fullmatch(workspace_id) is None
            or _TENANT_ID.fullmatch(tenant_id) is None
            or _DEPLOYMENT_ID.fullmatch(deployment_id) is None
            or type(device_generation) is not int
            or device_generation < 1
            or not trusted_keys
        ):
            raise LicenseContractError("edge_license.owner_invalid")
        self._root = root
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._deployment_id = deployment_id
        self._device_generation = device_generation
        self._trusted_keys = dict(trusted_keys)
        self._lock = threading.RLock()
        self._verified: VerifiedEntitlement | None = None
        self._integrity_failed = False
        self._leases: dict[str, CapacityLease] = {}
        self._checkpoint_wall = datetime.now(UTC)
        self._checkpoint_monotonic = time.monotonic()
        self._load()

    def apply(
        self,
        value: object,
        *,
        now: datetime | None = None,
        minimum_revocation_generation: int = 0,
    ) -> bool:
        """先完成全合同验证，再以 generation/hash CAS 原子替换本地 head。"""

        instant = _aware(now or datetime.now(UTC))
        verified = verify_license_entitlement(value, trusted_keys=self._trusted_keys)
        entitlement = verified.signed.entitlement
        with self._lock:
            if (
                entitlement.workspace_id != self._workspace_id
                or entitlement.tenant_id != self._tenant_id
                or entitlement.deployment_id != self._deployment_id
                or entitlement.device_generation != self._device_generation
            ):
                self._integrity_failed = True
                raise LicenseContractError("edge_license.owner_mismatch")
            if entitlement.revocation_generation < minimum_revocation_generation:
                self._integrity_failed = True
                raise LicenseContractError("edge_license.revocation_stale")
            if _aware(entitlement.issued_at) > instant + _CLOCK_SKEW:
                self._integrity_failed = True
                raise LicenseContractError("edge_license.time_invalid")
            current = self._verified
            if current is not None:
                old = current.signed.entitlement
                if entitlement.license_id != old.license_id:
                    self._integrity_failed = True
                    raise LicenseContractError("edge_license.license_mismatch")
                if entitlement.generation < old.generation:
                    self._integrity_failed = True
                    raise LicenseContractError("edge_license.generation_stale")
                if entitlement.generation == old.generation:
                    if not hmac.compare_digest(
                        verified.signed.manifest_sha256,
                        current.signed.manifest_sha256,
                    ):
                        self._integrity_failed = True
                        raise LicenseContractError("edge_license.generation_conflict")
                    return False
                if entitlement.refresh_counter <= old.refresh_counter:
                    self._integrity_failed = True
                    raise LicenseContractError("edge_license.refresh_stale")
                if entitlement.revocation_generation < old.revocation_generation:
                    self._integrity_failed = True
                    raise LicenseContractError("edge_license.revocation_stale")
            self._store(verified, instant)
            self._verified = verified
            self._integrity_failed = False
            if entitlement.status == "revoked":
                self._leases.clear()
            return True

    def decision(self, *, now: datetime | None = None) -> LicenseDecision:
        """用内存 entitlement 和单调时间检查返回不触网、不落盘的授权决定。"""

        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            return self._decision_locked(instant)

    def acquire(
        self,
        lease_id: str,
        *,
        dimension: Literal["node", "concurrent_request"],
        units: int = 1,
        ttl: timedelta = timedelta(minutes=10),
        feature: str | None = None,
        now: datetime | None = None,
    ) -> CapacityLease:
        """在单一 immutable snapshot 下原子计数，过期或宽限状态禁止新增。"""

        instant = _aware(now or datetime.now(UTC))
        if (
            re.fullmatch(r"lsl_[0-9a-f]{32}", lease_id) is None
            or type(units) is not int
            or units < 1
            or not timedelta(seconds=1) <= ttl <= timedelta(hours=24)
        ):
            raise LicenseContractError("edge_license.lease_invalid")
        with self._lock:
            self._reap_locked(instant)
            existing = self._leases.get(lease_id)
            if existing is not None:
                if existing.dimension != dimension or existing.units != units:
                    raise LicenseContractError("edge_license.lease_conflict")
                return existing
            decision = self._decision_locked(instant)
            if not decision.new_capacity_allowed or self._verified is None:
                raise LicenseContractError("edge_license.capacity_restricted")
            entitlement = self._verified.signed.entitlement
            if feature is not None and feature not in entitlement.capacity.features:
                raise LicenseContractError("edge_license.feature_unavailable")
            limit = (
                entitlement.capacity.node_limit
                if dimension == "node"
                else entitlement.capacity.concurrent_request_limit
            )
            used = sum(
                lease.units for lease in self._leases.values() if lease.dimension == dimension
            )
            if used + units > limit:
                raise LicenseContractError("edge_license.capacity_exceeded")
            lease = CapacityLease(
                lease_id=lease_id,
                dimension=dimension,
                units=units,
                entitlement_generation=entitlement.generation,
                acquired_at=instant,
                expires_at=instant + ttl,
            )
            self._leases[lease_id] = lease
            return lease

    def release(self, lease_id: str) -> bool:
        """幂等释放本地容量，不生成任何数据访问权或云端调用。"""

        with self._lock:
            return self._leases.pop(lease_id, None) is not None

    def active_units(
        self,
        dimension: Literal["node", "concurrent_request"],
        *,
        now: datetime | None = None,
    ) -> int:
        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            self._reap_locked(instant)
            return sum(
                lease.units for lease in self._leases.values() if lease.dimension == dimension
            )

    def snapshot(self) -> VerifiedEntitlement | None:
        with self._lock:
            return self._verified

    def checkpoint_trusted_time(self, now: datetime | None = None) -> None:
        """刷新时持久可信 wall checkpoint，热路径本身不执行磁盘写入。"""

        instant = _aware(now or datetime.now(UTC))
        with self._lock:
            floor = self._trusted_floor()
            if instant < floor - _CLOCK_SKEW:
                raise LicenseContractError("edge_license.clock_rollback")
            self._set_checkpoint(max(instant, floor))
            self._write_checkpoint()

    def _decision_locked(self, instant: datetime) -> LicenseDecision:
        if self._verified is None:
            status = "integrity_failed" if self._integrity_failed else "unavailable"
            return _decision(status, None, None)
        signed = self._verified.signed
        entitlement = signed.entitlement
        if entitlement.status == "revoked":
            return _decision("revoked", entitlement, signed.manifest_sha256)
        if instant < self._trusted_floor() - _CLOCK_SKEW:
            return _decision("clock_rollback", entitlement, signed.manifest_sha256)
        if instant + _CLOCK_SKEW < _aware(entitlement.not_before):
            return _decision("not_yet_valid", entitlement, signed.manifest_sha256)
        if instant < _aware(entitlement.expires_at):
            return _decision("current", entitlement, signed.manifest_sha256)
        if instant < _aware(entitlement.grace_ends_at):
            return _decision("grace", entitlement, signed.manifest_sha256)
        return _decision("restricted", entitlement, signed.manifest_sha256)

    def _load(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        checkpoint = self._root / "trusted-time.json"
        if checkpoint.exists():
            try:
                value = strict_json_object(checkpoint.read_bytes(), canonical=True, maximum=4096)
                if set(value) != {"trusted_wall_time", "version"} or value["version"] != 1:
                    raise ValueError
                self._checkpoint_wall = _parse_time(str(value["trusted_wall_time"]))
            except (LicenseContractError, OSError, ValueError):
                self._integrity_failed = True
        self._checkpoint_monotonic = time.monotonic()
        head = self._root / "head.json"
        if not head.exists():
            return
        try:
            value = strict_json_object(
                head.read_bytes(), canonical=True, maximum=MAX_LICENSE_DOCUMENT_BYTES
            )
            verified = verify_license_entitlement(value, trusted_keys=self._trusted_keys)
            entitlement = verified.signed.entitlement
            if (
                entitlement.workspace_id != self._workspace_id
                or entitlement.tenant_id != self._tenant_id
                or entitlement.deployment_id != self._deployment_id
                or entitlement.device_generation != self._device_generation
            ):
                raise LicenseContractError("edge_license.owner_mismatch")
            self._verified = verified
        except (LicenseContractError, OSError, ValueError):
            self._integrity_failed = True

    def _store(self, verified: VerifiedEntitlement, instant: datetime) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self._root / "head.json",
            canonical_json_bytes(
                verified.signed.model_dump(mode="json"),
                maximum=MAX_LICENSE_DOCUMENT_BYTES,
            ),
        )
        self._set_checkpoint(instant)
        self._write_checkpoint()

    def _write_checkpoint(self) -> None:
        _atomic_write(
            self._root / "trusted-time.json",
            canonical_json_bytes(
                {
                    "trusted_wall_time": self._checkpoint_wall.isoformat().replace("+00:00", "Z"),
                    "version": 1,
                },
                maximum=4096,
            ),
        )

    def _set_checkpoint(self, instant: datetime) -> None:
        self._checkpoint_wall = instant
        self._checkpoint_monotonic = time.monotonic()

    def _trusted_floor(self) -> datetime:
        elapsed = max(0.0, time.monotonic() - self._checkpoint_monotonic)
        return self._checkpoint_wall + timedelta(seconds=elapsed)

    def _reap_locked(self, instant: datetime) -> None:
        expired = [key for key, lease in self._leases.items() if lease.expires_at <= instant]
        for key in expired:
            del self._leases[key]


def _decision(
    status: str,
    entitlement: LicenseEntitlement | None,
    manifest_sha256: str | None,
) -> LicenseDecision:
    current = status == "current"
    existing = status in {"current", "grace", "clock_rollback"}
    return LicenseDecision(
        status=status,  # type: ignore[arg-type]
        generation=entitlement.generation if entitlement is not None else None,
        manifest_sha256=manifest_sha256,
        existing_work_allowed=existing,
        new_capacity_allowed=current,
        reason_code=f"edge_license.{status}",
        expires_at=_aware(entitlement.expires_at) if entitlement is not None else None,
        grace_ends_at=(_aware(entitlement.grace_ends_at) if entitlement is not None else None),
    )


def _reject_sensitive_fields(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise LicenseContractError("edge_license.entitlement_invalid")
    if type(value) is dict:
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(
                marker in lowered
                for marker in (
                    "credential",
                    "password",
                    "private_key",
                    "provider_key",
                    "prompt",
                    "response_body",
                    "secret",
                    "token",
                )
            ):
                raise LicenseContractError("edge_license.sensitive_field_denied")
            _reject_sensitive_fields(item, depth=depth + 1)
    elif type(value) is list:
        for item in value:
            _reject_sensitive_fields(item, depth=depth + 1)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise LicenseContractError("edge_license.storage_unavailable") from exc


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LicenseContractError("edge_license.time_invalid")
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        raise LicenseContractError("edge_license.time_invalid") from None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str, *, expected: int) -> bytes:
    try:
        decoded = base64.b64decode(
            value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, UnicodeError):
        raise LicenseContractError("edge_license.signature_invalid") from None
    if len(decoded) != expected:
        raise LicenseContractError("edge_license.signature_invalid")
    return decoded


__all__ = (
    "CapacityLease",
    "EdgeLicenseStore",
    "LICENSE_SCHEMA_VERSION",
    "LICENSE_SIGNATURE_PURPOSE",
    "LicenseCapacity",
    "LicenseContractError",
    "LicenseDecision",
    "LicenseEntitlement",
    "LicenseSignature",
    "SignedEntitlement",
    "VerifiedEntitlement",
    "sign_license_entitlement",
    "verify_license_entitlement",
)
