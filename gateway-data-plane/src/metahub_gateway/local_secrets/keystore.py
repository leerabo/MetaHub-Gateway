"""OS key store and customer-KMS protection for local custody roots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_bytes
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from metahub_shared.private_gateway import LocalKek, LocalKekKeyring

_FORMAT = "metahub-local-keystore-v1"
_FILE_KEYS = frozenset(
    {
        "format",
        "current_kek_version",
        "current_kek_blob",
        "previous_kek_version",
        "previous_kek_blob",
        "session_signing_blob",
    }
)


class LocalKeyStoreError(RuntimeError):
    """Fixed key-store failure without paths, keys, or provider details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LocalKeyProtector(Protocol):
    def protect(self, value: bytes, *, purpose: str) -> bytes: ...

    def unprotect(self, value: bytes, *, purpose: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class WindowsDpapiProtector:
    """Bind local roots to the Windows service account using DPAPI."""

    deployment_id: str

    def protect(self, value: bytes, *, purpose: str) -> bytes:
        try:
            import win32crypt

            return win32crypt.CryptProtectData(
                value,
                "MetaHub local key material",
                self._entropy(purpose),
                None,
                None,
                1,
            )
        except Exception:
            raise LocalKeyStoreError("local_keystore.protection_unavailable") from None

    def unprotect(self, value: bytes, *, purpose: str) -> bytes:
        try:
            import win32crypt

            _description, plaintext = win32crypt.CryptUnprotectData(
                value,
                self._entropy(purpose),
                None,
                None,
                1,
            )
            return bytes(plaintext)
        except Exception:
            raise LocalKeyStoreError("local_keystore.integrity_failed") from None

    def _entropy(self, purpose: str) -> bytes:
        return hashlib.sha256(
            f"metahub/local-keystore/v1/{self.deployment_id}/{purpose}".encode("ascii")
        ).digest()


@dataclass(frozen=True, slots=True)
class TestKmsProtector:
    """Deterministic dev/test KMS facade; production construction is forbidden by config."""

    master_secret: str = field(repr=False)
    deployment_id: str

    def __post_init__(self) -> None:
        if len(self.master_secret.encode("utf-8")) < 32:
            raise LocalKeyStoreError("local_keystore.configuration_unavailable")

    def protect(self, value: bytes, *, purpose: str) -> bytes:
        nonce = token_bytes(12)
        try:
            return nonce + AESGCM(self._key(purpose)).encrypt(nonce, value, self._aad(purpose))
        except Exception:
            raise LocalKeyStoreError("local_keystore.protection_unavailable") from None

    def unprotect(self, value: bytes, *, purpose: str) -> bytes:
        if type(value) is not bytes or len(value) < 29:
            raise LocalKeyStoreError("local_keystore.integrity_failed")
        try:
            return AESGCM(self._key(purpose)).decrypt(
                value[:12], value[12:], self._aad(purpose)
            )
        except (InvalidTag, TypeError, ValueError):
            raise LocalKeyStoreError("local_keystore.integrity_failed") from None

    def _key(self, purpose: str) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"metahub/local-keystore/test-kms/v1",
            info=self._aad(purpose),
        ).derive(self.master_secret.encode("utf-8"))

    def _aad(self, purpose: str) -> bytes:
        return f"{self.deployment_id}/{purpose}".encode("ascii")


@dataclass(frozen=True, slots=True)
class AwsKmsProtector:
    """Wrap local roots with a customer-owned AWS KMS key."""

    key_id: str = field(repr=False)
    deployment_id: str

    def protect(self, value: bytes, *, purpose: str) -> bytes:
        try:
            import boto3

            response = boto3.client("kms").encrypt(
                KeyId=self.key_id,
                Plaintext=value,
                EncryptionContext=self._context(purpose),
            )
            encrypted = response.get("CiphertextBlob")
            if type(encrypted) not in {bytes, bytearray}:
                raise TypeError
            return bytes(encrypted)
        except Exception:
            raise LocalKeyStoreError("local_keystore.protection_unavailable") from None

    def unprotect(self, value: bytes, *, purpose: str) -> bytes:
        try:
            import boto3

            response = boto3.client("kms").decrypt(
                KeyId=self.key_id,
                CiphertextBlob=value,
                EncryptionContext=self._context(purpose),
            )
            plaintext = response.get("Plaintext")
            if type(plaintext) not in {bytes, bytearray}:
                raise TypeError
            return bytes(plaintext)
        except Exception:
            raise LocalKeyStoreError("local_keystore.integrity_failed") from None

    def _context(self, purpose: str) -> dict[str, str]:
        return {
            "metahub-deployment-id": self.deployment_id,
            "metahub-purpose": purpose,
        }


