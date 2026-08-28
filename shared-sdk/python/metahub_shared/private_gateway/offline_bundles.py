"""Signed, encrypted media bundles for fully isolated private Gateways."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .desired_state import canonical_json_bytes, strict_json_object

OFFLINE_BUNDLE_SCHEMA_VERSION = "metahub.edge-offline-bundle/v1"
OFFLINE_BUNDLE_SIGNATURE_PURPOSE = "metahub.edge-offline-bundle.manifest/v1"
OFFLINE_BUNDLE_ENVELOPE_FORMAT = "customer-kms-aes256-gcm/v1"
OFFLINE_BUNDLE_DOMAINS = (
    "desired_state",
    "license",
    "release",
    "status",
    "usage",
    "audit",
    "validation",
)
MAX_OFFLINE_BUNDLE_BYTES = 10 * 1024 * 1024 * 1024
MAX_OFFLINE_ENTRY_BYTES = 64 * 1024 * 1024
MAX_OFFLINE_ENTRIES = 1024
MAX_OFFLINE_MANIFEST_BYTES = 512 * 1024

_BUNDLE_ID = re.compile(r"^obb_[0-9a-f]{32}$")
_ENTRY_ID = re.compile(r"^obe_[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^(?:wsp_|edp_)[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MEDIA = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}/[a-z0-9][a-z0-9.+-]{0,63}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "credential",
        "device_private_key",
        "envelope",
        "key",
        "object_url",
        "password",
        "path",
        "prompt",
        "provider_key",
        "response",
        "secret",
        "secret_value",
        "storage_path",
        "token",
    }
)


class OfflineBundleContractError(ValueError):
    """Stable failure that never contains media content or filesystem paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OfflineDomainVersion(_StrictModel):
    domain: Literal["desired_state", "license", "release", "status", "usage", "audit", "validation"]
    base_generation: int = Field(ge=0, le=2_147_483_647)
    base_sha256: str | None = Field(default=None, pattern=_HEX_64.pattern)
    generation: int = Field(ge=1, le=2_147_483_647)
    value_sha256: str = Field(pattern=_HEX_64.pattern)

    @model_validator(mode="after")
    def _consistent(self) -> OfflineDomainVersion:
        if (
            self.generation < self.base_generation
            or (self.base_generation == 0) != (self.base_sha256 is None)
            or (
                self.generation == self.base_generation
                and not hmac.compare_digest(self.value_sha256, self.base_sha256 or "")
            )
        ):
            raise ValueError("edge_offline_bundle.version_vector_invalid")
        return self


class OfflineBundleEntry(_StrictModel):
    entry_id: str = Field(pattern=_ENTRY_ID.pattern)
    domain: Literal["desired_state", "license", "release", "status", "usage", "audit", "validation"]
    sequence: int = Field(ge=0, le=MAX_OFFLINE_ENTRIES - 1)
    media_type: str = Field(min_length=3, max_length=96)
    plaintext_sha256: str = Field(pattern=_HEX_64.pattern)
    ciphertext_sha256: str = Field(pattern=_HEX_64.pattern)
    plaintext_size: int = Field(ge=1, le=MAX_OFFLINE_ENTRY_BYTES)
    ciphertext_size: int = Field(ge=17, le=MAX_OFFLINE_ENTRY_BYTES + 16)
    nonce: str = Field(min_length=16, max_length=16, repr=False)

    @model_validator(mode="after")
    def _consistent(self) -> OfflineBundleEntry:
        try:
            nonce = _unb64(self.nonce)
        except ValueError:
            raise ValueError("edge_offline_bundle.entry_invalid") from None
        if (
            len(nonce) != 12
            or self.ciphertext_size != self.plaintext_size + 16
            or _SAFE_MEDIA.fullmatch(self.media_type) is None
        ):
            raise ValueError("edge_offline_bundle.entry_invalid")
        return self


class OfflineBundleKeyEnvelope(_StrictModel):
    format: Literal["customer-kms-aes256-gcm/v1"] = OFFLINE_BUNDLE_ENVELOPE_FORMAT
    encryption_key_version: int = Field(ge=1, le=2_147_483_647)
    kms_key_id_sha256: str = Field(pattern=_HEX_64.pattern)
    wrapped_key: str = Field(min_length=24, max_length=8192, repr=False)


