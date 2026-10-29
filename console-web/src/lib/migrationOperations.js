/** PRD-131 strict Workspace migration assessment, cutover, and rollback client. */

import { AuthApiError } from "./auth.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  assessment: /^mas_[0-9a-f]{32}$/u,
  plan: /^mpl_[0-9a-f]{32}$/u,
  run: /^mrn_[0-9a-f]{32}$/u,
  difference: /^mdf_[0-9a-f]{32}$/u,
  checkpoint: /^mcp_[0-9a-f]{32}$/u,
  rollbackPlan: /^mrp_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const CHAIN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const STEP_UP_GRANT = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,4095}$/u;
const MANIFEST_VERSION = /^[a-z0-9][a-z0-9._-]{2,39}$/u;
const SAFE_ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u;
const SOURCES = Object.freeze(["legacy_tenant", "direct_provider", "private_deployment"]);
const DOMAINS = Object.freeze([
  "audit", "edge", "identity", "ledger", "policy", "provider_reference", "workspace",
]);
const ASSESSMENT_REASONS = Object.freeze([
  "initial_assessment", "reassessment", "cutover_precheck",
]);
const RUN_REASONS = Object.freeze(["customer_approved", "scheduled_window", "recovery_retry"]);
const CUTOVER_REASONS = Object.freeze([
  "customer_approved", "scheduled_window", "validation_recovered",
]);
const ROLLBACK_REASONS = Object.freeze([
  "customer_requested_rollback", "validation_failed", "observation_failed",
]);
const PLAN_PHASES = Object.freeze([
  "assessment", "backfill", "dual_read", "freeze", "cutover", "observe",
]);
const CHECKPOINT_PHASES = Object.freeze([
  "assessment", "backfill", "dual_read", "freeze", "cutover", "rollback",
]);
const COMPENSATION_ORDER = Object.freeze([
  "freeze_writes", "restore_head", "replay_receipts", "verify_owner",
]);

