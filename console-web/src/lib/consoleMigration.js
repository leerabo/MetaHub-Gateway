/** Strict PRD-147 client for platform console-domain migration governance. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

export const CONSOLE_MIGRATION_DOMAINS = Object.freeze([
  "channel_attribution",
  "customer_success",
  "growth_analytics",
  "brand_distribution",
  "provider_engineering",
  "commercial_accounting",
  "console_experience",
  "workspace_navigation",
]);

const DOMAIN = new Set(CONSOLE_MIGRATION_DOMAINS);
const STATE = new Set([
  "inventoried", "backfill_queued", "backfilling", "shadowing", "ready",
  "quiescing", "cutting_over", "validating", "active", "rollback_queued",
  "rolling_back", "rolled_back", "blocked",
]);
const RECONCILIATION = new Set([
  "missing", "pending", "matched", "mismatched", "unavailable", "superseded",
]);
const RUNTIME_SURFACE = new Set([
  "scheduler-lease-coordinator", "risk-fact-sink", "control-telemetry",
]);
const RUNTIME_CLASS = new Set(["per_replica_control_bounded", "per_replica_bounded"]);
const ROLLBACK_REASON = new Set([
  "validation_failed", "dependency_failure", "operator_recovery", "release_regression",
]);
const MIGRATION_ID = /^cdm_[0-9a-f]{32}$/u;
const OPERATION_ID = /^(?:cdm|cmb)_[0-9a-f]{32}$/u;
const RECONCILIATION_ID = /^drc_[0-9a-f]{32}$/u;
const HEX64 = /^[0-9a-f]{64}$/u;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{2,79}$/u;
const SAFE_CHAIN = /^[A-Za-z0-9_.:-]{1,160}$/u;
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;

export async function getConsoleMigrationStatus({
  controlApiUrl,
  domainCode = "",
  state = "",
  workspaceRef = "",
  cursor = "",
  limit = 50,
  signal,
} = {}) {
  const query = new URLSearchParams();
  if (domainCode) query.set("domain_code", choice(domainCode, DOMAIN, "domain_code"));
  if (state) query.set("state", choice(state, STATE, "state"));
  if (workspaceRef) query.set("workspace_ref", pattern(workspaceRef, HEX64, "workspace_ref"));
  if (cursor) query.set("cursor", bounded(cursor, 43, 1024, "cursor"));
  query.set("limit", String(integer(limit, 1, 100, "limit")));
  const response = await request(
    controlApiUrl,
    `/platform/console-migration/status?${query}`,
    { signal },
  );
  return normalizeConsoleMigrationStatus(response.payload, response.headers);
}

export function submitConsoleMigrationBackfill(input = {}) {
  return submitCommand(input, "backfills", normalizeBackfillCommand(input.command));
}

export function submitConsoleMigrationCutover(input = {}) {
  return submitCommand(input, "cutovers", normalizeCutoverCommand(input.command));
}

export function submitConsoleMigrationRollback(input = {}) {
  return submitCommand(input, "rollbacks", normalizeRollbackCommand(input.command));
}

export function normalizeConsoleMigrationStatus(value, headers = new Headers()) {
  exact(value, [
    "contract", "domains", "items", "next_cursor", "permission_fingerprint",
    "request_id", "runtime_surfaces", "scheduler", "trace_id",
  ]);
  const normalized = {
    domains: array(value.domains, 8, 8, "domains").map(domainStatus),
    items: array(value.items, 0, 100, "items").map(migrationItem),
    next_cursor: nullableBounded(value.next_cursor, 43, 1024, "next_cursor"),
    scheduler: schedulerStatus(value.scheduler),
    runtime_surfaces: array(value.runtime_surfaces, 3, 3, "runtime_surfaces")
      .map(runtimeSurface),
    contract: contractStatus(value.contract),
    permission_fingerprint: pattern(
      value.permission_fingerprint, HEX64, "permission_fingerprint",
    ),
    request_id: pattern(value.request_id, SAFE_CHAIN, "request_id"),
    trace_id: pattern(value.trace_id, SAFE_CHAIN, "trace_id"),
  };
  if (new Set(normalized.domains.map((item) => item.domain_code)).size !== 8
    || CONSOLE_MIGRATION_DOMAINS.some(
      (domainCode) => !normalized.domains.some((item) => item.domain_code === domainCode),
    )
    || new Set(normalized.runtime_surfaces.map(
      (item) => item.runtime_surface_code,
    )).size !== 3) invalidResponse("manifest");
  requirePrivate(headers, normalized.permission_fingerprint);
  return deepFreeze(normalized);
}

export function normalizeConsoleMigrationCommand(value, headers = new Headers()) {
  exact(value, [
    "accepted", "contract_generation", "idempotent_replay", "migration_id",
    "operation_generation", "operation_id", "permission_fingerprint",
    "polling_location_code", "registry_generation", "request_id", "revision",
    "state", "trace_id",
  ]);
  const operationId = pattern(value.operation_id, OPERATION_ID, "operation_id");
  const migrationId = value.migration_id === null
    ? null
    : pattern(value.migration_id, MIGRATION_ID, "migration_id");
  if ((operationId.startsWith("cdm_") && migrationId !== operationId)
    || (operationId.startsWith("cmb_") && migrationId !== null)
    || value.accepted !== true
    || typeof value.idempotent_replay !== "boolean"
    || value.polling_location_code !== "console-migration-status") {
    invalidResponse("command");
  }
  const normalized = {
    operation_id: operationId,
    migration_id: migrationId,
    state: pattern(value.state, SAFE_CODE, "state"),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
    operation_generation: integer(
      value.operation_generation, 1, Number.MAX_SAFE_INTEGER, "operation_generation",
    ),
    accepted: true,
    idempotent_replay: value.idempotent_replay,
    polling_location_code: value.polling_location_code,
    contract_generation: integer(
      value.contract_generation, 0, Number.MAX_SAFE_INTEGER, "contract_generation",
    ),
    registry_generation: integer(
      value.registry_generation, 1, Number.MAX_SAFE_INTEGER, "registry_generation",
    ),
    permission_fingerprint: pattern(
      value.permission_fingerprint, HEX64, "permission_fingerprint",
    ),
    request_id: pattern(value.request_id, SAFE_CHAIN, "request_id"),
    trace_id: pattern(value.trace_id, SAFE_CHAIN, "trace_id"),
  };
  requirePrivate(headers, normalized.permission_fingerprint);
  const etag = headers.get?.("etag") ?? "";
  const polling = headers.get?.("x-polling-location-code") ?? "";
  if (etag !== `"${normalized.revision}"` || polling !== normalized.polling_location_code) {
    invalidResponse("headers");
  }
  return deepFreeze(normalized);
}

async function submitCommand(input, path, body) {
  const expectedRevision = integer(
    input.expectedRevision, 0, Number.MAX_SAFE_INTEGER, "expected_revision",
  );
  if (body.expected_revision !== expectedRevision) invalidRequest("expected_revision");
  const response = await request(input.controlApiUrl, `/platform/console-migration/${path}`, {
    method: "POST",
    body,
    headers: {
      "Idempotency-Key": pattern(input.idempotencyKey, IDEMPOTENCY, "idempotency_key"),
      "If-Match": `"${expectedRevision}"`,
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "X-Step-Up-Grant": pattern(input.stepUpGrant, OPAQUE, "step_up_grant"),
    },
    signal: input.signal,
  });
  return normalizeConsoleMigrationCommand(response.payload, response.headers);
}

function normalizeBackfillCommand(value) {
  object(value, "command");
  const action = choice(value.action, new Set(["prepare", "start", "resume", "cancel"]), "action");
  if (action === "prepare") {
    exactRequest(value, [
      "action", "confirmed", "domain_code", "expected_revision", "resource_profile",
      "scope", "workspace_ref",
    ]);
    const scope = choice(value.scope, new Set(["workspace", "all_active"]), "scope");
    const workspaceRef = value.workspace_ref === null
      ? null
      : pattern(value.workspace_ref, HEX64, "workspace_ref");
    if ((scope === "workspace") !== (workspaceRef !== null)) invalidRequest("scope");
    return {
      action,
      domain_code: choice(value.domain_code, DOMAIN, "domain_code"),
      scope,
      workspace_ref: workspaceRef,
      expected_revision: integer(value.expected_revision, 0, Number.MAX_SAFE_INTEGER, "expected_revision"),
      resource_profile: exactValue(value.resource_profile, "bounded-v1", "resource_profile"),
      confirmed: confirmed(value.confirmed),
    };
  }
  exactRequest(value, [
    "action", "confirmed", "expected_revision", "migration_id",
    "reconciliation_generation", "resource_profile",
  ]);
  return {
    action,
    migration_id: pattern(value.migration_id, MIGRATION_ID, "migration_id"),
    expected_revision: integer(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision"),
    reconciliation_generation: integer(
      value.reconciliation_generation, 0, Number.MAX_SAFE_INTEGER, "reconciliation_generation",
    ),
    resource_profile: exactValue(value.resource_profile, "bounded-v1", "resource_profile"),
    confirmed: confirmed(value.confirmed),
  };
}

function normalizeCutoverCommand(value) {
  object(value, "command");
  const action = choice(
    value.action, new Set(["prepare", "approve", "security_review", "execute"]), "action",
  );
  const keys = ["action", "confirmed", "expected_revision", "migration_id"];
  if (action === "security_review") keys.push("reconciliation_id", "reconciliation_revision");
  exactRequest(value, keys);
  const normalized = baseTransition(value, action);
  if (action === "security_review") {
    normalized.reconciliation_id = pattern(
      value.reconciliation_id, RECONCILIATION_ID, "reconciliation_id",
    );
    normalized.reconciliation_revision = integer(
      value.reconciliation_revision, 1, Number.MAX_SAFE_INTEGER, "reconciliation_revision",
    );
  }
  return normalized;
}

function normalizeRollbackCommand(value) {
  object(value, "command");
  const action = choice(
    value.action,
    new Set(["prepare", "approve", "security_review", "execute", "resume"]),
    "action",
  );
  const keys = [
    "action", "confirmed", "expected_revision", "migration_id", "reason_code",
    "target_generation",
  ];
  if (action === "security_review") keys.push("reconciliation_id", "reconciliation_revision");
  exactRequest(value, keys);
  const normalized = {
    ...baseTransition(value, action),
    reason_code: choice(value.reason_code, ROLLBACK_REASON, "reason_code"),
    target_generation: integer(
      value.target_generation, 1, Number.MAX_SAFE_INTEGER, "target_generation",
    ),
  };
  if (action === "security_review") {
    normalized.reconciliation_id = pattern(
      value.reconciliation_id, RECONCILIATION_ID, "reconciliation_id",
    );
    normalized.reconciliation_revision = integer(
      value.reconciliation_revision, 1, Number.MAX_SAFE_INTEGER, "reconciliation_revision",
    );
  }
  return normalized;
}

function baseTransition(value, action) {
  return {
    action,
    migration_id: pattern(value.migration_id, MIGRATION_ID, "migration_id"),
    expected_revision: integer(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision"),
    confirmed: confirmed(value.confirmed),
  };
}

function domainStatus(value) {
  exact(value, ["active_count", "blocked_count", "domain_code", "migration_count", "source_migration"]);
  return {
    domain_code: choice(value.domain_code, DOMAIN, "domain_code"),
    source_migration: pattern(value.source_migration, /^01(?:3[1-8])$/u, "source_migration"),
    migration_count: integer(value.migration_count, 0, Number.MAX_SAFE_INTEGER, "migration_count"),
    blocked_count: integer(value.blocked_count, 0, Number.MAX_SAFE_INTEGER, "blocked_count"),
    active_count: integer(value.active_count, 0, Number.MAX_SAFE_INTEGER, "active_count"),
  };
}

function migrationItem(value) {
  exact(value, [
    "allowed_actions", "approval_ready", "domain_code", "last_safe_at", "migration_id",
    "operation_generation", "progress_basis_points", "reason_code", "reconciliation_status",
    "revision", "route_ready", "scheduler_ready", "security_review_ready", "state",
    "target_generation", "workspace_ref",
  ]);
  return {
    migration_id: pattern(value.migration_id, MIGRATION_ID, "migration_id"),
    workspace_ref: pattern(value.workspace_ref, HEX64, "workspace_ref"),
    domain_code: choice(value.domain_code, DOMAIN, "domain_code"),
    state: choice(value.state, STATE, "state"),
    progress_basis_points: integer(value.progress_basis_points, 0, 10000, "progress_basis_points"),
    operation_generation: integer(value.operation_generation, 1, Number.MAX_SAFE_INTEGER, "operation_generation"),
    target_generation: integer(value.target_generation, 0, Number.MAX_SAFE_INTEGER, "target_generation"),
    revision: integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision"),
    reconciliation_status: choice(
      value.reconciliation_status, RECONCILIATION, "reconciliation_status",
    ),
    reason_code: pattern(value.reason_code, SAFE_CODE, "reason_code"),
    approval_ready: boolean(value.approval_ready, "approval_ready"),
    security_review_ready: boolean(value.security_review_ready, "security_review_ready"),
    route_ready: boolean(value.route_ready, "route_ready"),
    scheduler_ready: boolean(value.scheduler_ready, "scheduler_ready"),
    last_safe_at: timestamp(value.last_safe_at, "last_safe_at"),
    allowed_actions: array(value.allowed_actions, 0, 8, "allowed_actions")
      .map((item) => pattern(item, SAFE_CODE, "allowed_action")),
  };
}

function schedulerStatus(value) {
  exact(value, [
    "active_lease_count", "baseline_slot_count", "cluster_context_count",
    "enabled_context_count", "final_installer_count", "manifest_matches",
    "queue_contract_count", "registry_generation",
  ]);
  if (value.baseline_slot_count !== 41 || value.final_installer_count !== 36
    || value.cluster_context_count !== 37) invalidResponse("scheduler_manifest");
  return {
    ...value,
    queue_contract_count: integer(value.queue_contract_count, 37, Number.MAX_SAFE_INTEGER, "queue_contract_count"),
    registry_generation: integer(value.registry_generation, 1, Number.MAX_SAFE_INTEGER, "registry_generation"),
    enabled_context_count: integer(value.enabled_context_count, 0, 37, "enabled_context_count"),
    active_lease_count: integer(value.active_lease_count, 0, Number.MAX_SAFE_INTEGER, "active_lease_count"),
    manifest_matches: boolean(value.manifest_matches, "manifest_matches"),
  };
}

function runtimeSurface(value) {
  exact(value, [
    "batch_limit", "degraded_replica_count", "execution_class", "healthy_replica_count",
    "queue_capacity", "runtime_surface_code",
  ]);
  return {
    runtime_surface_code: choice(value.runtime_surface_code, RUNTIME_SURFACE, "runtime_surface_code"),
    execution_class: choice(value.execution_class, RUNTIME_CLASS, "execution_class"),
    queue_capacity: integer(value.queue_capacity, 1, Number.MAX_SAFE_INTEGER, "queue_capacity"),
    batch_limit: integer(value.batch_limit, 1, Number.MAX_SAFE_INTEGER, "batch_limit"),
    healthy_replica_count: integer(value.healthy_replica_count, 0, Number.MAX_SAFE_INTEGER, "healthy_replica_count"),
    degraded_replica_count: integer(value.degraded_replica_count, 0, Number.MAX_SAFE_INTEGER, "degraded_replica_count"),
  };
}

function contractStatus(value) {
  exact(value, [
    "blocking_check_count", "contract_generation", "legacy_write_fenced",
    "route_deny_epoch", "status",
  ]);
  return {
    contract_generation: integer(value.contract_generation, 0, Number.MAX_SAFE_INTEGER, "contract_generation"),
    route_deny_epoch: integer(value.route_deny_epoch, 1, Number.MAX_SAFE_INTEGER, "route_deny_epoch"),
    legacy_write_fenced: boolean(value.legacy_write_fenced, "legacy_write_fenced"),
    status: choice(value.status, new Set(["expanded", "active"]), "status"),
    blocking_check_count: integer(value.blocking_check_count, 0, Number.MAX_SAFE_INTEGER, "blocking_check_count"),
  };
}

async function request(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers ?? {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({
      code: "console_migration.dependency_unavailable",
      message: "迁移治理服务暂时不可用。",
    });
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const error = new PlatformApiError({
      status: response.status,
      code: safeErrorCode(payload?.code),
      message: safeErrorMessage(payload?.message),
      requestId: safeChain(payload?.request_id),
    });
    error.traceId = safeChain(payload?.trace_id);
    throw error;
  }
  return { payload, headers: response.headers };
}

function requirePrivate(headers, fingerprint) {
  const cacheControl = (headers.get?.("cache-control") ?? "").toLowerCase();
  if (!cacheControl.includes("no-store") || !cacheControl.includes("private")
    || headers.get?.("x-permission-fingerprint") !== fingerprint) {
    invalidResponse("private_headers");
  }
}

function apiUrl(controlApiUrl, path) {
  try {
    const base = new URL(String(controlApiUrl));
    if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
      throw new Error();
    }
    return new URL(path, base).toString();
  } catch {
    throw new PlatformApiError({
      code: "console_migration.invalid",
      message: "平台服务地址无效。",
    });
  }
}

function exact(value, keys) {
  object(value, "response");
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) {
    invalidResponse("shape");
  }
}
function exactRequest(value, keys) {
  object(value, "command");
  if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) {
    invalidRequest("command");
  }
}
function object(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)
    || Object.getPrototypeOf(value) !== Object.prototype) invalidRequest(field);
  return value;
}
function array(value, minimum, maximum, field) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    invalidResponse(field);
  }
  return value;
}
function choice(value, choices, field) {
  if (!choices.has(value)) invalidRequest(field);
  return value;
}
function pattern(value, expression, field) {
  if (typeof value !== "string" || !expression.test(value)) invalidRequest(field);
  return value;
}
function bounded(value, minimum, maximum, field) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f-\u009f]/u.test(value)) invalidRequest(field);
  return value;
}
function nullableBounded(value, minimum, maximum, field) {
  return value === null ? null : bounded(value, minimum, maximum, field);
}
function integer(value, minimum, maximum, field) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidRequest(field);
  return value;
}
function boolean(value, field) {
  if (typeof value !== "boolean") invalidRequest(field);
  return value;
}
function timestamp(value, field) {
  const checked = bounded(value, 20, 40, field);
  if (!Number.isFinite(Date.parse(checked))) invalidRequest(field);
  return checked;
}
function confirmed(value) {
  if (value !== true) invalidRequest("confirmed");
  return true;
}
function exactValue(value, expected, field) {
  if (value !== expected) invalidRequest(field);
  return value;
}
function safeErrorCode(value) {
  return typeof value === "string" && /^console_migration\.[a-z0-9_.-]{2,100}$/u.test(value)
    ? value
    : "console_migration.dependency_unavailable";
}
function safeErrorMessage(value) {
  return typeof value === "string" && value.length <= 240
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
    ? value
    : "迁移治理请求未完成。";
}
function safeChain(value) {
  return typeof value === "string" && SAFE_CHAIN.test(value) ? value : "-";
}
function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}
function invalidRequest(field) {
  throw new PlatformApiError({
    code: "console_migration.invalid",
    message: `迁移治理字段无效：${String(field).slice(0, 40)}`,
  });
}
function invalidResponse(field) {
  throw new PlatformApiError({
    status: 502,
    code: "console_migration.dependency_unavailable",
    message: `迁移治理响应无效：${String(field).slice(0, 40)}`,
  });
}
