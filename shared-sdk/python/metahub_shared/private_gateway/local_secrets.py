"""Secret-safe local custody contracts for customer-owned gateways."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_bytes
from typing import Annotated, Literal, Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type LocalSecretKind = Literal[
    "api_key",
    "bearer_token",
    "basic_password",
    "oauth_client_secret",
]
type SecretReferenceStatus = Literal["active", "rotating", "degraded", "disabled"]
type SecretVersionStatus = Literal["candidate", "active", "draining", "retired", "failed"]
type SecretRotationStatus = Literal[
    "preparing",
    "testing",
    "activating",
    "draining",
    "completed",
    "failed",
]
type ProviderTestOutcome = Literal[
    "succeeded",
    "authentication_failed",
    "authorization_failed",
    "rate_limited",
    "timeout",
    "tls_failed",
    "network_unreachable",
    "provider_unavailable",
    "unsupported",
]
type ProviderCapability = Literal["chat_completions", "embeddings", "models"]

LOCAL_SECRET_CIPHERTEXT_FORMAT = "local-secret-envelope-aes256gcm-v1"
LOCAL_SECRET_SCHEMA_VERSION = "1"
LOCAL_SECRET_MIN_BYTES = 1
LOCAL_SECRET_MAX_BYTES = 65_536
LOCAL_SECRET_NONCE_BYTES = 12
LOCAL_SECRET_DATA_KEY_BYTES = 32

_UTC_TIME = re.compile(
    r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?Z$"
)
_OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{2,79}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_REFERENCE_ID = re.compile(r"^lsr_[0-9a-f]{32}$")
_LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/()-]{0,79}$")
_SECRET_KINDS = frozenset({"api_key", "bearer_token", "basic_password", "oauth_client_secret"})


class LocalSecretError(ValueError):
    """Fixed-code failure that never includes secret or crypto material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SecretReference(_StrictModel):
    """The complete cloud-safe projection of one local secret reference."""

    schema_version: Literal["1"] = LOCAL_SECRET_SCHEMA_VERSION
    reference_id: str = Field(pattern=_REFERENCE_ID.pattern)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    tenant_id: str = Field(min_length=3, max_length=80)
    workspace_id: str = Field(min_length=3, max_length=80)
    provider_connection_id: str = Field(min_length=3, max_length=80)
    label: str = Field(min_length=1, max_length=80)
    kind: LocalSecretKind
    status: SecretReferenceStatus
    revision: int = Field(ge=1, le=2_147_483_647)
    active_version: int | None = Field(default=None, ge=1, le=2_147_483_647)
    last_test_outcome: ProviderTestOutcome | None = None
    capabilities: list[ProviderCapability] = Field(default_factory=list, max_length=3)
    rotated_at: str | None = Field(default=None, pattern=_UTC_TIME.pattern)
    updated_at: str = Field(pattern=_UTC_TIME.pattern)

    @field_validator("tenant_id", "workspace_id", "provider_connection_id")
    @classmethod
    def _validate_owner_id(cls, value: str) -> str:
        if _OWNER_ID.fullmatch(value) is None:
            raise ValueError("local_secret.reference_invalid")
        return value

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if _SAFE_LABEL.fullmatch(value) is None:
            raise ValueError("local_secret.reference_invalid")
        return value

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(
        cls, value: list[ProviderCapability]
    ) -> list[ProviderCapability]:
        if len(value) != len(set(value)) or value != sorted(value):
            raise ValueError("local_secret.reference_invalid")
        return value

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.status == "active" and self.active_version is None:
            raise ValueError("local_secret.reference_invalid")
        if self.status == "disabled" and self.active_version is not None:
            raise ValueError("local_secret.reference_invalid")
        if self.last_test_outcome is None and self.capabilities:
            raise ValueError("local_secret.reference_invalid")
        return self


class EdgeSecretReferenceChangedEvent(_StrictModel):
    event_id: str = Field(pattern=r"^lse_[0-9a-f]{32}$")
    event_type: Literal["edge.secret_reference_changed"]
    reference: SecretReference
    occurred_at: str = Field(pattern=_UTC_TIME.pattern)


