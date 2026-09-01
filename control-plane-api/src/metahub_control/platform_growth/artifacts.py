"""Private encrypted artifact storage for PRD-141 growth exports."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

_REFERENCE = re.compile(r"^growth-artifact-v1:(gxp_[0-9a-f]{32}):([1-9][0-9]*)$")


def store_artifact(
    config: AppConfig,
    *,
    export_id: str,
    generation: int,
    plaintext: bytes,
) -> tuple[str, str]:
    reference = f"growth-artifact-v1:{export_id}:{generation}"
    digest = hashlib.sha256(plaintext).hexdigest()
    nonce = secrets.token_bytes(12)
    try:
        encrypted = nonce + AESGCM(_key(config)).encrypt(
            nonce,
            plaintext,
            _aad(export_id, generation, digest),
        )
        path = _path(config, export_id, generation)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return reference, digest
    except ApiError:
        raise
    except (OSError, ValueError):
        raise ApiError("platform.growth_export_unavailable") from None


def load_artifact(
    config: AppConfig,
    *,
    reference: str,
    expected_sha256: str,
) -> bytes:
    match = _REFERENCE.fullmatch(reference)
    if match is None or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ApiError("platform.growth_export_unavailable")
    export_id, generation_text = match.groups()
    generation = int(generation_text)
    try:
        encrypted = _path(config, export_id, generation).read_bytes()
        if len(encrypted) < 29:
            raise ValueError
        plaintext = AESGCM(_key(config)).decrypt(
            encrypted[:12],
            encrypted[12:],
            _aad(export_id, generation, expected_sha256),
        )
        if not secrets.compare_digest(hashlib.sha256(plaintext).hexdigest(), expected_sha256):
            raise ValueError
        return plaintext
    except ApiError:
        raise
    except (OSError, ValueError):
        raise ApiError("platform.growth_export_unavailable") from None


def destroy_artifact(config: AppConfig, reference: str | None) -> None:
    if reference is None:
        return
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise ApiError("platform.growth_export_unavailable")
    export_id, generation_text = match.groups()
    try:
        _path(config, export_id, int(generation_text)).unlink(missing_ok=True)
    except OSError:
        raise ApiError("platform.growth_export_unavailable") from None


def _path(config: AppConfig, export_id: str, generation: int) -> Path:
    database_url = config.database_url
    if database_url.startswith("sqlite:///"):
        database = Path(database_url.removeprefix("sqlite:///"))
        root = database.parent / ".platform-growth-artifacts"
    else:
        root = Path.cwd() / ".data" / "platform-growth-artifacts"
    return root / export_id / f"{generation}.bin"


def _key(config: AppConfig) -> bytes:
    secret = config.tenant_data_encryption_secret
    if not secret or len(secret.encode("utf-8")) < 32:
        raise ApiError("platform.growth_export_unavailable")
    return hashlib.sha256(b"prd141/growth-export/aesgcm/v1\0" + secret.encode("utf-8")).digest()


def _aad(export_id: str, generation: int, digest: str) -> bytes:
    return f"prd141/growth-export/v1\0{export_id}\0{generation}\0{digest}".encode("ascii")


__all__ = ["destroy_artifact", "load_artifact", "store_artifact"]
