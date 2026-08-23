"""Strict signed-manifest protocol and atomic Gateway-local configuration owner."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_SNAPSHOT_ID = re.compile(r"^csn_[0-9a-f]{32}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,95}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "tenant_id",
        "generation",
        "revocation_watermark",
        "workspace",
        "placement",
        "entitlements",
        "policy",
        "provider_key_refs",
        "feature_capabilities",
        "source_sha256",
    }
)
_FULL_KEYS = frozenset(
    {
        "wire_schema_version",
        "kind",
        "snapshot_id",
        "workspace_id",
        "generation",
        "revocation_watermark",
        "manifest",
        "manifest_sha256",
        "kms_key_version",
        "signature_algorithm",
        "signature",
        "published_at",
        "stale_policy",
        "request_id",
        "trace_id",
    }
)
_DELTA_KEYS = (_FULL_KEYS - {"manifest"}) | {
    "from_generation",
    "from_manifest_sha256",
    "delta",
}
_FORBIDDEN_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "dsn",
        "endpoint_url",
        "password",
        "private_key",
        "prompt",
        "provider_secret",
        "response",
        "secret",
        "token",
    }
)


class ConfigDistributionError(ValueError):
    """Fixed-code protocol failure that never includes manifest or credential material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedConfigSnapshot:
    snapshot_id: str
    workspace_id: str
    tenant_id: str
    cell_code: str
    generation: int
    revocation_watermark: int
    manifest: Mapping[str, object]
    manifest_sha256: str
    kms_key_version: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class ConfigDecision:
    allowed: bool
    status: Literal["current", "lkg", "revocation_stale", "expired", "unavailable"]
    generation: int | None
    manifest_sha256: str | None
    reason_code: str


Verifier = Callable[[str, str, bytes, str], bool]


