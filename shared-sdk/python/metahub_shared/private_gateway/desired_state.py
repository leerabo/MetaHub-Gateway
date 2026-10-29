"""RFC 8785 signed desired state and crash-safe Edge-local LKG ownership."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from metahub_shared.config_distribution import validate_config_manifest

_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_TENANT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_SNAPSHOT_ID = re.compile(r"^edsn_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^esa_[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_KID = re.compile(r"^edge-ds-[a-z0-9][a-z0-9._-]{2,47}$")
_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_DOCUMENT_BYTES = 524_288
_MAX_TRUST_CHAIN = 32
_CLOCK_SKEW = timedelta(minutes=5)
_DESIRED_DOMAIN = "metahub.desired-state.v1"
_TRUST_DOMAIN = "metahub.edge-trust-manifest.v1"
_ENVELOPE_KEYS = frozenset(
    {
        "domain",
        "protocol_version",
        "snapshot_id",
        "artifact_id",
        "workspace_id",
        "tenant_id",
        "deployment_id",
        "generation",
        "base_generation",
        "revocation_generation",
        "issued_at",
        "not_before",
        "security_expires_at",
        "expires_at",
        "alg",
        "kid",
        "content_sha256",
        "content",
        "signature",
    }
)
_TRUST_KEYS = frozenset(
    {
        "domain",
        "protocol_version",
        "generation",
        "issued_at",
        "expires_at",
        "signer_kid",
        "current",
        "next",
        "revoked_kids",
        "signature",
    }
)
_CONTENT_KEYS = frozenset({"schema_version", "deployment", "source_manifest"})
_KEY_KEYS = frozenset({"alg", "kid", "public_key"})


class EdgeDesiredStateError(ValueError):
    """Fixed-code failure that never includes signed content or key material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedTrustManifest:
    generation: int
    manifest_sha256: str
    signer_kid: str
    current_kid: str
    keys: Mapping[str, bytes]
    revoked_kids: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    document: Mapping[str, object]


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedDesiredState:
    snapshot_id: str
    artifact_id: str
    workspace_id: str
    tenant_id: str
    deployment_id: str
    generation: int
    base_generation: int
    revocation_generation: int
    envelope_sha256: str
    content_sha256: str
    kid: str
    issued_at: datetime
    not_before: datetime
    security_expires_at: datetime
    expires_at: datetime
    content: Mapping[str, object]
    envelope: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EdgeDesiredStateDecision:
    configuration_allowed: bool
    security_allowed: bool
    status: Literal[
        "current",
        "lkg",
        "stale",
        "expired",
        "unavailable",
        "integrity_failed",
    ]
    generation: int | None
    envelope_sha256: str | None
    reason_code: str


def canonical_json_bytes(value: object, *, maximum: int = _MAX_DOCUMENT_BYTES) -> bytes:
    """Encode one RFC 8785 JSON value with a fixed resource ceiling."""

    try:
        encoded = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError, UnicodeError):
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid") from None
    if not 0 < len(encoded) <= maximum:
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    return encoded


def strict_json_object(
    value: bytes,
    *,
    canonical: bool,
    maximum: int = _MAX_DOCUMENT_BYTES,
) -> dict[str, object]:
    """Parse JSON while rejecting duplicate keys, non-finite values, and oversized input."""

    if not isinstance(value, bytes) or not 0 < len(value) <= maximum:
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
            result[key] = item
        return result

    def invalid_constant(_value: str) -> object:
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")

    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except EdgeDesiredStateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid") from None
    if type(parsed) is not dict or (
        canonical and canonical_json_bytes(parsed, maximum=maximum) != value
    ):
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    return parsed


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return _b64(public)


def sign_trust_manifest(
    unsigned: Mapping[str, object], private_key: Ed25519PrivateKey
) -> dict[str, object]:
    candidate = {**dict(unsigned), "signature": ""}
    _trust_shape(candidate)
    payload = canonical_json_bytes(dict(unsigned))
    return {**dict(unsigned), "signature": _b64(private_key.sign(payload))}


def sign_desired_state(
    unsigned: Mapping[str, object], private_key: Ed25519PrivateKey
) -> dict[str, object]:
    candidate = {**dict(unsigned), "signature": ""}
    _envelope_shape(candidate)
    payload = canonical_json_bytes(dict(unsigned))
    return {**dict(unsigned), "signature": _b64(private_key.sign(payload))}


