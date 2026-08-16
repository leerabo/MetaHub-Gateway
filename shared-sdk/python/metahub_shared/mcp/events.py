"""Exact ``mcp.tool_called`` payload and persisted-source envelope validator."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Final

from .contracts import McpContractError

MCP_TOOL_CALLED_EVENT_TYPE: Final[str] = "mcp.tool_called"

_EVENT_ID = re.compile(r"^mcpevt_[0-9a-f]{32}$")
_INVOCATION_ID = re.compile(r"^mcpi_[0-9a-f]{32}$")
_SERVER_ID = re.compile(r"^mcps_[0-9a-f]{32}$")
_TOOL_ID = re.compile(r"^mcpt_[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$", re.ASCII)
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_PAYLOAD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "mcp_invocation_id",
        "mcp_server_id",
        "mcp_tool_id",
        "status",
        "risk_level",
        "approval_required",
        "network_attempted",
        "input_dlp_status",
        "input_dlp_match_count",
        "output_dlp_status",
        "output_dlp_match_count",
        "redaction_count",
        "probe_attempt_count",
        "call_attempt_count",
        "duration_ms",
        "request_bytes",
        "response_bytes",
        "execution_status",
        "invocation_count",
        "tool_call_count",
        "cost_status",
        "error_code",
        "completed_at",
    }
)
_KNOWN_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "mcp.tool_failed",
        "mcp.result_invalid",
        "mcp.output_dlp_unavailable",
        "mcp.result_blocked",
        "mcp.unavailable",
    }
)


def validate_mcp_tool_called_payload(payload: object) -> dict[str, Any]:
    """Validate the exact 24-field public safety summary for one durable call intent."""

    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise McpContractError("mcp.unavailable")
    if (
        payload["schema_version"] != 1
        or type(payload["schema_version"]) is not int
        or type(payload["mcp_invocation_id"]) is not str
        or _INVOCATION_ID.fullmatch(payload["mcp_invocation_id"]) is None
        or type(payload["mcp_server_id"]) is not str
        or _SERVER_ID.fullmatch(payload["mcp_server_id"]) is None
        or type(payload["mcp_tool_id"]) is not str
        or _TOOL_ID.fullmatch(payload["mcp_tool_id"]) is None
        or payload["status"] not in {"succeeded", "failed", "blocked", "outcome_unknown"}
        or payload["risk_level"] not in {"standard", "high"}
        or type(payload["approval_required"]) is not bool
        or type(payload["network_attempted"]) is not bool
        or payload["network_attempted"] is not True
        or payload["input_dlp_status"] not in {"clean", "log_only"}
        or payload["output_dlp_status"]
        not in {"clean", "log_only", "blocked", "unavailable", "not_scanned"}
        or payload["execution_status"] not in {"called", "unknown"}
        or payload["cost_status"] not in {"not_reported", "unknown"}
        or type(payload["completed_at"]) is not str
        or _UTC_SECOND.fullmatch(payload["completed_at"]) is None
    ):
        raise McpContractError("mcp.unavailable")
    if payload["risk_level"] == "high" and payload["approval_required"] is not True:
        raise McpContractError("mcp.unavailable")
    _bounded_int(payload["input_dlp_match_count"], maximum=1_000_000)
    _bounded_int(payload["output_dlp_match_count"], maximum=1_000_000)
    _bounded_int(payload["redaction_count"], maximum=1_000_000)
    _bounded_int(payload["probe_attempt_count"], minimum=1, maximum=4)
    _bounded_int(payload["call_attempt_count"], minimum=1, maximum=1)
    _bounded_int(payload["duration_ms"], maximum=86_400_000)
    _bounded_int(payload["request_bytes"], maximum=1_048_576)
    _bounded_int(payload["response_bytes"], maximum=1_048_576)
    if type(payload["invocation_count"]) is not int or payload["invocation_count"] != 1:
        raise McpContractError("mcp.unavailable")
    if payload["input_dlp_status"] == "clean" and payload["input_dlp_match_count"] != 0:
        raise McpContractError("mcp.unavailable")
    if payload["input_dlp_status"] == "log_only" and payload["input_dlp_match_count"] < 1:
        raise McpContractError("mcp.unavailable")
    if payload["output_dlp_status"] in {"clean", "unavailable", "not_scanned"}:
        if payload["output_dlp_match_count"] != 0:
            raise McpContractError("mcp.unavailable")
    elif payload["output_dlp_match_count"] < 1:
        raise McpContractError("mcp.unavailable")
    _validate_outcome(payload)
    try:
        completed = datetime.strptime(payload["completed_at"], "%Y-%m-%dT%H:%M:%SZ")
        completed.replace(tzinfo=UTC)
    except ValueError:
        raise McpContractError("mcp.unavailable") from None
    return dict(payload)


def validate_mcp_tool_called_event(
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
    """Bind an exact payload to the persisted event, tenant, actor, trace, and completion second."""

    safe = validate_mcp_tool_called_payload(payload)
    if (
        type(event_id) is not str
        or _EVENT_ID.fullmatch(event_id) is None
        or any(
            type(value) is not str or _SAFE_ID.fullmatch(value) is None
            for value in (tenant_id, actor_id, trace_id, source_request_id)
        )
        or object_id != safe["mcp_invocation_id"]
        or type(occurred_at) is not datetime
        or occurred_at.utcoffset() is None
        or _to_iso(occurred_at) != safe["completed_at"]
    ):
        raise McpContractError("mcp.unavailable")
    return safe


def _validate_outcome(payload: dict[str, Any]) -> None:
    execution = payload["execution_status"]
    status = payload["status"]
    error_code = payload["error_code"]
    if execution == "unknown":
        if (
            status != "outcome_unknown"
            or payload["tool_call_count"] is not None
            or payload["cost_status"] != "unknown"
            or payload["output_dlp_status"] != "not_scanned"
            or payload["redaction_count"] != 0
            or error_code != "mcp.outcome_unknown"
        ):
            raise McpContractError("mcp.unavailable")
        return
    if (
        status == "outcome_unknown"
        or type(payload["tool_call_count"]) is not int
        or payload["tool_call_count"] != 1
        or payload["cost_status"] != "not_reported"
    ):
        raise McpContractError("mcp.unavailable")
    if status == "succeeded":
        if error_code is not None or payload["output_dlp_status"] not in {"clean", "log_only"}:
            raise McpContractError("mcp.unavailable")
    elif status == "blocked":
        if error_code != "mcp.result_blocked" or payload["output_dlp_status"] != "blocked":
            raise McpContractError("mcp.unavailable")
    elif (
        type(error_code) is not str
        or error_code not in _KNOWN_FAILURE_CODES - {"mcp.result_blocked"}
    ):
        raise McpContractError("mcp.unavailable")
    if error_code == "mcp.output_dlp_unavailable" and payload["output_dlp_status"] != "unavailable":
        raise McpContractError("mcp.unavailable")


def _bounded_int(value: object, *, minimum: int = 0, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise McpContractError("mcp.unavailable")


def _to_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
