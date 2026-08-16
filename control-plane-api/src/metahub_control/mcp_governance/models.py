"""Strict public DTOs for PRD-082 MCP governance.

Secret Endpoint, credential, arguments, result and schema fields are excluded from repr so
framework diagnostics cannot print them. Tenant, actor, lease and crypto owners never come from
public request models.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

ServerStatus = Literal["pending_sync", "active", "sync_failed", "disabled"]
ToolStatus = Literal["review_required", "active", "disabled"]
RiskLevel = Literal["standard", "high"]
OperationStatus = Literal["queued", "processing", "succeeded", "failed"]
InvocationStatus = Literal[
    "approval_pending",
    "queued",
    "probing",
    "calling",
    "succeeded",
    "failed",
    "blocked",
    "rejected",
    "outcome_unknown",
]
DlpStatus = Literal["clean", "log_only", "blocked", "unavailable"]
ExecutionStatus = Literal["not_called", "called", "unknown"]
CostStatus = Literal["not_applicable", "not_reported", "unknown"]

_RESOURCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,159}$")


class McpServerAuthRequest(BaseModel):
    """One optional bearer candidate; it is never returned by any response model."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["none", "bearer"]
    bearer_token: StrictStr | None = Field(default=None, min_length=1, max_length=8192, repr=False)

    @model_validator(mode="after")
    def validate_auth_pair(self) -> McpServerAuthRequest:
        if (self.type == "bearer") != (self.bearer_token is not None):
            raise ValueError("mcp.request_invalid")
        if self.bearer_token is not None and any(
            unicodedata.category(character) == "Cc" for character in self.bearer_token
        ):
            raise ValueError("mcp.request_invalid")
        return self


class RegisterMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr = Field(min_length=1, max_length=80)
    endpoint_url: StrictStr = Field(min_length=1, max_length=2048, repr=False)
    auth: McpServerAuthRequest


class UpdateMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)
    name: StrictStr | None = Field(default=None, min_length=1, max_length=80)
    status: Literal["active", "disabled"] | None = None
    auth: McpServerAuthRequest | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def require_mutation(self) -> UpdateMcpServerRequest:
        if not self.model_fields_set.intersection({"name", "status", "auth"}):
            raise ValueError("mcp.request_invalid")
        return self


class RefreshMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: StrictInt = Field(ge=1)


class UpdateMcpToolPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_policy_revision: StrictInt = Field(ge=1)
    status: ToolStatus | None = None
    risk_level: RiskLevel | None = None
    approval_required: StrictBool | None = None
    approval_expires_seconds: StrictInt | None = Field(default=None, ge=300, le=86400)
    required_permission_codes: list[StrictStr] | None = Field(default=None, max_length=8)
    allowed_role_ids: list[StrictStr] | None = Field(default=None, max_length=128)
    allowed_user_ids: list[StrictStr] | None = Field(default=None, max_length=128)
    result_redaction_pointers: list[StrictStr] | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_policy(self) -> UpdateMcpToolPolicyRequest:
        mutable = {
            "status",
            "risk_level",
            "approval_required",
            "approval_expires_seconds",
            "required_permission_codes",
            "allowed_role_ids",
            "allowed_user_ids",
            "result_redaction_pointers",
        }
        if not self.model_fields_set.intersection(mutable):
            raise ValueError("mcp.request_invalid")
        for values in (
            self.required_permission_codes,
            self.allowed_role_ids,
            self.allowed_user_ids,
            self.result_redaction_pointers,
        ):
            if values is not None and len(values) != len(set(values)):
                raise ValueError("mcp.request_invalid")
        for values in (self.allowed_role_ids, self.allowed_user_ids):
            if values is not None and any(
                _RESOURCE_ID.fullmatch(value) is None for value in values
            ):
                raise ValueError("mcp.request_invalid")
        return self


class InvokeMcpToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    arguments: dict[StrictStr, JsonValue] = Field(repr=False)


class McpServerInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mcp_server_id: str
    name: str
    endpoint_hint: str
    endpoint_has_path: bool
    transport: Literal["streamable_http"]
    status: ServerStatus
    state_revision: int = Field(ge=1)
    active_credential_revision: int | None = Field(default=None, ge=1)
    protocol_version: str | None
    tool_count: int = Field(ge=0)
    active_tool_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class McpServerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    servers: list[McpServerInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class McpServerOperationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mcp_server_operation_id: str
    mcp_server_id: str
    operation_type: Literal["register", "refresh", "rotate", "update", "disable", "enable"]
    status: OperationStatus
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None
    updated_at: datetime


class McpServerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    server: McpServerInfo
    operation: McpServerOperationInfo | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class McpServerOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: McpServerOperationInfo
    request_id: str
    trace_id: str


class McpToolInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mcp_tool_id: str
    mcp_server_id: str
    remote_name: str
    title: str | None
    schema_revision: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    input_schema_sha256: str
    output_schema_sha256: str | None
    risk_level: RiskLevel
    approval_required: bool
    approval_expires_seconds: int = Field(ge=300, le=86400)
    required_permission_codes: list[str]
    allowed_role_count: int = Field(ge=0)
    allowed_user_count: int = Field(ge=0)
    result_redaction_count: int = Field(ge=0)
    status: ToolStatus
    created_at: datetime
    updated_at: datetime


class McpToolDetailInfo(McpToolInfo):
    description: str | None = Field(default=None, repr=False)
    input_schema_original: dict[str, JsonValue] = Field(repr=False)
    input_schema_effective: dict[str, JsonValue] = Field(repr=False)
    output_schema_original: dict[str, JsonValue] | None = Field(default=None, repr=False)
    output_schema_effective: dict[str, JsonValue] | None = Field(default=None, repr=False)
    allowed_role_ids: list[str]
    allowed_user_ids: list[str]
    result_redaction_pointers: list[str]


class McpToolListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tools: list[McpToolInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class McpToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool: McpToolDetailInfo
    request_id: str
    trace_id: str


class McpDlpSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: DlpStatus | None
    match_count: int | None = Field(default=None, ge=0)
    rule_id: str | None


class McpInvocationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mcp_invocation_id: str
    mcp_server_id: str
    mcp_tool_id: str
    status: InvocationStatus
    risk_level: RiskLevel
    approval_required: bool
    approval_task_id: str | None
    input_dlp: McpDlpSummary
    output_dlp: McpDlpSummary
    result_redaction_count: int = Field(ge=0)
    probe_attempt_count: int = Field(ge=0)
    call_attempt_count: int = Field(ge=0)
    request_bytes: int = Field(ge=0)
    response_bytes: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    execution_status: ExecutionStatus
    invocation_count: Literal[1]
    tool_call_count: int | None = Field(default=None, ge=0, le=1)
    cost_status: CostStatus
    error_code: str | None
    created_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class McpInvocationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    invocation: McpInvocationInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class McpInvocationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    invocations: list[McpInvocationInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class McpInvocationResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mcp_invocation_id: str
    result: JsonValue = Field(repr=False)
    request_id: str
    trace_id: str


__all__ = [
    name
    for name in globals()
    if name.startswith(("Mcp", "Register", "Update", "Refresh", "Invoke"))
]