class OfflineBundleManifest(_StrictModel):
    schema_version: Literal["metahub.edge-offline-bundle/v1"] = OFFLINE_BUNDLE_SCHEMA_VERSION
    signature_purpose: Literal["metahub.edge-offline-bundle.manifest/v1"] = (
        OFFLINE_BUNDLE_SIGNATURE_PURPOSE
    )
    bundle_id: str = Field(pattern=_BUNDLE_ID.pattern)
    direction: Literal["cloud_to_edge", "edge_to_cloud"]
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tenant_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    producer_generation: int = Field(ge=1, le=2_147_483_647)
    version_vector: tuple[OfflineDomainVersion, ...] = Field(
        min_length=1, max_length=len(OFFLINE_BUNDLE_DOMAINS)
    )
    entries: tuple[OfflineBundleEntry, ...] = Field(min_length=1, max_length=MAX_OFFLINE_ENTRIES)
    entry_merkle_root: str = Field(pattern=_HEX_64.pattern)
    key_envelope_sha256: str = Field(pattern=_HEX_64.pattern)
    kms_key_id_sha256: str = Field(pattern=_HEX_64.pattern)
    encryption_key_version: int = Field(ge=1, le=2_147_483_647)
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> OfflineBundleManifest:
        versions = self.version_vector
        entries = self.entries
        version_domains = tuple(item.domain for item in versions)
        entry_keys = tuple((item.domain, item.sequence, item.entry_id) for item in entries)
        if (
            self.created_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.created_at
            or (self.expires_at - self.created_at).total_seconds() > 30 * 86400
            or version_domains != tuple(sorted(version_domains))
            or len(set(version_domains)) != len(version_domains)
            or entry_keys != tuple(sorted(entry_keys))
            or len({item.entry_id for item in entries}) != len(entries)
            or not set(item.domain for item in entries) <= set(version_domains)
            or self.producer_generation != max(item.generation for item in versions)
            or self.entry_merkle_root != _entry_merkle_root(entries)
            or sum(item.ciphertext_size for item in entries) > MAX_OFFLINE_BUNDLE_BYTES
        ):
            raise ValueError("edge_offline_bundle.manifest_invalid")
        return self


class OfflineBundleSignature(_StrictModel):
    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._:-]+$")
    public_key: str = Field(min_length=43, max_length=43, repr=False)
    value: str = Field(min_length=86, max_length=86, repr=False)


class SignedOfflineBundle(_StrictModel):
    manifest: OfflineBundleManifest
    manifest_sha256: str = Field(pattern=_HEX_64.pattern)
    signature: OfflineBundleSignature


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedOfflineBundle:
    signed: SignedOfflineBundle
    canonical_manifest: bytes


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedOfflineArchive:
    verified: VerifiedOfflineBundle
    staging_path: Path
    entry_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class OfflineBundleApplyResult:
    bundle_id: str
    manifest_sha256: str
    producer_generation: int
    domains: tuple[str, ...]
    changed: bool


@dataclass(frozen=True, slots=True)
class OfflineConflictCandidate:
    domain: str
    base_generation: int
    base_sha256: str | None
    current_generation: int
    current_sha256: str
    incoming_generation: int
    incoming_sha256: str


def sign_offline_bundle_manifest(
    manifest: OfflineBundleManifest,
    *,
    private_key: Ed25519PrivateKey,
    key_id: str,
) -> SignedOfflineBundle:
    canonical = canonical_json_bytes(manifest.model_dump(mode="json"))
    signature = private_key.sign(_signature_payload(canonical))
    public = private_key.public_key().public_bytes_raw()
    return SignedOfflineBundle(
        manifest=manifest,
        manifest_sha256=_sha(canonical),
        signature=OfflineBundleSignature(
            key_id=key_id,
            public_key=_b64(public),
            value=_b64(signature),
        ),
    )


