"""Strict public DTOs for PRD-132 customer support."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TicketCategory = Literal[
    "account", "billing", "integration", "migration", "reliability", "security"
]
TicketSeverity = Literal["p1", "p2", "p3", "p4"]
TicketStatus = Literal[
    "open", "triaged", "waiting_customer", "waiting_platform", "resolved", "closed"
]


class SupportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TicketCreateRequest(SupportModel):
    category: TicketCategory
    severity: TicketSeverity
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    related_kind: Literal["none", "request", "incident", "edge_deployment", "migration"] = "none"
    related_reference: str | None = Field(default=None, min_length=3, max_length=256)

    @model_validator(mode="after")
    def related_pair(self) -> TicketCreateRequest:
        if (self.related_kind == "none") != (self.related_reference is None):
            raise ValueError("related reference pair is invalid")
        return self


class TicketPatchRequest(SupportModel):
    status: TicketStatus | None = None
    message: str | None = Field(default=None, min_length=1, max_length=8000)
    message_visibility: Literal["customer", "internal"] | None = None
    assignee_operator_id: str | None = Field(default=None, pattern=r"^pto_[0-9a-f]{32}$")
    sla_action: Literal["pause_policy", "resume"] | None = None
    survey_rating: int | None = Field(default=None, ge=1, le=5)
    survey_comment: str | None = Field(default=None, min_length=1, max_length=2000)
    reason_code: str | None = Field(
        default=None, min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$"
    )

    @model_validator(mode="after")
    def useful_patch(self) -> TicketPatchRequest:
        if not any(
            value is not None
            for value in (
                self.status,
                self.message,
                self.assignee_operator_id,
                self.sla_action,
                self.survey_rating,
            )
        ):
            raise ValueError("empty patch")
        if self.message is None and self.message_visibility is not None:
            raise ValueError("message visibility requires message")
        if self.message is not None and self.message_visibility is None:
            self.message_visibility = "customer"
        if self.survey_comment is not None and self.survey_rating is None:
            raise ValueError("survey comment requires rating")
        if self.sla_action is not None and self.reason_code is None:
            raise ValueError("SLA action requires reason")
        return self


class AttachmentCreateRequest(SupportModel):
    file_id: str = Field(pattern=r"^file_[0-9a-f]{32}$")
    intent_code: Literal["diagnostic_evidence", "log_bundle", "reproduction"]
    confirmed: Literal[True]


class EscalationCreateRequest(SupportModel):
    reason_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    confirmed: Literal[True]


class TicketMessageSummary(SupportModel):
    message_id: str = Field(pattern=r"^msg_[0-9a-f]{32}$")
    author_domain: Literal["customer", "platform"]
    visibility: Literal["customer", "internal"]
    body: str
    created_at: datetime


class TicketAttachmentSummary(SupportModel):
    attachment_id: str = Field(pattern=r"^att_[0-9a-f]{32}$")
    intent_code: Literal["diagnostic_evidence", "log_bundle", "reproduction"]
    media_type: Literal[
        "text/plain",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]
    size_bytes: int = Field(ge=1, le=52_428_800)
    status: Literal["quarantined", "available", "rejected", "scan_timeout"]
    scan_version: Literal["ticket-attachment-scan-v1"]
    scan_outcome: (
        Literal[
            "clean", "malicious", "structure_invalid", "owner_changed", "timeout", "unavailable"
        ]
        | None
    )
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class SlaClockSummary(SupportModel):
    state: Literal["running", "paused_customer", "paused_policy", "stopped"]
    generation: int = Field(ge=1)
    first_response_deadline_at: datetime
    resolution_deadline_at: datetime
    risk_at: datetime
    first_responded_at: datetime | None
    risk_generation: int = Field(ge=0)
    escalation_generation: int = Field(ge=0)
    revision: int = Field(ge=1)


class EscalationSummary(SupportModel):
    escalation_id: str = Field(pattern=r"^esc_[0-9a-f]{32}$")
    source_code: Literal["customer_requested", "sla_at_risk", "sla_breached", "platform_manual"]
    reason_code: str
    generation: int = Field(ge=1)
    status: Literal["open", "acknowledged", "closed"]
    created_at: datetime


class SatisfactionSurveySummary(SupportModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None
    created_at: datetime


class SupportTicketSummary(SupportModel):
    ticket_id: str = Field(pattern=r"^tkt_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    category: TicketCategory
    severity: TicketSeverity
    status: TicketStatus
    subject: str
    reported_by_me: bool
    assignee_operator_id: str | None = Field(default=None, pattern=r"^pto_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    last_customer_activity_at: datetime
    last_platform_activity_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    messages: tuple[TicketMessageSummary, ...]
    attachments: tuple[TicketAttachmentSummary, ...]
    sla: SlaClockSummary
    escalations: tuple[EscalationSummary, ...]
    survey: SatisfactionSurveySummary | None


class TicketListResponse(SupportModel):
    actor_domain: Literal["workspace", "platform"]
    items: tuple[SupportTicketSummary, ...]
    request_id: str
    trace_id: str


class TicketMutationResponse(SupportModel):
    actor_domain: Literal["workspace", "platform"]
    ticket: SupportTicketSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class AttachmentMutationResponse(SupportModel):
    attachment: TicketAttachmentSummary
    ticket_revision: int = Field(ge=1)
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EscalationMutationResponse(SupportModel):
    escalation: EscalationSummary
    ticket_revision: int = Field(ge=1)
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


__all__ = [
    "AttachmentCreateRequest",
    "AttachmentMutationResponse",
    "EscalationCreateRequest",
    "EscalationMutationResponse",
    "TicketCreateRequest",
    "TicketListResponse",
    "TicketMutationResponse",
    "TicketPatchRequest",
]
