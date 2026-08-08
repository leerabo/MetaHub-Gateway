"""Strict public DTOs for PRD-074 Batch jobs.

The models intentionally have no tenant, actor, Provider, endpoint, key, prompt, response, lease,
grant, ciphertext, or internal routing fields. Sensitive names, operation parameters, and results
are excluded from repr so framework diagnostics cannot print them accidentally.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from metahub_shared.batch import BatchContractError, validate_operation_spec
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

BatchJobStatus = Literal[
    "queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired"
]
BatchTaskStatus = Literal[
    "queued",
    "leased",
    "calling",
    "retry_wait",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
    "outcome_unknown",
]
BatchOperation = Literal["summarize", "translate", "classify"]
BatchPriority = Literal["low", "normal", "high"]

_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FILE_ID = re.compile(r"^file_[0-9a-f]{32}$")


class CreateBatchJobRequest(BaseModel):
    """A fixed operation over one to one hundred already-approved files."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr = Field(min_length=1, max_length=120, repr=False)
    project_id: StrictStr = Field(min_length=1, max_length=128)
    environment_id: StrictStr = Field(min_length=1, max_length=128)
    model_alias_id: StrictStr = Field(min_length=1, max_length=128)
    operation: BatchOperation
    file_ids: list[StrictStr] = Field(min_length=1, max_length=100, repr=False)
    priority: BatchPriority = "normal"
    max_duration_seconds: StrictInt = Field(default=300, ge=30, le=3600)
    max_attempts: StrictInt = Field(default=2, ge=1, le=3)
    max_output_tokens: StrictInt = Field(default=1024, ge=16, le=4096)
    output_language: StrictStr | None = Field(default=None, repr=False)
    target_language: StrictStr | None = Field(default=None, repr=False)
    classification_labels: list[StrictStr] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_exact_operation(self) -> CreateBatchJobRequest:
        """Validate visible text, canonical owners, unique files, and operation exact-set."""

        if not self.name.strip() or any(
            unicodedata.category(character) == "Cc" for character in self.name
        ):
            raise ValueError("batch.request_invalid")
        if any(
            _RESOURCE_ID.fullmatch(value) is None
            for value in (self.project_id, self.environment_id, self.model_alias_id)
        ):
            raise ValueError("batch.request_invalid")
        if (
            any(_FILE_ID.fullmatch(file_id) is None for file_id in self.file_ids)
            or len(set(self.file_ids)) != len(self.file_ids)
        ):
            raise ValueError("batch.request_invalid")
        provided = self.model_fields_set
        try:
            if self.operation == "summarize":
                if provided.intersection({"target_language", "classification_labels"}):
                    raise BatchContractError()
                value: dict[str, object] = {"operation": "summarize"}
                if "output_language" in provided:
                    if self.output_language is None:
                        raise BatchContractError()
                    value["output_language"] = self.output_language
            elif self.operation == "translate":
                if (
                    "target_language" not in provided
                    or self.target_language is None
                    or provided.intersection({"output_language", "classification_labels"})
                ):
                    raise BatchContractError()
                value = {"operation": "translate", "target_language": self.target_language}
            else:
                if (
                    "classification_labels" not in provided
                    or self.classification_labels is None
                    or provided.intersection({"output_language", "target_language"})
                ):
                    raise BatchContractError()
                value = {
                    "operation": "classify",
                    "classification_labels": self.classification_labels,
                }
            validate_operation_spec(value)
        except BatchContractError:
            raise ValueError("batch.request_invalid") from None
        return self

    def operation_wire(self) -> dict[str, object]:
        """Return the exact validated operation object for encryption only."""

        value: dict[str, object] = {"operation": self.operation}
        if self.operation == "summarize" and self.output_language is not None:
            value["output_language"] = self.output_language
        elif self.operation == "translate":
            value["target_language"] = self.target_language
        elif self.operation == "classify":
            value["classification_labels"] = self.classification_labels
        return value


class BatchProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    total: int = Field(ge=1, le=100)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    expired: int = Field(ge=0)
    outcome_unknown: int = Field(ge=0)


class BatchCostSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    estimated_cny: str
    posted_cny: str
    possible_cny: str
    reconciliation_pending: bool


class BatchJobInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    batch_job_id: str
    name: str = Field(repr=False)
    project_id: str
    environment_id: str
    model_alias_id: str
    operation: BatchOperation
    priority: BatchPriority
    max_duration_seconds: int
    max_attempts: int
    max_output_tokens: int
    status: BatchJobStatus
    progress: BatchProgress
    cost: BatchCostSummary
    deadline_at: datetime
    cancel_requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime


class BatchJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job: BatchJobInfo
    request_id: str
    trace_id: str


class BatchJobListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    jobs: list[BatchJobInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class BatchTaskInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    batch_task_id: str
    file_id: str
    task_index: int = Field(ge=0, le=99)
    status: BatchTaskStatus
    attempt_count: int = Field(ge=0, le=3)
    request_id: str | None
    trace_id: str | None
    failure_code: str | None
    result_available: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_cny: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class BatchTaskListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tasks: list[BatchTaskInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class BatchTaskResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    batch_job_id: str
    batch_task_id: str
    assistant: str = Field(repr=False)
    request_id: str
    trace_id: str


class ReconcileBatchTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["confirm_not_charged", "confirm_charged"]
    adjustment_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_decision(self) -> ReconcileBatchTaskRequest:
        if (self.decision == "confirm_charged") != (self.adjustment_id is not None):
            raise ValueError("batch.request_invalid")
        return self


class BatchReconciliationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    batch_job_id: str
    batch_task_id: str
    decision: Literal["confirm_not_charged", "confirm_charged"]
    reconciliation_pending: bool
    request_id: str
    trace_id: str


__all__ = [
    "BatchJobInfo",
    "BatchJobListResponse",
    "BatchJobResponse",
    "BatchJobStatus",
    "BatchReconciliationResponse",
    "BatchTaskInfo",
    "BatchTaskListResponse",
    "BatchTaskResultResponse",
    "BatchTaskStatus",
    "CreateBatchJobRequest",
    "ReconcileBatchTaskRequest",
]