def verify_offline_bundle_manifest(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    expected_workspace_id: str,
    expected_tenant_id: str,
    expected_deployment_id: str,
    expected_direction: Literal["cloud_to_edge", "edge_to_cloud"],
    now: datetime,
) -> VerifiedOfflineBundle:
    try:
        signed = SignedOfflineBundle.model_validate_json(canonical_json_bytes(value))
        manifest = signed.manifest
        canonical = canonical_json_bytes(manifest.model_dump(mode="json"))
        if not hmac.compare_digest(signed.manifest_sha256, _sha(canonical)):
            raise OfflineBundleContractError("edge_offline_bundle.manifest_invalid")
        if not (
            hmac.compare_digest(manifest.workspace_id, expected_workspace_id)
            and hmac.compare_digest(manifest.tenant_id, expected_tenant_id)
            and hmac.compare_digest(manifest.deployment_id, expected_deployment_id)
            and manifest.direction == expected_direction
        ):
            raise OfflineBundleContractError("edge_offline_bundle.owner_mismatch")
        checked_now = _aware(now)
        if checked_now < _aware(manifest.created_at) or checked_now > _aware(manifest.expires_at):
            raise OfflineBundleContractError("edge_offline_bundle.expired")
        trusted = trusted_keys.get(signed.signature.key_id)
        public = _unb64(signed.signature.public_key)
        if trusted is None or len(trusted) != 32 or not hmac.compare_digest(trusted, public):
            raise OfflineBundleContractError("edge_offline_bundle.key_untrusted")
        Ed25519PublicKey.from_public_bytes(public).verify(
            _unb64(signed.signature.value), _signature_payload(canonical)
        )
        return VerifiedOfflineBundle(signed=signed, canonical_manifest=canonical)
    except OfflineBundleContractError:
        raise
    except (InvalidSignature, TypeError, ValueError):
        raise OfflineBundleContractError("edge_offline_bundle.signature_invalid") from None


def build_offline_bundle_archive(
    output: Path,
    *,
    direction: Literal["cloud_to_edge", "edge_to_cloud"],
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    version_vector: Sequence[OfflineDomainVersion],
    entries: Sequence[tuple[str, str, bytes]],
    content_key: bytes,
    key_envelope: OfflineBundleKeyEnvelope,
    private_key: Ed25519PrivateKey,
    key_id: str,
    created_at: datetime,
    expires_at: datetime,
    bundle_id: str | None = None,
) -> SignedOfflineBundle:
    if output.exists() or output.is_symlink() or len(content_key) != 32:
        raise OfflineBundleContractError("edge_offline_bundle.output_invalid")
    checked_bundle_id = bundle_id or f"obb_{uuid4().hex}"
    if _BUNDLE_ID.fullmatch(checked_bundle_id) is None:
        raise OfflineBundleContractError("edge_offline_bundle.manifest_invalid")
    if not entries or len(entries) > MAX_OFFLINE_ENTRIES:
        raise OfflineBundleContractError("edge_offline_bundle.entry_invalid")
    cipher = AESGCM(content_key)
    descriptors: list[OfflineBundleEntry] = []
    ciphertexts: dict[str, bytes] = {}
    domain_sequences: dict[str, int] = {}
    try:
        for domain, media_type, plaintext in entries:
            sequence = domain_sequences.get(domain, 0)
            domain_sequences[domain] = sequence + 1
            if (
                domain not in OFFLINE_BUNDLE_DOMAINS
                or type(plaintext) is not bytes
                or not 1 <= len(plaintext) <= MAX_OFFLINE_ENTRY_BYTES
            ):
                raise OfflineBundleContractError("edge_offline_bundle.entry_invalid")
            entry_id = f"obe_{uuid4().hex}"
            nonce = os.urandom(12)
            aad = _entry_aad(
                checked_bundle_id,
                direction,
                workspace_id,
                tenant_id,
                deployment_id,
                domain,
                sequence,
                entry_id,
            )
            ciphertext = cipher.encrypt(nonce, plaintext, aad)
            descriptor = OfflineBundleEntry(
                entry_id=entry_id,
                domain=domain,
                sequence=sequence,
                media_type=media_type,
                plaintext_sha256=_sha(plaintext),
                ciphertext_sha256=_sha(ciphertext),
                plaintext_size=len(plaintext),
                ciphertext_size=len(ciphertext),
                nonce=_b64(nonce),
            )
            descriptors.append(descriptor)
            ciphertexts[entry_id] = ciphertext
        descriptors.sort(key=lambda item: (item.domain, item.sequence, item.entry_id))
        versions = tuple(sorted(version_vector, key=lambda item: item.domain))
        envelope_bytes = canonical_json_bytes(key_envelope.model_dump(mode="json"))
        manifest = OfflineBundleManifest(
            bundle_id=checked_bundle_id,
            direction=direction,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            deployment_id=deployment_id,
            producer_generation=max(item.generation for item in versions),
            version_vector=versions,
            entries=tuple(descriptors),
            entry_merkle_root=_entry_merkle_root(tuple(descriptors)),
            key_envelope_sha256=_sha(envelope_bytes),
            kms_key_id_sha256=key_envelope.kms_key_id_sha256,
            encryption_key_version=key_envelope.encryption_key_version,
            created_at=_aware(created_at),
            expires_at=_aware(expires_at),
        )
        signed = sign_offline_bundle_manifest(manifest, private_key=private_key, key_id=key_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        with zipfile.ZipFile(
            temporary, "x", compression=zipfile.ZIP_STORED, allowZip64=True
        ) as archive:
            archive.writestr(
                _zip_info("manifest.json"),
                canonical_json_bytes(
                    {
                        "key_envelope": key_envelope.model_dump(mode="json"),
                        "signed": signed.model_dump(mode="json"),
                    }
                ),
            )
            for descriptor in descriptors:
                archive.writestr(
                    _zip_info(f"entries/{descriptor.entry_id}.bin"),
                    ciphertexts[descriptor.entry_id],
                )
        os.replace(temporary, output)
        if os.name != "nt":
            output.chmod(0o600)
        return signed
    except OfflineBundleContractError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile):
        raise OfflineBundleContractError("edge_offline_bundle.build_failed") from None
    finally:
        for value in ciphertexts.values():
            del value


