"""Strict PRD-139 channel, attribution, statement, and partner DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not 20 <= len(value) <= 40:
        raise ValueError("timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp offset required")
    return parsed


Timestamp = Annotated[datetime, BeforeValidator(_timestamp)]
PartnerTier = Literal["registered", "silver", "gold", "strategic"]
PartnerRegion = Literal["global", "cn", "apac", "emea", "americas"]
PartnerRole = Literal["partner_owner", "partner_operations", "partner_finance"]
PartnerStatus = Literal["active", "suspended", "retired"]


class ChannelModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PartnerCommand(ChannelModel):
    operation: Literal[
        "create", "update", "upsert_membership", "revoke_membership", "rotate_referral_code"
    ]
    channel_partner_id: str | None = Field(default=None, pattern=r"^par_[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)
    owner_workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    owner_account_id: str | None = Field(default=None, pattern=r"^gac_[0-9a-f]{32}$")
    partner_code: str | None = Field(
        default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$"
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    tier_code: PartnerTier | None = None
    region_scope: PartnerRegion | None = None
    contract_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: PartnerStatus | None = None
    account_id: str | None = Field(default=None, pattern=r"^gac_[0-9a-f]{32}$")
    role_code: PartnerRole | None = None
    channel_membership_id: str | None = Field(default=None, pattern=r"^chm_[0-9a-f]{32}$")
    referral_code: str | None = Field(
        default=None, min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    expires_at: Timestamp | None = None
    usage_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> PartnerCommand:
        create = (
            self.owner_workspace_id,
            self.owner_account_id,
            self.partner_code,
            self.display_name,
            self.tier_code,
            self.region_scope,
            self.contract_sha256,
        )
        if self.operation == "create":
            if self.channel_partner_id is not None or self.expected_revision is not None:
                raise ValueError("create identifiers forbidden")
            if any(value is None for value in create):
                raise ValueError("create fields required")
        else:
            if self.channel_partner_id is None or self.expected_revision is None:
                raise ValueError("partner and revision required")
            if any(value is not None for value in (self.owner_workspace_id, self.owner_account_id)):
                raise ValueError("owner fields forbidden")
        catalog = (
            self.partner_code,
            self.display_name,
            self.tier_code,
            self.region_scope,
            self.status,
        )
        if self.operation == "update":
            if not any(value is not None for value in catalog[1:]):
                raise ValueError("catalog update required")
            if self.partner_code is not None or self.contract_sha256 is not None:
                raise ValueError("immutable catalog fields")
        elif self.operation != "create" and any(value is not None for value in catalog):
            raise ValueError("catalog fields forbidden")
        if self.operation == "upsert_membership":
            if self.account_id is None or self.role_code is None:
                raise ValueError("membership fields required")
        elif self.account_id is not None or self.role_code is not None:
            raise ValueError("membership fields forbidden")
        if self.operation == "revoke_membership":
            if self.channel_membership_id is None:
                raise ValueError("membership identifier required")
        elif self.channel_membership_id is not None:
            raise ValueError("membership identifier forbidden")
        if self.operation == "rotate_referral_code":
            if self.referral_code is None:
                raise ValueError("referral code required")
        elif any(
            value is not None
            for value in (self.referral_code, self.expires_at, self.usage_limit)
        ):
            raise ValueError("referral fields forbidden")
        return self


class LeadCommand(ChannelModel):
    channel_partner_id: str = Field(pattern=r"^par_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    lead_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]{0,79}$")
    referral_code: str | None = Field(
        default=None, min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    region_code: str = Field(min_length=2, max_length=16, pattern=r"^[a-z0-9-]+$")
    contact_email: str | None = Field(default=None, min_length=3, max_length=320)
    contact_phone: str | None = Field(default=None, min_length=8, max_length=20)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    confirmed: Literal[True]

    @model_validator(mode="after")
    def contact_required(self) -> LeadCommand:
        if self.contact_email is None and self.contact_phone is None:
            raise ValueError("contact required")
        return self


class AttributionRequestCommand(ChannelModel):
    channel_partner_id: str = Field(pattern=r"^par_[0-9a-f]{32}$")
    target_workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    channel_lead_id: str | None = Field(default=None, pattern=r"^cld_[0-9a-f]{32}$")
    channel_commission_rule_snapshot_id: str = Field(pattern=r"^ccr_[0-9a-f]{32}$")
    reason_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    expected_attribution_revision: int = Field(ge=0)
    starts_at: Timestamp
    confirmed: Literal[True]


class AttributionApprovalCommand(ChannelModel):
    expected_revision: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    reason_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_.-]+$")
    confirmed: Literal[True]


class MembershipSummary(ChannelModel):
    channel_membership_id: str
    account_id: str
    role_code: PartnerRole
    status: Literal["active", "revoked"]
    revision: int


class ReferralSummary(ChannelModel):
    channel_referral_code_id: str
    masked_suffix: str
    status: Literal["active", "revoked", "expired"]
    usage_limit: int | None
    usage_count: int
    expires_at: datetime | None
    revision: int


class CommissionRuleSummary(ChannelModel):
    channel_commission_rule_snapshot_id: str
    currency: Literal["CNY", "USD"]
    commission_bps: int
    version: int
    effective_from: datetime
    effective_until: datetime | None


class PendingAttributionSummary(ChannelModel):
    workspace_attribution_id: str
    attribution_change_id: str
    target_workspace_id: str
    starts_at: datetime
    required_approvals: Literal[1, 2]
    approval_count: int
    approval_generation: int
    revision: int


class ChannelPartnerSummary(ChannelModel):
    channel_partner_id: str
    owner_workspace_id: str
    partner_code: str
    display_name: str
    tier_code: PartnerTier
    region_scope: PartnerRegion
    contract_sha256: str
    status: PartnerStatus
    revision: int
    memberships: tuple[MembershipSummary, ...]
    referral: ReferralSummary | None
    commission_rules: tuple[CommissionRuleSummary, ...]
    pending_attributions: tuple[PendingAttributionSummary, ...]
    created_at: datetime
    updated_at: datetime


class ChannelPartnerResponse(ChannelModel):
    partner: ChannelPartnerSummary
    issued_referral_code: str | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ChannelPartnerListResponse(ChannelModel):
    items: tuple[ChannelPartnerSummary, ...]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class LeadSummary(ChannelModel):
    channel_lead_id: str
    channel_partner_id: str
    lead_code: str
    region_code: str
    stage: Literal["legacy_unresolved", "registered", "qualified", "converted", "closed"]
    source_sha256: str
    candidate_workspace_id: str | None
    revision: int
    created_at: datetime


class LeadResponse(ChannelModel):
    lead: LeadSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ApprovalSummary(ChannelModel):
    attribution_change_approval_id: str
    approver_operator_id: str
    decision: Literal["approved", "rejected"]
    reason_code: str
    decided_at: datetime


class AttributionSummary(ChannelModel):
    workspace_attribution_id: str
    attribution_change_id: str
    channel_partner_id: str
    target_workspace_id: str
    status: Literal["pending_approval", "active", "superseded", "rejected", "cancelled", "expired"]
    starts_at: datetime
    ends_at: datetime | None
    required_approvals: Literal[1, 2]
    approval_count: int
    approval_generation: int
    revision: int
    approvals: tuple[ApprovalSummary, ...]


class AttributionResponse(ChannelModel):
    attribution: AttributionSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class StatementSummary(ChannelModel):
    channel_statement_id: str
    channel_partner_id: str
    period_start: datetime
    period_end: datetime
    currency: Literal["CNY", "USD"]
    total_amount_micros: int
    total_commission_micros: int
    line_count: int
    reconciliation_sha256: str
    status: Literal["imported", "open", "closed"]
    close_revision: int
    closed_at: datetime | None


class StatementListResponse(ChannelModel):
    items: tuple[StatementSummary, ...]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class PartnerWorkspaceSummary(ChannelModel):
    workspace_id: str
    display_name: str
    region_code: str
    attribution_starts_at: datetime
    operational_status: Literal[
        "active", "suspended", "deletion_pending", "deleted", "migration_conflict"
    ]
    statement_totals: dict[Literal["CNY", "USD"], int] | None


class PartnerWorkspaceListResponse(ChannelModel):
    partner: ChannelPartnerSummary
    role_code: PartnerRole
    items: tuple[PartnerWorkspaceSummary, ...]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


__all__ = [name for name in globals() if name.endswith(("Command", "Response", "Summary"))]