export async function createMigrationAssessment({
  controlApiUrl,
  sourceKind,
  manifestVersion,
  sourceManifestSha256,
  domains,
  reasonCode,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  const body = {
    source_kind: clientChoice(sourceKind, SOURCES, "source_kind"),
    manifest_version: clientPattern(manifestVersion, MANIFEST_VERSION, "manifest_version"),
    source_manifest_sha256: clientPattern(
      sourceManifestSha256, SHA256, "source_manifest_sha256",
    ),
    domains: clientDomains(domains),
    reason_code: clientChoice(reasonCode, ASSESSMENT_REASONS, "reason_code"),
    confirmed: clientConfirmation(confirmed),
  };
  const result = await request(controlApiUrl, "/migrations/assessments", {
    method: "POST",
    csrf: true,
    idempotencyKey: clientPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    body,
    signal,
  });
  exact(result.payload, [
    "assessment", "idempotent_replay", "request_id", "trace_id",
  ], "assessment_response");
  const assessment = assessmentSummary(result.payload.assessment);
  validateEtag(result.response, assessment.revision);
  return Object.freeze({
    assessment,
    idempotent_replay: responseBoolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

export async function createMigrationRun({
  controlApiUrl,
  assessmentId,
  expectedAssessmentRevision,
  reasonCode,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  const assessment = clientPattern(assessmentId, IDS.assessment, "assessment_id");
  const revision = clientInteger(
    expectedAssessmentRevision, 1, 2_147_483_647, "expected_assessment_revision",
  );
  const result = await request(controlApiUrl, "/migrations/runs", {
    method: "POST",
    csrf: true,
    idempotencyKey: clientPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    revision,
    body: {
      assessment_id: assessment,
      expected_assessment_revision: revision,
      reason_code: clientChoice(reasonCode, RUN_REASONS, "reason_code"),
      confirmed: clientConfirmation(confirmed),
    },
    signal,
  });
  exact(result.payload, ["run", "idempotent_replay", "request_id", "trace_id"], "run_response");
  const run = runSummary(result.payload.run);
  if (run.assessment_id !== assessment) invalidResponse("run_owner");
  validateEtag(result.response, run.revision);
  return Object.freeze({
    run,
    idempotent_replay: responseBoolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

export async function getMigrationDifferences({ controlApiUrl, migrationId, signal } = {}) {
  const id = clientMigrationId(migrationId);
  const result = await request(
    controlApiUrl,
    `/migrations/${encodeURIComponent(id)}/differences`,
    { signal },
  );
  exact(result.payload, [
    "assessment_id", "run_id", "items", "support_scoped", "request_id", "trace_id",
  ], "difference_response");
  const assessmentId = responsePattern(
    result.payload.assessment_id, IDS.assessment, "assessment_id",
  );
  const runId = nullablePattern(result.payload.run_id, IDS.run, "run_id");
  if ((id.startsWith("mas_") && assessmentId !== id) || (id.startsWith("mrn_") && runId !== id)) {
    invalidResponse("difference_owner");
  }
  boundedArray(result.payload.items, 500, "items");
  const items = result.payload.items.map((item) => differenceSummary(item));
  unique(items.map((item) => item.difference_id), "difference_id");
  if (items.some((item) => item.assessment_id !== assessmentId
    || (item.run_id !== null && item.run_id !== runId))) {
    invalidResponse("difference_owner");
  }
  return Object.freeze({
    assessment_id: assessmentId,
    run_id: runId,
    items: Object.freeze(items),
    support_scoped: responseBoolean(result.payload.support_scoped, "support_scoped"),
    ...trace(result.payload),
  });
}

export function cutoverMigrationRun(options = {}) {
  return transitionMigrationRun({ ...options, operation: "cutover" });
}

export function rollbackMigrationRun(options = {}) {
  return transitionMigrationRun({ ...options, operation: "rollback" });
}

export async function transitionMigrationRun({
  controlApiUrl,
  operation,
  runId,
  expectedRevision,
  reasonCode,
  confirmed,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const action = clientChoice(operation, ["cutover", "rollback"], "operation");
  const run = clientPattern(runId, IDS.run, "run_id");
  const revision = clientInteger(expectedRevision, 1, 2_147_483_647, "expected_revision");
  const reasons = action === "cutover" ? CUTOVER_REASONS : ROLLBACK_REASONS;
  const result = await request(
    controlApiUrl,
    `/migrations/${encodeURIComponent(run)}/${action}`,
    {
      method: "POST",
      csrf: true,
      revision,
      stepUpGrant: clientPattern(stepUpGrant, STEP_UP_GRANT, "step_up_grant"),
      idempotencyKey: clientPattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
      body: {
        expected_revision: revision,
        reason_code: clientChoice(reasonCode, reasons, "reason_code"),
        confirmed: clientConfirmation(confirmed),
      },
      signal,
    },
  );
  exact(result.payload, ["run", "idempotent_replay", "request_id", "trace_id"], "transition_response");
  const summary = runSummary(result.payload.run);
  if (summary.run_id !== run) invalidResponse("run_owner");
  validateEtag(result.response, summary.revision);
  return Object.freeze({
    run: summary,
    idempotent_replay: responseBoolean(result.payload.idempotent_replay, "idempotent_replay"),
    ...trace(result.payload),
  });
}

export function createMigrationIdempotencyKey(action = "command") {
  const normalizedAction = String(action).replace(/[^A-Za-z0-9._:-]/gu, "").slice(0, 32);
  const random = globalThis.crypto?.randomUUID?.();
  if (!normalizedAction || !random) invalidRequest("idempotency_key");
  return `migration_${normalizedAction}_${random}`;
}

export function isMigrationAbortError(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

function assessmentSummary(value) {
  exact(value, [
    "assessment_id", "workspace_id", "source_kind", "manifest_version",
    "source_manifest_sha256", "status", "object_count", "blocking_difference_count",
    "write_watermark_before", "write_watermark_after",
    "credential_reentry_required_count", "permission_expansion_count", "revision",
    "created_at", "completed_at", "snapshots", "plan",
  ], "assessment");
  boundedArray(value.snapshots, DOMAINS.length, "snapshots");
  if (value.snapshots.length < 1) invalidResponse("snapshots");
  const snapshots = value.snapshots.map((item) => domainSnapshot(item));
  unique(snapshots.map((item) => item.domain_code), "snapshot_domain");
  const ordered = [...snapshots].sort((left, right) => (
    DOMAINS.indexOf(left.domain_code) - DOMAINS.indexOf(right.domain_code)
  ));
  if (snapshots.some((item, index) => item !== ordered[index])) invalidResponse("snapshot_order");
  const assessmentId = responsePattern(value.assessment_id, IDS.assessment, "assessment_id");
  const plan = planSummary(value.plan);
  if (plan.assessment_id !== assessmentId) invalidResponse("plan_owner");
  const blocking = responseInteger(
    value.blocking_difference_count, 0, Number.MAX_SAFE_INTEGER, "blocking_difference_count",
  );
  if ((value.status === "ready") !== (blocking === 0 && plan.status === "ready")) {
    invalidResponse("assessment_status");
  }
  return Object.freeze({
    assessment_id: assessmentId,
    workspace_id: responsePattern(value.workspace_id, IDS.workspace, "workspace_id"),
    source_kind: responseChoice(value.source_kind, SOURCES, "source_kind"),
    manifest_version: responsePattern(value.manifest_version, MANIFEST_VERSION, "manifest_version"),
    source_manifest_sha256: responsePattern(
      value.source_manifest_sha256, SHA256, "source_manifest_sha256",
    ),
    status: responseChoice(value.status, ["ready", "validation_failed"], "status"),
    object_count: responseInteger(value.object_count, 0, Number.MAX_SAFE_INTEGER, "object_count"),
    blocking_difference_count: blocking,
    write_watermark_before: responseInteger(
      value.write_watermark_before, 0, Number.MAX_SAFE_INTEGER, "write_watermark_before",
    ),
    write_watermark_after: responseInteger(
      value.write_watermark_after, 0, Number.MAX_SAFE_INTEGER, "write_watermark_after",
    ),
    credential_reentry_required_count: responseInteger(
      value.credential_reentry_required_count, 0, Number.MAX_SAFE_INTEGER,
      "credential_reentry_required_count",
    ),
    permission_expansion_count: responseInteger(
      value.permission_expansion_count, 0, Number.MAX_SAFE_INTEGER,
      "permission_expansion_count",
    ),
    revision: responseInteger(value.revision, 1, 2_147_483_647, "revision"),
    created_at: timestamp(value.created_at, "created_at"),
    completed_at: timestamp(value.completed_at, "completed_at"),
    snapshots: Object.freeze(snapshots),
    plan,
  });
}

function domainSnapshot(value) {
  exact(value, [
    "domain_code", "adapter_version", "source_count", "target_count", "source_sha256",
    "target_sha256", "source_sequence", "target_sequence", "source_watermark",
    "target_watermark", "matches",
  ], "snapshot");
  return Object.freeze({
    domain_code: responseChoice(value.domain_code, DOMAINS, "domain_code"),
    adapter_version: value.adapter_version === "migration-adapter-v1"
      ? value.adapter_version : invalidResponse("adapter_version"),
    source_count: responseInteger(value.source_count, 0, Number.MAX_SAFE_INTEGER, "source_count"),
    target_count: responseInteger(value.target_count, 0, Number.MAX_SAFE_INTEGER, "target_count"),
    source_sha256: responsePattern(value.source_sha256, SHA256, "source_sha256"),
    target_sha256: responsePattern(value.target_sha256, SHA256, "target_sha256"),
    source_sequence: responseInteger(
      value.source_sequence, 0, Number.MAX_SAFE_INTEGER, "source_sequence",
    ),
    target_sequence: responseInteger(
      value.target_sequence, 0, Number.MAX_SAFE_INTEGER, "target_sequence",
    ),
    source_watermark: responseInteger(
      value.source_watermark, 0, Number.MAX_SAFE_INTEGER, "source_watermark",
    ),
    target_watermark: responseInteger(
      value.target_watermark, 0, Number.MAX_SAFE_INTEGER, "target_watermark",
    ),
    matches: responseBoolean(value.matches, "matches"),
  });
}

function planSummary(value) {
  exact(value, [
    "plan_id", "assessment_id", "generation", "status", "phases", "plan_sha256",
    "planned_downtime_seconds", "rollback_deadline_seconds", "revision", "created_at",
  ], "plan");
  if (!Array.isArray(value.phases) || JSON.stringify(value.phases) !== JSON.stringify(PLAN_PHASES)) {
    invalidResponse("plan_phases");
  }
  return Object.freeze({
    plan_id: responsePattern(value.plan_id, IDS.plan, "plan_id"),
    assessment_id: responsePattern(value.assessment_id, IDS.assessment, "assessment_id"),
    generation: responseInteger(value.generation, 1, 2_147_483_647, "generation"),
    status: responseChoice(value.status, ["ready", "blocked"], "status"),
    phases: PLAN_PHASES,
    plan_sha256: responsePattern(value.plan_sha256, SHA256, "plan_sha256"),
    planned_downtime_seconds: responseInteger(
      value.planned_downtime_seconds, 0, 7200, "planned_downtime_seconds",
    ),
    rollback_deadline_seconds: responseInteger(
      value.rollback_deadline_seconds, 1, 7200, "rollback_deadline_seconds",
    ),
    revision: responseInteger(value.revision, 1, 2_147_483_647, "revision"),
    created_at: timestamp(value.created_at, "created_at"),
  });
}

function differenceSummary(value) {
  exact(value, [
    "difference_id", "assessment_id", "run_id", "domain_code", "difference_code",
    "severity", "source_count", "target_count", "source_sha256", "target_sha256",
    "source_sequence", "target_sequence", "remediation_code", "status", "detected_at",
  ], "difference");
  return Object.freeze({
    difference_id: responsePattern(value.difference_id, IDS.difference, "difference_id"),
    assessment_id: responsePattern(value.assessment_id, IDS.assessment, "assessment_id"),
    run_id: nullablePattern(value.run_id, IDS.run, "run_id"),
    domain_code: responseChoice(value.domain_code, DOMAINS, "domain_code"),
    difference_code: responseChoice(value.difference_code, [
      "count_mismatch", "hash_mismatch", "sequence_mismatch", "watermark_mismatch",
      "permission_expansion",
    ], "difference_code"),
    severity: value.severity === "blocking" ? "blocking" : invalidResponse("severity"),
    source_count: responseInteger(value.source_count, 0, Number.MAX_SAFE_INTEGER, "source_count"),
    target_count: responseInteger(value.target_count, 0, Number.MAX_SAFE_INTEGER, "target_count"),
    source_sha256: responsePattern(value.source_sha256, SHA256, "source_sha256"),
    target_sha256: responsePattern(value.target_sha256, SHA256, "target_sha256"),
    source_sequence: responseInteger(
      value.source_sequence, 0, Number.MAX_SAFE_INTEGER, "source_sequence",
    ),
    target_sequence: responseInteger(
      value.target_sequence, 0, Number.MAX_SAFE_INTEGER, "target_sequence",
    ),
    remediation_code: responseChoice(value.remediation_code, [
      "rebuild_target_projection", "replay_missing_sequence", "wait_for_watermark",
      "reassess_source", "review_role_mapping",
    ], "remediation_code"),
    status: value.status === "open" ? "open" : invalidResponse("status"),
    detected_at: timestamp(value.detected_at, "detected_at"),
  });
}

function runSummary(value) {
  exact(value, [
    "run_id", "assessment_id", "plan_id", "status", "generation", "freeze_generation",
    "blocking_difference_count", "revision", "started_at", "updated_at", "completed_at",
    "checkpoints", "rollback_plan",
  ], "run");
  boundedArray(value.checkpoints, CHECKPOINT_PHASES.length, "checkpoints");
  const checkpoints = value.checkpoints.map((item) => checkpointSummary(item));
  unique(checkpoints.map((item) => item.checkpoint_id), "checkpoint_id");
  const runId = responsePattern(value.run_id, IDS.run, "run_id");
  if (checkpoints.some((item, index) => (
    item.run_id !== runId || item.phase_sequence !== index + 1
    || CHECKPOINT_PHASES.indexOf(item.phase_code) !== index
  ))) invalidResponse("checkpoint_order");
  const rollbackPlan = value.rollback_plan === null ? null : rollbackPlanSummary(value.rollback_plan);
  if (rollbackPlan && rollbackPlan.run_id !== runId) invalidResponse("rollback_owner");
  return Object.freeze({
    run_id: runId,
    assessment_id: responsePattern(value.assessment_id, IDS.assessment, "assessment_id"),
    plan_id: responsePattern(value.plan_id, IDS.plan, "plan_id"),
    status: responseChoice(value.status, [
      "observing", "completed", "failed", "rolling_back", "rolled_back",
    ], "status"),
    generation: responseInteger(value.generation, 1, 2_147_483_647, "generation"),
    freeze_generation: responseInteger(
      value.freeze_generation, 1, 2_147_483_647, "freeze_generation",
    ),
    blocking_difference_count: responseInteger(
      value.blocking_difference_count, 0, Number.MAX_SAFE_INTEGER,
      "blocking_difference_count",
    ),
    revision: responseInteger(value.revision, 1, 2_147_483_647, "revision"),
    started_at: timestamp(value.started_at, "started_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
    completed_at: nullableTimestamp(value.completed_at, "completed_at"),
    checkpoints: Object.freeze(checkpoints),
    rollback_plan: rollbackPlan,
  });
}

function checkpointSummary(value) {
  exact(value, [
    "checkpoint_id", "run_id", "phase_code", "phase_sequence", "input_sha256",
    "output_sha256", "source_watermark", "target_watermark", "freeze_generation",
    "status", "created_at",
  ], "checkpoint");
  return Object.freeze({
    checkpoint_id: responsePattern(value.checkpoint_id, IDS.checkpoint, "checkpoint_id"),
    run_id: responsePattern(value.run_id, IDS.run, "run_id"),
    phase_code: responseChoice(value.phase_code, CHECKPOINT_PHASES, "phase_code"),
    phase_sequence: responseInteger(value.phase_sequence, 1, 6, "phase_sequence"),
    input_sha256: responsePattern(value.input_sha256, SHA256, "input_sha256"),
    output_sha256: responsePattern(value.output_sha256, SHA256, "output_sha256"),
    source_watermark: responseInteger(
      value.source_watermark, 0, Number.MAX_SAFE_INTEGER, "source_watermark",
    ),
    target_watermark: responseInteger(
      value.target_watermark, 0, Number.MAX_SAFE_INTEGER, "target_watermark",
    ),
    freeze_generation: responseInteger(
      value.freeze_generation, 1, 2_147_483_647, "freeze_generation",
    ),
    status: responseChoice(value.status, ["verified", "failed"], "status"),
    created_at: timestamp(value.created_at, "created_at"),
  });
}

function rollbackPlanSummary(value) {
  exact(value, [
    "rollback_plan_id", "run_id", "previous_run_id", "last_safe_checkpoint_id",
    "compensation_order", "rollback_plan_sha256", "status", "revision", "completed_at",
  ], "rollback_plan");
  if (!Array.isArray(value.compensation_order)
    || JSON.stringify(value.compensation_order) !== JSON.stringify(COMPENSATION_ORDER)) {
    invalidResponse("compensation_order");
  }
  return Object.freeze({
    rollback_plan_id: responsePattern(
      value.rollback_plan_id, IDS.rollbackPlan, "rollback_plan_id",
    ),
    run_id: responsePattern(value.run_id, IDS.run, "run_id"),
    previous_run_id: nullablePattern(value.previous_run_id, IDS.run, "previous_run_id"),
    last_safe_checkpoint_id: responsePattern(
      value.last_safe_checkpoint_id, IDS.checkpoint, "last_safe_checkpoint_id",
    ),
    compensation_order: COMPENSATION_ORDER,
    rollback_plan_sha256: responsePattern(
      value.rollback_plan_sha256, SHA256, "rollback_plan_sha256",
    ),
    status: responseChoice(value.status, ["ready", "completed", "failed"], "status"),
    revision: responseInteger(value.revision, 1, 2_147_483_647, "revision"),
    completed_at: nullableTimestamp(value.completed_at, "completed_at"),
  });
}

async function request(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = readWorkspaceCsrfCookie();
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  if (options.revision) headers["If-Match"] = `"${options.revision}"`;
  if (options.stepUpGrant) headers["X-Step-Up-Grant"] = options.stepUpGrant;
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
    if (isMigrationAbortError(error)) throw error;
    throw clientError("migration.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = SAFE_ERROR_CODE.test(String(payload?.code ?? ""))
      ? String(payload.code) : "migration.unavailable";
    throw clientError(
      code,
      Number(response.status) || 0,
      payload?.request_id ?? response.headers?.get?.("X-Request-ID"),
      payload?.trace_id ?? response.headers?.get?.("X-Trace-ID"),
    );
  }
  validateBoundary(response, payload);
  return { payload, response };
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest("control_api_url");
  }
  return new URL(path, base).toString();
}

async function readJson(response) {
  try {
    const payload = await response.json();
    plain(payload, "response");
    return payload;
  } catch (error) {
    if (error instanceof AuthApiError) throw error;
    invalidResponse("json");
  }
}

function validateBoundary(response, payload) {
  const directives = String(response.headers?.get?.("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim()).filter(Boolean);
  const requestId = String(response.headers?.get?.("X-Request-ID") ?? "");
  const traceId = String(response.headers?.get?.("X-Trace-ID") ?? "");
  if (!directives.includes("no-store") || !directives.includes("private")
    || !CHAIN_ID.test(requestId) || !CHAIN_ID.test(traceId)
    || payload.request_id !== requestId || payload.trace_id !== traceId) {
    invalidResponse("boundary");
  }
}

function validateEtag(response, revision) {
  if (response.headers?.get?.("ETag") !== `"${revision}"`) invalidResponse("etag");
}

function trace(payload) {
  return {
    request_id: responsePattern(payload.request_id, CHAIN_ID, "request_id"),
    trace_id: responsePattern(payload.trace_id, CHAIN_ID, "trace_id"),
  };
}

function clientMigrationId(value) {
  const text = String(value ?? "");
  if (!IDS.assessment.test(text) && !IDS.run.test(text)) invalidRequest("migration_id");
  return text;
}

function clientDomains(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > DOMAINS.length) {
    invalidRequest("domains");
  }
  const domains = value.map((item) => clientChoice(item, DOMAINS, "domain"));
  if (new Set(domains).size !== domains.length
    || JSON.stringify(domains) !== JSON.stringify([...domains].sort())) {
    invalidRequest("domains");
  }
  return domains;
}

function clientConfirmation(value) {
  if (value !== true) invalidRequest("confirmed");
  return true;
}

function clientChoice(value, choices, label) {
  const text = String(value ?? "");
  if (!choices.includes(text)) invalidRequest(label);
  return text;
}

function responseChoice(value, choices, label) {
  const text = String(value ?? "");
  if (!choices.includes(text)) invalidResponse(label);
  return text;
}

function clientPattern(value, pattern, label) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(label);
  return text;
}

function responsePattern(value, pattern, label) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidResponse(label);
  return text;
}

function nullablePattern(value, pattern, label) {
  return value === null ? null : responsePattern(value, pattern, label);
}

function clientInteger(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidRequest(label);
  return value;
}

function responseInteger(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidResponse(label);
  return value;
}

function responseBoolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}

function timestamp(value, label) {
  if (typeof value !== "string" || value.length < 20 || value.length > 40
    || Number.isNaN(Date.parse(value))) invalidResponse(label);
  return value;
}

function nullableTimestamp(value, label) {
  return value === null ? null : timestamp(value, label);
}

function exact(value, keys, label) {
  plain(value, label);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse(label);
}

function plain(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(label);
}

function boundedArray(value, maximum, label) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(label);
}

function unique(values, label) {
  if (values.length !== new Set(values).size) invalidResponse(label);
}

function invalidRequest(label) {
  throw clientError("migration.request_invalid", 400, "-", "-", label);
}

function invalidResponse(label) {
  throw clientError("migration.response_invalid", 502, "-", "-", label);
}

function clientError(code, status, requestId = "-", traceId = "-", _label = "") {
  return new AuthApiError({
    status,
    code,
    message: code,
    requestId: CHAIN_ID.test(String(requestId ?? "")) ? String(requestId) : "-",
    traceId: CHAIN_ID.test(String(traceId ?? "")) ? String(traceId) : "-",
  });
}
