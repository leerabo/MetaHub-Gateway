"""Customer-owned private Gateway backup envelopes and restore verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from secrets import token_bytes
from typing import Literal

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .desired_state import canonical_json_bytes, strict_json_object

BACKUP_SCHEMA_VERSION = "1"
BACKUP_SIGNATURE_PURPOSE = "metahub.edge-backup.manifest/v1"
BACKUP_CIPHER_FORMAT = "metahub-edge-backup-aesgcm-v1"
BACKUP_KEY_ENVELOPE_FORMAT = "metahub-edge-backup-key-envelope-v1"
BACKUP_DOMAIN_CODES = (
    "config_policy",
    "ledger_watermark",
    "audit_chain",
    "local_secrets",
)
MAX_BACKUP_CHUNK_BYTES = 16 * 1024 * 1024
MAX_BACKUP_CHUNKS = 1_000_000
_ID = re.compile(r"^ebk_[0-9a-f]{32}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^[a-z][a-z0-9_:-]{2,79}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_ID = re.compile(r"^bch_[0-9a-f]{32}$")
_SCHEMA = re.compile(r"^[0-9]{4}$")

type BackupDomainCode = Literal[
    "config_policy", "ledger_watermark", "audit_chain", "local_secrets"
]


class EdgeBackupContractError(ValueError):
    """Fixed backup failure that never carries paths, keys, or archive content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class BackupChunkDescriptor(_StrictModel):
    chunk_id: str = Field(pattern=_CHUNK_ID.pattern)
    domain_code: BackupDomainCode
    sequence: int = Field(ge=0, le=MAX_BACKUP_CHUNKS - 1)
    plaintext_size: int = Field(ge=1, le=MAX_BACKUP_CHUNK_BYTES)
    ciphertext_size: int = Field(ge=17, le=MAX_BACKUP_CHUNK_BYTES + 16)
    plaintext_sha256: str = Field(pattern=_HEX_64.pattern)
    ciphertext_sha256: str = Field(pattern=_HEX_64.pattern)
    nonce_b64: str = Field(pattern=r"^[A-Za-z0-9_-]{16}$", repr=False)


class BackupKeyEnvelope(_StrictModel):
    format: Literal["metahub-edge-backup-key-envelope-v1"] = BACKUP_KEY_ENVELOPE_FORMAT
    key_version: int = Field(ge=1, le=2_147_483_647)
    kms_key_id_sha256: str = Field(pattern=_HEX_64.pattern)
    wrapped_data_key_b64: str = Field(
        min_length=40, max_length=16_384, pattern=r"^[A-Za-z0-9_-]+$", repr=False
    )


class EdgeBackupManifest(_StrictModel):
    schema_version: Literal["1"] = BACKUP_SCHEMA_VERSION
    backup_id: str = Field(pattern=_ID.pattern)
    workspace_id: str = Field(pattern=_OWNER_ID.pattern)
    tenant_id: str = Field(pattern=_OWNER_ID.pattern)
    source_deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    source_generation: int = Field(ge=1)
    source_schema_head: str = Field(pattern=_SCHEMA.pattern)
    purpose: Literal["metahub.edge-backup.manifest/v1"] = BACKUP_SIGNATURE_PURPOSE
    cipher_format: Literal["metahub-edge-backup-aesgcm-v1"] = BACKUP_CIPHER_FORMAT
    chunk_merkle_root: str = Field(pattern=_HEX_64.pattern)
    key_envelope_sha256: str = Field(pattern=_HEX_64.pattern)
    chunks: tuple[BackupChunkDescriptor, ...] = Field(
        min_length=4, max_length=MAX_BACKUP_CHUNKS
    )
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _canonical_chunks(self) -> EdgeBackupManifest:
        normalized = tuple(
            sorted(self.chunks, key=lambda item: (item.domain_code, item.sequence))
        )
        if (
            self.chunks != normalized
            or len({item.chunk_id for item in self.chunks}) != len(self.chunks)
            or {item.domain_code for item in self.chunks} != set(BACKUP_DOMAIN_CODES)
            or any(
                tuple(item.sequence for item in self.chunks if item.domain_code == domain)
                != tuple(range(sum(item.domain_code == domain for item in self.chunks)))
                for domain in BACKUP_DOMAIN_CODES
            )
            or _aware(self.expires_at) <= _aware(self.created_at)
        ):
            raise ValueError("edge_backup.manifest_invalid")
        return self


class BackupSignature(_StrictModel):
    key_id: str = Field(min_length=3, max_length=80)
    public_key_b64: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    value: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$", repr=False)


