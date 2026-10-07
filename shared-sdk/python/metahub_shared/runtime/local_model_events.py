"""Exact public contract for the durable ``local_model.called`` event."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Final

from .errors import ERROR_SPECS

LOCAL_MODEL_CALLED_EVENT_TYPE: Final[str] = "local_model.called"

_LOCAL_ENDPOINT_ID = re.compile(r"^lme_[0-9a-f]{32}$")
_ROUTING_ATTEMPT_ID = re.compile(r"^rta_[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$", re.ASCII)
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", re.ASCII)
_KNOWN_ERROR_CODES: Final[frozenset[str]] = frozenset(ERROR_SPECS)
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "local_endpoint_id",
        "provider_id",
        "model_id",
        "routing_attempt_id",
        "request_id",
        "attempt_sequence",
        "attempt_role",
        "execution_status",
        "outcome",
        "error_code",
        "latency_ms",
        "usage_status",
        "completed_at",
    }
)


def validate_local_model_called_payload(payload: object) -> dict[str, Any]:
    """Validate the exact safe summary for one local Provider call attempt."""

    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("webhook.payload_invalid")
    if (
        payload["schema_version"] != "1.0"
        or type(payload["schema_version"]) is not str
        or type(payload["local_endpoint_id"]) is not str
        or _LOCAL_ENDPOINT_ID.fullmatch(payload["local_endpoint_id"]) is None
        or type(payload["routing_attempt_id"]) is not str
        or _ROUTING_ATTEMPT_ID.fullmatch(payload["routing_attempt_id"]) is None
        or any(
            type(payload[field]) is not str or _SAFE_ID.fullmatch(payload[field]) is None
            for field in ("provider_id", "model_id", "request_id")
        )
        or type(payload["attempt_sequence"]) is not int
        or not 1 <= payload["attempt_sequence"] <= 32
        or payload["attempt_role"] not in {"primary", "fallback"}
        or (payload["attempt_sequence"] == 1) != (payload["attempt_role"] == "primary")
        or payload["execution_status"] != "called"
        or payload["outcome"] not in {"succeeded", "failed", "outcome_unknown"}
        or type(payload["latency_ms"]) is not int
        or not 0 <= payload["latency_ms"] <= 86_400_000
        or payload["usage_status"] not in {"pending", "recorded", "unavailable", "not_applicable"}
        or type(payload["completed_at"]) is not str
        or _UTC_SECOND.fullmatch(payload["completed_at"]) is None
    ):
        raise ValueError("webhook.payload_invalid")
    _validate_outcome(payload)
    try:
        datetime.strptime(payload["completed_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError("webhook.payload_invalid") from None
    return dict(payload)


def validate_local_model_called_event(
    payload: object,
    *,
    event_id: object,
    tenant_id: object,
    actor_id: object,
    object_id: object,
    trace_id: object,
    source_request_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Bind the payload to its persisted source owner and original request chain."""

    safe = validate_local_model_called_payload(payload)
    if (
        type(event_id) is not str
        or not event_id.startswith("evt_")
        or _SAFE_ID.fullmatch(event_id) is None
        or any(
            type(value) is not str or _SAFE_ID.fullmatch(value) is None
            for value in (tenant_id, trace_id, source_request_id)
        )
        or actor_id is not None
        or object_id != safe["local_endpoint_id"]
        or source_request_id != safe["request_id"]
        or type(occurred_at) is not datetime
        or occurred_at.utcoffset() is None
        or _to_iso(occurred_at) != safe["completed_at"]
    ):
        raise ValueError("webhook.payload_invalid")
    return safe


def _validate_outcome(payload: dict[str, Any]) -> None:
    outcome = payload["outcome"]
    error_code = payload["error_code"]
    usage_status = payload["usage_status"]
    if outcome == "succeeded":
        if error_code is not None or usage_status not in {"pending", "recorded"}:
            raise ValueError("webhook.payload_invalid")
        return
    if (
        type(error_code) is not str
        or _ERROR_CODE.fullmatch(error_code) is None
        or error_code not in _KNOWN_ERROR_CODES
    ):
        raise ValueError("webhook.payload_invalid")
    if outcome == "outcome_unknown":
        if error_code != "gateway.local_endpoint_outcome_unknown" or usage_status != "unavailable":
            raise ValueError("webhook.payload_invalid")
    elif usage_status not in {"unavailable", "not_applicable"}:
        raise ValueError("webhook.payload_invalid")


def _to_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "LOCAL_MODEL_CALLED_EVENT_TYPE",
    "validate_local_model_called_event",
    "validate_local_model_called_payload",
]