class EdgeSecretRotationFailedEvent(_StrictModel):
    event_id: str = Field(pattern=r"^lse_[0-9a-f]{32}$")
    event_type: Literal["edge.secret_rotation_failed"]
    reference_id: str = Field(pattern=_REFERENCE_ID.pattern)
    revision: int = Field(ge=1, le=2_147_483_647)
    outcome: ProviderTestOutcome
    occurred_at: str = Field(pattern=_UTC_TIME.pattern)


class EdgeProviderTestCompletedEvent(_StrictModel):
    event_id: str = Field(pattern=r"^lse_[0-9a-f]{32}$")
    event_type: Literal["edge.provider_test_completed"]
    reference_id: str = Field(pattern=_REFERENCE_ID.pattern)
    version: int = Field(ge=1, le=2_147_483_647)
    outcome: ProviderTestOutcome
    capabilities: list[ProviderCapability] = Field(default_factory=list, max_length=3)
    latency_bucket: Literal["lt_100ms", "100_499ms", "500_1999ms", "gte_2000ms"]
    occurred_at: str = Field(pattern=_UTC_TIME.pattern)

    @model_validator(mode="after")
    def _validate_capabilities(self) -> Self:
        if self.capabilities != sorted(set(self.capabilities)):
            raise ValueError("edge.secret_event_invalid")
        return self


EdgeSecretSyncEvent = Annotated[
    EdgeSecretReferenceChangedEvent
    | EdgeSecretRotationFailedEvent
    | EdgeProviderTestCompletedEvent,
    Field(discriminator="event_type"),
]