class SignedEdgeBackup(_StrictModel):
    manifest: EdgeBackupManifest
    manifest_sha256: str = Field(pattern=_HEX_64.pattern)
    signature: BackupSignature


@dataclass(frozen=True, slots=True, repr=False)
class EdgeBackupPackage:
    signed: SignedEdgeBackup
    key_envelope: BackupKeyEnvelope = field(repr=False)
    chunks: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.signed) is not SignedEdgeBackup
            or type(self.key_envelope) is not BackupKeyEnvelope
            or type(self.chunks) is not dict
            or any(
                type(key) is not str or type(value) is not bytes
                for key, value in self.chunks.items()
            )
        ):
            raise EdgeBackupContractError("edge_backup.package_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedEdgeBackup:
    signed: SignedEdgeBackup
    domains: Mapping[BackupDomainCode, bytes] = field(repr=False)


def build_edge_backup(
    *,
    backup_id: str,
    workspace_id: str,
    tenant_id: str,
    source_deployment_id: str,
    source_generation: int,
    source_schema_head: str,
    domains: Mapping[str, bytes],
    key_id: str,
    kms_key_id: str,
    key_version: int,
    wrap_data_key: Callable[[bytes], bytes],
    signing_key: Ed25519PrivateKey,
    created_at: datetime,
    expires_at: datetime,
    chunk_bytes: int = MAX_BACKUP_CHUNK_BYTES,
) -> EdgeBackupPackage:
    """Encrypt exact backup domains before producing a signed root manifest."""

    if (
        _ID.fullmatch(backup_id) is None
        or _OWNER_ID.fullmatch(workspace_id) is None
        or _OWNER_ID.fullmatch(tenant_id) is None
        or _DEPLOYMENT_ID.fullmatch(source_deployment_id) is None
        or type(source_generation) is not int
        or source_generation < 1
        or _SCHEMA.fullmatch(source_schema_head) is None
        or set(domains) != set(BACKUP_DOMAIN_CODES)
        or any(type(value) is not bytes or not value for value in domains.values())
        or type(key_id) is not str
        or not 3 <= len(key_id) <= 80
        or type(kms_key_id) is not str
        or not 3 <= len(kms_key_id) <= 512
        or type(key_version) is not int
        or key_version < 1
        or not callable(wrap_data_key)
        or not isinstance(signing_key, Ed25519PrivateKey)
        or type(chunk_bytes) is not int
        or not 1024 <= chunk_bytes <= MAX_BACKUP_CHUNK_BYTES
        or _aware(expires_at) <= _aware(created_at)
    ):
        raise EdgeBackupContractError("edge_backup.request_invalid")

    data_key = token_bytes(32)
    try:
        wrapped = wrap_data_key(data_key)
        if type(wrapped) is not bytes or not 32 <= len(wrapped) <= 12_288:
            raise EdgeBackupContractError("edge_backup.key_wrap_failed")
        envelope = BackupKeyEnvelope(
            key_version=key_version,
            kms_key_id_sha256=_sha(kms_key_id.encode("utf-8")),
            wrapped_data_key_b64=_b64(wrapped),
        )
        envelope_hash = _sha(canonical_json_bytes(envelope.model_dump(mode="json")))
        descriptors: list[BackupChunkDescriptor] = []
        encrypted_chunks: dict[str, bytes] = {}
        cipher = AESGCM(data_key)
        for domain in BACKUP_DOMAIN_CODES:
            source = domains[domain]
            for sequence, offset in enumerate(range(0, len(source), chunk_bytes)):
                plaintext = source[offset : offset + chunk_bytes]
                nonce = token_bytes(12)
                chunk_id = _chunk_id(backup_id, domain, sequence)
                aad = _chunk_aad(
                    backup_id=backup_id,
                    workspace_id=workspace_id,
                    tenant_id=tenant_id,
                    deployment_id=source_deployment_id,
                    source_generation=source_generation,
                    schema_head=source_schema_head,
                    domain=domain,
                    sequence=sequence,
                    chunk_id=chunk_id,
                )
                ciphertext = cipher.encrypt(nonce, plaintext, aad)
                descriptor = BackupChunkDescriptor(
                    chunk_id=chunk_id,
                    domain_code=domain,  # type: ignore[arg-type]
                    sequence=sequence,
                    plaintext_size=len(plaintext),
                    ciphertext_size=len(ciphertext),
                    plaintext_sha256=_sha(plaintext),
                    ciphertext_sha256=_sha(ciphertext),
                    nonce_b64=_b64(nonce),
                )
                descriptors.append(descriptor)
                encrypted_chunks[chunk_id] = ciphertext
                if len(descriptors) > MAX_BACKUP_CHUNKS:
                    raise EdgeBackupContractError("edge_backup.size_exceeded")
        normalized = tuple(
            sorted(descriptors, key=lambda item: (item.domain_code, item.sequence))
        )
        manifest = EdgeBackupManifest(
            backup_id=backup_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            source_deployment_id=source_deployment_id,
            source_generation=source_generation,
            source_schema_head=source_schema_head,
            chunk_merkle_root=_merkle_root(normalized),
            key_envelope_sha256=envelope_hash,
            chunks=normalized,
            created_at=_aware(created_at),
            expires_at=_aware(expires_at),
        )
        signature = signing_key.sign(_signature_payload(manifest))
        public = signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        signed = SignedEdgeBackup(
            manifest=manifest,
            manifest_sha256=_sha(canonical_json_bytes(manifest.model_dump(mode="json"))),
            signature=BackupSignature(
                key_id=key_id,
                public_key_b64=_b64(public),
                value=_b64(signature),
            ),
        )
        return EdgeBackupPackage(signed, envelope, encrypted_chunks)
    except EdgeBackupContractError:
        raise
    except (OSError, TypeError, ValueError):
        raise EdgeBackupContractError("edge_backup.creation_failed") from None


def verify_edge_backup(
    package: EdgeBackupPackage,
    *,
    expected_workspace_id: str,
    expected_tenant_id: str,
    expected_source_deployment_id: str,
    target_schema_head: str,
    unwrap_data_key: Callable[[bytes], bytes],
    now: datetime,
) -> VerifiedEdgeBackup:
    """Verify the complete archive before returning any plaintext domain."""

    if type(package) is not EdgeBackupPackage or not callable(unwrap_data_key):
        raise EdgeBackupContractError("edge_backup.package_invalid")
    manifest = package.signed.manifest
    if not (
        hmac.compare_digest(manifest.workspace_id, expected_workspace_id)
        and hmac.compare_digest(manifest.tenant_id, expected_tenant_id)
        and hmac.compare_digest(
            manifest.source_deployment_id, expected_source_deployment_id
        )
    ):
        raise EdgeBackupContractError("edge_backup.owner_mismatch")
    if not _schema_compatible(manifest.source_schema_head, target_schema_head):
        raise EdgeBackupContractError("edge_backup.schema_incompatible")
    if _aware(now) > _aware(manifest.expires_at) or _aware(now) < _aware(
        manifest.created_at
    ):
        raise EdgeBackupContractError("edge_backup.expired")

    manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
    if not hmac.compare_digest(package.signed.manifest_sha256, _sha(manifest_bytes)):
        raise EdgeBackupContractError("edge_backup.manifest_invalid")
    envelope_bytes = canonical_json_bytes(package.key_envelope.model_dump(mode="json"))
    if not hmac.compare_digest(manifest.key_envelope_sha256, _sha(envelope_bytes)):
        raise EdgeBackupContractError("edge_backup.envelope_invalid")
    if not hmac.compare_digest(
        manifest.chunk_merkle_root, _merkle_root(manifest.chunks)
    ):
        raise EdgeBackupContractError("edge_backup.merkle_invalid")
    if set(package.chunks) != {item.chunk_id for item in manifest.chunks}:
        raise EdgeBackupContractError("edge_backup.chunk_missing")
    try:
        public = Ed25519PublicKey.from_public_bytes(
            _unb64(package.signed.signature.public_key_b64)
        )
        public.verify(
            _unb64(package.signed.signature.value), _signature_payload(manifest)
        )
        data_key = unwrap_data_key(_unb64(package.key_envelope.wrapped_data_key_b64))
        if type(data_key) is not bytes or len(data_key) != 32:
            raise EdgeBackupContractError("edge_backup.key_unwrap_failed")
        cipher = AESGCM(data_key)
        parts: dict[str, list[bytes]] = {domain: [] for domain in BACKUP_DOMAIN_CODES}
        for descriptor in manifest.chunks:
            ciphertext = package.chunks[descriptor.chunk_id]
            if (
                len(ciphertext) != descriptor.ciphertext_size
                or not hmac.compare_digest(
                    _sha(ciphertext), descriptor.ciphertext_sha256
                )
            ):
                raise EdgeBackupContractError("edge_backup.chunk_invalid")
            plaintext = cipher.decrypt(
                _unb64(descriptor.nonce_b64),
                ciphertext,
                _chunk_aad(
                    backup_id=manifest.backup_id,
                    workspace_id=manifest.workspace_id,
                    tenant_id=manifest.tenant_id,
                    deployment_id=manifest.source_deployment_id,
                    source_generation=manifest.source_generation,
                    schema_head=manifest.source_schema_head,
                    domain=descriptor.domain_code,
                    sequence=descriptor.sequence,
                    chunk_id=descriptor.chunk_id,
                ),
            )
            if (
                len(plaintext) != descriptor.plaintext_size
                or not hmac.compare_digest(
                    _sha(plaintext), descriptor.plaintext_sha256
                )
            ):
                raise EdgeBackupContractError("edge_backup.chunk_invalid")
            parts[descriptor.domain_code].append(plaintext)
        domains = {domain: b"".join(parts[domain]) for domain in BACKUP_DOMAIN_CODES}
        return VerifiedEdgeBackup(package.signed, domains)  # type: ignore[arg-type]
    except EdgeBackupContractError:
        raise
    except (InvalidSignature, InvalidTag, TypeError, ValueError):
        raise EdgeBackupContractError("edge_backup.integrity_failed") from None


def parse_signed_backup(value: bytes) -> SignedEdgeBackup:
    try:
        strict_json_object(value, canonical=True, maximum=8_388_608)
        return SignedEdgeBackup.model_validate_json(value, strict=True)
    except (TypeError, ValueError):
        raise EdgeBackupContractError("edge_backup.manifest_invalid") from None


def backup_manifest_sha256(value: EdgeBackupManifest) -> str:
    if type(value) is not EdgeBackupManifest:
        raise EdgeBackupContractError("edge_backup.manifest_invalid")
    return _sha(canonical_json_bytes(value.model_dump(mode="json")))


def _signature_payload(manifest: EdgeBackupManifest) -> bytes:
    return BACKUP_SIGNATURE_PURPOSE.encode("ascii") + b"\x00" + canonical_json_bytes(
        manifest.model_dump(mode="json")
    )


def _chunk_aad(
    *,
    backup_id: str,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    source_generation: int,
    schema_head: str,
    domain: str,
    sequence: int,
    chunk_id: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "format": BACKUP_CIPHER_FORMAT,
            "purpose": BACKUP_SIGNATURE_PURPOSE,
            "backup_id": backup_id,
            "workspace_id": workspace_id,
            "tenant_id": tenant_id,
            "deployment_id": deployment_id,
            "source_generation": source_generation,
            "schema_head": schema_head,
            "domain": domain,
            "sequence": sequence,
            "chunk_id": chunk_id,
        }
    )


