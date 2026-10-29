"""PRD-104 发票、下载、冲红和对账的严格公开 DTO。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Currency = Literal["CNY", "USD"]


class InvoiceResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BillingStatementInfo(InvoiceResponseModel):
    billing_statement_id: str
    billing_cycle_id: str
    statement_kind: Literal["standard", "credit", "replacement"]
    immutable_version: int = Field(ge=1)
    currency: Currency
    subtotal_minor: int
    tax_rate_basis_points: int = Field(ge=0, le=10_000)
    tax_minor: int
    total_minor: int
    status: Literal["published"]
    issued_at: datetime


class BillingStatementsResponse(InvoiceResponseModel):
    statements: tuple[BillingStatementInfo, ...]
    request_id: str
    trace_id: str


class InvoiceCreateRequest(InvoiceResponseModel):
    billing_statement_id: str
    invoice_type: Literal["electronic_general", "electronic_special"]


class InvoiceArtifactInfo(InvoiceResponseModel):
    invoice_artifact_id: str
    artifact_kind: Literal["invoice_pdf", "invoice_csv", "red_letter_pdf"]
    invoice_number_masked: str
    mime_type: Literal["application/pdf", "text/csv"]
    size_bytes: int = Field(gt=0, le=20_971_520)
    content_sha256: str
    issued_at: datetime
    download_action: str


class InvoiceInfo(InvoiceResponseModel):
    invoice_request_id: str
    billing_statement_id: str
    invoice_type: Literal["electronic_general", "electronic_special"]
    status: Literal["requested", "processing", "issued", "failed", "reversal_pending", "reversed"]
    currency: Currency
    total_minor: int
    billing_profile_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    artifacts: tuple[InvoiceArtifactInfo, ...]
    requested_at: datetime
    updated_at: datetime


class InvoiceResponse(InvoiceResponseModel):
    invoice: InvoiceInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class InvoiceDownloadGrantResponse(InvoiceResponseModel):
    invoice_request_id: str
    invoice_artifact_id: str
    download_token: str
    expires_at: datetime
    request_id: str
    trace_id: str


class ReconciliationCreateRequest(InvoiceResponseModel):
    scope: Literal["all"]
    confirmed: Literal[True]


class ReconciliationRunInfo(InvoiceResponseModel):
    reconciliation_run_id: str
    scope: Literal["all"]
    order_watermark: int = Field(ge=0)
    payment_watermark: int = Field(ge=0)
    meter_watermark: int = Field(ge=0)
    statement_watermark: int = Field(ge=0)
    invoice_watermark: int = Field(ge=0)
    status: Literal["queued", "processing", "completed", "failed"]
    generation: int = Field(ge=1)
    created_at: datetime
    completed_at: datetime | None = None


class ReconciliationResponse(InvoiceResponseModel):
    reconciliation: ReconciliationRunInfo
    request_id: str
    trace_id: str


class ReconciliationDifferenceInfo(InvoiceResponseModel):
    reconciliation_difference_id: str
    difference_type: Literal[
        "order_payment_amount_mismatch",
        "paid_order_missing_capture",
        "closed_cycle_missing_statement",
        "meter_summary_watermark_mismatch",
        "issued_invoice_missing_artifact",
    ]
    left_source_type: Literal["order", "billing_cycle", "meter_summary", "invoice_request"]
    left_source_id: str
    right_source_type: (
        Literal["payment_intent", "billing_statement", "meter_reading", "invoice_artifact"] | None
    ) = None
    right_source_id: str | None = None
    currency: Currency
    amount_difference_minor: int
    status: Literal["open", "acknowledged", "adjusted"]
    adjustment_reference: str | None = None
    detected_at: datetime


class ReconciliationDifferencesResponse(InvoiceResponseModel):
    reconciliation: ReconciliationRunInfo
    differences: tuple[ReconciliationDifferenceInfo, ...]
    next_cursor: str | None = None
    request_id: str
    trace_id: str


class InvoiceReversalCreateRequest(InvoiceResponseModel):
    reason_code: Literal["invoice_error", "customer_correction", "contract_canceled"]
    confirmed: Literal[True]


class InvoiceReversalApprovalRequest(InvoiceResponseModel):
    decision: Literal["approve", "reject"]
    reason_code: Literal[
        "facts_confirmed", "policy_confirmed", "evidence_missing", "policy_blocked"
    ]
    confirmed: Literal[True]


class InvoiceReversalInfo(InvoiceResponseModel):
    invoice_reversal_request_id: str
    invoice_request_id: str
    reason_code: Literal["invoice_error", "customer_correction", "contract_canceled"]
    status: Literal["pending_approvals", "queued", "completed", "rejected"]
    approved_domains: tuple[Literal["billing", "compliance"], ...]
    state_revision: int = Field(ge=1)
    requested_at: datetime
    updated_at: datetime


class InvoiceReversalResponse(InvoiceResponseModel):
    reversal: InvoiceReversalInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
