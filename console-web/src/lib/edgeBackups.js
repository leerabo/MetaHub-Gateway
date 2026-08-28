/** PRD-129 strict Workspace and Platform private-Gateway backup clients. */

import { AuthApiError } from "./auth.js";
import { PlatformApiError } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const IDS = Object.freeze({
  deployment: /^edp_[0-9a-f]{32}$/u,
  backup: /^ebk_[0-9a-f]{32}$/u,
  drill: /^pdd_[0-9a-f]{32}$/u,
  restore: /^rst_[0-9a-f]{32}$/u,
});
const SHA256 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{1,79}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE_GRANT = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,4095}$/u;
const CHAIN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/u;
const QUARTER = /^[0-9]{4}-Q[1-4]$/u;
const SCHEMA = /^[0-9]{4}$/u;
const PLATFORM_CSRF_COOKIE = "__Host-metahub-platform-csrf";

export async function getWorkspaceBackupStatus({
  controlApiUrl,
  deploymentId,
  signal,
} = {}) {
  const id = clientPattern(deploymentId, IDS.deployment, "deployment_id", "workspace");
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(id)}/backup-status`,
    { signal },
    "workspace",
  );
  const response = backupStatusResponse(result.payload, "workspace");
  if (response.status.deployment_id !== id) invalidResponse("workspace", "backup_owner");
  validateEtag(result.response, response.status.revision, "workspace");
  return response;
}

export async function mutateWorkspaceRestoreDrill({
  controlApiUrl,
  deploymentId,
  action,
  sourceBackupId = null,
  drillId = null,
  quarterCode = null,
  rpoObjectiveSeconds = null,
  rtoObjectiveSeconds = null,
  isolatedTargetSha256 = null,
  expectedRevision = null,
  reasonCode,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const deployment = clientPattern(
    deploymentId, IDS.deployment, "deployment_id", "workspace",
  );
  const operation = clientChoice(action, ["request", "approve"], "action", "workspace");
  const body = operation === "request"
    ? {
        action: operation,
        source_backup_id: clientPattern(
          sourceBackupId, IDS.backup, "source_backup_id", "workspace",
        ),
        drill_id: null,
        quarter_code: clientPattern(quarterCode, QUARTER, "quarter_code", "workspace"),
        rpo_objective_seconds: clientInteger(
          rpoObjectiveSeconds, 900, 604_800, "rpo_objective_seconds", "workspace",
        ),
        rto_objective_seconds: clientInteger(
          rtoObjectiveSeconds, 900, 604_800, "rto_objective_seconds", "workspace",
        ),
        isolated_target_sha256: clientPattern(
          isolatedTargetSha256, SHA256, "isolated_target_sha256", "workspace",
        ),
        expected_revision: null,
        reason_code: clientPattern(reasonCode, SAFE_CODE, "reason_code", "workspace"),
      }
    : {
        action: operation,
        source_backup_id: null,
        drill_id: clientPattern(drillId, IDS.drill, "drill_id", "workspace"),
        quarter_code: null,
        rpo_objective_seconds: null,
        rto_objective_seconds: null,
        isolated_target_sha256: null,
        expected_revision: clientInteger(
          expectedRevision, 1, Number.MAX_SAFE_INTEGER, "expected_revision", "workspace",
        ),
        reason_code: clientPattern(reasonCode, SAFE_CODE, "reason_code", "workspace"),
      };
  const result = await request(
    controlApiUrl,
    `/edge/deployments/${encodeURIComponent(deployment)}/restore-drills`,
    {
      method: "POST",
      csrf: true,
      stepUpGrant: clientPattern(stepUpGrant, OPAQUE_GRANT, "step_up_grant", "workspace"),
      idempotencyKey: clientPattern(
        idempotencyKey, IDEMPOTENCY, "idempotency_key", "workspace",
      ),
      body,
      signal,
    },
    "workspace",
  );
  const response = restoreDrillResponse(result.payload, "workspace");
  if (response.deployment_id !== deployment
    || (operation === "request" && response.drill.source_backup_id !== sourceBackupId)
    || (operation === "approve" && response.drill.drill_id !== drillId)) {
    invalidResponse("workspace", "drill_owner");
  }
  validateEtag(result.response, response.drill.revision, "workspace");
  return response;
}

export async function listPlatformBackupStatuses({ controlApiUrl, signal } = {}) {
  const result = await request(controlApiUrl, "/platform/edge/backups", { signal }, "platform");
  exact(result.payload, ["statuses", "request_id", "trace_id"], "backup_list", "platform");
  responseArray(result.payload.statuses, 500, "statuses", "platform");
  const statuses = result.payload.statuses.map((item) => backupStatus(item, "platform"));
  unique(statuses.map((item) => item.deployment_id), "deployments", "platform");
  return Object.freeze({ statuses: Object.freeze(statuses), ...trace(result.payload, "platform") });
}

function backupStatusResponse(value, domain) {
  exact(value, ["status", "request_id", "trace_id"], "backup_status_response", domain);
  return Object.freeze({ status: backupStatus(value.status, domain), ...trace(value, domain) });
}

function backupStatus(value, domain) {
  exact(value, [
    "deployment_id", "status", "rpo_objective_seconds", "latest_source_generation",
    "last_success_at", "last_check_outcome", "revision", "updated_at", "latest_backup",
    "recent_backups", "recent_drills",
  ], "backup_status", domain);
  responseArray(value.recent_backups, 20, "recent_backups", domain);
  responseArray(value.recent_drills, 20, "recent_drills", domain);
  const recentBackups = value.recent_backups.map((item) => backupReport(item, domain));
  const recentDrills = value.recent_drills.map((item) => restoreDrill(item, domain));
  unique(recentBackups.map((item) => item.backup_id), "recent_backups", domain);
  unique(recentDrills.map((item) => item.drill_id), "recent_drills", domain);
  const latest = value.latest_backup === null ? null : backupReport(value.latest_backup, domain);
  if (latest && !recentBackups.some((item) => item.backup_id === latest.backup_id)) {
    invalidResponse(domain, "latest_backup");
  }
  return Object.freeze({
    deployment_id: responsePattern(value.deployment_id, IDS.deployment, "deployment_id", domain),
    status: responseChoice(value.status, ["never", "current", "overdue", "failed"],
      "status", domain),
    rpo_objective_seconds: responseInteger(
      value.rpo_objective_seconds, 900, 604_800, "rpo_objective_seconds", domain,
    ),
    latest_source_generation: responseInteger(
      value.latest_source_generation, 0, 2_147_483_647, "latest_source_generation", domain,
    ),
    last_success_at: nullableTimestamp(value.last_success_at, "last_success_at", domain),
    last_check_outcome: value.last_check_outcome === null ? null : responseChoice(
      value.last_check_outcome, ["verified", "failed"], "last_check_outcome", domain,
    ),
    revision: responseInteger(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision", domain),
    updated_at: timestamp(value.updated_at, "updated_at", domain),
    latest_backup: latest,
    recent_backups: Object.freeze(recentBackups),
    recent_drills: Object.freeze(recentDrills),
  });
}

function backupReport(value, domain) {
  exact(value, [
    "backup_id", "source_generation", "schema_head", "storage_kind", "status",
    "size_bucket", "chunk_count", "manifest_sha256", "kms_key_id_sha256",
    "check_outcome", "failure_code", "started_at", "completed_at", "retention_until",
  ], "backup_report", domain);
  const status = responseChoice(value.status, ["completed", "failed", "deleted"],
    "backup_status", domain);
  const checkOutcome = responseChoice(value.check_outcome, ["verified", "failed"],
    "check_outcome", domain);
  const manifest = nullablePattern(value.manifest_sha256, SHA256, "manifest_sha256", domain);
  const kmsKey = nullablePattern(value.kms_key_id_sha256, SHA256, "kms_key_id_sha256", domain);
  const failure = nullablePattern(value.failure_code, SAFE_CODE, "failure_code", domain);
  if (["completed", "deleted"].includes(status) !== (
    checkOutcome === "verified" && Boolean(manifest && kmsKey) && value.chunk_count > 0 && !failure
  )) invalidResponse(domain, "backup_outcome");
  const started = timestamp(value.started_at, "started_at", domain);
  const completed = timestamp(value.completed_at, "completed_at", domain);
  const retained = timestamp(value.retention_until, "retention_until", domain);
  if (Date.parse(completed) < Date.parse(started) || Date.parse(retained) <= Date.parse(completed)) {
    invalidResponse(domain, "backup_timeline");
  }
  return Object.freeze({
    backup_id: responsePattern(value.backup_id, IDS.backup, "backup_id", domain),
    source_generation: responseInteger(
      value.source_generation, 1, 2_147_483_647, "source_generation", domain,
    ),
    schema_head: responsePattern(value.schema_head, SCHEMA, "schema_head", domain),
    storage_kind: responseChoice(value.storage_kind, ["local", "customer_object_store"],
      "storage_kind", domain),
    status,
    size_bucket: responseChoice(value.size_bucket,
      ["lt_100mb", "100mb_1gb", "1gb_10gb", "gte_10gb"], "size_bucket", domain),
    chunk_count: responseInteger(value.chunk_count, 0, 1_000_000, "chunk_count", domain),
    manifest_sha256: manifest,
    kms_key_id_sha256: kmsKey,
    check_outcome: checkOutcome,
    failure_code: failure,
    started_at: started,
    completed_at: completed,
    retention_until: retained,
  });
}

function restoreDrillResponse(value, domain) {
  exact(value, ["deployment_id", "drill", "idempotent_replay", "request_id", "trace_id"],
    "drill_response", domain);
  return Object.freeze({
    deployment_id: responsePattern(value.deployment_id, IDS.deployment, "deployment_id", domain),
    drill: restoreDrill(value.drill, domain),
    idempotent_replay: responseBoolean(value.idempotent_replay, "idempotent_replay", domain),
    ...trace(value, domain),
  });
}

function restoreDrill(value, domain) {
  exact(value, [
    "drill_id", "restore_job_id", "source_backup_id", "status", "quarter_code",
    "rpo_objective_seconds", "rto_objective_seconds", "actual_rpo_seconds",
    "actual_rto_seconds", "isolated_target_sha256", "production_untouched",
    "objective_outcome", "revision", "requested_by_account_id", "approved_by_account_id",
    "created_at", "started_at", "completed_at", "updated_at",
  ], "restore_drill", domain);
  const status = responseChoice(value.status,
    ["requested", "approved", "running", "succeeded", "failed"], "drill_status", domain);
  const actualRpo = nullableInteger(
    value.actual_rpo_seconds, 0, 31_536_000, "actual_rpo_seconds", domain,
  );
  const actualRto = nullableInteger(
    value.actual_rto_seconds, 0, 31_536_000, "actual_rto_seconds", domain,
  );
  const outcome = value.objective_outcome === null ? null : responseChoice(
    value.objective_outcome, ["met", "missed"], "objective_outcome", domain,
  );
  const approvedBy = nullableText(value.approved_by_account_id, 160, "approved_by_account_id", domain);
  const started = nullableTimestamp(value.started_at, "started_at", domain);
  const completed = nullableTimestamp(value.completed_at, "completed_at", domain);
  if (value.production_untouched !== true
    || (["succeeded", "failed"].includes(status)) !== Boolean(completed && outcome)
    || (["approved", "running", "succeeded", "failed"].includes(status)) !== Boolean(approvedBy)
    || (status === "running") !== Boolean(started && !completed)
    || ((actualRpo === null) !== (actualRto === null))) {
    invalidResponse(domain, "drill_state");
  }
  return Object.freeze({
    drill_id: responsePattern(value.drill_id, IDS.drill, "drill_id", domain),
    restore_job_id: responsePattern(value.restore_job_id, IDS.restore, "restore_job_id", domain),
    source_backup_id: responsePattern(value.source_backup_id, IDS.backup, "source_backup_id", domain),
    status,
    quarter_code: responsePattern(value.quarter_code, QUARTER, "quarter_code", domain),
    rpo_objective_seconds: responseInteger(
      value.rpo_objective_seconds, 900, 604_800, "rpo_objective_seconds", domain,
    ),
    rto_objective_seconds: responseInteger(
      value.rto_objective_seconds, 900, 604_800, "rto_objective_seconds", domain,
    ),
    actual_rpo_seconds: actualRpo,
    actual_rto_seconds: actualRto,
    isolated_target_sha256: responsePattern(
      value.isolated_target_sha256, SHA256, "isolated_target_sha256", domain,
    ),
    production_untouched: true,
    objective_outcome: outcome,
    revision: responseInteger(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision", domain),
    requested_by_account_id: responseText(
      value.requested_by_account_id, 160, "requested_by_account_id", domain,
    ),
    approved_by_account_id: approvedBy,
    created_at: timestamp(value.created_at, "created_at", domain),
    started_at: started,
    completed_at: completed,
    updated_at: timestamp(value.updated_at, "updated_at", domain),
  });
}

async function request(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.csrf) headers["X-CSRF-Token"] = domain === "workspace"
    ? readWorkspaceCsrfCookie() : readCookie(PLATFORM_CSRF_COOKIE);
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
    throw apiError(domain, 0, "edge_backup.network_unavailable", "Backup service unavailable");
  }
  const payload = await readJson(response, domain);
  if (!response.ok) {
    const code = SAFE_CODE.test(String(payload?.code ?? ""))
      ? payload.code : "edge_backup.request_failed";
    throw apiError(domain, response.status, code, safeText(payload?.message), payload?.request_id);
  }
  const directives = String(response.headers.get("Cache-Control") ?? "")
    .toLowerCase().split(",").map((item) => item.trim());
  const requestId = response.headers.get("X-Request-ID") ?? "";
  const traceId = response.headers.get("X-Trace-ID") ?? "";
  if (!directives.includes("no-store") || !directives.includes("private")
    || !CHAIN.test(requestId) || !CHAIN.test(traceId)
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

function plain(value, label, domain) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse(domain, label);
}

function trace(value, domain) {
  return Object.freeze({
    request_id: responseText(value.request_id, 160, "request_id", domain),
    trace_id: responseText(value.trace_id, 160, "trace_id", domain),
  });
}

function validateEtag(response, revision, domain) {
  if (response.headers.get("ETag") !== `"${revision}"`) invalidResponse(domain, "etag");
}

function responseArray(value, maximum, label, domain) {
  if (!Array.isArray(value) || value.length > maximum) invalidResponse(domain, label);
}

function unique(values, label, domain) {
  if (values.length !== new Set(values).size) invalidResponse(domain, label);
}

function clientPattern(value, pattern, label, domain) {
  const text = String(value ?? "");
  if (!pattern.test(text)) invalidRequest(domain, label);
  return text;
}

function clientChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidRequest(domain, label);
  return value;
}

function clientInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(domain, label);
  }
  return value;
}

function responsePattern(value, pattern, label, domain) {
  const text = responseText(value, 4096, label, domain);
  if (!pattern.test(text)) invalidResponse(domain, label);
  return text;
}

function nullablePattern(value, pattern, label, domain) {
  return value === null ? null : responsePattern(value, pattern, label, domain);
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

function responseInteger(value, minimum, maximum, label, domain) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(domain, label);
  }
  return value;
}

function nullableInteger(value, minimum, maximum, label, domain) {
  return value === null ? null : responseInteger(value, minimum, maximum, label, domain);
}

function responseChoice(value, choices, label, domain) {
  if (!choices.includes(value)) invalidResponse(domain, label);
  return value;
}

function responseBoolean(value, label, domain) {
  if (typeof value !== "boolean") invalidResponse(domain, label);
  return value;
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
  throw apiError(domain, 422, "edge_backup.request_invalid", `Invalid ${label}`);
}

function invalidResponse(domain, label) {
  throw apiError(domain, 502, "edge_backup.response_invalid", `Invalid ${label}`);
}