def verify_trust_manifest(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    now: datetime,
    allow_expired: bool = False,
) -> VerifiedTrustManifest:
    document = _trust_shape(value)
    issued_at = _time(document["issued_at"])
    expires_at = _time(document["expires_at"])
    checked_now = _aware_now(now)
    if issued_at > checked_now + _CLOCK_SKEW or expires_at <= issued_at:
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    if not allow_expired and expires_at <= checked_now:
        raise EdgeDesiredStateError("edge_desired_state.trust_expired")
    signer_kid = str(document["signer_kid"])
    signer = trusted_keys.get(signer_kid)
    if signer is None:
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    unsigned = {key: item for key, item in document.items() if key != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(signer).verify(
            _unb64(str(document["signature"]), expected=64),
            canonical_json_bytes(unsigned),
        )
    except (InvalidSignature, ValueError):
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid") from None
    current_kid, current_key = _trust_key(document["current"])
    keys = {current_kid: current_key}
    if document["next"] is not None:
        next_kid, next_key = _trust_key(document["next"])
        if next_kid == current_kid:
            raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
        keys[next_kid] = next_key
    revoked = frozenset(str(item) for item in document["revoked_kids"])
    if current_kid in revoked or not set(revoked) <= set(trusted_keys) | set(keys):
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    digest = hashlib.sha256(canonical_json_bytes(document)).hexdigest()
    return VerifiedTrustManifest(
        generation=int(document["generation"]),
        manifest_sha256=digest,
        signer_kid=signer_kid,
        current_kid=current_kid,
        keys=keys,
        revoked_kids=revoked,
        issued_at=issued_at,
        expires_at=expires_at,
        document=document,
    )


def verify_desired_state(
    value: object,
    *,
    trust: VerifiedTrustManifest,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    now: datetime,
    allow_expired: bool = False,
) -> VerifiedDesiredState:
    envelope = _envelope_shape(value)
    if (
        envelope["workspace_id"] != workspace_id
        or envelope["tenant_id"] != tenant_id
        or envelope["deployment_id"] != deployment_id
    ):
        raise EdgeDesiredStateError("edge_desired_state.owner_invalid")
    kid = str(envelope["kid"])
    public_key = trust.keys.get(kid)
    if public_key is None or kid in trust.revoked_kids:
        raise EdgeDesiredStateError("edge_desired_state.signature_invalid")
    issued_at = _time(envelope["issued_at"])
    not_before = _time(envelope["not_before"])
    security_expires_at = _time(envelope["security_expires_at"])
    expires_at = _time(envelope["expires_at"])
    checked_now = _aware_now(now)
    if not (
        issued_at <= not_before <= security_expires_at <= expires_at
        and issued_at <= checked_now + _CLOCK_SKEW
    ):
        raise EdgeDesiredStateError("edge_desired_state.time_invalid")
    if not_before > checked_now + _CLOCK_SKEW:
        raise EdgeDesiredStateError("edge_desired_state.not_yet_valid")
    if not allow_expired and expires_at <= checked_now:
        raise EdgeDesiredStateError("edge_desired_state.expired")
    content = _content(envelope["content"], deployment_id=deployment_id)
    content_sha256 = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    if content_sha256 != envelope["content_sha256"]:
        raise EdgeDesiredStateError("edge_desired_state.hash_invalid")
    unsigned = {key: item for key, item in envelope.items() if key != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            _unb64(str(envelope["signature"]), expected=64),
            canonical_json_bytes(unsigned),
        )
    except (InvalidSignature, ValueError):
        raise EdgeDesiredStateError("edge_desired_state.signature_invalid") from None
    envelope_sha256 = hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()
    return VerifiedDesiredState(
        snapshot_id=str(envelope["snapshot_id"]),
        artifact_id=str(envelope["artifact_id"]),
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        deployment_id=deployment_id,
        generation=int(envelope["generation"]),
        base_generation=int(envelope["base_generation"]),
        revocation_generation=int(envelope["revocation_generation"]),
        envelope_sha256=envelope_sha256,
        content_sha256=content_sha256,
        kid=kid,
        issued_at=issued_at,
        not_before=not_before,
        security_expires_at=security_expires_at,
        expires_at=expires_at,
        content=content,
        envelope=envelope,
    )


class EdgeDesiredStateStore:
    """Own one Deployment's verified active slot and persistent monotonic watermark."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        tenant_id: str,
        deployment_id: str,
        pinned_keys: Mapping[str, bytes],
        now=None,
    ) -> None:
        if not (
            _WORKSPACE_ID.fullmatch(workspace_id)
            and _TENANT_ID.fullmatch(tenant_id)
            and _DEPLOYMENT_ID.fullmatch(deployment_id)
            and pinned_keys
            and all(_KID.fullmatch(key) and len(value) == 32 for key, value in pinned_keys.items())
        ):
            raise EdgeDesiredStateError("edge_desired_state.owner_invalid")
        self._root = root.resolve()
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._deployment_id = deployment_id
        self._pinned_keys = dict(pinned_keys)
        self._trusted_keys = dict(pinned_keys)
        self._trust: VerifiedTrustManifest | None = None
        self._trust_chain: list[dict[str, object]] = []
        self._snapshot: VerifiedDesiredState | None = None
        self._observed_at: datetime | None = None
        self._reachable = False
        self._integrity_failed = False
        self._now = now or (lambda: datetime.now(UTC))
        self._apply_lock = threading.Lock()
        self._lock = threading.RLock()
        self._restore()

    def apply(self, value: object) -> bool:
        with self._apply_lock:
            if type(value) is not dict or set(value) != {"trust_manifest", "snapshot"}:
                self.mark_integrity_failed()
                raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
            checked_now = _aware_now(self._now())
            try:
                trust = verify_trust_manifest(
                    value["trust_manifest"],
                    trusted_keys=self._trusted_keys,
                    now=checked_now,
                )
                with self._lock:
                    previous_trust = self._trust
                    previous = self._snapshot
                if previous_trust is not None:
                    _validate_trust_transition(previous_trust, trust)
                snapshot = verify_desired_state(
                    value["snapshot"],
                    trust=trust,
                    workspace_id=self._workspace_id,
                    tenant_id=self._tenant_id,
                    deployment_id=self._deployment_id,
                    now=checked_now,
                )
                if previous is not None:
                    if snapshot.generation < previous.generation:
                        raise EdgeDesiredStateError("edge_desired_state.generation_stale")
                    if (
                        snapshot.generation == previous.generation
                        and snapshot.envelope_sha256 != previous.envelope_sha256
                    ):
                        raise EdgeDesiredStateError("edge_desired_state.generation_conflict")
                    if snapshot.revocation_generation < previous.revocation_generation:
                        raise EdgeDesiredStateError("edge_desired_state.revocation_stale")
                    if snapshot.generation > previous.generation and (
                        snapshot.base_generation != previous.generation
                    ):
                        raise EdgeDesiredStateError("edge_desired_state.base_conflict")
                changed = previous is None or snapshot.generation > previous.generation
                trust_changed = (
                    previous_trust is None
                    or trust.manifest_sha256 != previous_trust.manifest_sha256
                )
                if changed or trust_changed:
                    self._persist(value, trust, snapshot, checked_now)
                self._clear_quarantine()
                with self._lock:
                    self._install_trust(trust)
                    self._snapshot = snapshot
                    self._observed_at = checked_now
                    self._reachable = True
                    self._integrity_failed = False
                return changed
            except EdgeDesiredStateError:
                self.mark_integrity_failed()
                raise

    def mark_unreachable(self) -> None:
        with self._lock:
            self._reachable = False

    def mark_current(self) -> None:
        with self._lock:
            if self._snapshot is None or self._integrity_failed:
                raise EdgeDesiredStateError("edge_desired_state.unavailable")
            self._observed_at = _aware_now(self._now())
            self._reachable = True

    def mark_integrity_failed(self) -> None:
        with self._lock:
            self._integrity_failed = True
            self._reachable = False
        self._record_quarantine()

    def snapshot(self) -> VerifiedDesiredState | None:
        with self._lock:
            return self._snapshot

    def content(self) -> Mapping[str, object] | None:
        with self._lock:
            return None if self._snapshot is None else self._snapshot.content

    def decision(self) -> EdgeDesiredStateDecision:
        checked_now = _aware_now(self._now())
        with self._lock:
            snapshot = self._snapshot
            trust = self._trust
            reachable = self._reachable
            integrity_failed = self._integrity_failed
        if integrity_failed:
            return EdgeDesiredStateDecision(
                False,
                False,
                "integrity_failed",
                snapshot.generation if snapshot else None,
                snapshot.envelope_sha256 if snapshot else None,
                "edge_desired_state.integrity_failed",
            )
        if snapshot is None or trust is None:
            return EdgeDesiredStateDecision(
                False, False, "unavailable", None, None, "edge_desired_state.unavailable"
            )
        if checked_now >= snapshot.expires_at:
            return self._decision(snapshot, False, False, "expired", "edge_desired_state.expired")
        security_valid = (
            checked_now < snapshot.security_expires_at and checked_now < trust.expires_at
        )
        if reachable and security_valid:
            return self._decision(snapshot, True, True, "current", "edge_desired_state.current")
        if security_valid:
            return self._decision(snapshot, True, True, "lkg", "edge_desired_state.lkg")
        return self._decision(snapshot, True, False, "stale", "edge_desired_state.security_stale")

    @staticmethod
    def _decision(
        snapshot: VerifiedDesiredState,
        configuration_allowed: bool,
        security_allowed: bool,
        status,
        reason: str,
    ) -> EdgeDesiredStateDecision:
        return EdgeDesiredStateDecision(
            configuration_allowed,
            security_allowed,
            status,
            snapshot.generation,
            snapshot.envelope_sha256,
            reason,
        )

    def _persist(
        self,
        value: Mapping[str, object],
        trust: VerifiedTrustManifest,
        snapshot: VerifiedDesiredState,
        observed_at: datetime,
    ) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        slots = self._root / "slots"
        slots.mkdir(parents=True, exist_ok=True)
        wire = canonical_json_bytes(value)
        slot_name = f"{snapshot.generation}-{snapshot.envelope_sha256}.json"
        slot_path = slots / slot_name
        _atomic_write(slot_path, wire)
        chain = list(self._trust_chain)
        if self._trust is None or trust.manifest_sha256 != self._trust.manifest_sha256:
            chain.append(dict(trust.document))
        if len(chain) > _MAX_TRUST_CHAIN:
            raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
        _atomic_write(
            self._root / "trust.json",
            canonical_json_bytes({"manifests": chain, "version": 1}),
        )
        watermark = {
            "deployment_id": self._deployment_id,
            "envelope_sha256": snapshot.envelope_sha256,
            "generation": snapshot.generation,
            "observed_at": _stamp(observed_at),
            "revocation_generation": snapshot.revocation_generation,
            "trust_generation": trust.generation,
            "version": 1,
        }
        _atomic_write(self._root / "watermark.json", canonical_json_bytes(watermark))
        pointer = {
            "envelope_sha256": snapshot.envelope_sha256,
            "generation": snapshot.generation,
            "slot": slot_name,
            "version": 1,
        }
        _atomic_write(self._root / "active.json", canonical_json_bytes(pointer))

    def _restore(self) -> None:
        trust_path = self._root / "trust.json"
        watermark_path = self._root / "watermark.json"
        active_path = self._root / "active.json"
        quarantine_path = self._root / "quarantine.json"
        quarantined = quarantine_path.exists()
        present = [path.exists() for path in (trust_path, watermark_path, active_path)]
        if not any(present):
            self._integrity_failed = quarantined
            return
        if not all(present):
            self._integrity_failed = True
            self._record_quarantine()
            return
        try:
            if quarantined:
                quarantine = strict_json_object(quarantine_path.read_bytes(), canonical=True)
                if quarantine != {"status": "integrity_failed", "version": 1}:
                    raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            trust_document = strict_json_object(
                trust_path.read_bytes(), canonical=True, maximum=_MAX_DOCUMENT_BYTES
            )
            if set(trust_document) != {"manifests", "version"} or trust_document["version"] != 1:
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            manifests = trust_document["manifests"]
            if type(manifests) is not list or not 1 <= len(manifests) <= _MAX_TRUST_CHAIN:
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            trusted = dict(self._pinned_keys)
            previous: VerifiedTrustManifest | None = None
            chain: list[dict[str, object]] = []
            for document in manifests:
                verified = verify_trust_manifest(
                    document,
                    trusted_keys=trusted,
                    now=_aware_now(self._now()),
                    allow_expired=True,
                )
                if previous is not None:
                    _validate_trust_transition(previous, verified)
                    if verified.generation == previous.generation:
                        raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
                trusted = {
                    **{
                        key: item
                        for key, item in trusted.items()
                        if key not in verified.revoked_kids
                    },
                    **dict(verified.keys),
                }
                previous = verified
                chain.append(dict(verified.document))
            watermark = strict_json_object(watermark_path.read_bytes(), canonical=True)
            pointer = strict_json_object(active_path.read_bytes(), canonical=True)
            if (
                set(watermark)
                != {
                    "deployment_id",
                    "envelope_sha256",
                    "generation",
                    "observed_at",
                    "revocation_generation",
                    "trust_generation",
                    "version",
                }
                or set(pointer) != {"envelope_sha256", "generation", "slot", "version"}
                or watermark["version"] != 1
                or pointer["version"] != 1
                or watermark["deployment_id"] != self._deployment_id
                or watermark["generation"] != pointer["generation"]
                or watermark["envelope_sha256"] != pointer["envelope_sha256"]
                or previous is None
                or watermark["trust_generation"] != previous.generation
            ):
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            slot_name = str(pointer["slot"])
            expected_slot = f"{pointer['generation']}-{pointer['envelope_sha256']}.json"
            if slot_name != expected_slot or Path(slot_name).name != slot_name:
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            wire = strict_json_object(
                (self._root / "slots" / slot_name).read_bytes(), canonical=True
            )
            if set(wire) != {"trust_manifest", "snapshot"}:
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            snapshot = verify_desired_state(
                wire["snapshot"],
                trust=previous,
                workspace_id=self._workspace_id,
                tenant_id=self._tenant_id,
                deployment_id=self._deployment_id,
                now=_aware_now(self._now()),
                allow_expired=True,
            )
            if (
                snapshot.generation != watermark["generation"]
                or snapshot.envelope_sha256 != watermark["envelope_sha256"]
                or snapshot.revocation_generation != watermark["revocation_generation"]
            ):
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            self._trusted_keys = trusted
            self._trust = previous
            self._trust_chain = chain
            self._snapshot = snapshot
            observed_at = _time(watermark["observed_at"])
            if observed_at > _aware_now(self._now()) + _CLOCK_SKEW:
                raise EdgeDesiredStateError("edge_desired_state.storage_invalid")
            self._observed_at = observed_at
            self._reachable = False
            self._integrity_failed = quarantined
        except (EdgeDesiredStateError, OSError, TypeError, ValueError):
            self._integrity_failed = True
            self._record_quarantine()

    def _record_quarantine(self) -> None:
        try:
            _atomic_write(
                self._root / "quarantine.json",
                canonical_json_bytes({"status": "integrity_failed", "version": 1}),
            )
        except EdgeDesiredStateError:
            pass

    def _clear_quarantine(self) -> None:
        path = self._root / "quarantine.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise EdgeDesiredStateError("edge_desired_state.storage_unavailable") from None

    def _install_trust(self, trust: VerifiedTrustManifest) -> None:
        if self._trust is None or trust.manifest_sha256 != self._trust.manifest_sha256:
            self._trust_chain.append(dict(trust.document))
        self._trusted_keys = {
            **{
                key: item
                for key, item in self._trusted_keys.items()
                if key not in trust.revoked_kids
            },
            **dict(trust.keys),
        }
        self._trust = trust


def _trust_shape(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _TRUST_KEYS:
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    if (
        value.get("domain") != _TRUST_DOMAIN
        or value.get("protocol_version") != 1
        or type(value.get("generation")) is not int
        or value["generation"] < 1
        or _KID.fullmatch(str(value.get("signer_kid") or "")) is None
        or type(value.get("revoked_kids")) is not list
        or len(value["revoked_kids"]) > 32
        or value["revoked_kids"] != sorted(set(value["revoked_kids"]))
        or any(_KID.fullmatch(str(item)) is None for item in value["revoked_kids"])
        or not isinstance(value.get("signature"), str)
    ):
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    _trust_key(value.get("current"))
    if value.get("next") is not None:
        _trust_key(value["next"])
    _time(value.get("issued_at"))
    _time(value.get("expires_at"))
    if value["signature"] and len(_unb64(str(value["signature"]), expected=64)) != 64:
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    return dict(value)


def _validate_trust_transition(
    previous: VerifiedTrustManifest,
    candidate: VerifiedTrustManifest,
) -> None:
    if candidate.generation < previous.generation:
        raise EdgeDesiredStateError("edge_desired_state.trust_stale")
    if candidate.generation == previous.generation:
        if candidate.manifest_sha256 != previous.manifest_sha256:
            raise EdgeDesiredStateError("edge_desired_state.trust_conflict")
        return
    previous_current = previous.keys.get(candidate.current_kid)
    if (
        candidate.signer_kid != previous.current_kid
        or previous_current is None
        or candidate.keys[candidate.current_kid] != previous_current
        or not previous.revoked_kids <= candidate.revoked_kids
    ):
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")


def _trust_key(value: object) -> tuple[str, bytes]:
    if (
        type(value) is not dict
        or set(value) != _KEY_KEYS
        or value.get("alg") != "Ed25519"
        or _KID.fullmatch(str(value.get("kid") or "")) is None
    ):
        raise EdgeDesiredStateError("edge_desired_state.trust_invalid")
    return str(value["kid"]), _unb64(str(value.get("public_key") or ""), expected=32)


def _envelope_shape(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ENVELOPE_KEYS:
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    if (
        value.get("domain") != _DESIRED_DOMAIN
        or value.get("protocol_version") != 1
        or _SNAPSHOT_ID.fullmatch(str(value.get("snapshot_id") or "")) is None
        or _ARTIFACT_ID.fullmatch(str(value.get("artifact_id") or "")) is None
        or _WORKSPACE_ID.fullmatch(str(value.get("workspace_id") or "")) is None
        or _TENANT_ID.fullmatch(str(value.get("tenant_id") or "")) is None
        or _DEPLOYMENT_ID.fullmatch(str(value.get("deployment_id") or "")) is None
        or type(value.get("generation")) is not int
        or value["generation"] < 1
        or type(value.get("base_generation")) is not int
        or not 0 <= value["base_generation"] < value["generation"]
        or type(value.get("revocation_generation")) is not int
        or value["revocation_generation"] < 1
        or value.get("alg") != "Ed25519"
        or _KID.fullmatch(str(value.get("kid") or "")) is None
        or _HASH.fullmatch(str(value.get("content_sha256") or "")) is None
        or not isinstance(value.get("signature"), str)
    ):
        raise EdgeDesiredStateError("edge_desired_state.payload_invalid")
    for name in ("issued_at", "not_before", "security_expires_at", "expires_at"):
        _time(value.get(name))
    if value["signature"] and len(_unb64(str(value["signature"]), expected=64)) != 64:
        raise EdgeDesiredStateError("edge_desired_state.signature_invalid")
    _content(value.get("content"), deployment_id=str(value["deployment_id"]))
    return dict(value)


def _content(value: object, *, deployment_id: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != _CONTENT_KEYS or value.get("schema_version") != 1:
        raise EdgeDesiredStateError("edge_desired_state.content_invalid")
    deployment = value.get("deployment")
    if (
        type(deployment) is not dict
        or set(deployment) != {"deployment_id", "mode", "region_code", "revision"}
        or deployment.get("deployment_id") != deployment_id
        or deployment.get("mode") not in {"connected", "isolated"}
        or _REGION.fullmatch(str(deployment.get("region_code") or "")) is None
        or type(deployment.get("revision")) is not int
        or deployment["revision"] < 1
    ):
        raise EdgeDesiredStateError("edge_desired_state.content_invalid")
    try:
        source = validate_config_manifest(value.get("source_manifest"))
    except ValueError:
        raise EdgeDesiredStateError("edge_desired_state.content_invalid") from None
    return {
        "schema_version": 1,
        "deployment": dict(deployment),
        "source_manifest": source,
    }


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".edge-state-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_replace(Path(temporary), path)
        temporary = None
    except OSError:
        raise EdgeDesiredStateError("edge_desired_state.storage_unavailable") from None
    finally:
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
        replace_existing = 0x1
        write_through = 0x8
        if not move_file(str(source), str(target), replace_existing | write_through):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")
        return
    os.replace(source, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _aware_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EdgeDesiredStateError("edge_desired_state.time_invalid")
    return value.astimezone(UTC)


def _time(value: object) -> datetime:
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise EdgeDesiredStateError("edge_desired_state.time_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EdgeDesiredStateError("edge_desired_state.time_invalid") from None
    if parsed.tzinfo is None:
        raise EdgeDesiredStateError("edge_desired_state.time_invalid")
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _aware_now(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str, *, expected: int) -> bytes:
    if not value or "=" in value or _B64.fullmatch(value) is None:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * ((4 - len(value) % 4) % 4),
        altchars=b"-_",
        validate=True,
    )
    if len(decoded) != expected:
        raise ValueError
    return decoded


__all__ = (
    "EdgeDesiredStateDecision",
    "EdgeDesiredStateError",
    "EdgeDesiredStateStore",
    "VerifiedDesiredState",
    "VerifiedTrustManifest",
    "canonical_json_bytes",
    "public_key_b64",
    "sign_desired_state",
    "sign_trust_manifest",
    "strict_json_object",
    "verify_desired_state",
    "verify_trust_manifest",
)
