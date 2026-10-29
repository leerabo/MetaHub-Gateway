/** Strict PRD-138 Platform client for release-candidate launch governance. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

export const LAUNCH_GATE_CODES = Object.freeze([
  "wb", "api", "bb", "gr", "perf", "web", "ui", "ucd", "comp", "sec", "flow",
  "migration", "disaster_recovery", "payment", "edge", "privacy", "compliance",
  "supply_chain", "tenant_isolation", "long_stability",
]);
export const LAUNCH_WAVES = Object.freeze([
  "internal", "personal_pilot", "enterprise_pilot", "managed_private_pilot",
]);
export const LAUNCH_ACTORS = Object.freeze([
  "product", "sre", "security", "test", "finance", "legal",
]);

const ID = Object.freeze({
  candidate: /^lvc_[0-9a-f]{32}$/u,
  decision: /^lgd_[0-9a-f]{32}$/u,
  evidence: /^lve_[0-9a-f]{32}$/u,
  gate: /^lgt_[0-9a-f]{32}$/u,
  operator: /^pto_[0-9a-f]{32}$/u,
  review: /^lpr_[0-9a-f]{32}$/u,
  rollback: /^lrb_[0-9a-f]{32}$/u,
  wave: /^lvw_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{2,79}$/u;
const SAFE_METRIC = /^[a-z][a-z0-9_./%-]{0,31}$/u;
const SAFE_TRACE = /^[A-Za-z0-9_.:-]{1,160}$/u;
const VERIFIER = /^[a-z][a-z0-9_-]{3,47}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;
const SIGNATURE = /^[A-Za-z0-9_-]{43,512}$/u;
const CANDIDATE_STATUSES = [
  "collecting", "signing", "ready", "launching", "paused", "review_pending",
  "release_candidate", "rollback_pending", "rolled_back", "failed", "stale",
];
const WAVE_STATUSES = ["pending", "running", "paused", "completed", "rolled_back"];
const ROLLBACK_TARGETS = ["pre_launch", "internal", "personal_pilot", "enterprise_pilot"];
const ROLLBACK_TRIGGERS = [
  "gate_failed", "health_slo", "payment_failure", "security_finding", "operator_request",
];

export async function getLaunchGates({ controlApiUrl, signal } = {}) {
  const value = await requestJson(controlApiUrl, "/platform/launch-gates", { signal });
  exact(value, ["candidate", "gates", "request_id", "trace_id"]);
  responseArray(value.gates, 20, "gates", 20);
  const gates = Object.freeze(value.gates.map(gateSummary));
  if (new Set(gates.map((item) => item.gate_code)).size !== 20
    || new Set(gates.map((item) => item.launch_gate_id)).size !== 20
    || LAUNCH_GATE_CODES.some((code) => !gates.some((item) => item.gate_code === code))) {
    invalidResponse("gate_registry");
  }
  const candidate = value.candidate === null ? null : candidateSummary(value.candidate);
  if (candidate) {
    const registry = new Map(gates.map((item) => [item.launch_gate_id, item]));
    if (candidate.evidence.some((item) => {
      const gate = registry.get(item.launch_gate_id);
      return !gate || gate.gate_code !== item.gate_code
        || gate.verifier_code !== item.verifier_code
        || gate.verifier_version !== item.verifier_version;
    })) invalidResponse("evidence_registry");
  }
  return Object.freeze({ gates, candidate, ...trace(value) });
}

/** Read-only final GA projection. The GA boundary deliberately has a separate
 * endpoint and DTO so RC actions cannot be replayed against a GA launch. */
export async function getGaGates({ controlApiUrl, signal } = {}) {
  const value = await requestJson(controlApiUrl, "/platform/ga-gates", { signal });
  gaExact(value, ["candidate", "evidence", "gates", "launch", "request_id", "trace_id"]);
  if (!Array.isArray(value.gates) || value.gates.length !== 20) invalidResponse("ga_gate_registry");
  const gates = Object.freeze(value.gates.map(gaGateSummary));
  if (new Set(gates.map((item) => item.gate_code)).size !== 20
    || LAUNCH_GATE_CODES.some((code) => !gates.some((item) => item.gate_code === code))) {
    invalidResponse("ga_gate_registry");
  }
  const candidate = value.candidate === null ? null : candidateSummary(value.candidate);
  const launch = value.launch === null ? null : gaLaunchSummary(value.launch);
  responseArray(value.evidence, 20, "ga_evidence");
  const evidence = Object.freeze(value.evidence.map(gaEvidenceSummary));
  const gateIds = new Set(gates.map((item) => item.ga_gate_id));
  if (gateIds.size !== gates.length
    || (launch !== null && launch.launch_candidate_id !== candidate?.launch_candidate_id)) {
    invalidResponse("ga_candidate_projection");
  }
  for (const records of [evidence, launch?.evidence ?? []]) {
    if (new Set(records.map((item) => item.ga_gate_id)).size !== records.length
      || new Set(records.map((item) => item.ga_evidence_id)).size !== records.length
      || new Set(records.map((item) => item.source_evidence_id)).size !== records.length
      || records.some((item) => item.launch_candidate_id !== candidate?.launch_candidate_id
        || !gateIds.has(item.ga_gate_id))) invalidResponse("ga_evidence_projection");
  }
  return Object.freeze({
    gates,
    candidate,
    evidence,
    launch,
    ...trace(value),
  });
}

