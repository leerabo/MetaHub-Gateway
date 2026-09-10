"""In-process signed-artifact catalog for executable platform job types."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class PlatformRuntimeProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    marker: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    outcome: str = Field(default="success", pattern=r"^(?:success|retry|fail)$")


class JobHandler(Protocol):
    def __call__(self, context: Any, payload: BaseModel) -> str: ...


class ExternalJobHandler(Protocol):
    def __call__(self, context: Any, continuation: object) -> str: ...


@dataclass(frozen=True, slots=True)
class JobHandlerDefinition:
    type_code: str
    contract_version: int
    artifact_sha256: str
    input_model: type[BaseModel]
    handler: JobHandler
    external_handler: ExternalJobHandler | None = None


ARTIFACTS = {
    type_code: hashlib.sha256(f"{type_code}:v1".encode("ascii")).hexdigest()
    for type_code in (
        "platform.runtime.probe",
        "feature.rollout.step",
        "config.snapshot.rebuild",
        "observability.aggregate",
        "risk.restriction.expire",
        "dr.run",
        "status.publish",
        "capacity.run",
        "data.retention.delete",
        "fleet.rollout.advance",
        "console.domain.migrate",
    )
}
ARTIFACTS["console.domain.migrate"] = hashlib.sha256(
    b"console.domain.migrate:v1:migration_id,operation_generation,result_revision,action,target_generation"
).hexdigest()
_HANDLERS: dict[str, JobHandlerDefinition] = {}
_LOCK = threading.RLock()


def register_job_handler(
    type_code: str,
    *,
    input_model: type[BaseModel],
    handler: JobHandler,
    contract_version: int = 1,
    external_handler: ExternalJobHandler | None = None,
) -> None:
    artifact = ARTIFACTS.get(type_code)
    if artifact is None or contract_version != 1:
        raise ValueError("job handler is not present in the signed artifact catalog")
    definition = JobHandlerDefinition(
        type_code=type_code,
        contract_version=contract_version,
        artifact_sha256=artifact,
        input_model=input_model,
        handler=handler,
        external_handler=external_handler,
    )
    with _LOCK:
        existing = _HANDLERS.get(type_code)
        if existing is not None and existing != definition:
            raise ValueError("job handler registration conflicts with the signed artifact")
        _HANDLERS[type_code] = definition


def handler_definition(type_code: str) -> JobHandlerDefinition | None:
    with _LOCK:
        return _HANDLERS.get(type_code)


def executable_type_codes() -> tuple[str, ...]:
    with _LOCK:
        return tuple(sorted(_HANDLERS))


def validate_job_input(type_code: str, payload: object) -> BaseModel:
    definition = handler_definition(type_code)
    if definition is None:
        raise ValueError("job type is not executable in this artifact")
    return definition.input_model.model_validate(payload)


__all__ = [
    "ARTIFACTS",
    "JobHandlerDefinition",
    "PlatformRuntimeProbeInput",
    "executable_type_codes",
    "handler_definition",
    "register_job_handler",
    "validate_job_input",
]
