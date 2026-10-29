"""Synthetic signed fixtures for orchestration tests, never real security evidence."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_control.security_assurance.receipts import (
    RESOURCE_FIXTURES,
    ExecutionReceipt,
    FixtureReceipt,
    ScenarioReceipt,
    SignedExecutionReceipt,
    digest,
    execution_request_payload,
    receipt_signing_bytes,
)

TEST_KEY = Ed25519PrivateKey.generate()
TEST_KEY_ID = "test-orchestration-only"
TEST_IMAGE = digest("synthetic-test-image-not-an-actual-runner")
TEST_POLICY = digest("synthetic-test-policy-not-isolation-proof")


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def write_test_trust(path):
    now = datetime.now(UTC)
    bundle = {"schema_version": "metahub.assurance-trust.v1", "keys": [{
        "key_id": TEST_KEY_ID, "public_key": b64(TEST_KEY.public_key().public_bytes_raw()),
        "runner_image_sha256": TEST_IMAGE, "isolation_policy_sha256": TEST_POLICY,
        "not_before": (now - timedelta(days=1)).isoformat(),
        "not_after": (now + timedelta(days=1)).isoformat(),
    }]}
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return bundle


def synthetic_receipt(request, results) -> ExecutionReceipt:
    finished = datetime.now(UTC)
    runs = []
    for scenario, result in zip(request.scenarios, results, strict=True):
        codes = ["source_subject", "target_subject"]
        if scenario.target_kind in RESOURCE_FIXTURES:
            codes.append(RESOURCE_FIXTURES[scenario.target_kind])
        fixtures = tuple(FixtureReceipt(
            fixture_code=code,
            fixture_id_sha256=digest([
                request.security_assessment_id, scenario.scenario_code, code,
            ]),
            created_at=request.started_at, cleanup_status="deleted", cleanup_attempted_at=finished,
        ) for code in codes)
        runs.append(ScenarioReceipt(
            result=result.model_copy(update={
                "duration_ms": (finished - request.started_at) // timedelta(milliseconds=1),
            }),
            source_subject_sha256=fixtures[0].fixture_id_sha256,
            target_subject_sha256=fixtures[1].fixture_id_sha256,
            started_at=request.started_at, finished_at=finished, fixtures=fixtures,
        ))
    return ExecutionReceipt(
        schema_version="metahub.assurance-execution.v1",
        request_sha256=digest(execution_request_payload(request)),
        environment_id="aenv_" + uuid4().hex, runner_image_sha256=TEST_IMAGE,
        isolation_policy_sha256=TEST_POLICY, started_at=request.started_at,
        finished_at=finished, runs=tuple(runs),
    )


def sign_receipt(receipt: ExecutionReceipt) -> bytes:
    return SignedExecutionReceipt(
        key_id=TEST_KEY_ID, receipt=receipt,
        signature=b64(TEST_KEY.sign(receipt_signing_bytes(TEST_KEY_ID, receipt))),
    ).model_dump_json().encode("utf-8")