export async function submitGaEvidence(input = {}) {
  if (input.confirmed !== true || (input.metricValue === null) !== (input.metricUnit === null)) {
    invalidRequest("ga_evidence");
  }
  const body = {
    launch_candidate_id: requestPattern(input.launchCandidateId, ID.candidate, "launch_candidate_id"),
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    workspace_id: requestPattern(input.workspaceId, ID.workspace, "workspace_id"),
    artifact_sha256: requestPattern(input.artifactSha256, SHA256, "artifact_sha256"),
    verifier_code: requestPattern(input.verifierCode, VERIFIER, "verifier_code"),
    verifier_version: 1,
    outcome: requestExact(input.outcome, "passed", "outcome"),
    metric_value: input.metricValue === null ? null : requestFinite(input.metricValue, "metric_value"),
    metric_unit: input.metricUnit === null ? null : requestPattern(input.metricUnit, SAFE_METRIC, "metric_unit"),
    environment_sha256: requestPattern(input.environmentSha256, SHA256, "environment_sha256"),
    provenance_sha256: requestPattern(input.provenanceSha256, SHA256, "provenance_sha256"),
    started_at: requestTimestamp(input.startedAt, "started_at"),
    finished_at: requestTimestamp(input.finishedAt, "finished_at"),
    signature: requestPattern(input.signature, SIGNATURE, "signature"),
    confirmed: true,
  };
  try {
    const value = await requestJson(input.controlApiUrl,
      `/platform/ga-gates/${encodeURIComponent(requestPattern(input.gaGateId, /^gag_[0-9a-f]{32}$/u, "ga_gate_id"))}/evidence`, {
        method: "POST", body, revision: body.expected_revision,
        stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
        idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"), signal: input.signal,
      });
    gaExact(value, ["evidence", "request_id", "trace_id"]);
    return Object.freeze({ evidence: gaEvidenceSummary(value.evidence), ...trace(value) });
  } finally {
    body.signature = "";
  }
}

