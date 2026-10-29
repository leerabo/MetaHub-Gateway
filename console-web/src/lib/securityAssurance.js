/** Strict PRD-137 Platform client for fixed-profile security assurance. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const ID = Object.freeze({
  assessment: /^sas_[0-9a-f]{32}$/u,
  run: /^itr_[0-9a-f]{32}$/u,
  finding: /^ptf_[0-9a-f]{32}$/u,
  remediation: /^rem_[0-9a-f]{32}$/u,
  risk: /^rsk_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9._-]{1,79}$/u;
const SAFE_TRACE = /^[A-Za-z0-9_.:-]{1,160}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;
const PROFILES = ["incremental_release", "quarterly_full"];
const SCENARIOS = [
  "api_idor", "write_authorization", "cache_owner", "queue_owner", "object_prefix",
  "metrics_labels", "dynamic_revocation", "replay_fencing", "late_response", "input_fuzz",
];
const TARGETS = ["public_contract", "cache", "queue", "object_store", "metrics", "authorization"];
const OWNERS = [
  "workspace_resource", "asynchronous_job", "cached_projection", "object_artifact",
  "metric_series", "workspace_session",
];

export async function startSecurityAssessment({
  controlApiUrl,
  workspaceId,
  profileCode,
  profileVersion = 1,
  codeTreeSha256,
  migrationSetSha256,
  contractSetSha256,
  artifactSha256,
  confirmed,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true || profileVersion !== 1) invalidRequest("assessment");
  const body = {
    workspace_id: requestPattern(workspaceId, ID.workspace, "workspace_id"),
    profile_code: requestChoice(profileCode, PROFILES, "profile_code"),
    profile_version: 1,
    code_tree_sha256: requestPattern(codeTreeSha256, SHA256, "code_tree_sha256"),
    migration_set_sha256: requestPattern(
      migrationSetSha256, SHA256, "migration_set_sha256",
    ),
    contract_set_sha256: requestPattern(contractSetSha256, SHA256, "contract_set_sha256"),
    artifact_sha256: requestPattern(artifactSha256, SHA256, "artifact_sha256"),
    confirmed: true,
  };
  const result = assessmentResponse(await requestJson(
    controlApiUrl,
    "/platform/security/assessments",
    {
      method: "POST",
      body,
      revision: 1,
      stepUpGrant: requestPattern(stepUpGrant, OPAQUE, "step_up_grant"),
      idempotencyKey: requestPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
      signal,
    },
  ));
  if (result.assessment.workspace_id !== body.workspace_id
    || result.assessment.profile_code !== body.profile_code
    || result.assessment.profile_version !== body.profile_version
    || result.assessment.code_tree_sha256 !== body.code_tree_sha256
    || result.assessment.migration_set_sha256 !== body.migration_set_sha256
    || result.assessment.contract_set_sha256 !== body.contract_set_sha256
    || result.assessment.artifact_sha256 !== body.artifact_sha256) {
    invalidResponse("assessment_owner");
  }
  return result;
}

export async function getSecurityAssessment({ controlApiUrl, assessmentId, signal } = {}) {
  const id = requestPattern(assessmentId, ID.assessment, "assessment_id");
  const result = assessmentResponse(await requestJson(
    controlApiUrl,
    `/platform/security/assessments/${encodeURIComponent(id)}`,
    { signal },
  ));
  if (result.assessment.security_assessment_id !== id) invalidResponse("assessment_owner");
  return result;
}

export async function applySecurityRemediation({
  controlApiUrl,
  findingId,
  operation,
  expectedRevision,
  changeSha256,
  remediationId,
  verificationAssessmentId,
  confirmed,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  if (confirmed !== true) invalidRequest("confirmed");
  const finding = requestPattern(findingId, ID.finding, "finding_id");
  const checkedOperation = requestChoice(operation, ["propose", "verify"], "operation");
  const revision = requestInteger(expectedRevision, "expected_revision");
  const body = checkedOperation === "propose"
    ? {
      operation: "propose",
      expected_revision: revision,
      change_sha256: requestPattern(changeSha256, SHA256, "change_sha256"),
      remediation_id: null,
      verification_assessment_id: null,
      confirmed: true,
    }
    : {
      operation: "verify",
      expected_revision: revision,
      change_sha256: null,
      remediation_id: requestPattern(remediationId, ID.remediation, "remediation_id"),
      verification_assessment_id: requestPattern(
        verificationAssessmentId, ID.assessment, "verification_assessment_id",
      ),
      confirmed: true,
    };
  const value = await requestJson(
    controlApiUrl,
    `/platform/security/findings/${encodeURIComponent(finding)}/remediations`,
    {
      method: "POST",
      body,
      revision,
      stepUpGrant: requestPattern(stepUpGrant, OPAQUE, "step_up_grant"),
      idempotencyKey: requestPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
      signal,
    },
  );
  exact(value, ["finding", "idempotent_replay", "remediation", "request_id", "trace_id"]);
  const result = Object.freeze({
    finding: findingSummary(value.finding),
    remediation: remediationSummary(value.remediation),
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
  if (result.finding.pen_test_finding_id !== finding
    || !result.finding.remediations.some((item) => (
      item.remediation_id === result.remediation.remediation_id
    ))
    || (checkedOperation === "propose"
      && (result.remediation.outcome !== "proposed"
        || result.remediation.change_sha256 !== body.change_sha256))
    || (checkedOperation === "verify"
      && result.remediation.verification_assessment_id
        !== body.verification_assessment_id)) invalidResponse("remediation_owner");
  return result;
}

function assessmentResponse(value) {
  exact(value, ["assessment", "idempotent_replay", "request_id", "trace_id"]);
  return Object.freeze({
    assessment: assessmentSummary(value.assessment),
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay"),
    ...trace(value),
  });
}

function assessmentSummary(value) {
  exact(value, [
    "artifact_sha256", "code_tree_sha256", "contract_set_sha256", "environment_receipt_sha256",
    "findings", "finished_at", "generation", "migration_set_sha256", "profile_code",
    "profile_sha256", "profile_signature_sha256", "profile_version", "requested_at", "revision",
    "runs", "scope_sha256", "security_assessment_id", "started_at", "status", "summary_sha256",
    "workspace_id",
  ]);
  responseArray(value.runs, 10, "runs");
  responseArray(value.findings, 10, "findings");
  const result = {
    security_assessment_id: responsePattern(
      value.security_assessment_id, ID.assessment, "security_assessment_id",
    ),
    workspace_id: responsePattern(value.workspace_id, ID.workspace, "workspace_id"),
    profile_code: responseChoice(value.profile_code, PROFILES, "profile_code"),
    profile_version: responseExactInteger(value.profile_version, 1, "profile_version"),
    profile_sha256: responsePattern(value.profile_sha256, SHA256, "profile_sha256"),
    profile_signature_sha256: responsePattern(
      value.profile_signature_sha256, SHA256, "profile_signature_sha256",
    ),
    code_tree_sha256: responsePattern(value.code_tree_sha256, SHA256, "code_tree_sha256"),
    migration_set_sha256: responsePattern(
      value.migration_set_sha256, SHA256, "migration_set_sha256",
    ),
    contract_set_sha256: responsePattern(
      value.contract_set_sha256, SHA256, "contract_set_sha256",
    ),
    artifact_sha256: responsePattern(value.artifact_sha256, SHA256, "artifact_sha256"),
    scope_sha256: responsePattern(value.scope_sha256, SHA256, "scope_sha256"),
    status: responseChoice(value.status, ["queued", "running", "passed", "failed"], "status"),
    generation: responseInteger(value.generation, 1, "generation"),
    environment_receipt_sha256: nullablePattern(
      value.environment_receipt_sha256, SHA256, "environment_receipt_sha256",
    ),
    summary_sha256: nullablePattern(value.summary_sha256, SHA256, "summary_sha256"),
    revision: responseInteger(value.revision, 1, "revision"),
    requested_at: responseTimestamp(value.requested_at, "requested_at"),
    started_at: nullableTimestamp(value.started_at, "started_at"),
    finished_at: nullableTimestamp(value.finished_at, "finished_at"),
    runs: Object.freeze(value.runs.map(runSummary)),
    findings: Object.freeze(value.findings.map(findingSummary)),
  };
  if (new Set(result.runs.map((item) => item.scenario_code)).size !== result.runs.length
    || result.runs.some((item) => !SCENARIOS.includes(item.scenario_code))) {
    invalidResponse("run_registry");
  }
  const runs = new Map(result.runs.map((item) => [item.isolation_test_run_id, item]));
  if (result.findings.some((item) => {
    const run = runs.get(item.isolation_test_run_id);
    return !run || item.owner_kind !== run.owner_kind || item.contract_code !== run.contract_code
      || item.weakness_code !== run.weakness_code;
  })) invalidResponse("finding_owner");
  return Object.freeze(result);
}

function runSummary(value) {
  exact(value, [
    "cleanup_receipt_sha256", "contract_code", "duration_ms", "evidence_sha256", "finished_at",
    "isolation_test_run_id", "outcome", "owner_kind", "owner_registry_version",
    "runner_image_sha256", "scenario_code", "started_at", "target_kind", "weakness_code",
  ]);
  return Object.freeze({
    isolation_test_run_id: responsePattern(value.isolation_test_run_id, ID.run, "run_id"),
    scenario_code: responseChoice(value.scenario_code, SCENARIOS, "scenario_code"),
    target_kind: responseChoice(value.target_kind, TARGETS, "target_kind"),
    owner_kind: responseChoice(value.owner_kind, OWNERS, "owner_kind"),
    contract_code: responsePattern(value.contract_code, SAFE_CODE, "contract_code"),
    weakness_code: responsePattern(value.weakness_code, SAFE_CODE, "weakness_code"),
    owner_registry_version: responseExactInteger(
      value.owner_registry_version, 1, "owner_registry_version",
    ),
    runner_image_sha256: responsePattern(
      value.runner_image_sha256, SHA256, "runner_image_sha256",
    ),
    outcome: responseChoice(value.outcome, ["passed", "failed", "error"], "outcome"),
    duration_ms: responseInteger(value.duration_ms, 0, "duration_ms", 1_800_000),
    evidence_sha256: responsePattern(value.evidence_sha256, SHA256, "evidence_sha256"),
    cleanup_receipt_sha256: responsePattern(
      value.cleanup_receipt_sha256, SHA256, "cleanup_receipt_sha256",
    ),
    started_at: responseTimestamp(value.started_at, "started_at"),
    finished_at: responseTimestamp(value.finished_at, "finished_at"),
  });
}

function findingSummary(value) {
  exact(value, [
    "closed_at", "contract_code", "evidence_sha256", "first_seen_at", "isolation_test_run_id",
    "last_seen_at", "owner_kind", "pen_test_finding_id", "remediations", "revision",
    "risk_acceptances", "severity", "sla_deadline_at", "status", "weakness_code",
  ]);
  responseArray(value.remediations, 100, "remediations");
  responseArray(value.risk_acceptances, 20, "risk_acceptances");
  return Object.freeze({
    pen_test_finding_id: responsePattern(value.pen_test_finding_id, ID.finding, "finding_id"),
    isolation_test_run_id: responsePattern(value.isolation_test_run_id, ID.run, "run_id"),
    owner_kind: responseChoice(value.owner_kind, OWNERS, "owner_kind"),
    contract_code: responsePattern(value.contract_code, SAFE_CODE, "contract_code"),
    severity: responseChoice(value.severity, ["p0", "p1", "p2", "p3"], "severity"),
    weakness_code: responsePattern(value.weakness_code, SAFE_CODE, "weakness_code"),
    status: responseChoice(
      value.status, ["open", "remediation_pending", "closed", "risk_accepted"], "status",
    ),
    sla_deadline_at: responseTimestamp(value.sla_deadline_at, "sla_deadline_at"),
    evidence_sha256: responsePattern(value.evidence_sha256, SHA256, "evidence_sha256"),
    revision: responseInteger(value.revision, 1, "revision"),
    first_seen_at: responseTimestamp(value.first_seen_at, "first_seen_at"),
    last_seen_at: responseTimestamp(value.last_seen_at, "last_seen_at"),
    closed_at: nullableTimestamp(value.closed_at, "closed_at"),
    remediations: Object.freeze(value.remediations.map(remediationSummary)),
    risk_acceptances: Object.freeze(value.risk_acceptances.map(riskSummary)),
  });
}

function remediationSummary(value) {
  exact(value, [
    "change_sha256", "created_at", "finding_revision", "outcome", "remediation_id",
    "verification_assessment_id", "verified_at",
  ]);
  return Object.freeze({
    remediation_id: responsePattern(value.remediation_id, ID.remediation, "remediation_id"),
    change_sha256: responsePattern(value.change_sha256, SHA256, "change_sha256"),
    verification_assessment_id: nullablePattern(
      value.verification_assessment_id, ID.assessment, "verification_assessment_id",
    ),
    outcome: responseChoice(
      value.outcome, ["proposed", "verification_failed", "verified_passed"], "outcome",
    ),
    finding_revision: responseInteger(value.finding_revision, 1, "finding_revision"),
    created_at: responseTimestamp(value.created_at, "created_at"),
    verified_at: nullableTimestamp(value.verified_at, "verified_at"),
  });
}

function riskSummary(value) {
  exact(value, [
    "approved_at", "compensating_controls", "expires_at", "revision", "risk_acceptance_id",
    "scope_sha256", "status", "updated_at",
  ]);
  responseArray(value.compensating_controls, 4, "compensating_controls", 1);
  return Object.freeze({
    risk_acceptance_id: responsePattern(value.risk_acceptance_id, ID.risk, "risk_id"),
    scope_sha256: responsePattern(value.scope_sha256, SHA256, "scope_sha256"),
    compensating_controls: Object.freeze(value.compensating_controls.map((item) => (
      responseChoice(item, [
        "network_isolation", "runtime_detection", "feature_disablement", "rate_limit",
      ], "compensating_control")
    ))),
    status: responseChoice(value.status, [
      "pending_owner", "pending_security", "pending_platform", "active", "expired", "revoked",
    ], "risk_status"),
    revision: responseInteger(value.revision, 1, "risk_revision"),
    expires_at: responseTimestamp(value.expires_at, "expires_at"),
    approved_at: nullableTimestamp(value.approved_at, "approved_at"),
    updated_at: responseTimestamp(value.updated_at, "updated_at"),
  });
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
      code: "security_assurance.unavailable",
      message: "Security assurance is unavailable",
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
    ? payload.code : "security_assurance.request_invalid";
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

function exact(value, keys) {
  plain(value, "object");
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse("keys");
}

function plain(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(label);
}

function requestPattern(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) invalidRequest(label);
  return value;
}

function requestChoice(value, choices, label) {
  if (!choices.includes(value)) invalidRequest(label);
  return value;
}

function requestInteger(value, label) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 1) invalidRequest(label);
  return number;
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

function trace(value) {
  return {
    request_id: responsePattern(value.request_id, SAFE_TRACE, "request_id"),
    trace_id: responsePattern(value.trace_id, SAFE_TRACE, "trace_id"),
  };
}

function invalidRequest(label) {
  throw new PlatformApiError({
    status: 400,
    code: "security_assurance.request_invalid",
    message: `Invalid ${label}`,
  });
}

function invalidResponse(label) {
  throw new PlatformApiError({
    status: 502,
    code: "security_assurance.response_invalid",
    message: `Invalid ${label}`,
  });
}

function safeText(value) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240) : "";
}
