"""Bound, short-lived commands for a deployment-owned restore service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backups import BackupRuntimeError
from .restore_execution import (
    RestoreRequest,
    RestoreResult,
    restore_request_digest,
    validate_restore_result,
)

_HASH = r"^[0-9a-f]{64}$"
_PERMIT_DOMAIN = b"metahub-restore-permit/v1\0"
_MAX_PERMIT_BYTES = 8192


class RestoreDispatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class RestoreDispatch(RestoreDispatchModel):
    request: RestoreRequest
    target_code: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    environment_class: Literal["dev", "staging", "production"]
    deadline_at: datetime

    @model_validator(mode="after")
    def aware_deadline(self) -> Self:
        if self.deadline_at.utcoffset() is None:
            raise ValueError("restore.dispatch_invalid")
        return self


class RestorePermit(RestoreDispatchModel):
    dispatch: RestoreDispatch
    action: Literal["submit", "status", "cancel"]
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def bounded_permit(self) -> Self:
        if (
            self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
            or not timedelta(0) < self.expires_at - self.issued_at <= timedelta(seconds=45)
            or self.dispatch.deadline_at - self.issued_at > timedelta(hours=2)
        ):
            raise ValueError("restore.permit_invalid")
        return self


class RestoreRunnerCommand(RestoreDispatchModel):
    permit: str = Field(min_length=80, max_length=_MAX_PERMIT_BYTES, repr=False)


class RestoreObservation(RestoreDispatchModel):
    execution_id: str = Field(pattern=r"^rex_[0-9a-f]{32}$")
    dispatch_sha256: str = Field(pattern=_HASH)
    request_sha256: str = Field(pattern=_HASH)
    result: RestoreResult | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if self.result is not None and (
            self.result.execution_id != self.execution_id
            or self.result.request_sha256 != self.request_sha256
        ):
            raise ValueError("restore.result_invalid")
        return self

    def require_binding(self, dispatch: RestoreDispatch) -> None:
        if (
            self.execution_id != dispatch.request.execution_id
            or self.dispatch_sha256 != dispatch_digest(dispatch)
            or self.request_sha256 != restore_request_digest(dispatch.request)
        ):
            raise ValueError("restore.result_invalid")
        if self.result is not None:
            try:
                validate_restore_result(dispatch.request, self.result)
            except BackupRuntimeError:
                raise ValueError("restore.result_invalid") from None


def dispatch_digest(dispatch: RestoreDispatch) -> str:
    return hashlib.sha256(_canonical(dispatch)).hexdigest()


def _canonical(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


def _secret(secret: str) -> bytes:
    try:
        encoded = secret.encode("ascii")
        if not 32 <= len(encoded) <= 512 or any(byte < 33 or byte > 126 for byte in encoded):
            raise ValueError
        return encoded
    except (AttributeError, ValueError, UnicodeError):
        raise ValueError("restore.credentials_invalid") from None


def encode_restore_permit(permit: RestorePermit, secret: str) -> str:
    payload = base64.urlsafe_b64encode(_canonical(permit)).rstrip(b"=")
    signature = hmac.new(_secret(secret), _PERMIT_DOMAIN + payload, hashlib.sha256).hexdigest()
    encoded = payload.decode("ascii") + "." + signature
    if len(encoded) > _MAX_PERMIT_BYTES:
        raise ValueError("restore.permit_invalid")
    return encoded


def decode_restore_permit(
    value: str, secret: str, *, action: str, now: datetime | None = None
) -> RestorePermit:
    key = _secret(secret)
    try:
        if (
            not isinstance(value, str)
            or len(value) > _MAX_PERMIT_BYTES
            or re.fullmatch(r"[A-Za-z0-9_-]+\.[0-9a-f]{64}", value) is None
        ):
            raise ValueError
        payload, signature = value.split(".")
        expected = hmac.new(key, _PERMIT_DOMAIN + payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != payload:
            raise ValueError
        permit = RestorePermit.model_validate_json(decoded)
        observed = datetime.now(UTC) if now is None else now
        if (
            observed.utcoffset() is None
            or permit.action != action
            or permit.issued_at > observed + timedelta(seconds=5)
            or permit.expires_at <= observed
        ):
            raise ValueError
        return permit
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("restore.permit_invalid") from None


__all__ = [
    "RestoreDispatch", "RestorePermit", "RestoreRunnerCommand", "RestoreObservation",
    "dispatch_digest", "encode_restore_permit", "decode_restore_permit",
]