class EdgeSecretSyncBatch(_StrictModel):
    schema_version: Literal["1"] = "1"
    batch_id: str = Field(pattern=r"^lsb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    events: list[EdgeSecretSyncEvent] = Field(min_length=1, max_length=64)
    sent_at: str = Field(pattern=_UTC_TIME.pattern)

    @model_validator(mode="after")
    def _validate_events(self) -> Self:
        event_ids = [event.event_id for event in self.events]
        occurred = [event.occurred_at for event in self.events]
        if len(event_ids) != len(set(event_ids)) or occurred != sorted(occurred):
            raise ValueError("edge.secret_batch_invalid")
        if occurred[-1] > self.sent_at:
            raise ValueError("edge.secret_batch_invalid")
        for event in self.events:
            if (
                isinstance(event, EdgeSecretReferenceChangedEvent)
                and event.reference.deployment_id != self.deployment_id
            ):
                raise ValueError("edge.secret_batch_invalid")
        return self


class EdgeSecretSyncReceipt(_StrictModel):
    schema_version: Literal["1"] = "1"
    receipt_id: str = Field(pattern=r"^lsa_[0-9a-f]{32}$")
    batch_id: str = Field(pattern=r"^lsb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    disposition: Literal["accepted", "duplicate"]
    accepted_event_count: int = Field(ge=0, le=64)
    duplicate_event_count: int = Field(ge=0, le=64)
    received_at: str = Field(pattern=_UTC_TIME.pattern)
    request_id: str = Field(min_length=1, max_length=160)
    trace_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        total = self.accepted_event_count + self.duplicate_event_count
        if not 1 <= total <= 64:
            raise ValueError("edge.secret_receipt_invalid")
        return self


@dataclass(frozen=True, slots=True)
class LocalSecretOwner:
    """Immutable owner fields used to reconstruct authenticated encryption AAD."""

    deployment_id: str
    tenant_id: str
    workspace_id: str
    reference_id: str
    version: int
    kind: LocalSecretKind
    binding_digest: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LocalKek:
    """One root supplied by the local OS key store or customer KMS."""

    version: int
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.version) is not int or not 1 <= self.version <= 2_147_483_647:
            raise LocalSecretError("local_secret.keyring_invalid")
        if type(self.key) is not bytes or len(self.key) != 32:
            raise LocalSecretError("local_secret.keyring_invalid")


@dataclass(frozen=True, slots=True)
class LocalKekKeyring:
    """A bounded current/previous keyring used for online local KEK rotation."""

    current: LocalKek
    previous: LocalKek | None = None

    def __post_init__(self) -> None:
        if type(self.current) is not LocalKek:
            raise LocalSecretError("local_secret.keyring_invalid")
        if self.previous is not None:
            if type(self.previous) is not LocalKek:
                raise LocalSecretError("local_secret.keyring_invalid")
            if self.previous.version >= self.current.version or hmac.compare_digest(
                self.previous.key, self.current.key
            ):
                raise LocalSecretError("local_secret.keyring_invalid")

    def get(self, version: int) -> LocalKek:
        if self.current.version == version:
            return self.current
        if self.previous is not None and self.previous.version == version:
            return self.previous
        raise LocalSecretError("local_secret.key_unavailable")


@dataclass(frozen=True, slots=True)
class EncryptedLocalSecret:
    """Persistent local-only envelope; all byte fields stay out of repr."""

    ciphertext_format: str
    kek_version: int
    wrap_nonce: bytes = field(repr=False)
    wrapped_data_key: bytes = field(repr=False)
    value_nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


class MutableSecretLease:
    """A short-lived mutable plaintext view that is zeroed on release."""

    __slots__ = ("_closed", "_value")

    def __init__(self, value: bytes) -> None:
        if type(value) is not bytes or not LOCAL_SECRET_MIN_BYTES <= len(value) <= (
            LOCAL_SECRET_MAX_BYTES
        ):
            raise LocalSecretError("local_secret.integrity_failed")
        self._value = bytearray(value)
        self._closed = False

    def view(self) -> memoryview:
        if self._closed:
            raise LocalSecretError("local_secret.lease_closed")
        return memoryview(self._value).toreadonly()

    def close(self) -> None:
        if self._closed:
            return
        self._value[:] = b"\x00" * len(self._value)
        self._closed = True

    def __enter__(self) -> Self:
        if self._closed:
            raise LocalSecretError("local_secret.lease_closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return "MutableSecretLease(<redacted>)"


def encrypt_local_secret(
    plaintext: object,
    *,
    owner: LocalSecretOwner,
    keyring: LocalKekKeyring,
    random_bytes: Callable[[int], bytes] = token_bytes,
) -> EncryptedLocalSecret:
    """Encrypt one local value under a random DEK wrapped by the current KEK."""

    checked_owner = _validated_owner(owner)
    checked_keyring = _validated_keyring(keyring)
    value = _plaintext_bytes(plaintext)
    data_key = bytearray(_random(random_bytes, LOCAL_SECRET_DATA_KEY_BYTES))
    wrap_nonce = _random(random_bytes, LOCAL_SECRET_NONCE_BYTES)
    value_nonce = _random(random_bytes, LOCAL_SECRET_NONCE_BYTES)
    if hmac.compare_digest(wrap_nonce, value_nonce):
        _zero(data_key)
        raise LocalSecretError("local_secret.configuration_unavailable")
    aad = local_secret_aad(checked_owner)
    try:
        ciphertext = AESGCM(bytes(data_key)).encrypt(value_nonce, value, aad)
        wrapped_data_key = AESGCM(checked_keyring.current.key).encrypt(
            wrap_nonce,
            bytes(data_key),
            _wrap_aad(checked_owner, checked_keyring.current.version),
        )
    except LocalSecretError:
        raise
    except Exception:
        raise LocalSecretError("local_secret.configuration_unavailable") from None
    finally:
        _zero(data_key)
    return EncryptedLocalSecret(
        ciphertext_format=LOCAL_SECRET_CIPHERTEXT_FORMAT,
        kek_version=checked_keyring.current.version,
        wrap_nonce=wrap_nonce,
        wrapped_data_key=wrapped_data_key,
        value_nonce=value_nonce,
        ciphertext=ciphertext,
    )


def decrypt_local_secret(
    encrypted: object,
    *,
    owner: LocalSecretOwner,
    keyring: LocalKekKeyring,
) -> MutableSecretLease:
    """Authenticate owner and envelope, returning plaintext only through a mutable lease."""

    checked_owner = _validated_owner(owner)
    checked_keyring = _validated_keyring(keyring)
    checked = _validated_envelope(encrypted)
    kek = checked_keyring.get(checked.kek_version)
    data_key = bytearray()
    try:
        data_key.extend(
            AESGCM(kek.key).decrypt(
                checked.wrap_nonce,
                checked.wrapped_data_key,
                _wrap_aad(checked_owner, checked.kek_version),
            )
        )
        plaintext = AESGCM(bytes(data_key)).decrypt(
            checked.value_nonce,
            checked.ciphertext,
            local_secret_aad(checked_owner),
        )
        return MutableSecretLease(plaintext)
    except LocalSecretError:
        raise
    except (InvalidTag, TypeError, ValueError):
        raise LocalSecretError("local_secret.integrity_failed") from None
    except Exception:
        raise LocalSecretError("local_secret.integrity_failed") from None
    finally:
        _zero(data_key)


def rewrap_local_secret(
    encrypted: object,
    *,
    owner: LocalSecretOwner,
    keyring: LocalKekKeyring,
    random_bytes: Callable[[int], bytes] = token_bytes,
) -> EncryptedLocalSecret:
    """Rewrap only the DEK under the current KEK without decrypting the secret value."""

    checked_owner = _validated_owner(owner)
    checked_keyring = _validated_keyring(keyring)
    checked = _validated_envelope(encrypted)
    if checked.kek_version == checked_keyring.current.version:
        return checked
    old_kek = checked_keyring.get(checked.kek_version)
    data_key = bytearray()
    try:
        data_key.extend(
            AESGCM(old_kek.key).decrypt(
                checked.wrap_nonce,
                checked.wrapped_data_key,
                _wrap_aad(checked_owner, checked.kek_version),
            )
        )
        wrap_nonce = _random(random_bytes, LOCAL_SECRET_NONCE_BYTES)
        wrapped = AESGCM(checked_keyring.current.key).encrypt(
            wrap_nonce,
            bytes(data_key),
            _wrap_aad(checked_owner, checked_keyring.current.version),
        )
    except (InvalidTag, TypeError, ValueError):
        raise LocalSecretError("local_secret.integrity_failed") from None
    except LocalSecretError:
        raise
    except Exception:
        raise LocalSecretError("local_secret.integrity_failed") from None
    finally:
        _zero(data_key)
    return EncryptedLocalSecret(
        ciphertext_format=LOCAL_SECRET_CIPHERTEXT_FORMAT,
        kek_version=checked_keyring.current.version,
        wrap_nonce=wrap_nonce,
        wrapped_data_key=wrapped,
        value_nonce=checked.value_nonce,
        ciphertext=checked.ciphertext,
    )


def local_secret_aad(owner: object) -> bytes:
    """Return canonical AAD bound to every immutable owner dimension."""

    checked = _validated_owner(owner)
    return _canonical(
        {
            "binding_digest": checked.binding_digest,
            "ciphertext_format": LOCAL_SECRET_CIPHERTEXT_FORMAT,
            "deployment_id": checked.deployment_id,
            "kind": checked.kind,
            "reference_id": checked.reference_id,
            "tenant_id": checked.tenant_id,
            "version": checked.version,
            "workspace_id": checked.workspace_id,
        }
    )


def secret_reference_sha256(reference: SecretReference) -> str:
    """Digest only the already-safe cloud projection for idempotent synchronization."""

    if type(reference) is not SecretReference:
        raise LocalSecretError("local_secret.reference_invalid")
    return hashlib.sha256(_canonical(reference.model_dump(mode="json"))).hexdigest()


def secret_sync_batch_sha256(batch: EdgeSecretSyncBatch) -> str:
    """Digest the exact safe Agent-to-Control synchronization contract."""

    if type(batch) is not EdgeSecretSyncBatch:
        raise LocalSecretError("local_secret.sync_batch_invalid")
    return hashlib.sha256(_canonical(batch.model_dump(mode="json"))).hexdigest()


def _wrap_aad(owner: LocalSecretOwner, kek_version: int) -> bytes:
    return _canonical(
        {
            "content_aad_sha256": hashlib.sha256(local_secret_aad(owner)).hexdigest(),
            "kek_version": kek_version,
            "purpose": "local-secret-data-key-wrap",
        }
    )


def _validated_owner(value: object) -> LocalSecretOwner:
    if type(value) is not LocalSecretOwner:
        raise LocalSecretError("local_secret.owner_invalid")
    if _DEPLOYMENT_ID.fullmatch(value.deployment_id) is None:
        raise LocalSecretError("local_secret.owner_invalid")
    if _REFERENCE_ID.fullmatch(value.reference_id) is None:
        raise LocalSecretError("local_secret.owner_invalid")
    if any(_OWNER_ID.fullmatch(item) is None for item in (value.tenant_id, value.workspace_id)):
        raise LocalSecretError("local_secret.owner_invalid")
    if type(value.version) is not int or not 1 <= value.version <= 2_147_483_647:
        raise LocalSecretError("local_secret.owner_invalid")
    if value.kind not in _SECRET_KINDS:
        raise LocalSecretError("local_secret.owner_invalid")
    if type(value.binding_digest) is not str or _LOWER_HEX_64.fullmatch(
        value.binding_digest
    ) is None:
        raise LocalSecretError("local_secret.owner_invalid")
    return value


def _validated_keyring(value: object) -> LocalKekKeyring:
    if type(value) is not LocalKekKeyring:
        raise LocalSecretError("local_secret.keyring_invalid")
    return value


def _validated_envelope(value: object) -> EncryptedLocalSecret:
    if type(value) is not EncryptedLocalSecret:
        raise LocalSecretError("local_secret.integrity_failed")
    if (
        value.ciphertext_format != LOCAL_SECRET_CIPHERTEXT_FORMAT
        or type(value.kek_version) is not int
        or value.kek_version < 1
        or type(value.wrap_nonce) is not bytes
        or len(value.wrap_nonce) != LOCAL_SECRET_NONCE_BYTES
        or type(value.value_nonce) is not bytes
        or len(value.value_nonce) != LOCAL_SECRET_NONCE_BYTES
        or type(value.wrapped_data_key) is not bytes
        or len(value.wrapped_data_key) != LOCAL_SECRET_DATA_KEY_BYTES + 16
        or type(value.ciphertext) is not bytes
        or not LOCAL_SECRET_MIN_BYTES + 16 <= len(value.ciphertext) <= (
            LOCAL_SECRET_MAX_BYTES + 16
        )
    ):
        raise LocalSecretError("local_secret.integrity_failed")
    return value


def _plaintext_bytes(value: object) -> bytes:
    if type(value) is bytes:
        checked = value
    elif type(value) is bytearray:
        checked = bytes(value)
    else:
        raise LocalSecretError("local_secret.payload_invalid")
    if not LOCAL_SECRET_MIN_BYTES <= len(checked) <= LOCAL_SECRET_MAX_BYTES:
        raise LocalSecretError("local_secret.payload_invalid")
    return checked


def _random(factory: Callable[[int], bytes], length: int) -> bytes:
    if not callable(factory):
        raise LocalSecretError("local_secret.configuration_unavailable")
    try:
        value = factory(length)
    except Exception:
        raise LocalSecretError("local_secret.configuration_unavailable") from None
    if type(value) is not bytes or len(value) != length:
        raise LocalSecretError("local_secret.configuration_unavailable")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise LocalSecretError("local_secret.contract_invalid") from None


def _zero(value: bytearray) -> None:
    value[:] = b"\x00" * len(value)


__all__ = (
    "EdgeProviderTestCompletedEvent",
    "EdgeSecretReferenceChangedEvent",
    "EdgeSecretRotationFailedEvent",
    "EdgeSecretSyncBatch",
    "EdgeSecretSyncEvent",
    "EdgeSecretSyncReceipt",
    "EncryptedLocalSecret",
    "LOCAL_SECRET_CIPHERTEXT_FORMAT",
    "LOCAL_SECRET_MAX_BYTES",
    "LOCAL_SECRET_SCHEMA_VERSION",
    "LocalKek",
    "LocalKekKeyring",
    "LocalSecretError",
    "LocalSecretKind",
    "LocalSecretOwner",
    "MutableSecretLease",
    "ProviderCapability",
    "ProviderTestOutcome",
    "SecretReference",
    "SecretReferenceStatus",
    "SecretRotationStatus",
    "SecretVersionStatus",
    "decrypt_local_secret",
    "encrypt_local_secret",
    "local_secret_aad",
    "rewrap_local_secret",
    "secret_reference_sha256",
    "secret_sync_batch_sha256",
)
