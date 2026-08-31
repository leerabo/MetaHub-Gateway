"""Strict PRD-140 customer-success API contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z") or not 20 <= len(value) <= 27:
        raise ValueError("UTC timestamp required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("UTC timestamp required")
    return parsed


def _date(value: object) -> date:
    if type(value) is date:
        return value
    if type(value) is not str or len(value) != 10:
        raise ValueError("date required")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date required")
    return parsed


def _tuple(value: object) -> object:
    if type(value) is tuple:
        return value
    if type(value) is not list:
        raise ValueError("array required")
    return tuple(value)


Timestamp = Annotated[datetime, BeforeValidator(_timestamp)]
Date = Annotated[date, BeforeValidator(_date)]
AccountId = Annotated[str, Field(pattern=r"^csa_[0-9a-f]{32}$")]
AccountIds = Annotated[tuple[AccountId, ...], BeforeValidator(_tuple)]
PlanId = Annotated[str, Field(pattern=r"^csp_[0-9a-f]{32}$")]
TaskId = Annotated[str, Field(pattern=r"^ctk_[0-9a-f]{32}$")]
BatchId = Annotated[str, Field(pattern=r"^ctb_[0-9a-f]{32}$")]
OperatorId = Annotated[str, Field(pattern=r"^pto_[0-9a-f]{32}$")]
Risk = Literal["unknown", "healthy", "watch", "at_risk", "critical"]


class CustomerSuccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CustomerSuccessAccountSummary(CustomerSuccessModel):
    scope: Literal["customer_success"] = "customer_success"
    customer_account_id: AccountId
    workspace_id: str
    display_name: str
    slug: str
    kind: Literal["personal", "organization"]
    region: str
    operational_status: Literal[
        "active", "suspended", "deletion_pending", "deleted", "migration_conflict"
    ]
    activation: Literal["not_started", "in_progress", "activated"]
    activation_completed_steps: int = Field(ge=0, le=8)
    adoption: Literal["unknown", "low", "medium", "high"]
    health_risk: Risk
    renewal_status: str
    open_support_ticket_count: int = Field(ge=0)
    plan_count: int = Field(ge=0)
    open_task_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    projection_updated_at: datetime


class SupportAccountSummary(CustomerSuccessModel):
    scope: Literal["support"] = "support"
    customer_account_id: AccountId
    workspace_id: str
    operational_status: str
    health_risk: Risk
    open_support_ticket_count: int = Field(ge=0)
    breached_sla_count: int = Field(ge=0)
    projection_updated_at: datetime


class FinanceAccountSummary(CustomerSuccessModel):
    scope: Literal["finance"] = "finance"
    customer_account_id: AccountId
    workspace_id: str
    region: str
    subscription_id: str | None
    plan_version_id: str | None
    subscription_status: str
    renewal_at: datetime | None
    projection_updated_at: datetime


class OwnerAccountSummary(CustomerSuccessModel):
    scope: Literal["owner"] = "owner"
    customer_account_id: AccountId
    workspace_id: str
    display_name: str
    slug: str
    kind: Literal["personal", "organization"]
    region: str
    operational_status: Literal[
        "active", "suspended", "deletion_pending", "deleted", "migration_conflict"
    ]
    activation: Literal["not_started", "in_progress", "activated"]
    activation_completed_steps: int = Field(ge=0, le=8)
    adoption: Literal["unknown", "low", "medium", "high"]
    health_risk: Risk
    subscription_id: str | None
    plan_version_id: str | None
    subscription_status: str
    renewal_at: datetime | None
    open_support_ticket_count: int = Field(ge=0)
    breached_sla_count: int = Field(ge=0)
    plan_count: int = Field(ge=0)
    open_task_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    projection_updated_at: datetime


AccountSummary = (
    CustomerSuccessAccountSummary
    | SupportAccountSummary
    | FinanceAccountSummary
    | OwnerAccountSummary
)


class SuccessPlanSummary(CustomerSuccessModel):
    customer_success_plan_id: PlanId
    customer_account_id: AccountId
    objective_code: str
    starts_on: Date
    ends_on: Date
    target_health_bps: int = Field(ge=0, le=10_000)
    target_activation_steps: int = Field(ge=0, le=8)
    target_adoption_bps: int = Field(ge=0, le=10_000)
    source_health_sequence: int | None
    status: Literal["draft", "active", "at_risk", "completed", "canceled"]
    revision: int = Field(ge=1)
    created_by_operator_id: OperatorId
    updated_by_operator_id: OperatorId
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class CustomerTaskSummary(CustomerSuccessModel):
    customer_task_id: TaskId
    customer_account_id: AccountId
    customer_success_plan_id: PlanId | None
    renewal_opportunity_id: str | None
    task_type: str
    priority: Literal["low", "normal", "high", "critical"]
    reason_code: str
    assignee_operator_id: OperatorId
    due_at: datetime
    required: bool
    status: Literal["open", "in_progress", "completed", "canceled"]
    due_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class QbrSummary(CustomerSuccessModel):
    qbr_summary_id: str
    quarter: str
    health_sequence: int | None
    adoption_bps: int | None
    support_ticket_count: int = Field(ge=0)
    renewal_risk_count: int = Field(ge=0)
    outcome_code: str
    action_code: str
    source_digest: str
    generated_at: datetime


class ContactSummary(CustomerSuccessModel):
    customer_contact_id: str
    contact_role: Literal["primary", "billing", "technical", "security"]
    status: Literal["active", "unavailable"]
    account_id: str
    display_name: str
    email: str
    phone: str | None
    membership_revision: int = Field(ge=1)
    notification_preference_revision: int = Field(ge=0)
    preference_summary: tuple[dict[str, object], ...]


class AccountDetail(CustomerSuccessModel):
    plans: tuple[SuccessPlanSummary, ...]
    plans_count: int = Field(ge=0)
    plans_truncated: bool
    tasks: tuple[CustomerTaskSummary, ...]
    tasks_count: int = Field(ge=0)
    tasks_truncated: bool
    qbr_summaries: tuple[QbrSummary, ...]
    qbr_count: int = Field(ge=0)
    qbr_truncated: bool
    operation_count: int = Field(ge=0)
    operations_truncated: bool


class CustomerAccountListResponse(CustomerSuccessModel):
    items: tuple[AccountSummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: str | None
    detail: AccountDetail | None = None
    contacts: tuple[ContactSummary, ...] | None = None
    request_id: str
    trace_id: str


class HealthFactor(CustomerSuccessModel):
    code: Literal[
        "usage_trend",
        "reliability",
        "cost_anomaly_health",
        "budget_readiness",
        "team_activity",
        "ticket_health",
    ]
    score_bps: int | None = Field(ge=0, le=10_000)
    contribution_bps: int = Field(ge=0, le=10_000)
    current_value: int | None = Field(default=None, ge=0)
    comparison_value: int | None = Field(default=None, ge=0)


ExcludeUnknownRisk = Literal["healthy", "watch", "at_risk", "critical"]


class CustomerSuccessHealthResponse(CustomerSuccessModel):
    customer_account_id: AccountId
    scope: Literal["customer_success"] = "customer_success"
    formula_version: Literal["customer-health-v1", "customer-health-platform-v2"]
    window_start: datetime
    window_end: datetime
    comparison_start: datetime
    comparison_end: datetime
    score_bps: int = Field(ge=0, le=10_000)
    risk_level: ExcludeUnknownRisk
    factors: tuple[HealthFactor, ...]
    risk_codes: tuple[str, ...]
    action_codes: tuple[str, ...]
    freshness: Literal["fresh", "stale"]
    source_sequence: int = Field(ge=1)
    evaluated_at: datetime
    request_id: str
    trace_id: str


class SupportCustomerHealthResponse(CustomerSuccessModel):
    customer_account_id: AccountId
    scope: Literal["support"] = "support"
    formula_version: Literal["customer-health-v1", "customer-health-platform-v2"]
    risk_level: ExcludeUnknownRisk
    factors: tuple[HealthFactor, ...]
    risk_codes: tuple[str, ...]
    action_codes: tuple[str, ...]
    freshness: Literal["fresh", "stale"]
    source_sequence: int = Field(ge=1)
    evaluated_at: datetime
    request_id: str
    trace_id: str


CustomerHealthResponse = CustomerSuccessHealthResponse | SupportCustomerHealthResponse


class SuccessPlanCommand(CustomerSuccessModel):
    operation: Literal["create", "activate", "revise_targets", "complete", "cancel"]
    customer_account_id: AccountId
    customer_success_plan_id: PlanId | None = None
    expected_revision: int = Field(ge=0)
    objective_code: (
        Literal["activation", "adoption", "reliability", "renewal", "security_readiness"] | None
    ) = None
    starts_on: Date | None = None
    ends_on: Date | None = None
    target_health_bps: int | None = Field(default=None, ge=0, le=10_000)
    target_activation_steps: int | None = Field(default=None, ge=0, le=8)
    target_adoption_bps: int | None = Field(default=None, ge=0, le=10_000)
    source_health_sequence: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def exact_shape(self) -> SuccessPlanCommand:
        common = {"operation", "customer_account_id", "expected_revision", "confirmed"}
        allowed = {
            "create": common
            | {
                "objective_code",
                "starts_on",
                "ends_on",
                "target_health_bps",
                "target_activation_steps",
                "target_adoption_bps",
                "source_health_sequence",
            },
            "revise_targets": common
            | {
                "customer_success_plan_id",
                "ends_on",
                "target_health_bps",
                "target_activation_steps",
                "target_adoption_bps",
                "source_health_sequence",
            },
            "activate": common | {"customer_success_plan_id"},
            "complete": common | {"customer_success_plan_id"},
            "cancel": common | {"customer_success_plan_id"},
        }[self.operation]
        if not self.model_fields_set <= allowed:
            raise ValueError("operation fields forbidden")
        targets = (
            self.target_health_bps,
            self.target_activation_steps,
            self.target_adoption_bps,
            self.source_health_sequence,
        )
        if self.operation == "create":
            if self.customer_success_plan_id is not None or self.expected_revision != 0:
                raise ValueError("create identity invalid")
            if any(
                value is None
                for value in (self.objective_code, self.starts_on, self.ends_on, *targets)
            ):
                raise ValueError("create fields required")
            if self.ends_on < self.starts_on:
                raise ValueError("plan date range invalid")
        else:
            if self.customer_success_plan_id is None or self.expected_revision < 1:
                raise ValueError("plan identity required")
            if self.objective_code is not None or self.starts_on is not None:
                raise ValueError("immutable fields forbidden")
            if self.operation == "revise_targets":
                if self.ends_on is None or any(value is None for value in targets):
                    raise ValueError("target fields required")
            elif self.ends_on is not None or any(value is not None for value in targets):
                raise ValueError("target fields forbidden")
        return self


class SuccessPlanResponse(CustomerSuccessModel):
    plan: SuccessPlanSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class CustomerTaskCommand(CustomerSuccessModel):
    operation: Literal["create_one", "transition_one", "prepare_batch", "approve_batch"]
    customer_account_id: AccountId | None = None
    customer_account_ids: AccountIds | None = Field(default=None, max_length=100)
    customer_task_id: TaskId | None = None
    customer_task_batch_id: BatchId | None = None
    customer_success_plan_id: PlanId | None = None
    renewal_opportunity_id: str | None = Field(default=None, pattern=r"^rno_[0-9a-f]{32}$")
    task_type: (
        Literal[
            "activation_review",
            "adoption_follow_up",
            "health_review",
            "renewal_review",
            "security_review",
            "qbr_prepare",
        ]
        | None
    ) = None
    priority: Literal["low", "normal", "high", "critical"] | None = None
    reason_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9._-]+$")
    assignee_operator_id: OperatorId | None = None
    due_at: Timestamp | None = None
    required: bool | None = None
    target_status: Literal["open", "in_progress", "completed", "canceled"] | None = None
    decision: Literal["approved", "rejected"] | None = None
    expected_revision: int = Field(ge=0)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def exact_shape(self) -> CustomerTaskCommand:
        common = {"operation", "reason_code", "expected_revision", "confirmed"}
        allowed = {
            "create_one": common
            | {
                "customer_account_id",
                "customer_success_plan_id",
                "renewal_opportunity_id",
                "task_type",
                "priority",
                "assignee_operator_id",
                "due_at",
                "required",
            },
            "transition_one": common | {"customer_task_id", "target_status"},
            "prepare_batch": common
            | {
                "customer_account_ids",
                "task_type",
                "priority",
                "assignee_operator_id",
                "due_at",
            },
            "approve_batch": common | {"customer_task_batch_id", "decision"},
        }[self.operation]
        if not self.model_fields_set <= allowed:
            raise ValueError("operation fields forbidden")
        create_fields = (self.task_type, self.priority, self.assignee_operator_id, self.due_at)
        if self.operation == "create_one":
            if self.customer_account_id is None or self.expected_revision != 0:
                raise ValueError("account and zero revision required")
            if any(value is None for value in create_fields) or self.required is None:
                raise ValueError("task fields required")
        elif self.operation == "transition_one":
            if (
                self.customer_task_id is None
                or self.target_status is None
                or self.expected_revision < 1
            ):
                raise ValueError("task transition fields required")
        elif self.operation == "prepare_batch":
            if not self.customer_account_ids or self.expected_revision != 0:
                raise ValueError("batch accounts required")
            if len(self.customer_account_ids) != len(set(self.customer_account_ids)):
                raise ValueError("batch accounts duplicate")
            if any(value is None for value in create_fields):
                raise ValueError("batch task fields required")
        else:
            if self.customer_task_batch_id is None or self.decision is None:
                raise ValueError("batch approval fields required")
            if self.expected_revision < 1:
                raise ValueError("batch revision required")
        return self


class TaskBatchSummary(CustomerSuccessModel):
    customer_task_batch_id: BatchId
    prepared_by_operator_id: OperatorId
    approved_by_operator_id: OperatorId | None
    account_count: int = Field(ge=1, le=100)
    manifest_sha256: str
    task_type: str
    priority: str
    reason_code: str
    assignee_operator_id: OperatorId
    due_at: datetime
    status: Literal["awaiting_approval", "applied", "rejected"]
    result_count: int = Field(ge=0, le=100)
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class CustomerTaskResponse(CustomerSuccessModel):
    task: CustomerTaskSummary | None = None
    batch: TaskBatchSummary | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class CustomerSuccessRenewalSummary(CustomerSuccessModel):
    scope: Literal["customer_success"] = "customer_success"
    renewal_opportunity_id: str
    customer_account_id: AccountId
    workspace_id: str
    display_name: str
    renewal_at: datetime
    health_risk_code: Risk
    adoption_risk_code: str
    payment_risk_code: str
    invoice_status: str
    status: str
    open_plan_count: int = Field(ge=0)
    open_task_count: int = Field(ge=0)
    revision: int = Field(ge=1)


class FinanceRenewalSummary(CustomerSuccessModel):
    scope: Literal["finance"] = "finance"
    renewal_opportunity_id: str
    customer_account_id: AccountId
    workspace_id: str
    subscription_id: str
    plan_version_id: str
    renewal_at: datetime
    currency: Literal["CNY", "USD"] | None
    amount_minor: int | None = Field(ge=0)
    amount_status: Literal["available", "not_available"]
    payment_risk_code: str
    invoice_status: str
    status: str
    revision: int = Field(ge=1)


class OwnerRenewalSummary(CustomerSuccessModel):
    scope: Literal["owner"] = "owner"
    renewal_opportunity_id: str
    customer_account_id: AccountId
    workspace_id: str
    display_name: str
    subscription_id: str
    plan_version_id: str
    renewal_at: datetime
    currency: Literal["CNY", "USD"] | None
    amount_minor: int | None = Field(ge=0)
    amount_status: Literal["available", "not_available"]
    health_risk_code: Risk
    adoption_risk_code: str
    payment_risk_code: str
    invoice_status: str
    status: str
    open_plan_count: int = Field(ge=0)
    open_task_count: int = Field(ge=0)
    revision: int = Field(ge=1)


RenewalOpportunitySummary = (
    CustomerSuccessRenewalSummary | FinanceRenewalSummary | OwnerRenewalSummary
)


class RenewalPipelineResponse(CustomerSuccessModel):
    items: tuple[RenewalOpportunitySummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: str | None
    request_id: str
    trace_id: str
