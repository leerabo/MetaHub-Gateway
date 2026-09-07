"""Verify isolated-runner receipts against deployment-owned Ed25519 trust.

Signatures authenticate executor claims. The executor must still measure the actual
artifact, environment, fixture creation and cleanup; hashes alone prove none of those.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from metahub_shared.config_distribution import canonical_json_bytes
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from .models import ScenarioExecution

if TYPE_CHECKING:
    from .service import AssessmentExecutionRequest

SIGNATURE_PURPOSE = b"metahub.assurance-execution.v1\0"
MAX_RECEIPT_BYTES = 262_144
MAX_TRUST_BYTES = 65_536
UNKNOWN_SHA256 = "0" * 64


def _nonzero_digest(value: str) -> str:
    if value == UNKNOWN_SHA256:
        raise ValueError("missing measured digest")
    return value


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$"), AfterValidator(_nonzero_digest)]
FixtureCode = Literal[
    "source_subject", "target_subject", "cache_entry", "queue_message",
    "object_artifact", "metric_series",
]
RESOURCE_FIXTURES = {
    "cache": "cache_entry", "queue": "queue_message",
    "object_store": "object_artifact", "metrics": "metric_series",
}


class ReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FixtureReceipt(ReceiptModel):
    fixture_code: FixtureCode
    fixture_id_sha256: Digest
    created_at: AwareDatetime
    cleanup_status: Literal["deleted", "failed"]
    cleanup_attempted_at: AwareDatetime


class ScenarioReceipt(ReceiptModel):
    result: ScenarioExecution
    source_subject_sha256: Digest
    target_subject_sha256: Digest
    started_at: AwareDatetime
    finished_at: AwareDatetime
    fixtures: tuple[FixtureReceipt, ...] = Field(min_length=2, max_length=6)

    def seed_digest(self) -> str:
        return digest([
            {
                "fixture_code": item.fixture_code, "fixture_id_sha256": item.fixture_id_sha256,
                "created_at": utc_text(item.created_at),
            }
            for item in self.fixtures
        ])

    def cleanup_digest(self) -> str:
        return digest([item.model_dump(mode="json") for item in self.fixtures])


class ExecutionReceipt(ReceiptModel):
    schema_version: Literal["metahub.assurance-execution.v1"]
    request_sha256: Digest
    environment_id: str = Field(pattern=r"^aenv_[0-9a-f]{32}$")
    runner_image_sha256: Digest
    isolation_policy_sha256: Digest
    started_at: AwareDatetime
    finished_at: AwareDatetime
    runs: tuple[ScenarioReceipt, ...] = Field(min_length=10, max_length=10)

    def environment_digest(self) -> str:
        return digest({
            "environment_id": self.environment_id, "request_sha256": self.request_sha256,
            "runner_image_sha256": self.runner_image_sha256,
            "isolation_policy_sha256": self.isolation_policy_sha256,
        })


class SignedExecutionReceipt(ReceiptModel):
    key_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    receipt: ExecutionReceipt
    signature: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")


class TrustKey(ReceiptModel):
    key_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    public_key: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    runner_image_sha256: Digest
    isolation_policy_sha256: Digest
    not_before: AwareDatetime
    not_after: AwareDatetime

    @model_validator(mode="after")
    def validity(self):
        if self.not_before >= self.not_after:
            raise ValueError("invalid trust interval")
        decode_base64url(self.public_key, 32)
        return self


class TrustBundle(ReceiptModel):
    schema_version: Literal["metahub.assurance-trust.v1"]
    keys: tuple[TrustKey, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def identities(self):
        if len({key.key_id for key in self.keys}) != len(self.keys):
            raise ValueError("duplicate trust key")
        return self


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def execution_request_payload(request: AssessmentExecutionRequest) -> dict[str, object]:
    # Owner identifiers bind the receipt but never select a customer target on the runner.
    return {
        "schema_version": "metahub.assurance-request.v1",
        "security_assessment_id": request.security_assessment_id,
        "owner_scope_sha256": digest([request.workspace_id, request.tenant_id]),
        "profile_code": request.profile_code, "profile_version": request.profile_version,
        "profile_sha256": request.profile_sha256,
        "profile_signature_sha256": request.profile_signature_sha256,
        "generation": request.generation, "artifact_sha256": request.artifact_sha256,
        "code_tree_sha256": request.code_tree_sha256,
        "migration_set_sha256": request.migration_set_sha256,
        "contract_set_sha256": request.contract_set_sha256,
        "lease_challenge_sha256": request.lease_challenge_sha256,
        "started_at": utc_text(request.started_at), "deadline_at": utc_text(request.deadline_at),
        "scenarios": [asdict(scenario) for scenario in request.scenarios],
    }


def receipt_signing_bytes(key_id: str, receipt: ExecutionReceipt) -> bytes:
    return SIGNATURE_PURPOSE + canonical_json_bytes({
        "key_id": key_id, "receipt": receipt.model_dump(mode="json"),
    })


def decode_base64url(value: str, length: int) -> bytes:
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != length or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("invalid canonical base64url")
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt property")
        result[key] = value
    return result


def _strict_json(model, content: bytes, limit: int):
    if not isinstance(content, bytes) or len(content) > limit:
        raise ValueError("invalid receipt size or transport")
    parsed = json.loads(content, object_pairs_hook=_unique_object)
    return model.model_validate_json(json.dumps(parsed))


@dataclass(frozen=True, slots=True)
class FileAssuranceReceiptVerifier:
    trust_path: Path = field(repr=False)

    def __call__(
        self, request: AssessmentExecutionRequest, content: bytes,
    ) -> ExecutionReceipt:
        signed = _strict_json(SignedExecutionReceipt, content, MAX_RECEIPT_BYTES)
        with self.trust_path.open("rb") as stream:
            trust = _strict_json(TrustBundle, stream.read(MAX_TRUST_BYTES + 1), MAX_TRUST_BYTES)
        receipt = signed.receipt
        now = datetime.now(UTC)
        key = next((item for item in trust.keys if item.key_id == signed.key_id), None)
        if key is None or not key.not_before <= receipt.started_at <= now < key.not_after:
            raise ValueError("runner trust missing, revoked or expired")
        Ed25519PublicKey.from_public_bytes(decode_base64url(key.public_key, 32)).verify(
            decode_base64url(signed.signature, 64), receipt_signing_bytes(signed.key_id, receipt),
        )
        if (
            receipt.request_sha256 != digest(execution_request_payload(request))
            or receipt.runner_image_sha256 != key.runner_image_sha256
            or receipt.isolation_policy_sha256 != key.isolation_policy_sha256
            or not request.started_at <= receipt.started_at <= receipt.finished_at <= now
            or receipt.finished_at > request.deadline_at
            or now > request.deadline_at
            or [run.result.scenario_code for run in receipt.runs]
            != [scenario.scenario_code for scenario in request.scenarios]
        ):
            raise ValueError("runner receipt binding or time mismatch")
        for scenario, run in zip(request.scenarios, receipt.runs, strict=True):
            _validate_run(receipt, run, scenario.target_kind)
        return receipt


def _validate_run(receipt: ExecutionReceipt, run: ScenarioReceipt, target_kind: str) -> None:
    if not receipt.started_at <= run.started_at <= run.finished_at <= receipt.finished_at:
        raise ValueError("scenario time outside receipt")
    elapsed_ms = (run.finished_at - run.started_at) // timedelta(milliseconds=1)
    if run.result.duration_ms != elapsed_ms or run.result.evidence_sha256 == UNKNOWN_SHA256:
        raise ValueError("scenario duration mismatch")
    fixtures = {item.fixture_code: item for item in run.fixtures}
    required = {"source_subject", "target_subject"}
    if target_kind in RESOURCE_FIXTURES:
        required.add(RESOURCE_FIXTURES[target_kind])
    if len(fixtures) != len(run.fixtures) or set(fixtures) != required:
        raise ValueError("fixture manifest mismatch")
    if (
        fixtures["source_subject"].fixture_id_sha256 != run.source_subject_sha256
        or fixtures["target_subject"].fixture_id_sha256 != run.target_subject_sha256
        or run.source_subject_sha256 == run.target_subject_sha256
        or len({item.fixture_id_sha256 for item in run.fixtures}) != len(run.fixtures)
    ):
        raise ValueError("fixture subject binding mismatch")
    for item in run.fixtures:
        if not (
            receipt.started_at <= item.created_at <= run.started_at
            and run.finished_at <= item.cleanup_attempted_at <= receipt.finished_at
        ):
            raise ValueError("fixture lifecycle time mismatch")