def verify_offline_bundle_archive(
    source: Path,
    staging_root: Path,
    *,
    trusted_keys: Mapping[str, bytes],
    unwrap_key: Callable[[OfflineBundleKeyEnvelope], bytes],
    expected_workspace_id: str,
    expected_tenant_id: str,
    expected_deployment_id: str,
    expected_direction: Literal["cloud_to_edge", "edge_to_cloud"],
    now: datetime,
) -> VerifiedOfflineArchive:
    temporary: Path | None = None
    try:
        info = source.stat()
        if (
            source.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or not 1 <= info.st_size <= MAX_OFFLINE_BUNDLE_BYTES + MAX_OFFLINE_MANIFEST_BYTES
        ):
            raise OfflineBundleContractError("edge_offline_bundle.input_invalid")
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".offline-verify-", dir=staging_root))
        with zipfile.ZipFile(source, "r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (
                len(names) != len(set(names))
                or "manifest.json" not in names
                or any(not _safe_archive_info(item) for item in infos)
            ):
                raise OfflineBundleContractError("edge_offline_bundle.archive_invalid")
            manifest_info = archive.getinfo("manifest.json")
            if not 1 <= manifest_info.file_size <= MAX_OFFLINE_MANIFEST_BYTES:
                raise OfflineBundleContractError("edge_offline_bundle.archive_invalid")
            root = strict_json_object(
                archive.read(manifest_info), canonical=True, maximum=MAX_OFFLINE_MANIFEST_BYTES
            )
            if set(root) != {"key_envelope", "signed"}:
                raise OfflineBundleContractError("edge_offline_bundle.manifest_invalid")
            envelope = OfflineBundleKeyEnvelope.model_validate(root["key_envelope"])
            verified = verify_offline_bundle_manifest(
                root["signed"],
                trusted_keys=trusted_keys,
                expected_workspace_id=expected_workspace_id,
                expected_tenant_id=expected_tenant_id,
                expected_deployment_id=expected_deployment_id,
                expected_direction=expected_direction,
                now=now,
            )
            manifest = verified.signed.manifest
            envelope_bytes = canonical_json_bytes(envelope.model_dump(mode="json"))
            if (
                not hmac.compare_digest(manifest.key_envelope_sha256, _sha(envelope_bytes))
                or not hmac.compare_digest(manifest.kms_key_id_sha256, envelope.kms_key_id_sha256)
                or manifest.encryption_key_version != envelope.encryption_key_version
            ):
                raise OfflineBundleContractError("edge_offline_bundle.envelope_invalid")
            key = unwrap_key(envelope)
            if type(key) is not bytes or len(key) != 32:
                raise OfflineBundleContractError("edge_offline_bundle.key_invalid")
            expected_names = {"manifest.json"} | {
                f"entries/{item.entry_id}.bin" for item in manifest.entries
            }
            if set(names) != expected_names:
                raise OfflineBundleContractError("edge_offline_bundle.archive_invalid")
            cipher = AESGCM(key)
            entry_paths: dict[str, Path] = {}
            for descriptor in manifest.entries:
                member = archive.getinfo(f"entries/{descriptor.entry_id}.bin")
                if (
                    member.file_size != descriptor.ciphertext_size
                    or member.compress_size != member.file_size
                ):
                    raise OfflineBundleContractError("edge_offline_bundle.entry_invalid")
                ciphertext = archive.read(member)
                if not hmac.compare_digest(_sha(ciphertext), descriptor.ciphertext_sha256):
                    raise OfflineBundleContractError("edge_offline_bundle.entry_corrupt")
                plaintext = cipher.decrypt(
                    _unb64(descriptor.nonce),
                    ciphertext,
                    _entry_aad(
                        manifest.bundle_id,
                        manifest.direction,
                        manifest.workspace_id,
                        manifest.tenant_id,
                        manifest.deployment_id,
                        descriptor.domain,
                        descriptor.sequence,
                        descriptor.entry_id,
                    ),
                )
                if len(plaintext) != descriptor.plaintext_size or not hmac.compare_digest(
                    _sha(plaintext), descriptor.plaintext_sha256
                ):
                    raise OfflineBundleContractError("edge_offline_bundle.entry_corrupt")
                target = temporary / f"{descriptor.entry_id}.bin"
                _write_private(target, plaintext)
                entry_paths[descriptor.entry_id] = target
            receipt = temporary / "receipt.json"
            _write_private(
                receipt,
                canonical_json_bytes(
                    {
                        "bundle_id": manifest.bundle_id,
                        "manifest_sha256": verified.signed.manifest_sha256,
                        "schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
                    }
                ),
            )
        final = staging_root / f"verified-{manifest.bundle_id}-{verified.signed.manifest_sha256}"
        if final.exists():
            existing = strict_json_object(
                (final / "receipt.json").read_bytes(), canonical=True, maximum=4096
            )
            if existing.get("manifest_sha256") != verified.signed.manifest_sha256:
                raise OfflineBundleContractError("edge_offline_bundle.receipt_conflict")
            shutil.rmtree(temporary)
            temporary = None
        else:
            os.replace(temporary, final)
            temporary = None
        paths = {item.entry_id: final / f"{item.entry_id}.bin" for item in manifest.entries}
        return VerifiedOfflineArchive(verified=verified, staging_path=final, entry_paths=paths)
    except OfflineBundleContractError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile):
        raise OfflineBundleContractError("edge_offline_bundle.archive_invalid") from None
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def detect_offline_conflicts(
    current: Mapping[str, tuple[int, str]],
    incoming: Sequence[OfflineDomainVersion],
) -> tuple[OfflineConflictCandidate, ...]:
    conflicts: list[OfflineConflictCandidate] = []
    for item in incoming:
        existing = current.get(item.domain)
        if existing is None:
            continue
        current_generation, current_sha256 = existing
        if current_generation < item.base_generation:
            raise OfflineBundleContractError("edge_offline_bundle.cursor_invalid")
        if current_generation == item.base_generation:
            if item.base_sha256 is not None and not hmac.compare_digest(
                current_sha256, item.base_sha256
            ):
                raise OfflineBundleContractError("edge_offline_bundle.base_mismatch")
            continue
        if current_generation == item.generation and hmac.compare_digest(
            current_sha256, item.value_sha256
        ):
            continue
        conflicts.append(
            OfflineConflictCandidate(
                domain=item.domain,
                base_generation=item.base_generation,
                base_sha256=item.base_sha256,
                current_generation=current_generation,
                current_sha256=current_sha256,
                incoming_generation=item.generation,
                incoming_sha256=item.value_sha256,
            )
        )
    return tuple(conflicts)


