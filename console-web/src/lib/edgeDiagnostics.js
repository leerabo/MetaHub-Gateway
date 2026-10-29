/** PRD-126 domain-separated Fleet health and controlled diagnostic clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const DEPLOYMENT_ID = /^edp_[0-9a-f]{32}$/u;
const GRANT_ID = /^dgr_[0-9a-f]{32}$/u;
const COMMAND_ID = /^dcm_[0-9a-f]{32}$/u;
const RESULT_ID = /^drs_[0-9a-f]{32}$/u;
const SNAPSHOT_ID = /^ehs_[0-9a-f]{32}$/u;
const SHA256 = /^[0-9a-f]{64}$/u;
const CHAIN_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const REASON_CODE = /^[a-z][a-z0-9_.-]{1,79}$/u;
const REGION_CODE = /^[a-z0-9][a-z0-9-]{1,39}$/u;
const CURSOR = /^[A-Za-z0-9_-]{16,2048}$/u;
const STEP_UP_GRANT = /^[A-Za-z0-9_-]{43,128}$/u;
const SEMVER = /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/u;
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";

const COMMAND_CODES = Object.freeze([
  "runtime.version",
  "runtime.watermarks",
  "runtime.disk_budget",
  "runtime.connectivity_summary",
]);
const HEALTH_CODES = Object.freeze([
  "healthy", "degraded", "critical", "offline", "revoked", "unknown",
]);

export async function getWorkspaceEdgeHealth({ controlApiUrl, deploymentId, signal } = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/health`,
    { signal },
    "workspace",
  );
  exact(result.payload, [
    "health", "diagnostic_grants", "diagnostic_results", "request_id", "trace_id",
  ], "health", "workspace");
  const health = validateHealth(result.payload.health);
  if (health.deployment_id !== id) invalidResponse("workspace", "health_owner");
  array(result.payload.diagnostic_grants, 20, "diagnostic_grants", "workspace");
  array(result.payload.diagnostic_results, 20, "diagnostic_results", "workspace");
  const diagnosticGrants = result.payload.diagnostic_grants.map((item) => (
    validateGrant(item, "workspace")
  ));
  const diagnosticResults = result.payload.diagnostic_results.map(validateResult);
  if (diagnosticGrants.some((item) => item.deployment_id !== id)
    || diagnosticResults.some((item) => item.deployment_id !== id)) {
    invalidResponse("workspace", "diagnostic_owner");
  }
  unique(diagnosticGrants.map((item) => item.diagnostic_grant_id), "workspace", "grants");
  unique(diagnosticResults.map((item) => item.diagnostic_result_id), "workspace", "results");
  return Object.freeze({
    health,
    diagnostic_grants: Object.freeze(diagnosticGrants),
    diagnostic_results: Object.freeze(diagnosticResults),
    ...trace(result.payload, "workspace"),
  });
}

export async function decideWorkspaceDiagnosticGrant({
  controlApiUrl,
  deploymentId,
  grantId,
  action,
  reasonCode,
  ttlSeconds,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id", "workspace");
  const normalizedAction = choice(action, ["approve", "revoke"], "action", "workspace");
  const body = {
    action: normalizedAction,
    diagnostic_grant_id: clientId(grantId, GRANT_ID, "grant_id", "workspace"),
    reason_code: reason(reasonCode, "workspace"),
  };
  if (normalizedAction === "approve") {
    body.ttl_seconds = integer(ttlSeconds, 60, 300, "ttl_seconds", "workspace");
  }
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/diagnostic-grants`,
    {
      method: "POST",
      body,
      csrf: true,
      stepUpGrant: stepUp(stepUpGrant, "workspace"),
      idempotencyKey: idempotency(idempotencyKey, "workspace"),
      signal,
    },
    "workspace",
  );
  return validateGrantResponse(result, id, "workspace");
}

export async function listPlatformFleet({
  controlApiUrl,
  filters = {},
  cursor,
  limit = 50,
  signal,
} = {}) {
  const parameters = new URLSearchParams({
    limit: String(integer(limit, 1, 200, "limit", "platform")),
  });
  const status = optionalChoice(filters.status, ["pending", "active", "revoked", "retired"]);
  const health = optionalChoice(filters.health, HEALTH_CODES);
  const region = optionalRegion(filters.region);
  if (status) parameters.set("status", status);
  if (health) parameters.set("health", health);
  if (region) parameters.set("region", region);
  if (cursor !== undefined && cursor !== null && cursor !== "") {
    const value = String(cursor);
    if (!CURSOR.test(value)) invalidRequest("platform", "cursor");
    parameters.set("cursor", value);
  }
  const result = await request(
    controlApiUrl,
    `/platform/fleet?${parameters.toString()}`,
    { signal },
    "platform",
  );
  exact(result.payload, ["deployments", "next_cursor", "count", "request_id", "trace_id"],
    "fleet", "platform");
  array(result.payload.deployments, 200, "deployments", "platform");
  const deployments = result.payload.deployments.map(validateFleetDeployment);
  unique(deployments.map((item) => item.deployment_id), "platform", "deployments");
  if (result.payload.count !== deployments.length) invalidResponse("platform", "count");
  const nextCursor = result.payload.next_cursor === null
    ? null
    : responsePattern(result.payload.next_cursor, CURSOR, "next_cursor", "platform");
  return Object.freeze({
    deployments: Object.freeze(deployments),
    next_cursor: nextCursor,
    count: deployments.length,
    ...trace(result.payload, "platform"),
  });
}

export async function requestPlatformDiagnosticGrant({
  controlApiUrl,
  deploymentId,
  invocation,
  reasonCode,
  maxRuns = 1,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id", "platform");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/diagnostic-grants`,
    {
      method: "POST",
      body: {
        action: "request",
        invocation: validateClientInvocation(invocation),
        reason_code: reason(reasonCode, "platform"),
        max_runs: integer(maxRuns, 1, 3, "max_runs", "platform"),
      },
      csrf: true,
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      signal,
    },
    "platform",
  );
  return validateGrantResponse(result, id, "platform");
}

export async function runPlatformDiagnostic({
  controlApiUrl,
  deploymentId,
  grantId,
  deadlineSeconds,
  reasonCode,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id", "platform");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/diagnostics`,
    {
      method: "POST",
      body: {
        action: "run",
        diagnostic_grant_id: clientId(grantId, GRANT_ID, "grant_id", "platform"),
        deadline_seconds: integer(deadlineSeconds, 1, 120, "deadline_seconds", "platform"),
        reason_code: reason(reasonCode, "platform"),
      },
      csrf: true,
      stepUpGrant: stepUp(stepUpGrant, "platform"),
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      signal,
    },
    "platform",
  );
  return validateCommandResponse(result, id);
}

export async function cancelPlatformDiagnostic({
  controlApiUrl,
  deploymentId,
  commandId,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = clientId(deploymentId, DEPLOYMENT_ID, "deployment_id", "platform");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/diagnostics`,
    {
      method: "POST",
      body: {
        action: "cancel",
        diagnostic_command_id: clientId(commandId, COMMAND_ID, "command_id", "platform"),
        reason_code: reason(reasonCode, "platform"),
      },
      csrf: true,
      idempotencyKey: idempotency(idempotencyKey, "platform"),
      signal,
    },
    "platform",
  );
  return validateCommandResponse(result, id);
}

function validateFleetDeployment(value) {
  plain(value, "fleet_deployment", "platform");
  exact(value, [
    "deployment_id", "region_code", "mode", "status", "health_code", "health_score",
    "alert_code", "maintenance_mode", "agent_version", "desired_generation",
    "applied_generation", "lkg_generation", "telemetry_lag_seconds",
    "certificate_signal", "source_health_generation", "index_revision", "updated_at",
  ], "fleet_deployment", "platform");
  const desired = responseInteger(value.desired_generation, 0, "desired_generation", "platform");
  const applied = responseInteger(value.applied_generation, 0, "applied_generation", "platform");
  const lkg = responseInteger(value.lkg_generation, 0, "lkg_generation", "platform");
  if (applied > desired || lkg > applied) invalidResponse("platform", "generation_order");
  return Object.freeze({
    deployment_id: responsePattern(value.deployment_id, DEPLOYMENT_ID, "deployment_id", "platform"),
    region_code: responsePattern(value.region_code, REGION_CODE, "region_code", "platform"),
    mode: responseChoice(value.mode, ["connected", "isolated"], "mode", "platform"),
    status: responseChoice(value.status, ["pending", "active", "revoked", "retired"], "status", "platform"),
    health_code: responseChoice(value.health_code, HEALTH_CODES, "health_code", "platform"),
    health_score: responseInteger(value.health_score, 0, "health_score", "platform", 100),
    alert_code: responseChoice(value.alert_code, [
      "none", "certificate", "version", "capacity", "configuration", "telemetry",
      "validation", "offline", "revoked",
    ], "alert_code", "platform"),
    maintenance_mode: responseBoolean(value.maintenance_mode, "maintenance_mode", "platform"),
    agent_version: nullableText(value.agent_version, 64, "agent_version", "platform"),
    desired_generation: desired,
    applied_generation: applied,
    lkg_generation: lkg,
    telemetry_lag_seconds: nullableInteger(value.telemetry_lag_seconds, "telemetry_lag_seconds", "platform"),
    certificate_signal: responseChoice(value.certificate_signal, [
      "healthy", "expiring", "expired", "missing", "revoked",
    ], "certificate_signal", "platform"),
    source_health_generation: responseInteger(value.source_health_generation, 1, "source_health_generation", "platform"),
    index_revision: responseInteger(value.index_revision, 1, "index_revision", "platform"),
    updated_at: timestamp(value.updated_at, "updated_at", "platform"),
  });
}

function validateHealth(value) {
  plain(value, "health", "workspace");
  exact(value, [
    "edge_health_snapshot_id", "deployment_id", "generation", "score_model_version",
    "health_score", "health_code", "certificate_signal", "version_signal",
    "capacity_signal", "config_signal", "telemetry_signal", "validation_signal",
    "desired_generation", "applied_generation", "lkg_generation",
    "telemetry_lag_seconds", "certificate_expires_at", "agent_version", "observed_at",
    "created_at",
  ], "health", "workspace");
  const health = Object.freeze({
    edge_health_snapshot_id: responsePattern(value.edge_health_snapshot_id, SNAPSHOT_ID, "snapshot_id", "workspace"),
    deployment_id: responsePattern(value.deployment_id, DEPLOYMENT_ID, "deployment_id", "workspace"),
    generation: responseInteger(value.generation, 1, "generation", "workspace"),
    score_model_version: responseChoice(value.score_model_version, ["edge-health-v1"], "score_model_version", "workspace"),
    health_score: responseInteger(value.health_score, 0, "health_score", "workspace", 100),
    health_code: responseChoice(value.health_code, HEALTH_CODES, "health_code", "workspace"),
    certificate_signal: responseChoice(value.certificate_signal, ["healthy", "expiring", "expired", "missing", "revoked"], "certificate_signal", "workspace"),
    version_signal: responseChoice(value.version_signal, ["current", "outdated", "unknown"], "version_signal", "workspace"),
    capacity_signal: responseChoice(value.capacity_signal, ["healthy", "warning", "critical", "unknown"], "capacity_signal", "workspace"),
    config_signal: responseChoice(value.config_signal, ["converged", "drifted", "not_published", "unknown"], "config_signal", "workspace"),
    telemetry_signal: responseChoice(value.telemetry_signal, ["current", "lagging", "stale", "missing"], "telemetry_signal", "workspace"),
    validation_signal: responseChoice(value.validation_signal, ["passed", "failed", "pending", "missing"], "validation_signal", "workspace"),
    desired_generation: responseInteger(value.desired_generation, 0, "desired_generation", "workspace"),
    applied_generation: responseInteger(value.applied_generation, 0, "applied_generation", "workspace"),
    lkg_generation: responseInteger(value.lkg_generation, 0, "lkg_generation", "workspace"),
    telemetry_lag_seconds: nullableInteger(value.telemetry_lag_seconds, "telemetry_lag_seconds", "workspace"),
    certificate_expires_at: nullableTimestamp(value.certificate_expires_at, "certificate_expires_at", "workspace"),
    agent_version: nullableText(value.agent_version, 64, "agent_version", "workspace"),
    observed_at: timestamp(value.observed_at, "observed_at", "workspace"),
    created_at: timestamp(value.created_at, "created_at", "workspace"),
  });
  if (health.applied_generation > health.desired_generation
    || health.lkg_generation > health.applied_generation) {
    invalidResponse("workspace", "generation_order");
  }
  return health;
}

function validateGrant(value, domain) {
  plain(value, "grant", domain);
  exact(value, [
    "diagnostic_grant_id", "deployment_id", "status", "command_code", "invocation",
    "parameters_sha256", "reason_code", "max_runs", "run_count", "revision",
    "requested_at", "approved_at", "expires_at", "revoked_at", "updated_at",
  ], "grant", domain);
  const invocation = validateInvocation(value.invocation, domain);
  if (invocation.command_code !== value.command_code) invalidResponse(domain, "command_code");
  const grant = Object.freeze({
    diagnostic_grant_id: responsePattern(value.diagnostic_grant_id, GRANT_ID, "grant_id", domain),
    deployment_id: responsePattern(value.deployment_id, DEPLOYMENT_ID, "deployment_id", domain),
    status: responseChoice(value.status, ["pending_customer", "active", "revoked", "expired", "exhausted"], "status", domain),
    command_code: responseChoice(value.command_code, COMMAND_CODES, "command_code", domain),
    invocation,
    parameters_sha256: responsePattern(value.parameters_sha256, SHA256, "parameters_sha256", domain),
    reason_code: responsePattern(value.reason_code, REASON_CODE, "reason_code", domain),
    max_runs: responseInteger(value.max_runs, 1, "max_runs", domain, 3),
    run_count: responseInteger(value.run_count, 0, "run_count", domain, 3),
    revision: responseInteger(value.revision, 1, "revision", domain),
    requested_at: timestamp(value.requested_at, "requested_at", domain),
    approved_at: nullableTimestamp(value.approved_at, "approved_at", domain),
    expires_at: nullableTimestamp(value.expires_at, "expires_at", domain),
    revoked_at: nullableTimestamp(value.revoked_at, "revoked_at", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
  if (grant.run_count > grant.max_runs) invalidResponse(domain, "run_count");
  return grant;
}

function validateResult(value) {
  plain(value, "result", "workspace");
  exact(value, [
    "diagnostic_result_id", "diagnostic_grant_id", "diagnostic_command_id",
    "deployment_id", "generation", "status", "reason_code", "result", "report_sha256",
    "result_sha256", "duration_ms", "started_at", "completed_at", "received_at",
  ], "result", "workspace");
  const result = Object.freeze({
    diagnostic_result_id: responsePattern(value.diagnostic_result_id, RESULT_ID, "result_id", "workspace"),
    diagnostic_grant_id: responsePattern(value.diagnostic_grant_id, GRANT_ID, "grant_id", "workspace"),
    diagnostic_command_id: responsePattern(value.diagnostic_command_id, COMMAND_ID, "command_id", "workspace"),
    deployment_id: responsePattern(value.deployment_id, DEPLOYMENT_ID, "deployment_id", "workspace"),
    generation: responseInteger(value.generation, 1, "generation", "workspace"),
    status: responseChoice(value.status, ["completed", "failed", "cancelled", "timed_out"], "status", "workspace"),
    reason_code: responseChoice(value.reason_code, [
      "completed", "cancelled", "deadline_exceeded", "handler_failed", "result_invalid",
      "sensitive_output",
    ], "reason_code", "workspace"),
    result: value.result === null ? null : validateDiagnosticResult(value.result),
    report_sha256: responsePattern(value.report_sha256, SHA256, "report_sha256", "workspace"),
    result_sha256: value.result_sha256 === null ? null
      : responsePattern(value.result_sha256, SHA256, "result_sha256", "workspace"),
    duration_ms: responseInteger(value.duration_ms, 0, "duration_ms", "workspace", 120_000),
    started_at: timestamp(value.started_at, "started_at", "workspace"),
    completed_at: timestamp(value.completed_at, "completed_at", "workspace"),
    received_at: timestamp(value.received_at, "received_at", "workspace"),
  });
  const completed = result.status === "completed";
  if (completed !== (result.reason_code === "completed")
    || completed !== (result.result !== null)
    || completed !== (result.result_sha256 !== null)) {
    invalidResponse("workspace", "result_state");
  }
  return result;
}

function validateDiagnosticResult(value) {
  plain(value, "diagnostic_result", "workspace");
  const code = responseChoice(value.command_code, COMMAND_CODES, "command_code", "workspace");
  const keys = {
    "runtime.version": ["command_code", "agent_version", "gateway_schema_version", "os_family", "architecture"],
    "runtime.watermarks": ["command_code", "desired_generation", "applied_generation", "lkg_generation", "telemetry_health_sequence", "telemetry_failure_sequence"],
    "runtime.disk_budget": ["command_code", "volume_code", "capacity_bucket", "available_bucket", "pressure_code"],
    "runtime.connectivity_summary": ["command_code", "target_code", "status_code", "latency_bucket", "checked_target_count", "reachable_target_count"],
  }[code];
  exact(value, keys, "diagnostic_result", "workspace");
  if (code === "runtime.version") {
    if (!SEMVER.test(String(value.agent_version ?? ""))) {
      invalidResponse("workspace", "agent_version");
    }
    responseInteger(value.gateway_schema_version, 1, "gateway_schema_version", "workspace", 9_999);
    responseChoice(value.os_family, ["linux", "windows"], "os_family", "workspace");
    responseChoice(value.architecture, ["x86_64", "aarch64"], "architecture", "workspace");
  } else if (code === "runtime.watermarks") {
    for (const key of keys.slice(1)) nullableInteger(value[key], key, "workspace");
  } else if (code === "runtime.disk_budget") {
    responseChoice(value.volume_code, ["runtime", "state", "support_bundle"], "volume_code", "workspace");
    responseChoice(value.capacity_bucket, ["small", "medium", "large", "very_large"], "capacity_bucket", "workspace");
    responseChoice(value.available_bucket, ["critical", "low", "medium", "high"], "available_bucket", "workspace");
    responseChoice(value.pressure_code, ["none", "warning", "critical"], "pressure_code", "workspace");
  } else {
    responseChoice(value.target_code, ["control_plane", "provider_pool"], "target_code", "workspace");
    responseChoice(value.status_code, ["reachable", "degraded", "unreachable"], "status_code", "workspace");
    responseChoice(value.latency_bucket, ["unknown", "fast", "normal", "slow", "timeout"], "latency_bucket", "workspace");
    const checked = responseInteger(
      value.checked_target_count, 1, "checked_target_count", "workspace", 64,
    );
    const reachable = responseInteger(
      value.reachable_target_count, 0, "reachable_target_count", "workspace", 64,
    );
    const expected = reachable === 0
      ? "unreachable" : reachable === checked ? "reachable" : "degraded";
    if (reachable > checked || value.status_code !== expected) {
      invalidResponse("workspace", "connectivity_counts");
    }
  }
  return Object.freeze({ ...value });
}

function validateGrantResponse(result, deploymentId, domain) {
  exact(result.payload, ["grant", "idempotent_replay", "request_id", "trace_id"],
    "grant_response", domain);
  const grant = validateGrant(result.payload.grant, domain);
  if (grant.deployment_id !== deploymentId) invalidResponse(domain, "grant_owner");
  validateEtag(result.response, grant.revision, domain);
  return Object.freeze({
    grant,
    idempotent_replay: responseBoolean(result.payload.idempotent_replay, "idempotent_replay", domain),
    ...trace(result.payload, domain),
  });
}

function validateCommandResponse(result, deploymentId) {
  exact(result.payload, ["command", "idempotent_replay", "request_id", "trace_id"],
    "command_response", "platform");
  const value = result.payload.command;
  plain(value, "command", "platform");
  exact(value, [
    "diagnostic_command_id", "diagnostic_grant_id", "deployment_id", "generation",
    "status", "command_code", "envelope_sha256", "revision", "issued_at", "deadline_at",
    "expires_at", "lease_expires_at", "started_at", "completed_at", "updated_at",
  ], "command", "platform");
  const command = Object.freeze({
    diagnostic_command_id: responsePattern(value.diagnostic_command_id, COMMAND_ID, "command_id", "platform"),
    diagnostic_grant_id: responsePattern(value.diagnostic_grant_id, GRANT_ID, "grant_id", "platform"),
    deployment_id: responsePattern(value.deployment_id, DEPLOYMENT_ID, "deployment_id", "platform"),
    generation: responseInteger(value.generation, 1, "generation", "platform"),
    status: responseChoice(value.status, ["authorized", "leased", "running", "completed", "failed", "cancelled", "expired"], "status", "platform"),
    command_code: responseChoice(value.command_code, COMMAND_CODES, "command_code", "platform"),
    envelope_sha256: responsePattern(value.envelope_sha256, SHA256, "envelope_sha256", "platform"),
    revision: responseInteger(value.revision, 1, "revision", "platform"),
    issued_at: timestamp(value.issued_at, "issued_at", "platform"),
    deadline_at: timestamp(value.deadline_at, "deadline_at", "platform"),
    expires_at: timestamp(value.expires_at, "expires_at", "platform"),
    lease_expires_at: nullableTimestamp(value.lease_expires_at, "lease_expires_at", "platform"),
    started_at: nullableTimestamp(value.started_at, "started_at", "platform"),
    completed_at: nullableTimestamp(value.completed_at, "completed_at", "platform"),
    updated_at: timestamp(value.updated_at, "updated_at", "platform"),
  });
  if (command.deployment_id !== deploymentId) invalidResponse("platform", "command_owner");
  validateEtag(result.response, command.revision, "platform");
  return Object.freeze({
    command,
    idempotent_replay: responseBoolean(result.payload.idempotent_replay, "idempotent_replay", "platform"),
    ...trace(result.payload, "platform"),
  });
}

function validateInvocation(value, domain) {
  plain(value, "invocation", domain);
  const code = responseChoice(value.command_code, COMMAND_CODES, "command_code", domain);
  const keys = {
    "runtime.version": ["command_code"],
    "runtime.watermarks": ["command_code", "watermark_codes"],
    "runtime.disk_budget": ["command_code", "volume_code"],
    "runtime.connectivity_summary": ["command_code", "target_code"],
  }[code];
  exact(value, keys, "invocation", domain);
  if (code === "runtime.watermarks") {
    const allowed = [
      "desired_generation", "applied_generation", "lkg_generation",
      "telemetry_health_sequence", "telemetry_failure_sequence",
    ];
    array(value.watermark_codes, 5, "watermark_codes", domain);
    if (value.watermark_codes.length < 1
      || value.watermark_codes.length !== new Set(value.watermark_codes).size
      || value.watermark_codes.some((item) => !allowed.includes(item))
      || value.watermark_codes.some((item, index) => (
        index > 0 && allowed.indexOf(item) < allowed.indexOf(value.watermark_codes[index - 1])
      ))) invalidResponse(domain, "watermark_codes");
    return Object.freeze({ ...value, watermark_codes: Object.freeze([...value.watermark_codes]) });
  }
  if (code === "runtime.disk_budget") {
    responseChoice(value.volume_code, ["runtime", "state", "support_bundle"], "volume_code", domain);
  } else if (code === "runtime.connectivity_summary") {
    responseChoice(value.target_code, ["control_plane", "provider_pool"], "target_code", domain);
  }
  return Object.freeze({ ...value });
}

function validateClientInvocation(value) {
  plain(value, "invocation", "platform", true);
  const code = choice(value.command_code, COMMAND_CODES, "command_code", "platform");
  const keys = {
    "runtime.version": ["command_code"],
    "runtime.watermarks": ["command_code", "watermark_codes"],
    "runtime.disk_budget": ["command_code", "volume_code"],
    "runtime.connectivity_summary": ["command_code", "target_code"],
  }[code];
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) {
    invalidRequest("platform", "invocation");
  }
  if (code === "runtime.watermarks") {
    const allowed = [
      "desired_generation", "applied_generation", "lkg_generation",
      "telemetry_health_sequence", "telemetry_failure_sequence",
    ];
    array(value.watermark_codes, 5, "watermark_codes", "platform", true);
    if (value.watermark_codes.length < 1
      || value.watermark_codes.length !== new Set(value.watermark_codes).size
      || value.watermark_codes.some((item) => !allowed.includes(item))
      || value.watermark_codes.some((item, index) => (
        index > 0 && allowed.indexOf(item) < allowed.indexOf(value.watermark_codes[index - 1])
      ))) invalidRequest("platform", "watermark_codes");
  } else if (code === "runtime.disk_budget") {
    choice(value.volume_code, ["runtime", "state", "support_bundle"], "volume_code", "platform");
  } else if (code === "runtime.connectivity_summary") {
    choice(value.target_code, ["control_plane", "provider_pool"], "target_code", "platform");
  }
  return Object.freeze({ ...value });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) {
    headers["X-CSRF-Token"] = domain === "workspace"
      ? readWorkspaceCsrfCookie()
      : readCookie(PLATFORM_CSRF_COOKIE);
  }
  if (options.stepUpGrant) headers["X-Step-Up-Grant"] = options.stepUpGrant;
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, domain), {
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
    throw apiError(domain, 0, `${domain}.network_unavailable`, "Service unavailable");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/u.test(String(payload?.code ?? ""))
      ? payload.code : `${domain}.request_failed`;
    throw apiError(domain, response.status, code, safeText(payload?.message), payload?.request_id);
  }
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  const requestId = response.headers.get("X-Request-ID") ?? "";
  const traceId = response.headers.get("X-Trace-ID") ?? "";
  if (!directives.includes("no-store") || !directives.includes("private")
    || !CHAIN_ID.test(requestId) || !CHAIN_ID.test(traceId)
    || payload.request_id !== requestId || payload.trace_id !== traceId) {
    invalidResponse(domain, "boundary");
  }
  return { payload, response };
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(domain, "control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain, "control_api_url");
  }
  return new URL(path, base).toString();
}

async function readJson(response, domain) {
  try {
    const payload = await response.json();
    plain(payload, "response", domain);
    return payload;
  } catch (error) {
    if (error instanceof AuthApiError || error instanceof PlatformApiError) throw error;
    invalidResponse(domain, "json");
  }
}

function trace(value, domain) {
  return {
    request_id: responseText(value.request_id, 160, "request_id", domain),
    trace_id: responseText(value.trace_id, 160, "trace_id", domain),
  };
}

function validateEtag(response, revision, domain) {
  if (response.headers.get("ETag") !== `"${revision}"`) invalidResponse(domain, "etag");
}

function readCookie(name) {
  const prefix = `${name}=`;
  const matches = String(globalThis.document?.cookie ?? "").split(";")
    .map((item) => item.trim()).filter((item) => item.startsWith(prefix));
  if (matches.length !== 1) invalidRequest("platform", "csrf");
  const value = matches[0].slice(prefix.length);
  if (!/^[A-Za-z0-9_-]{32,128}$/u.test(value)) invalidRequest("platform", "csrf");
  return value;
}

function exact(value, keys, label, domain) {
  plain(value, label, domain);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    invalidResponse(domain, label);
  }
}

function plain(value, label, domain, client = false) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) {
    if (client) invalidRequest(domain, label);
    invalidResponse(domain, label);
  }
}

function array(value, maximum, label, domain, client = false) {
  if (!Array.isArray(value) || value.length > maximum) {
    if (client) invalidRequest(domain, label);
    invalidResponse(domain, label);
  }
}

function unique(values, domain, label) {
  if (values.length !== new Set(values).size) invalidResponse(domain, label);
}

function clientId(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function reason(value, domain) {
  const text = String(value ?? "");
  if (!REASON_CODE.test(text)) invalidRequest(domain, "reason_code");
  return text;
}

function idempotency(value, domain) {
  const text = String(value ?? "");
  if (!IDEMPOTENCY_KEY.test(text)) invalidRequest(domain, "idempotency_key");
  return text;
}

function stepUp(value, domain) {
  const text = String(value ?? "");
  if (!STEP_UP_GRANT.test(text)) invalidRequest(domain, "step_up_grant");
  return text;
}

function choice(value, values, label, domain) {
  if (!values.includes(value)) invalidRequest(domain, label);
  return value;
}

function integer(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function optionalChoice(value, values) {
  if (value === undefined || value === null || value === "") return "";
  const text = String(value);
  if (!values.includes(text)) invalidRequest("platform", "filter");
  return text;
}

function optionalRegion(value) {
  if (value === undefined || value === null || value === "") return "";
  const text = String(value).trim().toLowerCase();
  if (!REGION_CODE.test(text)) invalidRequest("platform", "region");
  return text;
}

function responsePattern(value, pattern, label, domain) {
  const text = responseText(value, 2048, label, domain);
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function responseChoice(value, values, label, domain) {
  if (!values.includes(value)) invalidResponse(domain, label);
  return value;
}

function responseInteger(value, minimum, label, domain, maximum = Number.MAX_SAFE_INTEGER) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableInteger(value, label, domain) {
  return value === null ? null : responseInteger(value, 0, label, domain);
}

function responseBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
}

function responseText(value, maximum, label, domain) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableText(value, maximum, label, domain) {
  return value === null ? null : responseText(value, maximum, label, domain);
}

function timestamp(value, label, domain) {
  const text = responseText(value, 40, label, domain);
  if (!Number.isFinite(Date.parse(text))) invalidResponse(domain, label);
  return text;
}

function nullableTimestamp(value, label, domain) {
  return value === null ? null : timestamp(value, label, domain);
}

function safeText(value) {
  return typeof value === "string" && value.length <= 240 ? value : "Request failed";
}

function apiError(domain, status, code, message, requestId = "-") {
  if (domain === "platform") return new PlatformApiError({ status, code, message, requestId });
  return new AuthApiError({ status, code, message, requestId });
}

function invalidRequest(domain, label) {
  throw apiError(domain, 422, domain === "platform"
    ? "platform.edge_fleet_request_invalid" : "edge.diagnostic_request_invalid",
  `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, domain === "platform"
    ? "platform.edge_fleet_response_invalid" : "edge.response_invalid",
  `Invalid ${label}`);
}