class ConfigSnapshotOwner:
    """Atomically owns one workspace/cell snapshot and applies monotonic integrity fencing."""

    def __init__(
        self,
        *,
        workspace_id: str,
        tenant_id: str,
        cell_code: str,
        verifier: Verifier,
        lkg_max_seconds: float = 30.0,
        revocation_max_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not (
            _WORKSPACE_ID.fullmatch(workspace_id)
            and _UUID.fullmatch(tenant_id)
            and _CODE.fullmatch(cell_code)
        ):
            raise ConfigDistributionError("config.owner_invalid")
        if not 0 < revocation_max_seconds <= lkg_max_seconds <= 3600:
            raise ConfigDistributionError("config.policy_invalid")
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._cell_code = cell_code
        self._verifier = verifier
        self._lkg_max_seconds = float(lkg_max_seconds)
        self._revocation_max_seconds = float(revocation_max_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._snapshot: VerifiedConfigSnapshot | None = None
        self._last_success_at: datetime | None = None
        self._reachable = True
        self._integrity_failed = False

    def apply(self, wire: object) -> bool:
        """Validate a full/delta wire completely before replacing the local atomic reference."""

        try:
            parsed = _wire(wire)
            with self._lock:
                previous = self._snapshot
            manifest = parsed["manifest"] if parsed["kind"] == "full" else _delta_manifest(
                parsed, previous
            )
            snapshot = self._verified_snapshot(parsed, manifest)
            with self._lock:
                current = self._snapshot
                if current is not None and snapshot.generation < current.generation:
                    raise ConfigDistributionError("config.generation_stale")
                if (
                    current is not None
                    and snapshot.revocation_watermark < current.revocation_watermark
                ):
                    raise ConfigDistributionError("config.revocation_stale")
                if current is not None and snapshot.generation == current.generation:
                    if snapshot.manifest_sha256 != current.manifest_sha256:
                        raise ConfigDistributionError("config.generation_conflict")
                    self._reachable = True
                    self._integrity_failed = False
                    self._last_success_at = self._now()
                    return False
                self._snapshot = snapshot
                self._last_success_at = self._now()
                self._reachable = True
                self._integrity_failed = False
                return True
        except ConfigDistributionError:
            with self._lock:
                self._integrity_failed = True
                self._reachable = False
            raise

    def mark_unreachable(self) -> None:
        """Record a proven transport outage without converting it into an integrity failure."""

        with self._lock:
            self._reachable = False

    def mark_integrity_failed(self) -> None:
        """Fence local decisions after an authenticated transport or protocol contradiction."""

        with self._lock:
            self._integrity_failed = True
            self._reachable = False

    def mark_current(self) -> None:
        """Accept an authenticated not-modified response as a fresh reachability observation."""

        with self._lock:
            if self._snapshot is None:
                raise ConfigDistributionError("config.integrity_unavailable")
            self._last_success_at = self._now()
            self._reachable = True
            self._integrity_failed = False

    def restore(self, wire: object, *, last_success_at: datetime) -> bool:
        """Reverify a persisted full wire while preserving its bounded LKG age."""

        now = self._now()
        if last_success_at.tzinfo is None or last_success_at.astimezone(UTC) > now:
            with self._lock:
                self._integrity_failed = True
                self._reachable = False
            raise ConfigDistributionError("config.payload_invalid")
        changed = self.apply(wire)
        with self._lock:
            self._last_success_at = last_success_at.astimezone(UTC)
            self._reachable = False
        return changed

    def decision(
        self,
        *,
        required_revocation_watermark: int = 0,
        security_sensitive: bool = True,
    ) -> ConfigDecision:
        """Return a bounded local decision without network, storage, or mutable caller overrides."""

        if type(required_revocation_watermark) is not int or required_revocation_watermark < 0:
            raise ConfigDistributionError("config.revocation_invalid")
        now = self._now()
        with self._lock:
            snapshot = self._snapshot
            last_success = self._last_success_at
            reachable = self._reachable
            integrity_failed = self._integrity_failed
        if snapshot is None or last_success is None or integrity_failed:
            return ConfigDecision(False, "unavailable", None, None, "config.integrity_unavailable")
        if required_revocation_watermark > snapshot.revocation_watermark:
            return ConfigDecision(
                False,
                "revocation_stale",
                snapshot.generation,
                snapshot.manifest_sha256,
                "config.revocation_stale",
            )
        age = max(0.0, (now - last_success).total_seconds())
        if reachable:
            return ConfigDecision(
                True, "current", snapshot.generation, snapshot.manifest_sha256, "config.current"
            )
        limit = self._revocation_max_seconds if security_sensitive else self._lkg_max_seconds
        if age <= limit:
            return ConfigDecision(
                True, "lkg", snapshot.generation, snapshot.manifest_sha256, "config.lkg"
            )
        status: Literal["revocation_stale", "expired"] = (
            "revocation_stale" if security_sensitive else "expired"
        )
        return ConfigDecision(
            False,
            status,
            snapshot.generation,
            snapshot.manifest_sha256,
            "config.revocation_stale" if security_sensitive else "config.lkg_expired",
        )

    def snapshot(self) -> VerifiedConfigSnapshot | None:
        with self._lock:
            return self._snapshot

    def _verified_snapshot(
        self, wire: Mapping[str, object], manifest: Mapping[str, object]
    ) -> VerifiedConfigSnapshot:
        normalized = _manifest(manifest)
        if (
            normalized["workspace_id"] != self._workspace_id
            or normalized["tenant_id"] != self._tenant_id
            or wire["workspace_id"] != self._workspace_id
            or normalized["placement"]["cell_code"] != self._cell_code  # type: ignore[index]
        ):
            raise ConfigDistributionError("config.owner_invalid")
        canonical = canonical_json_bytes(normalized)
        digest = hashlib.sha256(canonical).hexdigest()
        if digest != wire["manifest_sha256"]:
            raise ConfigDistributionError("config.hash_invalid")
        if not self._verifier(
            str(wire["signature_algorithm"]),
            str(wire["kms_key_version"]),
            canonical,
            str(wire["signature"]),
        ):
            raise ConfigDistributionError("config.signature_invalid")
        if (
            normalized["generation"] != wire["generation"]
            or normalized["revocation_watermark"] != wire["revocation_watermark"]
        ):
            raise ConfigDistributionError("config.owner_invalid")
        return VerifiedConfigSnapshot(
            snapshot_id=str(wire["snapshot_id"]),
            workspace_id=self._workspace_id,
            tenant_id=self._tenant_id,
            cell_code=self._cell_code,
            generation=int(wire["generation"]),
            revocation_watermark=int(wire["revocation_watermark"]),
            manifest=normalized,
            manifest_sha256=digest,
            kms_key_version=str(wire["kms_key_version"]),
            published_at=_time(wire["published_at"]),
        )


def create_manifest_delta(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, object]:
    """Create a deterministic top-level replacement delta without accepting executable paths."""

    old = _manifest(before)
    new = _manifest(after)
    changes = {key: new[key] for key in sorted(_MANIFEST_KEYS) if old[key] != new[key]}
    return {"changes": changes, "removed": []}


def apply_manifest_delta(
    before: Mapping[str, object], delta: Mapping[str, object]
) -> dict[str, object]:
    current = dict(_manifest(before))
    if set(delta) != {"changes", "removed"} or delta.get("removed") != []:
        raise ConfigDistributionError("config.delta_invalid")
    changes = delta.get("changes")
    if type(changes) is not dict or not set(changes) <= _MANIFEST_KEYS:
        raise ConfigDistributionError("config.delta_invalid")
    current.update(changes)
    return _manifest(current)


def canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ConfigDistributionError("config.payload_invalid") from None
    if len(encoded) > 131_072:
        raise ConfigDistributionError("config.payload_invalid")
    return encoded


def test_kms_verifier(master_key: str, *, key_version: str = "test-v1") -> Verifier:
    """Build a trusted test verifier without retaining the derived private key object."""

    if not isinstance(master_key, str) or len(master_key.encode("utf-8")) < 32:
        raise ConfigDistributionError("config.kms_unavailable")
    seed = hashlib.sha256(b"prd108/test-kms-signing/v1\0" + master_key.encode("utf-8")).digest()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    public = Ed25519PrivateKey.from_private_bytes(seed).public_key()

    def verify(algorithm: str, version: str, payload: bytes, signature: str) -> bool:
        if algorithm != "ed25519" or version != key_version:
            return False
        try:
            public.verify(_unb64(signature), payload)
            return True
        except Exception:
            return False

    return verify


def _wire(value: object) -> dict[str, object]:
    if type(value) is not dict or value.get("kind") not in {"full", "delta"}:
        raise ConfigDistributionError("config.payload_invalid")
    expected = _FULL_KEYS if value["kind"] == "full" else _DELTA_KEYS
    if set(value) != expected:
        raise ConfigDistributionError("config.payload_invalid")
    if (
        value.get("wire_schema_version") != 1
        or not _SNAPSHOT_ID.fullmatch(str(value.get("snapshot_id") or ""))
        or not _WORKSPACE_ID.fullmatch(str(value.get("workspace_id") or ""))
        or type(value.get("generation")) is not int
        or value["generation"] < 1
        or type(value.get("revocation_watermark")) is not int
        or value["revocation_watermark"] < 0
        or not _HASH.fullmatch(str(value.get("manifest_sha256") or ""))
        or value.get("signature_algorithm") not in {"ed25519", "rsassa_pss_sha256"}
        or not 3 <= len(str(value.get("kms_key_version") or "")) <= 96
        or not _B64.fullmatch(str(value.get("signature") or ""))
        or set(value.get("stale_policy") or {}) != {"lkg_max_seconds", "revocation_max_seconds"}
        or value["stale_policy"] != {"lkg_max_seconds": 30, "revocation_max_seconds": 2}
        or not 1 <= len(str(value.get("request_id") or "")) <= 128
        or not 1 <= len(str(value.get("trace_id") or "")) <= 128
    ):
        raise ConfigDistributionError("config.payload_invalid")
    _time(value["published_at"])
    if value["kind"] == "full":
        _manifest(value["manifest"])
    else:
        if (
            type(value.get("from_generation")) is not int
            or not 1 <= value["from_generation"] < value["generation"]
            or not _HASH.fullmatch(str(value.get("from_manifest_sha256") or ""))
            or type(value.get("delta")) is not dict
        ):
            raise ConfigDistributionError("config.delta_invalid")
    return dict(value)


def _delta_manifest(
    wire: Mapping[str, object], previous: VerifiedConfigSnapshot | None
) -> dict[str, object]:
    if (
        previous is None
        or previous.generation != wire["from_generation"]
        or previous.manifest_sha256 != wire["from_manifest_sha256"]
    ):
        raise ConfigDistributionError("config.delta_base_invalid")
    return apply_manifest_delta(previous.manifest, wire["delta"])  # type: ignore[arg-type]


def _manifest(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _MANIFEST_KEYS:
        raise ConfigDistributionError("config.manifest_invalid")
    if (
        value.get("schema_version") != 1
        or not _WORKSPACE_ID.fullmatch(str(value.get("workspace_id") or ""))
        or not _UUID.fullmatch(str(value.get("tenant_id") or ""))
        or type(value.get("generation")) is not int
        or value["generation"] < 1
        or type(value.get("revocation_watermark")) is not int
        or value["revocation_watermark"] < 0
        or not _HASH.fullmatch(str(value.get("source_sha256") or ""))
    ):
        raise ConfigDistributionError("config.manifest_invalid")
    source = dict(value)
    source_sha256 = str(source.pop("source_sha256"))
    if hashlib.sha256(canonical_json_bytes(source)).hexdigest() != source_sha256:
        raise ConfigDistributionError("config.hash_invalid")
    workspace = value.get("workspace")
    placement = value.get("placement")
    entitlements = value.get("entitlements")
    policy = value.get("policy")
    if (
        type(workspace) is not dict
        or set(workspace) != {"status", "revision"}
        or workspace.get("status")
        not in {"active", "suspended", "deletion_pending", "deleted", "migration_conflict"}
        or type(workspace.get("revision")) is not int
        or workspace["revision"] < 1
        or type(placement) is not dict
        or set(placement) != {"cell_code", "region_code", "revision"}
        or not _CODE.fullmatch(str(placement.get("cell_code") or ""))
        or not _CODE.fullmatch(str(placement.get("region_code") or ""))
        or type(placement.get("revision")) is not int
        or placement["revision"] < 1
        or not _optional_projection(entitlements)
        or not _optional_projection(policy)
    ):
        raise ConfigDistributionError("config.manifest_invalid")
    refs = value.get("provider_key_refs")
    features = value.get("feature_capabilities")
    if (
        type(refs) is not list
        or len(refs) > 500
        or type(features) is not list
        or len(features) > 500
    ):
        raise ConfigDistributionError("config.manifest_invalid")
    for item in refs:
        if (
            type(item) is not dict
            or set(item) != {"credential_id", "provider_id", "status"}
            or not _UUID.fullmatch(str(item.get("credential_id") or ""))
            or not _UUID.fullmatch(str(item.get("provider_id") or ""))
            or item.get("status") not in {"active", "disabled"}
        ):
            raise ConfigDistributionError("config.manifest_invalid")
    for item in features:
        if (
            type(item) is not dict
            or set(item) != {"flag_code", "version", "enabled", "kill_generation"}
            or not _CODE.fullmatch(str(item.get("flag_code") or ""))
            or type(item.get("version")) is not int
            or item["version"] < 1
            or type(item.get("enabled")) is not bool
            or type(item.get("kill_generation")) is not int
            or item["kill_generation"] < 0
        ):
            raise ConfigDistributionError("config.manifest_invalid")
    _reject_forbidden_names(value)
    canonical_json_bytes(value)
    return dict(value)


def _optional_projection(value: object) -> bool:
    return value is None or (
        type(value) is dict
        and set(value) == {"version", "sha256"}
        and type(value.get("version")) is int
        and value["version"] >= 1
        and _HASH.fullmatch(str(value.get("sha256") or "")) is not None
    )


def _reject_forbidden_names(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_NAMES or normalized.endswith(
                ("_secret", "_token", "_password")
            ):
                raise ConfigDistributionError("config.sensitive_field")
            _reject_forbidden_names(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_names(item)


def _time(value: object) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ConfigDistributionError("config.payload_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigDistributionError("config.payload_invalid") from None
    if parsed.tzinfo is None:
        raise ConfigDistributionError("config.payload_invalid")
    return parsed.astimezone(UTC)


def _unb64(value: str) -> bytes:
    if not value or "=" in value or _B64.fullmatch(value) is None:
        raise ValueError
    return base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
    )