export async function launchGa(input = {}) {
  if (input.confirmed !== true) invalidRequest("confirmed");
  const body = {
    operation: "launch",
    launch_candidate_id: requestPattern(input.launchCandidateId, ID.candidate, "launch_candidate_id"),
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    confirmed: true,
  };
  const value = await requestJson(input.controlApiUrl, "/platform/ga-launches", {
    method: "POST", body, revision: body.expected_revision,
    stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"), signal: input.signal,
  });
  gaExact(value, ["idempotent_replay", "launch", "request_id", "trace_id"]);
  return Object.freeze({ launch: gaLaunchSummary(value.launch), idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"), ...trace(value) });
}

export async function requestGaRollback(input = {}) {
  const body = {
    operation: "request",
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    trigger_code: requestChoice(input.triggerCode, ROLLBACK_TRIGGERS, "trigger_code"),
    trigger_sha256: requestPattern(input.triggerSha256, SHA256, "trigger_sha256"),
    checkpoint_sha256: requestPattern(input.checkpointSha256, SHA256, "checkpoint_sha256"),
    job_receipt_sha256: requestPattern(input.jobReceiptSha256, SHA256, "job_receipt_sha256"),
    confirmed: true,
  };
  return postGaCommand(input, body);
}

export async function verifyGaRollback(input = {}) {
  const body = {
    operation: "verify",
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    ga_rollback_id: requestPattern(input.gaRollbackId, /^gar_[0-9a-f]{32}$/u, "ga_rollback_id"),
    outcome: requestChoice(input.outcome, ["passed", "failed"], "outcome"),
    verification_sha256: requestPattern(input.verificationSha256, SHA256, "verification_sha256"),
    confirmed: true,
  };
  return postGaCommand(input, body);
}

export async function submitGaReview(input = {}) {
  const body = {
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    summary_sha256: requestPattern(input.summarySha256, SHA256, "summary_sha256"),
    action_items_sha256: requestPattern(input.actionItemsSha256, SHA256, "action_items_sha256"),
    conclusion: requestChoice(input.conclusion, ["ga_launched", "ga_rolled_back"], "conclusion"),
    confirmed: true,
  };
  const launchId = requestPattern(input.gaLaunchId, /^gal_[0-9a-f]{32}$/u, "ga_launch_id");
  const value = await requestJson(input.controlApiUrl, `/platform/ga-launches/${encodeURIComponent(launchId)}/review`, {
    method: "POST", body, revision: body.expected_revision,
    stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"), signal: input.signal,
  });
  gaExact(value, ["idempotent_replay", "launch", "request_id", "review", "trace_id"]);
  return Object.freeze({ review: gaReviewSummary(value.review), launch: gaLaunchSummary(value.launch), idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"), ...trace(value) });
}

export async function createLaunchCandidate({
  controlApiUrl,
  codeTreeSha256,
  migrationSetSha256,
  contractSetSha256,
  testManifestSha256,
  artifacts,
  waves,
  confirmed,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("confirmed");
  const checkedArtifacts = requestArtifacts(artifacts);
  const body = {
    operation: "create",
    code_tree_sha256: requestPattern(codeTreeSha256, SHA256, "code_tree_sha256"),
    migration_set_sha256: requestPattern(
      migrationSetSha256, SHA256, "migration_set_sha256",
    ),
    contract_set_sha256: requestPattern(contractSetSha256, SHA256, "contract_set_sha256"),
    artifact_set_sha256: await launchArtifactSetSha256(checkedArtifacts),
    test_manifest_sha256: requestPattern(
      testManifestSha256, SHA256, "test_manifest_sha256",
    ),
    artifacts: checkedArtifacts,
    waves: requestWaves(waves),
    confirmed: true,
  };
  const result = launchResponse(await requestJson(controlApiUrl, "/platform/launches", {
    method: "POST",
    body,
    revision: 1,
    stepUpGrant: requestPattern(stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    signal,
  }));
  if (result.candidate.code_tree_sha256 !== body.code_tree_sha256
    || result.candidate.migration_set_sha256 !== body.migration_set_sha256
    || result.candidate.contract_set_sha256 !== body.contract_set_sha256
    || result.candidate.artifact_set_sha256 !== body.artifact_set_sha256
    || result.candidate.test_manifest_sha256 !== body.test_manifest_sha256) {
    invalidResponse("candidate_inputs");
  }
  return result;
}

export async function submitLaunchEvidence({
  controlApiUrl,
  launchGateId,
  launchCandidateId,
  expectedRevision,
  workspaceId,
  artifactSha256,
  verifierCode,
  outcome,
  metricValue = null,
  metricUnit = null,
  environmentSha256,
  provenanceSha256,
  startedAt,
  finishedAt,
  signature,
  confirmed,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true || (metricValue === null) !== (metricUnit === null)) {
    invalidRequest("evidence");
  }
  const gateId = requestPattern(launchGateId, ID.gate, "launch_gate_id");
  const revision = requestInteger(expectedRevision, "expected_revision");
  const body = {
    launch_candidate_id: requestPattern(
      launchCandidateId, ID.candidate, "launch_candidate_id",
    ),
    expected_revision: revision,
    workspace_id: requestPattern(workspaceId, ID.workspace, "workspace_id"),
    artifact_sha256: requestPattern(artifactSha256, SHA256, "artifact_sha256"),
    verifier_code: requestPattern(verifierCode, VERIFIER, "verifier_code"),
    verifier_version: 1,
    outcome: requestChoice(outcome, ["passed", "failed"], "outcome"),
    metric_value: metricValue === null ? null : requestFinite(metricValue, "metric_value"),
    metric_unit: metricUnit === null ? null : requestPattern(metricUnit, SAFE_METRIC, "metric_unit"),
    environment_sha256: requestPattern(
      environmentSha256, SHA256, "environment_sha256",
    ),
    provenance_sha256: requestPattern(provenanceSha256, SHA256, "provenance_sha256"),
    started_at: requestTimestamp(startedAt, "started_at"),
    finished_at: requestTimestamp(finishedAt, "finished_at"),
    signature: requestPattern(signature, SIGNATURE, "signature"),
    confirmed: true,
  };
  try {
    const result = evidenceResponse(await requestJson(
      controlApiUrl,
      `/platform/launch-gates/${encodeURIComponent(gateId)}/evidence`,
      {
        method: "POST",
        body,
        revision,
        stepUpGrant: requestPattern(stepUpGrant, OPAQUE, "step_up_grant"),
        idempotencyKey: requestPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
        signal,
      },
    ));
    if (result.evidence.launch_gate_id !== gateId
      || result.evidence.workspace_id !== body.workspace_id
      || result.evidence.artifact_sha256 !== body.artifact_sha256
      || result.evidence.verifier_code !== body.verifier_code
      || result.evidence.outcome !== body.outcome
      || result.candidate.launch_candidate_id !== body.launch_candidate_id) {
      invalidResponse("evidence_owner");
    }
    return result;
  } finally {
    body.signature = "";
  }
}

export async function decideLaunchCandidate(input = {}) {
  const body = candidateCommand(input, "decide", {
    actor_kind: requestChoice(input.actorKind, LAUNCH_ACTORS, "actor_kind"),
    approved: requestBoolean(input.approved, "approved"),
    reason_code: requestPattern(input.reasonCode, SAFE_CODE, "reason_code"),
  });
  const result = await postLaunchCommand(input, body);
  const decision = result.candidate.decisions.find((item) => item.actor_kind === body.actor_kind);
  if (!decision || (decision.decision === "approved") !== body.approved
    || decision.reason_code !== body.reason_code) invalidResponse("decision_projection");
  return result;
}

export async function commandLaunchWave(input = {}) {
  const operation = requestChoice(
    input.operation,
    ["start_wave", "pause_wave", "resume_wave", "complete_wave"],
    "operation",
  );
  const body = candidateCommand(input, operation, {
    wave_kind: requestChoice(input.waveKind, LAUNCH_WAVES, "wave_kind"),
    health_watermark_sha256: operation === "complete_wave"
      ? requestPattern(input.healthWatermarkSha256, SHA256, "health_watermark_sha256")
      : undefined,
  });
  const result = await postLaunchCommand(input, body);
  const wave = result.candidate.waves.find((item) => item.wave_kind === body.wave_kind);
  const expectedStatus = operation === "pause_wave" ? "paused"
    : operation === "complete_wave" ? "completed" : "running";
  if (!wave || wave.status !== expectedStatus
    || (operation === "complete_wave"
      && wave.health_watermark_sha256 !== body.health_watermark_sha256)) {
    invalidResponse("wave_projection");
  }
  return result;
}

export async function reviewLaunchCandidate(input = {}) {
  const body = candidateCommand(input, "review", {
    summary_sha256: requestPattern(input.summarySha256, SHA256, "summary_sha256"),
    action_items_sha256: requestPattern(
      input.actionItemsSha256, SHA256, "action_items_sha256",
    ),
    conclusion: requestExact(input.conclusion, "release_candidate", "conclusion"),
  });
  const result = await postLaunchCommand(input, body);
  if (result.candidate.status !== "release_candidate" || !result.candidate.review
    || result.candidate.review.summary_sha256 !== body.summary_sha256
    || result.candidate.review.action_items_sha256 !== body.action_items_sha256) {
    invalidResponse("review_projection");
  }
  return result;
}

export async function requestLaunchRollback(input = {}) {
  const candidateId = requestPattern(
    input.launchCandidateId, ID.candidate, "launch_candidate_id",
  );
  const revision = requestInteger(input.expectedRevision, "expected_revision");
  const body = {
    operation: "request",
    expected_revision: revision,
    launch_rollback_id: null,
    from_wave_kind: requestChoice(input.fromWaveKind, LAUNCH_WAVES, "from_wave_kind"),
    to_wave_kind: requestChoice(input.toWaveKind, ROLLBACK_TARGETS, "to_wave_kind"),
    trigger_code: requestChoice(input.triggerCode, ROLLBACK_TRIGGERS, "trigger_code"),
    trigger_sha256: requestPattern(input.triggerSha256, SHA256, "trigger_sha256"),
    checkpoint_sha256: requestPattern(input.checkpointSha256, SHA256, "checkpoint_sha256"),
    job_receipt_sha256: requestPattern(
      input.jobReceiptSha256, SHA256, "job_receipt_sha256",
    ),
    outcome: null,
    verification_sha256: null,
    confirmed: requireConfirmed(input.confirmed),
  };
  return postRollback(input, candidateId, revision, body);
}

export async function verifyLaunchRollback(input = {}) {
  const candidateId = requestPattern(
    input.launchCandidateId, ID.candidate, "launch_candidate_id",
  );
  const revision = requestInteger(input.expectedRevision, "expected_revision");
  const body = {
    operation: "verify",
    expected_revision: revision,
    launch_rollback_id: requestPattern(
      input.launchRollbackId, ID.rollback, "launch_rollback_id",
    ),
    from_wave_kind: null,
    to_wave_kind: null,
    trigger_code: null,
    trigger_sha256: null,
    checkpoint_sha256: null,
    job_receipt_sha256: null,
    outcome: requestChoice(input.outcome, ["passed", "failed"], "outcome"),
    verification_sha256: requestPattern(
      input.verificationSha256, SHA256, "verification_sha256",
    ),
    confirmed: requireConfirmed(input.confirmed),
  };
  return postRollback(input, candidateId, revision, body);
}

export async function launchArtifactSetSha256(artifacts) {
  const checked = requestArtifacts(artifacts);
  const canonical = checked
    .map((item) => ({
      artifact_sha256: item.artifact_sha256,
      gate_code: item.gate_code,
      workspace_id: item.workspace_id,
    }))
    .sort((left, right) => left.gate_code.localeCompare(right.gate_code));
  if (!globalThis.crypto?.subtle || typeof globalThis.TextEncoder !== "function") {
    invalidRequest("artifact_set_sha256");
  }
  const bytes = new TextEncoder().encode(JSON.stringify(canonical));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function candidateCommand(input, operation, fields) {
  if (input.confirmed !== true) invalidRequest("confirmed");
  const body = {
    operation,
    launch_candidate_id: requestPattern(
      input.launchCandidateId, ID.candidate, "launch_candidate_id",
    ),
    expected_revision: requestInteger(input.expectedRevision, "expected_revision"),
    ...fields,
    confirmed: true,
  };
  for (const key of Object.keys(body)) if (body[key] === undefined) delete body[key];
  return body;
}

async function postLaunchCommand(input, body) {
  const result = launchResponse(await requestJson(input.controlApiUrl, "/platform/launches", {
    method: "POST",
    body,
    revision: body.expected_revision,
    stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    signal: input.signal,
  }));
  if (result.candidate.launch_candidate_id !== body.launch_candidate_id) {
    invalidResponse("candidate_owner");
  }
  return result;
}

async function postGaCommand(input, body) {
  const launchId = requestPattern(input.gaLaunchId, /^gal_[0-9a-f]{32}$/u, "ga_launch_id");
  const value = await requestJson(input.controlApiUrl, `/platform/ga-launches/${encodeURIComponent(launchId)}/rollback`, {
    method: "POST", body, revision: body.expected_revision,
    stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
    idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"), signal: input.signal,
  });
  gaExact(value, ["idempotent_replay", "launch", "request_id", "rollback", "trace_id"]);
  return Object.freeze({ rollback: gaRollbackSummary(value.rollback), launch: gaLaunchSummary(value.launch), idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"), ...trace(value) });
}

function gaGateSummary(value) {
  gaExact(value, ["blocking_severity", "freshness_seconds", "ga_gate_id", "gate_code", "policy_sha256", "policy_version", "verifier_code", "verifier_version"]);
  return Object.freeze({
    ga_gate_id: responsePattern(value.ga_gate_id, /^gag_[0-9a-f]{32}$/u, "ga_gate_id"),
    gate_code: responseChoice(value.gate_code, LAUNCH_GATE_CODES, "gate_code"),
    policy_version: responseExactInteger(value.policy_version, 1, "policy_version"),
    verifier_code: responsePattern(value.verifier_code, VERIFIER, "verifier_code"),
    verifier_version: responseExactInteger(value.verifier_version, 1, "verifier_version"),
    freshness_seconds: responseInteger(value.freshness_seconds, 300, "freshness_seconds"),
    blocking_severity: responseExact(value.blocking_severity, "p0", "blocking_severity"),
    policy_sha256: responsePattern(value.policy_sha256, SHA256, "policy_sha256"),
  });
}

function gaEvidenceSummary(value) {
  gaExact(value, ["created_at", "expires_at", "finished_at", "ga_evidence_id", "ga_gate_id", "launch_candidate_id", "outcome", "source_evidence_id"]);
  return Object.freeze({
    ga_evidence_id: responsePattern(value.ga_evidence_id, /^gae_[0-9a-f]{32}$/u, "ga_evidence_id"),
    launch_candidate_id: responsePattern(value.launch_candidate_id, ID.candidate, "launch_candidate_id"),
    ga_gate_id: responsePattern(value.ga_gate_id, /^gag_[0-9a-f]{32}$/u, "ga_gate_id"),
    source_evidence_id: responsePattern(value.source_evidence_id, ID.evidence, "source_evidence_id"),
    outcome: responseExact(value.outcome, "passed", "outcome"),
    finished_at: responseTimestamp(value.finished_at, "finished_at"),
    expires_at: responseTimestamp(value.expires_at, "expires_at"),
    created_at: responseTimestamp(value.created_at, "created_at"),
  });
}

function gaLaunchSummary(value) {
  gaExact(value, ["approved_at", "candidate_generation", "created_at", "decisions", "evidence", "ga_launch_id", "launch_candidate_id", "launched_at", "revision", "review", "rollbacks", "status", "updated_at", "workspace_id"]);
  responseArray(value.evidence, 20); responseArray(value.decisions, 6); responseArray(value.rollbacks, 20);
  return Object.freeze({
    ga_launch_id: responsePattern(value.ga_launch_id, /^gal_[0-9a-f]{32}$/u, "ga_launch_id"),
    launch_candidate_id: responsePattern(value.launch_candidate_id, ID.candidate, "launch_candidate_id"),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    status: responseChoice(value.status, ["approved", "launched", "rollback_pending", "rolled_back", "failed"], "ga_status"),
    candidate_generation: responseInteger(value.candidate_generation, 1, "candidate_generation"),
    revision: responseInteger(value.revision, 1, "ga_revision"),
    approved_at: responseTimestamp(value.approved_at, "approved_at"),
    launched_at: nullableTimestamp(value.launched_at, "launched_at"),
    created_at: responseTimestamp(value.created_at, "created_at"),
    updated_at: responseTimestamp(value.updated_at, "updated_at"),
    evidence: Object.freeze(value.evidence.map(gaEvidenceSummary)),
    decisions: Object.freeze(value.decisions.map(gaDecisionSummary)),
    rollbacks: Object.freeze(value.rollbacks.map(gaRollbackSummary)),
    review: value.review === null ? null : gaReviewSummary(value.review),
  });
}

function gaDecisionSummary(value) {
  gaExact(value, ["actor_kind", "decided_at", "ga_decision_id", "operator_id", "source_decision_id"]);
  return Object.freeze({ ga_decision_id: responsePattern(value.ga_decision_id, /^gad_[0-9a-f]{32}$/u,
    "ga_decision_id"),
    actor_kind: responseChoice(value.actor_kind, LAUNCH_ACTORS, "actor_kind"), operator_id: responsePattern(value.operator_id, ID.operator, "operator_id"), source_decision_id: responsePattern(value.source_decision_id, ID.decision, "source_decision_id"), decided_at: responseTimestamp(value.decided_at, "decided_at") });
}

function gaRollbackSummary(value) {
  gaExact(value, ["checkpoint_sha256", "from_status", "ga_launch_id", "ga_rollback_id", "job_receipt_sha256", "requested_at", "revision", "status", "trigger_code", "trigger_sha256", "verification_sha256", "verified_at"]);
  return Object.freeze({ ga_rollback_id: responsePattern(value.ga_rollback_id, /^gar_[0-9a-f]{32}$/u, "ga_rollback_id"), ga_launch_id: responsePattern(value.ga_launch_id, /^gal_[0-9a-f]{32}$/u, "ga_launch_id"), from_status: responseChoice(value.from_status, ["approved", "launched", "rollback_pending"], "from_status"), trigger_code: responseChoice(value.trigger_code, ROLLBACK_TRIGGERS, "trigger_code"), trigger_sha256: responsePattern(value.trigger_sha256, SHA256, "trigger_sha256"), checkpoint_sha256: responsePattern(value.checkpoint_sha256, SHA256, "checkpoint_sha256"), job_receipt_sha256: responsePattern(value.job_receipt_sha256, SHA256, "job_receipt_sha256"), verification_sha256: nullablePattern(value.verification_sha256, SHA256, "verification_sha256"), status: responseChoice(value.status, ["pending", "passed", "failed"], "ga_rollback_status"), revision: responseInteger(value.revision, 1, "ga_rollback_revision"), requested_at: responseTimestamp(value.requested_at, "requested_at"), verified_at: nullableTimestamp(value.verified_at, "verified_at") });
}

function gaReviewSummary(value) {
  gaExact(value, ["action_items_sha256", "conclusion", "ga_launch_id", "post_ga_review_id", "reviewed_at", "summary_sha256"]);
  return Object.freeze({ post_ga_review_id: responsePattern(value.post_ga_review_id, /^pgr_[0-9a-f]{32}$/u, "post_ga_review_id"), ga_launch_id: responsePattern(value.ga_launch_id, /^gal_[0-9a-f]{32}$/u, "ga_launch_id"), summary_sha256: responsePattern(value.summary_sha256, SHA256, "summary_sha256"), action_items_sha256: responsePattern(value.action_items_sha256, SHA256, "action_items_sha256"), conclusion: responseChoice(value.conclusion, ["ga_launched", "ga_rolled_back"], "ga_conclusion"), reviewed_at: responseTimestamp(value.reviewed_at, "reviewed_at") });
}

function gaExact(value, keys) { exact(value, keys); }

async function postRollback(input, candidateId, revision, body) {
  const result = rollbackResponse(await requestJson(
    input.controlApiUrl,
    `/platform/launches/${encodeURIComponent(candidateId)}/rollback`,
    {
      method: "POST",
      body,
      revision,
      stepUpGrant: requestPattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
      idempotencyKey: requestPattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"),
      signal: input.signal,
    },
  ));
  if (result.candidate.launch_candidate_id !== candidateId
    || (body.operation === "verify"
      && (result.rollback.launch_rollback_id !== body.launch_rollback_id
        || result.rollback.status !== body.outcome))
    || (body.operation === "request"
      && (result.rollback.from_wave_kind !== body.from_wave_kind
        || result.rollback.to_wave_kind !== body.to_wave_kind
        || result.rollback.trigger_code !== body.trigger_code
        || result.candidate.status !== "rollback_pending"))) {
    invalidResponse("rollback_owner");
  }
  return result;
}

function launchResponse(value) {
  exact(value, ["candidate", "idempotent_replay", "request_id", "trace_id"]);
  return Object.freeze({
    candidate: candidateSummary(value.candidate),
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

function evidenceResponse(value) {
  exact(value, ["candidate", "evidence", "idempotent_replay", "request_id", "trace_id"]);
  const candidate = candidateSummary(value.candidate);
  const evidence = evidenceSummary(value.evidence);
  if (!candidate.evidence.some((item) => item.launch_evidence_id === evidence.launch_evidence_id)) {
    invalidResponse("evidence_projection");
  }
  return Object.freeze({
    candidate,
    evidence,
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

function rollbackResponse(value) {
  exact(value, ["candidate", "idempotent_replay", "request_id", "rollback", "trace_id"]);
  const candidate = candidateSummary(value.candidate);
  const rollback = rollbackSummary(value.rollback);
  if (!candidate.rollbacks.some((item) => item.launch_rollback_id === rollback.launch_rollback_id)) {
    invalidResponse("rollback_projection");
  }
  return Object.freeze({
    candidate,
    rollback,
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

function gateSummary(value) {
  exact(value, [
    "blocking_severity", "freshness_seconds", "gate_code", "launch_gate_id",
    "policy_sha256", "policy_version", "threshold_rule", "verifier_code",
    "verifier_version",
  ]);
  return Object.freeze({
    launch_gate_id: responsePattern(value.launch_gate_id, ID.gate, "launch_gate_id"),
    gate_code: responseChoice(value.gate_code, LAUNCH_GATE_CODES, "gate_code"),
    policy_version: responseExactInteger(value.policy_version, 1, "policy_version"),
    verifier_code: responsePattern(value.verifier_code, VERIFIER, "verifier_code"),
    verifier_version: responseExactInteger(value.verifier_version, 1, "verifier_version"),
    threshold_rule: responseExact(value.threshold_rule, "exact_pass", "threshold_rule"),
    freshness_seconds: responseInteger(value.freshness_seconds, 300, "freshness_seconds"),
    blocking_severity: responseExact(value.blocking_severity, "p0", "blocking_severity"),
    policy_sha256: responsePattern(value.policy_sha256, SHA256, "policy_sha256"),
  });
}

function candidateSummary(value) {
  exact(value, [
    "artifact_set_sha256", "candidate_sha256", "code_tree_sha256", "completed_at",
    "contract_set_sha256", "created_at", "decisions", "evidence", "gate_policy_set_sha256",
    "generation", "launch_candidate_id", "migration_set_sha256", "review", "revision",
    "rollbacks", "status", "test_manifest_sha256", "updated_at", "waves",
  ]);
  responseArray(value.evidence, 20, "evidence");
  responseArray(value.decisions, 6, "decisions");
  responseArray(value.waves, 4, "waves", 4);
  responseArray(value.rollbacks, 20, "rollbacks");
  const result = {
    launch_candidate_id: responsePattern(
      value.launch_candidate_id, ID.candidate, "launch_candidate_id",
    ),
    candidate_sha256: responsePattern(value.candidate_sha256, SHA256, "candidate_sha256"),
    code_tree_sha256: responsePattern(value.code_tree_sha256, SHA256, "code_tree_sha256"),
    migration_set_sha256: responsePattern(
      value.migration_set_sha256, SHA256, "migration_set_sha256",
    ),
    contract_set_sha256: responsePattern(
      value.contract_set_sha256, SHA256, "contract_set_sha256",
    ),
    artifact_set_sha256: responsePattern(
      value.artifact_set_sha256, SHA256, "artifact_set_sha256",
    ),
    test_manifest_sha256: responsePattern(
      value.test_manifest_sha256, SHA256, "test_manifest_sha256",
    ),
    gate_policy_set_sha256: responsePattern(
      value.gate_policy_set_sha256, SHA256, "gate_policy_set_sha256",
    ),
    generation: responseInteger(value.generation, 1, "generation"),
    status: responseChoice(value.status, CANDIDATE_STATUSES, "candidate_status"),
    revision: responseInteger(value.revision, 1, "candidate_revision"),
    created_at: responseTimestamp(value.created_at, "created_at"),
    updated_at: responseTimestamp(value.updated_at, "updated_at"),
    completed_at: nullableTimestamp(value.completed_at, "completed_at"),
    evidence: Object.freeze(value.evidence.map(evidenceSummary)),
    decisions: Object.freeze(value.decisions.map(decisionSummary)),
    waves: Object.freeze(value.waves.map(waveSummary)),
    rollbacks: Object.freeze(value.rollbacks.map(rollbackSummary)),
    review: value.review === null ? null : reviewSummary(value.review),
  };
  if (new Set(result.evidence.map((item) => item.gate_code)).size !== result.evidence.length
    || new Set(result.decisions.map((item) => item.actor_kind)).size !== result.decisions.length
    || new Set(result.decisions.map((item) => item.operator_id)).size !== result.decisions.length
    || result.evidence.some((item) => (
      (item.metric_value === null) !== (item.metric_unit === null)
    ))
    || new Set(result.waves.map((item) => item.wave_kind)).size !== 4
    || result.waves.some((item) => LAUNCH_WAVES[item.wave_sequence - 1] !== item.wave_kind)
    || (result.review !== null && (result.status !== "release_candidate"
      || result.review.conclusion !== "release_candidate"))) invalidResponse("candidate_projection");
  return Object.freeze(result);
}

function evidenceSummary(value) {
  exact(value, [
    "artifact_sha256", "created_at", "envelope_sha256", "environment_sha256", "expires_at",
    "finished_at", "gate_code", "launch_evidence_id", "launch_gate_id", "metric_unit",
    "metric_value", "outcome", "provenance_sha256", "started_at", "verifier_code",
    "verifier_version", "workspace_id",
  ]);
  return Object.freeze({
    launch_evidence_id: responsePattern(value.launch_evidence_id, ID.evidence, "evidence_id"),
    launch_gate_id: responsePattern(value.launch_gate_id, ID.gate, "launch_gate_id"),
    gate_code: responseChoice(value.gate_code, LAUNCH_GATE_CODES, "gate_code"),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    artifact_sha256: responsePattern(value.artifact_sha256, SHA256, "artifact_sha256"),
    verifier_code: responsePattern(value.verifier_code, VERIFIER, "verifier_code"),
    verifier_version: responseExactInteger(value.verifier_version, 1, "verifier_version"),
    envelope_sha256: responsePattern(value.envelope_sha256, SHA256, "envelope_sha256"),
    outcome: responseChoice(value.outcome, ["passed", "failed"], "evidence_outcome"),
    metric_value: nullableFinite(value.metric_value, "metric_value"),
    metric_unit: nullablePattern(value.metric_unit, SAFE_METRIC, "metric_unit"),
    environment_sha256: responsePattern(
      value.environment_sha256, SHA256, "environment_sha256",
    ),
    provenance_sha256: responsePattern(value.provenance_sha256, SHA256, "provenance_sha256"),
    started_at: responseTimestamp(value.started_at, "started_at"),
    finished_at: responseTimestamp(value.finished_at, "finished_at"),
    expires_at: responseTimestamp(value.expires_at, "expires_at"),
    created_at: responseTimestamp(value.created_at, "created_at"),
  });
}

function decisionSummary(value) {
  exact(value, [
    "actor_kind", "decided_at", "decision", "decision_sha256", "go_no_go_decision_id",
    "operator_id", "operator_revision", "reason_code", "signature_sha256",
  ]);
  return Object.freeze({
    go_no_go_decision_id: responsePattern(value.go_no_go_decision_id, ID.decision, "decision_id"),
    actor_kind: responseChoice(value.actor_kind, LAUNCH_ACTORS, "actor_kind"),
    operator_id: responsePattern(value.operator_id, ID.operator, "operator_id"),
    operator_revision: responseInteger(value.operator_revision, 1, "operator_revision"),
    decision: responseChoice(value.decision, ["approved", "rejected"], "decision"),
    reason_code: responsePattern(value.reason_code, SAFE_CODE, "reason_code"),
    decision_sha256: responsePattern(value.decision_sha256, SHA256, "decision_sha256"),
    signature_sha256: responsePattern(value.signature_sha256, SHA256, "signature_sha256"),
    decided_at: responseTimestamp(value.decided_at, "decided_at"),
  });
}

function waveSummary(value) {
  exact(value, [
    "cohort_manifest_sha256", "completed_at", "feature_flag_generation",
    "health_watermark_sha256", "launch_wave_id", "migration_plan_sha256",
    "observation_deadline_at", "observation_seconds", "revision", "rollback_plan_sha256",
    "started_at", "status", "wave_kind", "wave_sequence", "workspace_id",
  ]);
  return Object.freeze({
    launch_wave_id: responsePattern(value.launch_wave_id, ID.wave, "launch_wave_id"),
    wave_kind: responseChoice(value.wave_kind, LAUNCH_WAVES, "wave_kind"),
    wave_sequence: responseInteger(value.wave_sequence, 1, "wave_sequence", 4),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    cohort_manifest_sha256: responsePattern(
      value.cohort_manifest_sha256, SHA256, "cohort_manifest_sha256",
    ),
    feature_flag_generation: responseInteger(
      value.feature_flag_generation, 1, "feature_flag_generation",
    ),
    migration_plan_sha256: responsePattern(
      value.migration_plan_sha256, SHA256, "migration_plan_sha256",
    ),
    rollback_plan_sha256: responsePattern(
      value.rollback_plan_sha256, SHA256, "rollback_plan_sha256",
    ),
    observation_seconds: responseInteger(value.observation_seconds, 60, "observation_seconds"),
    status: responseChoice(value.status, WAVE_STATUSES, "wave_status"),
    observation_deadline_at: nullableTimestamp(
      value.observation_deadline_at, "observation_deadline_at",
    ),
    health_watermark_sha256: nullablePattern(
      value.health_watermark_sha256, SHA256, "health_watermark_sha256",
    ),
    revision: responseInteger(value.revision, 1, "wave_revision"),
    started_at: nullableTimestamp(value.started_at, "started_at"),
    completed_at: nullableTimestamp(value.completed_at, "completed_at"),
  });
}

function rollbackSummary(value) {
  exact(value, [
    "checkpoint_sha256", "from_wave_kind", "job_receipt_sha256", "launch_rollback_id",
    "requested_at", "revision", "status", "to_wave_kind", "trigger_code", "trigger_sha256",
    "verification_sha256", "verified_at", "workspace_id",
  ]);
  return Object.freeze({
    launch_rollback_id: responsePattern(value.launch_rollback_id, ID.rollback, "rollback_id"),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    from_wave_kind: responseChoice(value.from_wave_kind, LAUNCH_WAVES, "from_wave_kind"),
    to_wave_kind: responseChoice(value.to_wave_kind, ROLLBACK_TARGETS, "to_wave_kind"),
    trigger_code: responseChoice(value.trigger_code, ROLLBACK_TRIGGERS, "trigger_code"),
    trigger_sha256: responsePattern(value.trigger_sha256, SHA256, "trigger_sha256"),
    checkpoint_sha256: responsePattern(value.checkpoint_sha256, SHA256, "checkpoint_sha256"),
    job_receipt_sha256: responsePattern(
      value.job_receipt_sha256, SHA256, "job_receipt_sha256",
    ),
    verification_sha256: nullablePattern(
      value.verification_sha256, SHA256, "verification_sha256",
    ),
    status: responseChoice(value.status, ["pending", "passed", "failed"], "rollback_status"),
    revision: responseInteger(value.revision, 1, "rollback_revision"),
    requested_at: responseTimestamp(value.requested_at, "requested_at"),
    verified_at: nullableTimestamp(value.verified_at, "verified_at"),
  });
}

function reviewSummary(value) {
  exact(value, [
    "action_items_sha256", "conclusion", "post_launch_review_id", "reviewed_at",
    "summary_sha256", "workspace_id",
  ]);
  return Object.freeze({
    post_launch_review_id: responsePattern(value.post_launch_review_id, ID.review, "review_id"),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    summary_sha256: responsePattern(value.summary_sha256, SHA256, "summary_sha256"),
    action_items_sha256: responsePattern(
      value.action_items_sha256, SHA256, "action_items_sha256",
    ),
    conclusion: responseExact(value.conclusion, "release_candidate", "conclusion"),
    reviewed_at: responseTimestamp(value.reviewed_at, "reviewed_at"),
  });
}

function requestArtifacts(value) {
  if (!Array.isArray(value) || value.length !== 20) invalidRequest("artifacts");
  const result = value.map((item) => {
    plain(item, "artifact", invalidRequest);
    exact(item, ["artifact_sha256", "gate_code", "workspace_id"], invalidRequest);
    return {
      gate_code: requestChoice(item.gate_code, LAUNCH_GATE_CODES, "gate_code"),
      workspace_id: requestPattern(item.workspace_id, ID.workspace, "workspace_id"),
      artifact_sha256: requestPattern(item.artifact_sha256, SHA256, "artifact_sha256"),
    };
  });
  if (new Set(result.map((item) => item.gate_code)).size !== 20
    || LAUNCH_GATE_CODES.some((code) => !result.some((item) => item.gate_code === code))) {
    invalidRequest("artifact_registry");
  }
  return result;
}

function requestWaves(value) {
  if (!Array.isArray(value) || value.length !== 4) invalidRequest("waves");
  const result = value.map((item) => {
    plain(item, "wave", invalidRequest);
    exact(item, [
      "cohort_manifest_sha256", "feature_flag_generation", "migration_plan_sha256",
      "observation_seconds", "rollback_plan_sha256", "wave_kind", "workspace_id",
    ], invalidRequest);
    return {
      wave_kind: requestChoice(item.wave_kind, LAUNCH_WAVES, "wave_kind"),
      workspace_id: requestPattern(item.workspace_id, ID.workspace, "workspace_id"),
      cohort_manifest_sha256: requestPattern(
        item.cohort_manifest_sha256, SHA256, "cohort_manifest_sha256",
      ),
      feature_flag_generation: requestInteger(
        item.feature_flag_generation, "feature_flag_generation",
      ),
      migration_plan_sha256: requestPattern(
        item.migration_plan_sha256, SHA256, "migration_plan_sha256",
      ),
      rollback_plan_sha256: requestPattern(
        item.rollback_plan_sha256, SHA256, "rollback_plan_sha256",
      ),
      observation_seconds: requestInteger(
        item.observation_seconds, "observation_seconds", 604800, 60,
      ),
    };
  });
  if (new Set(result.map((item) => item.wave_kind)).size !== 4
    || LAUNCH_WAVES.some((kind) => !result.some((item) => item.wave_kind === kind))) {
    invalidRequest("wave_registry");
  }
  return result;
}

async function requestJson(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.method === "POST") {
    Object.assign(headers, {
      "Content-Type": "application/json",
      "Idempotency-Key": options.idempotencyKey,
      "If-Match": `"${options.revision}"`,
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "X-Step-Up-Grant": options.stepUpGrant,
    });
  }
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({
      status: 0,
      code: "launch.unavailable",
      message: "Launch governance is unavailable",
    });
  }
  const payload = await readJson(response);
  if (!response.ok) throw serverError(response, payload);
  requirePrivateNoStore(response);
  return payload;
}

async function readJson(response) {
  if (!/^application\/json(?:\s*;|$)/iu.test(response.headers.get("Content-Type") ?? "")) {
    invalidResponse("content_type");
  }
  try {
    const value = await response.json();
    plain(value, "payload");
    return value;
  } catch (error) {
    if (error instanceof PlatformApiError) throw error;
    invalidResponse("json");
  }
}

function serverError(response, payload) {
  const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
    ? payload.code : "launch.request_invalid";
  return new PlatformApiError({
    status: response.status,
    code,
    message: safeText(payload?.message) || "Request failed",
    requestId: safeText(payload?.request_id) || "-",
  });
}

function requirePrivateNoStore(response) {
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  if (!directives.includes("private") || !directives.includes("no-store")) {
    invalidResponse("cache_control");
  }
}

function apiUrl(baseUrl, path) {
  let base;
  try { base = new URL(String(baseUrl ?? "")); } catch { invalidRequest("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password
    || base.search || base.hash || (base.pathname !== "/" && base.pathname !== "")) {
    invalidRequest("control_api_url");
  }
  return new URL(path, base).toString();
}

function exact(value, keys, fail = invalidResponse) {
  plain(value, "object", fail);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) fail("keys");
}

function plain(value, label, fail = invalidResponse) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) fail(label);
}

function requestPattern(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) invalidRequest(label);
  return value;
}

function requestChoice(value, choices, label) {
  if (!choices.includes(value)) invalidRequest(label);
  return value;
}

function requestExact(value, expected, label) {
  if (value !== expected) invalidRequest(label);
  return value;
}

function requestBoolean(value, label) {
  if (typeof value !== "boolean") invalidRequest(label);
  return value;
}

function requestInteger(value, label, maximum = Number.MAX_SAFE_INTEGER, minimum = 1) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < minimum || number > maximum) {
    invalidRequest(label);
  }
  return number;
}

function requestFinite(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number)) invalidRequest(label);
  return number;
}

function requestTimestamp(value, label) {
  if (typeof value !== "string" || value.length < 20 || value.length > 40
    || !Number.isFinite(Date.parse(value)) || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) {
    invalidRequest(label);
  }
  return value;
}

function requireConfirmed(value) {
  if (value !== true) invalidRequest("confirmed");
  return true;
}

function responsePattern(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) invalidResponse(label);
  return value;
}

function nullablePattern(value, pattern, label) {
  return value === null ? null : responsePattern(value, pattern, label);
}

function responseChoice(value, choices, label) {
  if (!choices.includes(value)) invalidResponse(label);
  return value;
}

function responseExact(value, expected, label) {
  if (value !== expected) invalidResponse(label);
  return value;
}

function responseInteger(value, minimum, label, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidResponse(label);
  return value;
}

function responseExactInteger(value, expected, label) {
  if (value !== expected) invalidResponse(label);
  return value;
}

function responseBoolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}

function responseArray(value, maximum, label, minimum = 0) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    invalidResponse(label);
  }
}

function responseTimestamp(value, label) {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))
    || !/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value)) invalidResponse(label);
  return value;
}

function nullableTimestamp(value, label) {
  return value === null ? null : responseTimestamp(value, label);
}

function nullableFinite(value, label) {
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) invalidResponse(label);
  return value;
}

function trace(value) {
  return {
    request_id: responsePattern(value.request_id, SAFE_TRACE, "request_id"),
    trace_id: responsePattern(value.trace_id, SAFE_TRACE, "trace_id"),
  };
}

function invalidRequest(label) {
  throw new PlatformApiError({
    status: 400,
    code: "launch.request_invalid",
    message: `Invalid ${label}`,
  });
}

function invalidResponse(label) {
  throw new PlatformApiError({
    status: 502,
    code: "launch.response_invalid",
    message: `Invalid ${label}`,
  });
}

function safeText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
