"""PRD-081 standardized SIEM security-event contracts and CEF formatting.

This module contains no database or HTTP behavior. It accepts only the exact safe event
projection, validates all owner-facing fields, and returns deterministic UTF-8 wire bytes.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

SECURITY_EVENT_TYPE = "security.event_pushed"
SECURITY_EVENT_ACTOR = "security-event-aggregator"
SECURITY_EVENT_WINDOW_SECONDS = 5
SECURITY_EVENT_MAX_SOURCE_COUNT = 1_000
SECURITY_EVENT_MAX_OCCURRENCE_COUNT = 1_000_000
SECURITY_EVENT_MAX_CEF_BYTES = 16_384
WEBHOOK_PAYLOAD_FORMATS = frozenset({"json", "cef"})

SECURITY_SOURCE_EVENT_TYPES = frozenset(
    {
        "security.dlp_matched",
        "security.session_high_risk",
        "security.provider_key_anomaly",
    }
)

_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "security_event_id",
        "status",
        "category",
        "source_event_type",
        "severity",
        "severity_score",
        "action",
        "outcome",
        "resource_type",
        "resource_id",
        "event_count",
        "source_event_count",
        "first_seen_at",
        "last_seen_at",
        "window_started_at",
        "window_ended_at",
        "first_request_id",
        "last_request_id",
        "created_at",
        "updated_at",
    }
)
_ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SECURITY_EVENT_ID = re.compile(r"^sevt_[0-9a-f]{32}$")
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_SOURCE_CONTRACTS: dict[str, dict[str, object]] = {
    "security.dlp_matched": {
        "category": "data_loss_prevention",
        "resource_type": "dlp_rule",
        "actions": {
            "allow": ("low", 3, "observed"),
            "log_only": ("medium", 5, "observed"),
            "block": ("high", 8, "blocked"),
        },
        "name": "DLP match",
    },
    "security.session_high_risk": {
        "category": "session_risk",
        "resource_type": "session_risk_score",
        "actions": {
            "high_risk": ("high", 8, "detected"),
            "freeze_session": ("critical", 10, "frozen"),
            "freeze_virtual_key": ("critical", 10, "frozen"),
            "dismiss": ("medium", 4, "resolved"),
            "release_session": ("medium", 4, "released"),
            "release_virtual_key": ("medium", 4, "released"),
            "expire": ("medium", 4, "expired"),
        },
        "name": "High-risk session",
    },
    "security.provider_key_anomaly": {
        "category": "provider_credential",
        "resource_type": "provider",
        "actions": {
            "authentication_failed": ("critical", 9, "rejected"),
            "decrypt_failed": ("critical", 9, "rejected"),
        },
        "name": "Provider credential anomaly",
    },
}


def normalize_webhook_payload_format(value: object) -> str:
    """Return the canonical webhook payload format or fail closed."""

    normalized = str(value or "").strip().lower()
    if normalized not in WEBHOOK_PAYLOAD_FORMATS:
        raise ValueError("webhook.format_invalid")
    return normalized


def validate_security_event_payload(payload: object) -> dict[str, Any]:
    """Validate and copy the exact public ``SecurityEventPayload`` object."""

    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise ValueError("webhook.payload_invalid")
    copied = dict(payload)
    if type(copied["schema_version"]) is not int or copied["schema_version"] != 1:
        raise ValueError("webhook.payload_invalid")
    event_id = _matched(copied["security_event_id"], _SECURITY_EVENT_ID)
    if copied["status"] != "aggregated":
        raise ValueError("webhook.payload_invalid")
    source_event_type = _required_text(copied["source_event_type"], maximum=120)
    contract = _SOURCE_CONTRACTS.get(source_event_type)
    if contract is None:
        raise ValueError("webhook.payload_invalid")
    action = _required_text(copied["action"], maximum=64)
    actions = contract["actions"]
    if not isinstance(actions, dict) or action not in actions:
        raise ValueError("webhook.payload_invalid")
    expected_severity, expected_score, expected_outcome = actions[action]
    if (
        copied["category"] != contract["category"]
        or copied["resource_type"] != contract["resource_type"]
        or copied["severity"] != expected_severity
        or type(copied["severity_score"]) is not int
        or copied["severity_score"] != expected_score
        or copied["outcome"] != expected_outcome
    ):
        raise ValueError("webhook.payload_invalid")
    resource_id = _safe_id(copied["resource_id"])
    event_count = _bounded_int(
        copied["event_count"],
        minimum=1,
        maximum=SECURITY_EVENT_MAX_OCCURRENCE_COUNT,
    )
    source_event_count = _bounded_int(
        copied["source_event_count"],
        minimum=1,
        maximum=SECURITY_EVENT_MAX_SOURCE_COUNT,
    )
    if source_event_count > event_count:
        raise ValueError("webhook.payload_invalid")
    window_started_at = _canonical_time(copied["window_started_at"])
    window_ended_at = _canonical_time(copied["window_ended_at"])
    if window_ended_at - window_started_at != timedelta(seconds=SECURITY_EVENT_WINDOW_SECONDS):
        raise ValueError("webhook.payload_invalid")
    first_seen_at = _canonical_time(copied["first_seen_at"])
    last_seen_at = _canonical_time(copied["last_seen_at"])
    if not (window_started_at <= first_seen_at <= last_seen_at < window_ended_at):
        raise ValueError("webhook.payload_invalid")
    created_at = _canonical_time(copied["created_at"])
    updated_at = _canonical_time(copied["updated_at"])
    if updated_at < created_at:
        raise ValueError("webhook.payload_invalid")
    first_request_id = _request_id(copied["first_request_id"])
    last_request_id = _request_id(copied["last_request_id"])
    copied.update(
        {
            "security_event_id": event_id,
            "source_event_type": source_event_type,
            "action": action,
            "resource_id": resource_id,
            "event_count": event_count,
            "source_event_count": source_event_count,
            "first_request_id": first_request_id,
            "last_request_id": last_request_id,
        }
    )
    return copied


def validate_security_event_envelope(
    envelope: object,
    *,
    source_request_id: object | None = None,
) -> dict[str, Any]:
    """Validate the exact public envelope and bind it to its aggregate payload."""

    if type(envelope) is not dict or set(envelope) != _ENVELOPE_FIELDS:
        raise ValueError("webhook.payload_invalid")
    copied = dict(envelope)
    payload = validate_security_event_payload(copied["payload"])
    event_id = _matched(copied["event_id"], _SECURITY_EVENT_ID)
    if (
        copied["event_type"] != SECURITY_EVENT_TYPE
        or copied["actor_id"] != SECURITY_EVENT_ACTOR
        or copied["object_id"] != event_id
        or payload["security_event_id"] != event_id
        or copied["timestamp"] != payload["window_ended_at"]
    ):
        raise ValueError("webhook.payload_invalid")
    tenant_id = _safe_id(copied["tenant_id"])
    trace_id = _request_id(copied["trace_id"])
    if (
        source_request_id is not None
        and _request_id(source_request_id) != payload["last_request_id"]
    ):
        raise ValueError("webhook.payload_invalid")
    copied.update(
        {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "payload": payload,
        }
    )
    return copied


def security_event_cef_bytes(envelope: object) -> bytes:
    """Return one deterministic CEF line for a standardized security event."""

    safe = validate_security_event_envelope(envelope)
    payload = safe["payload"]
    source_event_type = str(payload["source_event_type"])
    contract = _SOURCE_CONTRACTS[source_event_type]
    extensions = (
        ("externalId", payload["security_event_id"]),
        ("rt", _epoch_milliseconds(payload["last_seen_at"])),
        ("start", _epoch_milliseconds(payload["first_seen_at"])),
        ("end", _epoch_milliseconds(payload["last_seen_at"])),
        ("cnt", payload["event_count"]),
        ("cat", payload["category"]),
        ("act", payload["action"]),
        ("outcome", payload["outcome"]),
        ("cs1Label", "TenantID"),
        ("cs1", safe["tenant_id"]),
        ("cs2Label", "ResourceType"),
        ("cs2", payload["resource_type"]),
        ("cs3Label", "ResourceID"),
        ("cs3", payload["resource_id"]),
        ("cs4Label", "TraceID"),
        ("cs4", safe["trace_id"]),
        ("cs5Label", "FirstRequestID"),
        ("cs5", payload["first_request_id"]),
        ("cs6Label", "LastRequestID"),
        ("cs6", payload["last_request_id"]),
        ("cn1Label", "SourceEventCount"),
        ("cn1", payload["source_event_count"]),
    )
    line = "|".join(
        (
            "CEF:0",
            "MetaHub",
            "AI Gateway",
            "1",
            _cef_header(source_event_type),
            _cef_header(str(contract["name"])),
            str(payload["severity_score"]),
            " ".join(f"{key}={_cef_extension(str(value))}" for key, value in extensions),
        )
    )
    return _bounded_cef(line)


def webhook_test_cef_bytes(envelope: object) -> bytes:
    """Return a non-production CEF test line for a CEF endpoint."""

    if type(envelope) is not dict or set(envelope) != _ENVELOPE_FIELDS:
        raise ValueError("webhook.payload_invalid")
    event_id = _safe_id(envelope["event_id"])
    tenant_id = _safe_id(envelope["tenant_id"])
    trace_id = _request_id(envelope["trace_id"])
    if envelope["event_type"] != "webhook.test":
        raise ValueError("webhook.format_invalid")
    extensions = (
        ("externalId", event_id),
        ("cat", "metahub_test"),
        ("cs1Label", "TenantID"),
        ("cs1", tenant_id),
        ("cs4Label", "TraceID"),
        ("cs4", trace_id),
    )
    line = "|".join(
        (
            "CEF:0",
            "MetaHub",
            "AI Gateway",
            "1",
            "webhook.test",
            "Webhook test",
            "1",
            " ".join(f"{key}={_cef_extension(str(value))}" for key, value in extensions),
        )
    )
    return _bounded_cef(line)


def _bounded_cef(line: str) -> bytes:
    if "\r" in line or "\n" in line:
        raise ValueError("webhook.format_invalid")
    body = line.encode("utf-8")
    if not body or len(body) > SECURITY_EVENT_MAX_CEF_BYTES:
        raise ValueError("webhook.format_invalid")
    return body


def _cef_header(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _cef_extension(value: str) -> str:
    return value.replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def _epoch_milliseconds(value: object) -> int:
    parsed = _canonical_time(value)
    epoch = int(parsed.timestamp())
    if epoch < 0:
        raise ValueError("webhook.payload_invalid")
    return epoch * 1_000


def _canonical_time(value: object) -> datetime:
    if type(value) is not str or _UTC_SECOND.fullmatch(value) is None:
        raise ValueError("webhook.payload_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError("webhook.payload_invalid") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("webhook.payload_invalid")
    return parsed


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError("webhook.payload_invalid")
    return value


def _safe_id(value: object) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError("webhook.payload_invalid")
    return value


def _request_id(value: object) -> str:
    if type(value) is not str or _SAFE_REQUEST_ID.fullmatch(value) is None:
        raise ValueError("webhook.payload_invalid")
    return value


def _required_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("webhook.payload_invalid")
    return value


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("webhook.payload_invalid")
    return value


__all__ = [
    "SECURITY_EVENT_ACTOR",
    "SECURITY_EVENT_MAX_CEF_BYTES",
    "SECURITY_EVENT_MAX_OCCURRENCE_COUNT",
    "SECURITY_EVENT_MAX_SOURCE_COUNT",
    "SECURITY_EVENT_TYPE",
    "SECURITY_EVENT_WINDOW_SECONDS",
    "SECURITY_SOURCE_EVENT_TYPES",
    "WEBHOOK_PAYLOAD_FORMATS",
    "normalize_webhook_payload_format",
    "security_event_cef_bytes",
    "validate_security_event_envelope",
    "validate_security_event_payload",
    "webhook_test_cef_bytes",
]