def _chunk_id(backup_id: str, domain: str, sequence: int) -> str:
    digest = hashlib.sha256(
        f"{backup_id}\x1f{domain}\x1f{sequence}".encode("ascii")
    ).hexdigest()
    return "bch_" + digest[:32]


def _merkle_root(chunks: tuple[BackupChunkDescriptor, ...]) -> str:
    nodes = [
        hashlib.sha256(
            b"leaf\x00"
            + item.chunk_id.encode("ascii")
            + bytes.fromhex(item.ciphertext_sha256)
            + item.ciphertext_size.to_bytes(8, "big")
        ).digest()
        for item in chunks
    ]
    if not nodes:
        raise EdgeBackupContractError("edge_backup.merkle_invalid")
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        nodes = [
            hashlib.sha256(b"node\x00" + nodes[index] + nodes[index + 1]).digest()
            for index in range(0, len(nodes), 2)
        ]
    return nodes[0].hex()


def _schema_compatible(source: str, target: str) -> bool:
    if _SCHEMA.fullmatch(source) is None or _SCHEMA.fullmatch(target) is None:
        return False
    return int(source) in {int(target), int(target) - 1}


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EdgeBackupContractError("edge_backup.time_invalid")
    return value.astimezone(UTC)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if type(value) is not str or "=" in value:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _b64(decoded) != value:
        raise ValueError
    return decoded


__all__ = (
    "BACKUP_CIPHER_FORMAT",
    "BACKUP_DOMAIN_CODES",
    "BACKUP_KEY_ENVELOPE_FORMAT",
    "BACKUP_SCHEMA_VERSION",
    "BACKUP_SIGNATURE_PURPOSE",
    "MAX_BACKUP_CHUNK_BYTES",
    "MAX_BACKUP_CHUNKS",
    "BackupChunkDescriptor",
    "BackupKeyEnvelope",
    "BackupSignature",
    "EdgeBackupContractError",
    "EdgeBackupManifest",
    "EdgeBackupPackage",
    "SignedEdgeBackup",
    "VerifiedEdgeBackup",
    "backup_manifest_sha256",
    "build_edge_backup",
    "parse_signed_backup",
    "verify_edge_backup",
)
