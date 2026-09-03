/** Strict PlatformSession client for PRD-144 commercial accounting. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const PATTERN = Object.freeze({
  cursor: /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u,
  dispute: /^bdc_[0-9a-f]{32}$/u,
  idempotency: /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u,
  opaque: /^[A-Za-z0-9_-]{43,128}$/u,
  planCatalog: /^spc_[0-9a-f]{32}$/u,
  planRelease: /^pplr_[0-9a-f]{32}$/u,
  planVersion: /^spv_[0-9a-f]{32}$/u,
  referenceRate: /^pprr_[0-9a-f]{32}$/u,
  safeId: /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u,
  sha256: /^[0-9a-f]{64}$/u,
  subscription: /^sub_[0-9a-f]{32}$/u,
  workspaceRef: /^wref_[0-9a-f]{32}$/u,
});

const CURRENCIES = Object.freeze(["CNY", "USD"]);
const BILLING_PERIODS = Object.freeze(["none", "monthly", "yearly"]);
const PLAN_STATUSES = Object.freeze([
  "legacy_reconciled", "candidate", "review_required", "released", "retired",
]);
const PLAN_ACTIONS = Object.freeze(["create_candidate", "submit_review", "release", "retire"]);
const REFERENCE_ACTIONS = Object.freeze(["create_candidate", "release", "retire"]);
const PROJECTION_STATES = Object.freeze([
  "current", "partial", "stale", "reconciliation_required", "unknown",
]);
const DISPUTE_STATUSES = Object.freeze([
  "submitted", "under_review", "approved", "rejected", "withdrawn",
]);

export async function listPlatformCommercialPlans({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = planQuery(filters, cursor, limit);
  const payload = await request(controlApiUrl, `/platform/plans?${query}`, { signal });
  exact(payload, [
    "plans", "reference_rates", "next_cursor", "limit", "permission_fingerprint",
    "request_id", "trace_id",
  ]);
  const plans = frozenArray(payload.plans, 0, 100, "plans", planInfo);
  const referenceRates = frozenArray(
    payload.reference_rates, 0, 500, "reference_rates", referenceRateInfo,
  );
  integer(payload.limit, 1, 100, "limit");
  nullableCursor(payload.next_cursor);
  if (plans.length > payload.limit) invalidResponse("pagination");
  return Object.freeze({
    plans,
    reference_rates: referenceRates,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function applyPlatformCommercialPlanCommand({
  controlApiUrl,
  command,
  expectedRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const body = planCommand(command);
  const payload = await request(
    controlApiUrl,
    "/platform/plans",
    writeOptions(body, expectedRevision, idempotencyKey, stepUpGrant, signal),
  );
  exact(payload, [
    "plan", "command_id", "idempotent_replay", "permission_fingerprint",
    "request_id", "trace_id",
  ]);
  const plan = planInfo(payload.plan);
  if (body.plan_version_id && plan.plan_version_id !== body.plan_version_id) {
    invalidResponse("plan_owner");
  }
  boolean(payload.idempotent_replay, "idempotent_replay");
  return Object.freeze({
    plan,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    idempotent_replay: payload.idempotent_replay,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function listPlatformCommercialSubscriptions({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = subscriptionQuery(filters, cursor, limit);
  const payload = await request(controlApiUrl, `/platform/subscriptions?${query}`, { signal });
  exact(payload, [
    "subscriptions", "next_cursor", "limit", "permission_fingerprint", "request_id",
    "trace_id",
  ]);
  const subscriptions = frozenArray(
    payload.subscriptions, 0, 100, "subscriptions", subscriptionInfo,
  );
  integer(payload.limit, 1, 100, "limit");
  nullableCursor(payload.next_cursor);
  if (subscriptions.length > payload.limit) invalidResponse("pagination");
  const disputeId = filters.dispute_id;
  if (disputeId && subscriptions.some(
    (subscription) => !subscription.disputes.some(
      (dispute) => dispute.billing_dispute_case_id === disputeId,
    ),
  )) invalidResponse("dispute_owner");
  return Object.freeze({
    subscriptions,
    next_cursor: payload.next_cursor,
    limit: payload.limit,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

export async function decidePlatformCommercialDispute({
  controlApiUrl,
  disputeId,
  command,
  expectedRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const id = requestPattern(disputeId, PATTERN.dispute, "dispute_id");
  const body = disputeDecision(command);
  const payload = await request(
    controlApiUrl,
    `/platform/billing-disputes/${encodeURIComponent(id)}/decisions`,
    writeOptions(body, expectedRevision, idempotencyKey, stepUpGrant, signal),
  );
  exact(payload, [
    "dispute", "command_id", "adjustment_id", "idempotent_replay",
    "permission_revision", "permission_fingerprint", "request_id", "trace_id",
  ]);
  const dispute = disputeInfo(payload.dispute);
  if (dispute.billing_dispute_case_id !== id) invalidResponse("dispute_owner");
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

export async function applyPlatformProviderReferenceRateCommand({
  controlApiUrl,
  providerId,
  command,
  expectedRevision,
  idempotencyKey,
  stepUpGrant,
  signal,
} = {}) {
  const provider = requestPattern(providerId, PATTERN.safeId, "provider_id");
  const body = referenceRateCommand(command);
  const payload = await request(
    controlApiUrl,
    `/platform/provider-reference-rates/${encodeURIComponent(provider)}/commands`,
    writeOptions(body, expectedRevision, idempotencyKey, stepUpGrant, signal),
  );
  exact(payload, [
    "rate", "command_id", "idempotent_replay", "permission_fingerprint",
    "request_id", "trace_id",
  ]);
  const rate = referenceRateInfo(payload.rate);
  if (rate.provider_id !== provider) invalidResponse("reference_rate_owner");
  if (body.provider_reference_rate_release_id
    && rate.provider_reference_rate_release_id !== body.provider_reference_rate_release_id) {
    invalidResponse("reference_rate_owner");
  }
  boolean(payload.idempotent_replay, "idempotent_replay");
  return Object.freeze({
    rate,
    command_id: safeString(payload.command_id, 1, 128, "command_id"),
    idempotent_replay: payload.idempotent_replay,
    permission_fingerprint: hash(payload.permission_fingerprint, "permission_fingerprint"),
    ...trace(payload),
  });
}

function planQuery(filters, cursor, limit) {
  requestPlain(filters, "filters");
  const allowed = new Set([
    "plan_code", "status", "currency", "billing_period", "release_generation",
  ]);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (!allowed.has(key)) invalidRequest(`filter.${key}`);
    if (value === "" || value === null || value === undefined) continue;
    if (key === "plan_code") requestPattern(value, /^[a-z][a-z0-9-]{1,31}$/u, key);
    if (key === "status") requestChoice(value, PLAN_STATUSES, key);
    if (key === "currency") requestChoice(value, CURRENCIES, key);
    if (key === "billing_period") requestChoice(value, BILLING_PERIODS, key);
    if (key === "release_generation") requestInteger(value, 0, 2_147_483_647, key);
    query.set(key, String(value));
  }
  addCursorAndLimit(query, cursor, limit);
  return query.toString();
}

function subscriptionQuery(filters, cursor, limit) {
  requestPlain(filters, "filters");
  const allowed = new Set([
    "status", "plan_catalog_id", "projection_state", "from", "until", "dispute_id",
  ]);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (!allowed.has(key)) invalidRequest(`filter.${key}`);
    if (value === "" || value === null || value === undefined) continue;
    if (key === "status") requestString(value, 1, 80, key);
    if (key === "plan_catalog_id") requestPattern(value, PATTERN.planCatalog, key);
    if (key === "projection_state") requestChoice(value, PROJECTION_STATES, key);
    if (key === "from" || key === "until") requestTimestamp(value, key);
    if (key === "dispute_id") requestPattern(value, PATTERN.dispute, key);
    query.set(key, String(value));
  }
  if (filters.dispute_id) {
    if (Object.keys(filters).some((key) => key !== "dispute_id")
      || cursor !== null || limit !== 50) invalidRequest("dispute_filter");
    return query.toString();
  }
  if (filters.from && filters.until && String(filters.from) >= String(filters.until)) {
    invalidRequest("time_range");
  }
  addCursorAndLimit(query, cursor, limit);
  return query.toString();
}

function planCommand(value) {
  requestPlain(value, "command");
  const action = requestChoice(value.action, PLAN_ACTIONS, "action");
  if (action === "create_candidate") {
    requestExact(value, [
      "action", "plan_code", "display_name", "display_order", "billing_period",
      "currency", "unit_price_minor", "trial_days", "entitlements",
    ]);
    requestPattern(value.plan_code, /^[a-z][a-z0-9-]{1,31}$/u, "plan_code");
    requestString(value.display_name, 1, 80, "display_name");
    if (value.display_name !== value.display_name.trim()) invalidRequest("display_name");
    requestInteger(value.display_order, 0, 10_000, "display_order");
    requestChoice(value.billing_period, BILLING_PERIODS, "billing_period");
    requestChoice(value.currency, CURRENCIES, "currency");
    requestInteger(value.unit_price_minor, 0, 1_000_000_000_000, "unit_price_minor");
    requestInteger(value.trial_days, 0, 90, "trial_days");
    entitlement(value.entitlements);
  } else if (action === "release") {
    requestExact(value, ["action", "plan_version_id", "governance_approval_id"]);
    requestPattern(value.plan_version_id, PATTERN.planVersion, "plan_version_id");
    requestPattern(value.governance_approval_id, PATTERN.safeId, "governance_approval_id");
  } else {
    requestExact(value, ["action", "plan_version_id"]);
    requestPattern(value.plan_version_id, PATTERN.planVersion, "plan_version_id");
  }
  return freezeBody(value);
}

function entitlement(value) {
  requestPlain(value, "entitlements");
  requestExact(value, [
    "gateway_requests_per_month", "workspace_seats", "model_aliases",
    "retention_days", "priority_support",
  ]);
  requestInteger(value.gateway_requests_per_month, 0, 1_000_000_000, "gateway_requests_per_month");
  requestInteger(value.workspace_seats, 1, 100_000, "workspace_seats");
  requestInteger(value.model_aliases, 1, 100_000, "model_aliases");
  requestInteger(value.retention_days, 1, 3650, "retention_days");
  if (typeof value.priority_support !== "boolean") invalidRequest("priority_support");
}

function disputeDecision(value) {
  requestPlain(value, "command");
  const action = requestChoice(
    value.action, ["start_review", "approve_credit", "reject"], "action",
  );
  if (action === "start_review") {
    requestExact(value, ["action", "expected_source_revision"]);
  } else {
    requestExact(value, ["action", "decision_code", "expected_source_revision"]);
    const choices = action === "reject"
      ? ["claim_not_supported"]
      : ["facts_confirmed", "duplicate_confirmed", "service_credit"];
    requestChoice(value.decision_code, choices, "decision_code");
  }
  requestInteger(
    value.expected_source_revision, 1, Number.MAX_SAFE_INTEGER, "expected_source_revision",
  );
  return freezeBody(value);
}

function referenceRateCommand(value) {
  requestPlain(value, "command");
  const action = requestChoice(value.action, REFERENCE_ACTIONS, "action");
  if (action === "create_candidate") {
    const optionalUntil = Object.hasOwn(value, "effective_until") ? ["effective_until"] : [];
    requestExact(value, [
      "action", "model_id", "currency", "input_rate_micros_per_1k",
      "output_rate_micros_per_1k", "effective_from", "source_version", ...optionalUntil,
    ]);
    requestPattern(value.model_id, PATTERN.safeId, "model_id");
    requestChoice(value.currency, CURRENCIES, "currency");
    requestInteger(
      value.input_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "input_rate",
    );
    requestInteger(
      value.output_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "output_rate",
    );
    requestTimestamp(value.effective_from, "effective_from");
    requestString(value.source_version, 1, 128, "source_version");
    if (Object.hasOwn(value, "effective_until")) {
      requestTimestamp(value.effective_until, "effective_until");
      if (String(value.effective_until) <= String(value.effective_from)) {
        invalidRequest("effective_interval");
      }
    }
  } else if (action === "release") {
    requestExact(value, [
      "action", "provider_reference_rate_release_id", "governance_approval_id",
    ]);
    requestPattern(
      value.provider_reference_rate_release_id, PATTERN.referenceRate, "reference_rate_id",
    );
    requestPattern(value.governance_approval_id, PATTERN.safeId, "governance_approval_id");
  } else {
    requestExact(value, ["action", "provider_reference_rate_release_id"]);
    requestPattern(
      value.provider_reference_rate_release_id, PATTERN.referenceRate, "reference_rate_id",
    );
  }
  return freezeBody(value);
}

function planInfo(value) {
  exact(value, [
    "platform_plan_catalog_release_id", "plan_catalog_id", "plan_version_id", "plan_code",
    "display_name", "billing_period", "currency", "unit_price_minor",
    "entitlements_sha256", "contract_sha256", "status", "revision",
    "release_generation", "head_revision", "is_head", "readiness", "actions",
    "created_at", "updated_at", "released_at",
  ]);
  pattern(value.platform_plan_catalog_release_id, PATTERN.planRelease, "plan_release_id");
  pattern(value.plan_catalog_id, PATTERN.planCatalog, "plan_catalog_id");
  pattern(value.plan_version_id, PATTERN.planVersion, "plan_version_id");
  pattern(value.plan_code, /^[a-z][a-z0-9-]{1,31}$/u, "plan_code");
  safeString(value.display_name, 1, 80, "display_name");
  choice(value.billing_period, BILLING_PERIODS, "billing_period");
  choice(value.currency, CURRENCIES, "currency");
  integer(value.unit_price_minor, 0, Number.MAX_SAFE_INTEGER, "unit_price_minor");
  hash(value.entitlements_sha256, "entitlements_sha256");
  hash(value.contract_sha256, "contract_sha256");
  choice(value.status, PLAN_STATUSES, "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.release_generation, 0, Number.MAX_SAFE_INTEGER, "release_generation");
  nullableInteger(value.head_revision, 1, Number.MAX_SAFE_INTEGER, "head_revision");
  boolean(value.is_head, "is_head");
  choice(
    value.readiness, ["legacy", "candidate", "review", "released", "retired"], "readiness",
  );
  actionList(
    value.actions, PLAN_ACTIONS.filter((item) => item !== "create_candidate"), "actions",
  );
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.released_at, "released_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
}

function referenceRateInfo(value) {
  exact(value, [
    "provider_reference_rate_release_id", "provider_id", "model_id", "currency",
    "input_rate_micros_per_1k", "output_rate_micros_per_1k", "effective_from",
    "effective_until", "source_version", "content_sha256", "status", "revision",
    "release_generation", "head_revision", "is_head", "actions", "created_at",
    "updated_at", "released_at",
  ]);
  pattern(
    value.provider_reference_rate_release_id, PATTERN.referenceRate, "reference_rate_id",
  );
  pattern(value.provider_id, PATTERN.safeId, "provider_id");
  pattern(value.model_id, PATTERN.safeId, "model_id");
  choice(value.currency, CURRENCIES, "currency");
  integer(value.input_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "input_rate");
  integer(value.output_rate_micros_per_1k, 0, Number.MAX_SAFE_INTEGER, "output_rate");
  timestamp(value.effective_from, "effective_from");
  nullableTimestamp(value.effective_until, "effective_until");
  safeString(value.source_version, 1, 128, "source_version");
  hash(value.content_sha256, "content_sha256");
  choice(value.status, ["candidate", "released", "retired"], "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  integer(value.release_generation, 0, Number.MAX_SAFE_INTEGER, "release_generation");
  nullableInteger(value.head_revision, 1, Number.MAX_SAFE_INTEGER, "head_revision");
  boolean(value.is_head, "is_head");
  actionList(
    value.actions, REFERENCE_ACTIONS.filter((item) => item !== "create_candidate"), "actions",
  );
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.released_at, "released_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
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
  choice(value.status, DISPUTE_STATUSES, "status");
  integer(value.source_revision, 1, Number.MAX_SAFE_INTEGER, "source_revision");
  integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER, "state_revision");
  nullableChoice(value.decision_code, [
    "facts_confirmed", "duplicate_confirmed", "service_credit", "claim_not_supported",
  ], "decision_code");
  integer(value.credited_amount_minor, 0, Number.MAX_SAFE_INTEGER, "credited_amount_minor");
  actionList(value.actions, ["start_review", "approve_credit", "reject"], "actions");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.resolved_at, "resolved_at");
  return Object.freeze({ ...value, actions: Object.freeze([...value.actions]) });
}

function writeOptions(body, revision, idempotencyKey, stepUpGrant, signal) {
  return {
    method: "POST",
    body,
    headers: {
      "X-CSRF-Token": readPlatformCsrfCookie(),
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
    throw new PlatformApiError({
      code: "platform.network_unavailable",
      message: "暂时无法连接商业账务服务。",
    });
  }
  const payload = await responseJson(response);
  if (!response.ok) {
    throw new PlatformApiError({
      status: Number(response.status) || 0,
      code: safeErrorText(payload.code, "platform.request_failed"),
      message: safeErrorText(payload.message, "商业账务请求未完成。"),
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

function addCursorAndLimit(query, cursor, limit) {
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    query.set("cursor", requestPattern(cursor, PATTERN.cursor, "cursor"));
  }
  query.set("limit", String(requestInteger(limit, 1, 100, "limit")));
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
function requestString(value, minimum, maximum, label) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) invalidRequest(label);
  return value;
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
function freezeBody(value) {
  return Object.freeze({
    ...value,
    ...(value.entitlements
      ? { entitlements: Object.freeze({ ...value.entitlements }) }
      : {}),
  });
}

function safeErrorText(value, fallback) {
  return typeof value === "string" && value
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240)
    : fallback;
}
function invalidRequest(label) {
  throw new PlatformApiError({
    code: "platform.request_invalid",
    message: `商业账务请求字段无效：${String(label).slice(0, 80)}`,
  });
}
function invalidResponse(label) {
  throw new PlatformApiError({
    status: 502,
    code: "platform.commercial_accounting_response_invalid",
    message: `商业账务响应无效：${String(label).slice(0, 80)}`,
  });
}