@dataclass(frozen=True, slots=True)
class LocalKeyMaterial:
    keyring: LocalKekKeyring = field(repr=False)
    session_signing_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.keyring) is not LocalKekKeyring or (
            type(self.session_signing_key) is not bytes
            or len(self.session_signing_key) != 32
        ):
            raise LocalKeyStoreError("local_keystore.integrity_failed")


class LocalKeyMaterialStore:
    """Persist only protected roots in a local file, never in the custody database."""

    def __init__(self, path: str | Path, protector: LocalKeyProtector) -> None:
        self._path = Path(path).resolve()
        self._protector = protector
        if not callable(getattr(protector, "protect", None)) or not callable(
            getattr(protector, "unprotect", None)
        ):
            raise LocalKeyStoreError("local_keystore.configuration_unavailable")

    @property
    def path(self) -> Path:
        return self._path

    def load_or_create(self) -> LocalKeyMaterial:
        if self._path.exists():
            return self.load()
        current = token_bytes(32)
        session = token_bytes(32)
        payload = {
            "format": _FORMAT,
            "current_kek_version": 1,
            "current_kek_blob": _b64(
                self._protector.protect(current, purpose="local-secret-kek")
            ),
            "previous_kek_version": None,
            "previous_kek_blob": None,
            "session_signing_blob": _b64(
                self._protector.protect(session, purpose="local-admin-session")
            ),
        }
        self._write_once(payload)
        return LocalKeyMaterial(LocalKekKeyring(LocalKek(1, current)), session)

    def load(self) -> LocalKeyMaterial:
        try:
            raw = self._path.read_bytes()
            if not 1 <= len(raw) <= 1_048_576:
                raise ValueError
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
            if type(payload) is not dict or frozenset(payload) != _FILE_KEYS:
                raise ValueError
            if payload["format"] != _FORMAT:
                raise ValueError
            current_version = _version(payload["current_kek_version"])
            current = self._protector.unprotect(
                _unb64(payload["current_kek_blob"]), purpose="local-secret-kek"
            )
            session = self._protector.unprotect(
                _unb64(payload["session_signing_blob"]), purpose="local-admin-session"
            )
            previous_version = payload["previous_kek_version"]
            previous_blob = payload["previous_kek_blob"]
            previous = None
            if previous_version is not None or previous_blob is not None:
                if previous_version is None or previous_blob is None:
                    raise ValueError
                previous = LocalKek(
                    _version(previous_version),
                    self._protector.unprotect(
                        _unb64(previous_blob), purpose="local-secret-kek"
                    ),
                )
            return LocalKeyMaterial(
                LocalKekKeyring(LocalKek(current_version, current), previous),
                session,
            )
        except LocalKeyStoreError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise LocalKeyStoreError("local_keystore.integrity_failed") from None

    def _write_once(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb", dir=self._path.parent, prefix=".local-keystore-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                return
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise LocalKeyStoreError("local_keystore.storage_unavailable") from None


def build_key_protector(
    *,
    deployment_id: str,
    environment: str,
    kms_provider: str,
    kms_key_id: str | None,
    kms_test_master_key: str | None,
) -> LocalKeyProtector:
    if kms_provider == "aws" and kms_key_id:
        return AwsKmsProtector(kms_key_id, deployment_id)
    if kms_provider == "test" and environment in {"dev", "test"} and kms_test_master_key:
        return TestKmsProtector(kms_test_master_key, deployment_id)
    if kms_provider == "disabled" and sys.platform == "win32":
        return WindowsDpapiProtector(deployment_id)
    raise LocalKeyStoreError("local_keystore.configuration_unavailable")


def _version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValueError
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: object) -> bytes:
    if type(value) is not str or not 20 <= len(value) <= 8192 or "=" in value:
        raise ValueError
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


__all__ = (
    "AwsKmsProtector",
    "LocalKeyMaterial",
    "LocalKeyMaterialStore",
    "LocalKeyProtector",
    "LocalKeyStoreError",
    "TestKmsProtector",
    "WindowsDpapiProtector",
    "build_key_protector",
)
