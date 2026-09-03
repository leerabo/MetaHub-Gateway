/** Strict WorkspaceSession client for PRD-144 subscription and Internal FinOps. */

import { AuthApiError } from "./auth.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const PATTERN = Object.freeze({
  cursor: /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u,
  dispute: /^bdc_[0-9a-f]{32}$/u,
  idempotency: /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u,
  opaque: /^[A-Za-z0-9_-]{43,128}$/u,
  planCatalog: /^spc_[0-9a-f]{32}$/u,
  planRelease: /^pplr_[0-9a-f]{32}$/u,
  planVersion: /^spv_[0-9a-f]{32}$/u,
  rate: /^pcr_[0-9a-f]{32}$/u,
  rateVersion: /^pcrv_[0-9a-f]{32}$/u,
  referenceRate: /^pprr_[0-9a-f]{32}$/u,
  safeId: /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u,
  sha256: /^[0-9a-f]{64}$/u,
  subscription: /^sub_[0-9a-f]{32}$/u,
  workspaceRef: /^wref_[0-9a-f]{32}$/u,
});

const CURRENCIES = Object.freeze(["CNY", "USD"]);
const PROJECTION_STATES = Object.freeze([
  "current", "partial", "stale", "reconciliation_required", "unknown",
]);

export async function getWorkspaceCommercialSubscription({
  controlApiUrl,
  period = "current",
  cursor = null,
  signal,
} = {}) {
  const query = new URLSearchParams();
  if (period !== null && period !== "") {
    if (period !== "current") invalidRequest("period");
    query.set("period", period);
  }
  if (cursor !== null && cursor !== "") {
    query.set("cursor", requestPattern(cursor, PATTERN.cursor, "cursor"));
  }
  const suffix = query.size ? `?${query}` : "";
  const payload = await request(
    controlApiUrl, `/workspace/subscription${suffix}`, { signal },
  );
  exact(payload, [
    "subscription", "permission_revision", "permission_fingerprint", "request_id",
    "trace_id",
  ]);
  const subscription = payload.subscription === null
    ? null
    : subscriptionInfo(payload.subscription);
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision");
  return Object.freeze({
    subscription,
    permission_revision: payload.permission_revision,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function createWorkspaceCommercialDispute({
  controlApiUrl,
  command,
  expectedSourceRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = disputeCreateCommand(command);
  const payload = await request(
    controlApiUrl,
    "/workspace/billing-disputes",
    writeOptions(
      "POST", body, expectedSourceRevision, idempotencyKey, stepUpGrant, signal,
    ),
  );
  exact(payload, [
    "dispute", "command_id", "adjustment_id", "idempotent_replay",
    "permission_revision", "permission_fingerprint", "request_id", "trace_id",
  ]);
  const dispute = disputeInfo(payload.dispute);
  nullableString(payload.adjustment_id, 1, 128, "adjustment_id");
  boolean(payload.idempotent_replay, "idempotent_replay");
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision");
  return Object.freeze({
    dispute,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    adjustment_id: payload.adjustment_id,
    idempotent_replay: payload.idempotent_replay,
    permission_revision: payload.permission_revision,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function listWorkspaceProviderCostRates({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = rateQuery(filters, cursor, limit);
  const payload = await request(
    controlApiUrl, `/workspace/provider-cost-rates?${query}`, { signal },
  );
  exact(payload, [
    "rates", "next_cursor", "limit", "permission_revision", "permission_fingerprint",
    "request_id", "trace_id",
  ]);
  const rates = frozenArray(payload.rates, 0, 100, "rates", rateInfo);
  integer(payload.limit, 1, 100, "limit");
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision");
  nullableCursor(payload.next_cursor);
  if (rates.length > payload.limit) invalidResponse("pagination");
  return Object.freeze({
    rates,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_revision: payload.permission_revision,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function applyWorkspaceProviderCostRateCommand({
  controlApiUrl,
  command,
  expectedHeadRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = rateCommand(command);
  const payload = await request(
    controlApiUrl,
    "/workspace/provider-cost-rates",
    writeOptions(
      "PATCH", body, expectedHeadRevision, idempotencyKey, stepUpGrant, signal,
    ),
  );
  exact(payload, [
    "rate", "command_id", "idempotent_replay", "permission_revision",
    "permission_fingerprint", "request_id", "trace_id",
  ]);
  const rate = rateInfo(payload.rate);
  if (body.workspace_provider_cost_rate_id
    && rate.workspace_provider_cost_rate_id !== body.workspace_provider_cost_rate_id) {
    invalidResponse("rate_owner");
  }
  if (body.provider_id && rate.provider_id !== body.provider_id) invalidResponse("rate_owner");
  if (body.model_id && rate.model_id !== body.model_id) invalidResponse("rate_owner");
  boolean(payload.idempotent_replay, "idempotent_replay");
  integer(payload.permission_revision, 1, Number.MAX_SAFE_INTEGER, "permission_revision");
  return Object.freeze({
    rate,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    idempotent_replay: payload.idempotent_replay,
    permission_revision: payload.permission_revision,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

function rateQuery(filters, cursor, limit) {
  requestPlain(filters, "filters");
  const allowed = new Set([
    "provider_id", "model_id", "currency", "status", "effective_at",
  ]);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (!allowed.has(key)) invalidRequest(`filter.${key}`);
    if (value === "" || value === null || value === undefined) continue;
    if (key === "provider_id" || key === "model_id") {
      requestPattern(value, PATTERN.safeId, key);
    }
    if (key === "currency") requestChoice(value, CURRENCIES, key);
    if (key === "status") requestChoice(value, ["active", "superseded", "disabled"], key);
    if (key === "effective_at") requestTimestamp(value, key);
    query.set(key, String(value));
  }
  if (cursor !== null && cursor !== "") {
    query.set("cursor", requestPattern(cursor, PATTERN.cursor, "cursor"));
  }
  query.set("limit", String(requestInteger(limit, 1, 100, "limit")));
  return query.toString();
}

function disputeCreateCommand(value) {
  requestPlain(value, "command");
  const sourceKind = requestChoice(
    value.source_kind, ["statement", "invoice", "statement_line"], "source_kind",
  );
  const keys = sourceKind === "statement_line"
    ? ["source_kind", "source_id", "source_line_id", "reason_code"]
    : ["source_kind", "source_id", "reason_code"];
  requestExact(value, keys);
  requestPattern(value.source_id, PATTERN.safeId, "source_id");
  if (sourceKind === "statement_line") {
    requestPattern(value.source_line_id, PATTERN.safeId, "source_line_id");
  }
  requestChoice(value.reason_code, [
    "amount_mismatch", "duplicate_charge", "service_not_delivered", "tax_mismatch",
  ], "reason_code");
  return Object.freeze({ ...value });
}

function rateCommand(value) {
  requestPlain(value, "command");
  const action = requestChoice(
    value.action, ["create_version", "adopt_reference", "disable"], "action",
  );
  if (action === "create_version") {
    const optionalUntil = Object.hasOwn(value, "effective_until") ? ["effective_until"] : [];
    requestExact(value, [
      "action", "provider_id", "model_id", "currency", "input_rate_micros_per_1k",
      "output_rate_micros_per_1k", "effective_from", ...optionalUntil,
    ]);
    requestPattern(value.provider_id, PATTERN.safeId, "provider_id");
    requestPattern(value.model_id, PATTERN.safeId, "model_id");
    requestChoice(value.currency, CURRENCIES, "currency");
    requestInteger(
      value.input_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "input_rate",
    );
    requestInteger(
      value.output_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "output_rate",
    );
    requestTimestamp(value.effective_from, "effective_from");
    if (Object.hasOwn(value, "effective_until")) {
      requestTimestamp(value.effective_until, "effective_until");
      if (String(value.effective_until) <= String(value.effective_from)) {
        invalidRequest("effective_interval");
      }
    }
  } else if (action === "adopt_reference") {
    requestExact(value, [
      "action", "provider_id", "model_id", "currency",
      "provider_reference_rate_release_id",
    ]);
    requestPattern(value.provider_id, PATTERN.safeId, "provider_id");
    requestPattern(value.model_id, PATTERN.safeId, "model_id");
    requestChoice(value.currency, CURRENCIES, "currency");
    requestPattern(
      value.provider_reference_rate_release_id, PATTERN.referenceRate, "reference_rate_id",
    );
  } else {
    requestExact(value, ["action", "workspace_provider_cost_rate_id"]);
    requestPattern(
      value.workspace_provider_cost_rate_id, PATTERN.rate, "provider_cost_rate_id",
    );
  }
  return Object.freeze({ ...value });
}

function subscriptionInfo(value) {
  exact(value, [
    "workspace_customer_ref", "subscription_id", "plan_catalog_id", "plan_version_id",
    "platform_plan_catalog_release_id", "plan_code", "display_name",
    "subscription_status", "current_period_started_at", "current_period_ends_at",
    "entitlement_sha256", "currency", "current_due_minor", "paid_minor",
    "refunded_minor", "invoiced_minor", "order_status", "payment_status",
    "invoice_status", "dispute_status", "projection_state", "source_watermark",
    "source_sha256", "revision", "projection_generation", "invoices", "disputes",
    "actions", "projected_at",
  ]);
  pattern(value.workspace_customer_ref, PATTERN.workspaceRef, "workspace_customer_ref");
  pattern(value.subscription_id, PATTERN.subscription, "subscription_id");
  pattern(value.plan_catalog_id, PATTERN.planCatalog, "plan_catalog_id");
  pattern(value.plan_version_id, PATTERN.planVersion, "plan_version_id");
  nullablePattern(
    value.platform_plan_catalog_release_id, PATTERN.planRelease, "plan_release_id",
  );
  pattern(value.plan_code, /^[a-z][a-z0-9-]{1,31}$/u, "plan_code");
  safeString(value.display_name, 1, 80, "display_name");
  safeString(value.subscription_status, 1, 80, "subscription_status");
  timestamp(value.current_period_started_at, "current_period_started_at");
  nullableTimestamp(value.current_period_ends_at, "current_period_ends_at");
  hash(value.entitlement_sha256, "entitlement_sha256");
  choice(value.currency, CURRENCIES, "currency");
  for (const key of [
    "current_due_minor", "paid_minor", "refunded_minor", "invoiced_minor",
  ]) nullableInteger(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  for (const key of [
    "order_status", "payment_status", "invoice_status", "dispute_status",
  ]) safeString(value[key], 1, 80, key);
  choice(value.projection_state, PROJECTION_STATES, "projection_state");
  integer(value.source_watermark, 0, Number.MAX_SAFE_INTEGER, "source_watermark");
  hash(value.source_sha256, "source_sha256");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.projection_generation, 1, Number.MAX_SAFE_INTEGER, "projection_generation");
  const invoices = frozenArray(value.invoices, 0, 500, "invoices", invoiceInfo);
  const disputes = frozenArray(value.disputes, 0, 500, "disputes", disputeInfo);
  actionList(value.actions, null, "actions");
  timestamp(value.projected_at, "projected_at");
  return Object.freeze({
    ...value,
    invoices,
    disputes,
    actions: Object.freeze([...value.actions]),
  });
}

function invoiceInfo(value) {
  exact(value, [
    "platform_invoice_id", "currency", "subtotal_minor", "tax_rate_basis_points",
    "tax_minor", "total_minor", "status", "invoice_number_masked",
    "artifact_manifest_sha256", "source_revision", "generation", "updated_at",
  ]);
  pattern(value.platform_invoice_id, /^pinv_[0-9a-f]{32}$/u, "invoice_id");
  choice(value.currency, CURRENCIES, "currency");
  for (const key of ["subtotal_minor", "tax_minor", "total_minor"]) {
    integer(value[key], 0, Number.MAX_SAFE_INTEGER, key);
  }
  integer(value.tax_rate_basis_points, 0, 10_000, "tax_rate_basis_points");
  choice(value.status, [
    "requested", "processing", "issued", "failed", "reversal_pending", "reversed",
    "replaced",
  ], "invoice_status");
  nullableString(value.invoice_number_masked, 1, 160, "invoice_number_masked");
  nullablePattern(
    value.artifact_manifest_sha256, PATTERN.sha256, "artifact_manifest_sha256",
  );
  integer(value.source_revision, 1, Number.MAX_SAFE_INTEGER, "source_revision");
  integer(value.generation, 1, Number.MAX_SAFE_INTEGER, "generation");
  timestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value });
}

function disputeInfo(value) {
  exact(value, [
    "billing_dispute_case_id", "source_kind", "source_ref", "currency",
    "disputed_amount_minor", "reason_code", "status", "source_revision",
    "state_revision", "decision_code", "credited_amount_minor", "actions",
    "created_at", "updated_at", "resolved_at",
  ]);
  pattern(value.billing_dispute_case_id, PATTERN.dispute, "dispute_id");
  choice(value.source_kind, ["statement", "invoice", "statement_line"], "source_kind");
  safeString(value.source_ref, 1, 128, "source_ref");
  choice(value.currency, CURRENCIES, "currency");
  integer(value.disputed_amount_minor, 1, Number.MAX_SAFE_INTEGER, "disputed_amount_minor");
  choice(value.reason_code, [
    "amount_mismatch", "duplicate_charge", "service_not_delivered", "tax_mismatch",
  ], "reason_code");
  choice(
    value.status, ["submitted", "under_review", "approved", "rejected", "withdrawn"], "status",
  );
  integer(value.source_revision, 1, Number.MAX_SAFE_INTEGER, "source_revision");
  integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER, "state_revision");
  nullableChoice(value.decision_code, [
    "facts_confirmed", "duplicate_confirmed", "service_credit", "claim_not_supported",
  ], "decision_code");
  integer(value.credited_amount_minor, 0, Number.MAX_SAFE_INTEGER, "credited_amount_minor");
  actionList(value.actions, ["withdraw"], "actions");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.resolved_at, "resolved_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
}

function rateInfo(value) {
  exact(value, [
    "workspace_provider_cost_rate_id", "workspace_provider_cost_rate_version_id",
    "provider_id", "provider_code", "provider_name", "model_id", "model_alias",
    "currency", "version_number", "input_rate_micros_per_1k",
    "output_rate_micros_per_1k", "effective_from", "effective_until", "source_type",
    "source_ref", "status", "revision", "head_revision", "adoption_state",
    "content_sha256", "actions", "created_at",
  ]);
  pattern(value.workspace_provider_cost_rate_id, PATTERN.rate, "provider_cost_rate_id");
  pattern(
    value.workspace_provider_cost_rate_version_id, PATTERN.rateVersion, "rate_version_id",
  );
  pattern(value.provider_id, PATTERN.safeId, "provider_id");
  safeString(value.provider_code, 1, 80, "provider_code");
  safeString(value.provider_name, 1, 160, "provider_name");
  pattern(value.model_id, PATTERN.safeId, "model_id");
  safeString(value.model_alias, 1, 160, "model_alias");
  choice(value.currency, CURRENCIES, "currency");
  integer(value.version_number, 1, Number.MAX_SAFE_INTEGER, "version_number");
  integer(value.input_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "input_rate");
  integer(value.output_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "output_rate");
  timestamp(value.effective_from, "effective_from");
  nullableTimestamp(value.effective_until, "effective_until");
  choice(value.source_type, [
    "manual", "platform_reference", "legacy_model_price", "legacy_model",
  ], "source_type");
  safeString(value.source_ref, 1, 128, "source_ref");
  choice(value.status, ["active", "superseded", "disabled"], "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.head_revision, 1, Number.MAX_SAFE_INTEGER, "head_revision");
  choice(value.adoption_state, ["active", "disabled"], "adoption_state");
  hash(value.content_sha256, "content_sha256");
  actionList(value.actions, ["create_version", "adopt_reference", "disable"], "actions");
  timestamp(value.created_at, "created_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
}

function writeOptions(method, body, revision, idempotencyKey, stepUpGrant, signal) {
  return {
    method,
    body,
    headers: {
      "X-CSRF-Token": readWorkspaceCsrfCookie(),
      "If-Match": `"${requestInteger(
        revision, 0, Number.MAX_SAFE_INTEGER, "revision",
      )}"`,
      "Idempotency-Key": requestPattern(
        idempotencyKey, PATTERN.idempotency, "idempotency_key",
      ),
      "X-Step-Up-Grant": requestPattern(stepUpGrant, PATTERN.opaque, "step_up_grant"),
    },
    signal,
  };
}

async function request(controlApiUrl, path, options = {}) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest("control_api_url"); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest("control_api_url");
  }
  let response;
  try {
    response = await fetch(new URL(path, base).toString(), {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(options.headers ?? {}),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "include",
      cache: "no-store",
      redirect: "error",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new AuthApiError({
      code: "workspace_session.unavailable",
      message: "暂时无法连接 Workspace 商业账务服务。",
    });
  }
  const payload = await responseJson(response);
  if (!response.ok) {
    throw new AuthApiError({
      status: Number(response.status) || 0,
      code: safeErrorText(payload.code, "workspace_session.unavailable"),
      message: safeErrorText(payload.message, "Workspace 商业账务请求未完成。"),
      requestId: safeErrorText(
        payload.request_id, response.headers?.get?.("X-Request-ID") ?? "-",
      ),
      traceId: safeErrorText(
        payload.trace_id, response.headers?.get?.("X-Trace-ID") ?? "-",
      ),
    });
  }
  return payload;
}

async function responseJson(response) {
  try {
    const payload = await response.json();
    if (!payload || Object.getPrototypeOf(payload) !== Object.prototype) {
      throw new Error("shape");
    }
    return payload;
  } catch {
    invalidResponse("json");
  }
}

function exact(value, keys) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidResponse("shape");
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidResponse("shape");
  return value;
}
function frozenArray(value, minimum, maximum, label, mapper) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    invalidResponse(label);
  }
  return Object.freeze(value.map(mapper));
}
function actionList(value, choices, label) {
  if (!Array.isArray(value) || value.length > 32 || new Set(value).size !== value.length) {
    invalidResponse(label);
  }
  value.forEach((item) => (
    choices ? choice(item, choices, label) : safeString(item, 1, 80, label)
  ));
}
function safeString(value, minimum, maximum, label) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) invalidResponse(label);
  return value;
}
function nullableString(value, minimum, maximum, label) {
  return value === null ? null : safeString(value, minimum, maximum, label);
}
function pattern(value, expression, label) {
  if (typeof value !== "string" || !expression.test(value)) invalidResponse(label);
  return value;
}
function nullablePattern(value, expression, label) {
  return value === null ? null : pattern(value, expression, label);
}
function hash(value, label) { return pattern(value, PATTERN.sha256, label); }
function integer(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidResponse(label);
  }
  return value;
}
function nullableInteger(value, minimum, maximum, label) {
  return value === null ? null : integer(value, minimum, maximum, label);
}
function boolean(value, label) {
  if (typeof value !== "boolean") invalidResponse(label);
  return value;
}
function choice(value, choices, label) {
  if (!choices.includes(value)) invalidResponse(label);
  return value;
}
function nullableChoice(value, choices, label) {
  return value === null ? null : choice(value, choices, label);
}
function timestamp(value, label) {
  if (typeof value !== "string" || value.length > 80 || !Number.isFinite(Date.parse(value))) {
    invalidResponse(label);
  }
  return value;
}
function nullableTimestamp(value, label) {
  return value === null ? null : timestamp(value, label);
}
function nullableCursor(value) {
  if (value !== null && (
    typeof value !== "string" || value.length > 2048 || !PATTERN.cursor.test(value)
  )) invalidResponse("next_cursor");
}
function trace(value) {
  return {
    request_id: safeString(value.request_id, 1, 160, "request_id"),
    trace_id: safeString(value.trace_id, 1, 160, "trace_id"),
  };
}

function requestPlain(value, label) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) invalidRequest(label);
  return value;
}
function requestExact(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])) invalidRequest("shape");
}
function requestPattern(value, expression, label) {
  if (typeof value !== "string" || !expression.test(value)) invalidRequest(label);
  return value;
}
function requestChoice(value, choices, label) {
  if (!choices.includes(value)) invalidRequest(label);
  return value;
}
function requestInteger(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    invalidRequest(label);
  }
  return value;
}
function requestTimestamp(value, label) {
  if (typeof value !== "string" || value.length > 80 || !Number.isFinite(Date.parse(value))) {
    invalidRequest(label);
  }
  return value;
}

function safeErrorText(value, fallback) {
  return typeof value === "string" && value
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240)
    : fallback;
}
function invalidRequest(label) {
  throw new AuthApiError({
    code: "commercial.input_invalid",
    message: `Workspace 商业账务请求字段无效：${String(label).slice(0, 80)}`,
  });
}
function invalidResponse(label) {
  throw new AuthApiError({
    status: 502,
    code: "workspace.commercial_accounting_response_invalid",
    message: `Workspace 商业账务响应无效：${String(label).slice(0, 80)}`,
  });
}