class OfflineBundleStateStore:
    """Crash-safe multi-domain slot store with one atomic authoritative head."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        tenant_id: str,
        deployment_id: str,
    ) -> None:
        if not (
            re.fullmatch(r"^wsp_[0-9a-f]{32}$", workspace_id)
            and re.fullmatch(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                tenant_id,
            )
            and re.fullmatch(r"^edp_[0-9a-f]{32}$", deployment_id)
        ):
            raise OfflineBundleContractError("edge_offline_bundle.owner_mismatch")
        self._root = root
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._deployment_id = deployment_id
        self._lock = threading.RLock()

    def apply(self, archive: VerifiedOfflineArchive) -> OfflineBundleApplyResult:
        manifest = archive.verified.signed.manifest
        if not (
            manifest.direction == "cloud_to_edge"
            and hmac.compare_digest(manifest.workspace_id, self._workspace_id)
            and hmac.compare_digest(manifest.tenant_id, self._tenant_id)
            and hmac.compare_digest(manifest.deployment_id, self._deployment_id)
        ):
            raise OfflineBundleContractError("edge_offline_bundle.owner_mismatch")
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            head = self._head()
            current = {
                str(item["domain"]): (int(item["generation"]), str(item["value_sha256"]))
                for item in head.get("version_vector", [])
            }
            conflicts = detect_offline_conflicts(current, manifest.version_vector)
            if conflicts:
                raise OfflineBundleContractError("edge_offline_bundle.resolution_conflict")
            receipt = self._root / "receipts" / f"{manifest.bundle_id}.json"
            final_slot = self._root / "slots" / manifest.bundle_id
            slot_entries = self._slot_entries(archive)
            if receipt.exists():
                self._validate_receipt(receipt, archive)
                self._validate_slot(final_slot, archive, slot_entries)
                return self._result(archive, changed=False)
            temporary = self._root / "slots" / f".{manifest.bundle_id}.{os.getpid()}.tmp"
            if temporary.exists():
                shutil.rmtree(temporary)
            try:
                if final_slot.exists():
                    self._validate_slot(final_slot, archive, slot_entries)
                else:
                    temporary.mkdir(parents=True, exist_ok=False)
                    for descriptor in manifest.entries:
                        source = archive.entry_paths.get(descriptor.entry_id)
                        if source is None or source.is_symlink() or not source.is_file():
                            raise OfflineBundleContractError("edge_offline_bundle.entry_invalid")
                        target = temporary / f"{descriptor.entry_id}.bin"
                        _copy_private(source, target)
                        if not hmac.compare_digest(
                            _file_sha256(target), descriptor.plaintext_sha256
                        ):
                            raise OfflineBundleContractError("edge_offline_bundle.entry_corrupt")
                    _write_private(
                        temporary / "slot.json",
                        canonical_json_bytes(self._slot_document(archive, slot_entries)),
                    )
                    os.replace(temporary, final_slot)
                new_head = {
                    "bundle_id": manifest.bundle_id,
                    "deployment_id": manifest.deployment_id,
                    "manifest_sha256": archive.verified.signed.manifest_sha256,
                    "schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
                    "slot": f"slots/{manifest.bundle_id}",
                    "version_vector": [
                        item.model_dump(mode="json") for item in manifest.version_vector
                    ],
                }
                head_matches = all(head.get(key) == value for key, value in new_head.items())
                if not head_matches:
                    _atomic_private_json(self._root / "head.json", new_head)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                _atomic_private_json(
                    receipt,
                    {
                        "bundle_id": manifest.bundle_id,
                        "manifest_sha256": archive.verified.signed.manifest_sha256,
                        "schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
                    },
                )
                return self._result(archive, changed=not head_matches)
            except Exception:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
                raise

    @staticmethod
    def _slot_entries(archive: VerifiedOfflineArchive) -> list[dict[str, object]]:
        return [
            {
                "domain": descriptor.domain,
                "entry_id": descriptor.entry_id,
                "media_type": descriptor.media_type,
                "plaintext_sha256": descriptor.plaintext_sha256,
                "sequence": descriptor.sequence,
            }
            for descriptor in archive.verified.signed.manifest.entries
        ]

    @staticmethod
    def _slot_document(
        archive: VerifiedOfflineArchive, entries: list[dict[str, object]]
    ) -> dict[str, object]:
        signed = archive.verified.signed
        return {
            "bundle_id": signed.manifest.bundle_id,
            "entries": entries,
            "manifest_sha256": signed.manifest_sha256,
            "schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
        }

    def _validate_slot(
        self,
        path: Path,
        archive: VerifiedOfflineArchive,
        entries: list[dict[str, object]],
    ) -> None:
        try:
            if path.is_symlink() or not path.is_dir():
                raise ValueError
            value = strict_json_object(
                (path / "slot.json").read_bytes(),
                canonical=True,
                maximum=MAX_OFFLINE_MANIFEST_BYTES,
            )
            if value != self._slot_document(archive, entries):
                raise ValueError
            expected_names = {"slot.json"} | {
                f"{descriptor.entry_id}.bin"
                for descriptor in archive.verified.signed.manifest.entries
            }
            if {item.name for item in path.iterdir()} != expected_names:
                raise ValueError
            for descriptor in archive.verified.signed.manifest.entries:
                target = path / f"{descriptor.entry_id}.bin"
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or target.stat().st_size != descriptor.plaintext_size
                    or not hmac.compare_digest(_file_sha256(target), descriptor.plaintext_sha256)
                ):
                    raise OfflineBundleContractError("edge_offline_bundle.entry_corrupt")
        except OfflineBundleContractError:
            raise
        except (OSError, ValueError):
            raise OfflineBundleContractError("edge_offline_bundle.receipt_conflict") from None

    @staticmethod
    def _validate_receipt(path: Path, archive: VerifiedOfflineArchive) -> None:
        signed = archive.verified.signed
        try:
            value = strict_json_object(path.read_bytes(), canonical=True, maximum=4096)
        except (OSError, ValueError):
            raise OfflineBundleContractError("edge_offline_bundle.receipt_conflict") from None
        if value != {
            "bundle_id": signed.manifest.bundle_id,
            "manifest_sha256": signed.manifest_sha256,
            "schema_version": OFFLINE_BUNDLE_SCHEMA_VERSION,
        }:
            raise OfflineBundleContractError("edge_offline_bundle.receipt_conflict")

    def _head(self) -> dict[str, object]:
        path = self._root / "head.json"
        if not path.exists():
            return {"version_vector": []}
        value = strict_json_object(path.read_bytes(), canonical=True, maximum=64 * 1024)
        if set(value) != {
            "bundle_id",
            "deployment_id",
            "manifest_sha256",
            "schema_version",
            "slot",
            "version_vector",
        } or not (
            value.get("deployment_id") == self._deployment_id
            and value.get("schema_version") == OFFLINE_BUNDLE_SCHEMA_VERSION
            and type(value.get("bundle_id")) is str
            and _BUNDLE_ID.fullmatch(str(value["bundle_id"])) is not None
            and type(value.get("manifest_sha256")) is str
            and _HEX_64.fullmatch(str(value["manifest_sha256"])) is not None
            and value.get("slot") == f"slots/{value['bundle_id']}"
        ):
            raise OfflineBundleContractError("edge_offline_bundle.cursor_invalid")
        versions = value.get("version_vector")
        if type(versions) is not list:
            raise OfflineBundleContractError("edge_offline_bundle.cursor_invalid")
        try:
            checked = tuple(OfflineDomainVersion.model_validate(item) for item in versions)
            domains = tuple(item.domain for item in checked)
            if domains != tuple(sorted(domains)) or len(domains) != len(set(domains)):
                raise ValueError
        except ValueError:
            raise OfflineBundleContractError("edge_offline_bundle.cursor_invalid") from None
        return value

    @staticmethod
    def _result(archive: VerifiedOfflineArchive, *, changed: bool) -> OfflineBundleApplyResult:
        signed = archive.verified.signed
        return OfflineBundleApplyResult(
            bundle_id=signed.manifest.bundle_id,
            manifest_sha256=signed.manifest_sha256,
            producer_generation=signed.manifest.producer_generation,
            domains=tuple(item.domain for item in signed.manifest.version_vector),
            changed=changed,
        )


def reject_forbidden_offline_fields(value: object) -> None:
    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if type(key) is not str or key.lower() in _FORBIDDEN_KEYS:
                    raise OfflineBundleContractError("edge_offline_bundle.forbidden_field")
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif item is None or type(item) in {str, int, bool, float}:
            return
        else:
            raise OfflineBundleContractError("edge_offline_bundle.forbidden_field")

    visit(value)


def _entry_merkle_root(entries: Sequence[OfflineBundleEntry]) -> str:
    leaves = [
        hashlib.sha256(canonical_json_bytes(item.model_dump(mode="json"))).digest()
        for item in entries
    ]
    if not leaves:
        return hashlib.sha256(b"").hexdigest()
    while len(leaves) > 1:
        if len(leaves) % 2:
            leaves.append(leaves[-1])
        leaves = [
            hashlib.sha256(leaves[index] + leaves[index + 1]).digest()
            for index in range(0, len(leaves), 2)
        ]
    return leaves[0].hex()


def _signature_payload(canonical_manifest: bytes) -> bytes:
    return OFFLINE_BUNDLE_SIGNATURE_PURPOSE.encode("ascii") + b"\0" + canonical_manifest


def _entry_aad(
    bundle_id: str,
    direction: str,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    domain: str,
    sequence: int,
    entry_id: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "bundle_id": bundle_id,
            "deployment_id": deployment_id,
            "direction": direction,
            "domain": domain,
            "entry_id": entry_id,
            "purpose": OFFLINE_BUNDLE_SIGNATURE_PURPOSE,
            "sequence": sequence,
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
        }
    )


def _safe_archive_info(info: zipfile.ZipInfo) -> bool:
    path = PurePosixPath(info.filename)
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return (
        info.compress_type == zipfile.ZIP_STORED
        and not info.is_dir()
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in info.filename
        and not stat.S_ISLNK(unix_mode)
        and info.file_size == info.compress_size
    )


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_private(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _copy_private(source: Path, target: Path) -> None:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with source.open("rb") as reader, os.fdopen(descriptor, "wb", closefd=True) as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        _write_private(temporary, canonical_json_bytes(value))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OfflineBundleContractError("edge_offline_bundle.time_invalid")
    return value.astimezone(UTC)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    try:
        if type(value) is not str or "=" in value:
            raise ValueError
        return base64.b64decode(
            value + "=" * ((4 - len(value) % 4) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError):
        raise ValueError from None


__all__ = (
    "MAX_OFFLINE_BUNDLE_BYTES",
    "MAX_OFFLINE_ENTRIES",
    "MAX_OFFLINE_ENTRY_BYTES",
    "OFFLINE_BUNDLE_DOMAINS",
    "OFFLINE_BUNDLE_ENVELOPE_FORMAT",
    "OFFLINE_BUNDLE_SCHEMA_VERSION",
    "OFFLINE_BUNDLE_SIGNATURE_PURPOSE",
    "OfflineBundleContractError",
    "OfflineBundleApplyResult",
    "OfflineBundleEntry",
    "OfflineBundleKeyEnvelope",
    "OfflineBundleManifest",
    "OfflineBundleSignature",
    "OfflineBundleStateStore",
    "OfflineConflictCandidate",
    "OfflineDomainVersion",
    "SignedOfflineBundle",
    "VerifiedOfflineArchive",
    "VerifiedOfflineBundle",
    "build_offline_bundle_archive",
    "detect_offline_conflicts",
    "reject_forbidden_offline_fields",
    "sign_offline_bundle_manifest",
    "verify_offline_bundle_archive",
    "verify_offline_bundle_manifest",
)
