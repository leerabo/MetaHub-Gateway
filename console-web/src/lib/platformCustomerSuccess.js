/** Strict PRD-140 Platform customer-success client. */

import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const ID = Object.freeze({
  account: /^csa_[0-9a-f]{32}$/u,
  batch: /^ctb_[0-9a-f]{32}$/u,
  contact: /^cct_[0-9a-f]{32}$/u,
  operator: /^pto_[0-9a-f]{32}$/u,
  plan: /^csp_[0-9a-f]{32}$/u,
  qbr: /^qbr_[0-9a-f]{32}$/u,
  renewal: /^rno_[0-9a-f]{32}$/u,
  task: /^ctk_[0-9a-f]{32}$/u,
  workspace: /^wsp_[0-9a-f]{32}$/u,
});
const IDEMPOTENCY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const OPAQUE = /^[A-Za-z0-9_-]{43,128}$/u;
const SAFE_CODE = /^[a-z][a-z0-9._-]{2,79}$/u;
const CURSOR = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/u;
const ACCOUNT_FILTERS = Object.freeze({
  customer_account_id: null,
  workspace_id: null,
  slug: null,
  kind: new Set(["personal", "organization"]),
  region: null,
  operational_status: new Set([
    "active", "suspended", "deletion_pending", "deleted", "migration_conflict",
  ]),
  activation: new Set(["not_started", "in_progress", "activated"]),
  adoption: new Set(["unknown", "low", "medium", "high"]),
  health_risk: new Set(["unknown", "healthy", "watch", "at_risk", "critical"]),
  renewal_status: null,
  view: new Set(["directory", "detail", "contact"]),
});
const RENEWAL_FILTERS = new Set([
  "status", "risk", "renewal_from", "renewal_to", "region", "customer_account_id",
]);
const RISK = ["unknown", "healthy", "watch", "at_risk", "critical"];
const PLAN_STATUS = ["draft", "active", "at_risk", "completed", "canceled"];
const TASK_STATUS = ["open", "in_progress", "completed", "canceled"];
const PRIORITY = ["low", "normal", "high", "critical"];
const TASK_TYPES = [
  "activation_review", "adoption_follow_up", "health_review", "renewal_review",
  "security_review", "qbr_prepare",
];

export async function listPlatformCustomerAccounts({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const parameters = requestValue(() => accountParameters(filters, cursor, limit));
  const value = await requestJson(
    controlApiUrl,
    `/platform/customer-accounts?${parameters.toString()}`,
    { signal },
  );
  exact(value, ["items", "limit", "has_more", "next_cursor", "detail", "contacts", "request_id", "trace_id"]);
  array(value.items, 0, 100, "items");
  const items = Object.freeze(value.items.map(accountSummary));
  integer(value.limit, 1, 100, "limit");
  boolean(value.has_more, "has_more");
  nullableCursor(value.next_cursor);
  const detail = value.detail === null ? null : accountDetail(value.detail);
  const contacts = value.contacts === null ? null : frozenArray(
    value.contacts, 0, 100, "contacts", contactSummary,
  );
  if (items.length > value.limit || value.has_more !== (value.next_cursor !== null)) {
    invalidResponse("pagination");
  }
  return Object.freeze({
    items,
    limit: value.limit,
    has_more: value.has_more,
    next_cursor: value.next_cursor,
    detail,
    contacts,
    ...trace(value),
  });
}

export async function getPlatformCustomerHealth({ controlApiUrl, customerAccountId, signal } = {}) {
  const accountId = requestValue(
    () => pattern(customerAccountId, ID.account, "customer_account_id"),
  );
  const value = await requestJson(
    controlApiUrl,
    `/platform/customer-accounts/${encodeURIComponent(accountId)}/health`,
    { signal },
  );
  return healthResponse(value, accountId);
}

export async function applyPlatformCustomerSuccessPlan({
  controlApiUrl,
  command,
  idempotencyKey,
  signal,
} = {}) {
  const body = requestValue(() => planCommand(command));
  const value = await requestJson(controlApiUrl, "/platform/customer-success-plans", {
    method: "POST",
    body,
    revision: body.expected_revision,
    idempotencyKey: requestValue(
      () => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    ),
    signal,
  });
  exact(value, ["plan", "idempotent_replay", "request_id", "trace_id"]);
  const plan = planSummary(value.plan);
  boolean(value.idempotent_replay, "idempotent_replay");
  if (plan.customer_account_id !== body.customer_account_id
    || (body.customer_success_plan_id
      && plan.customer_success_plan_id !== body.customer_success_plan_id)) {
    invalidResponse("plan_owner");
  }
  return Object.freeze({ plan, idempotent_replay: value.idempotent_replay, ...trace(value) });
}

export async function applyPlatformCustomerTask({
  controlApiUrl,
  command,
  idempotencyKey,
  stepUpGrant = null,
  signal,
} = {}) {
  const body = requestValue(() => taskCommand(command));
  const options = {
    method: "POST",
    body,
    revision: body.expected_revision,
    idempotencyKey: requestValue(
      () => pattern(idempotencyKey, IDEMPOTENCY, "idempotency_key"),
    ),
    signal,
  };
  if (stepUpGrant !== null && stepUpGrant !== "") {
    options.stepUpGrant = requestValue(
      () => pattern(stepUpGrant, OPAQUE, "step_up_grant"),
    );
  }
  const value = await requestJson(controlApiUrl, "/platform/customer-tasks", options);
  exact(value, ["task", "batch", "idempotent_replay", "request_id", "trace_id"]);
  const task = value.task === null ? null : taskSummary(value.task);
  const batch = value.batch === null ? null : batchSummary(value.batch);
  boolean(value.idempotent_replay, "idempotent_replay");
  if ((task === null) === (batch === null)) invalidResponse("task_result");
  if (task && body.customer_account_id && task.customer_account_id !== body.customer_account_id) {
    invalidResponse("task_owner");
  }
  if (batch && body.customer_task_batch_id
    && batch.customer_task_batch_id !== body.customer_task_batch_id) {
    invalidResponse("batch_owner");
  }
  return Object.freeze({ task, batch, idempotent_replay: value.idempotent_replay, ...trace(value) });
}

export async function listPlatformRenewalPipeline({
  controlApiUrl,
  filters = {},
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const parameters = requestValue(() => renewalParameters(filters, cursor, limit));
  const value = await requestJson(
    controlApiUrl,
    `/platform/renewal-pipeline?${parameters.toString()}`,
    { signal },
  );
  exact(value, ["items", "limit", "has_more", "next_cursor", "request_id", "trace_id"]);
  const items = frozenArray(value.items, 0, 100, "items", renewalSummary);
  integer(value.limit, 1, 100, "limit");
  boolean(value.has_more, "has_more");
  nullableCursor(value.next_cursor);
  if (items.length > value.limit || value.has_more !== (value.next_cursor !== null)) {
    invalidResponse("pagination");
  }
  return Object.freeze({
    items,
    limit: value.limit,
    has_more: value.has_more,
    next_cursor: value.next_cursor,
    ...trace(value),
  });
}

function accountParameters(filters, cursor, limit) {
  plain(filters, "filters");
  const parameters = new URLSearchParams({ limit: String(integer(limit, 1, 100, "limit")) });
  for (const key of Object.keys(filters).sort()) {
    if (!Object.hasOwn(ACCOUNT_FILTERS, key)) invalidRequest(`filter.${key}`);
    const value = safeString(filters[key], 1, 160, `filter.${key}`);
    const catalog = ACCOUNT_FILTERS[key];
    if (catalog && !catalog.has(value)) invalidRequest(`filter.${key}`);
    if (key === "customer_account_id") pattern(value, ID.account, key);
    if (key === "workspace_id") pattern(value, ID.workspace, key);
    parameters.set(key, value);
  }
  if (filters.view && filters.view !== "directory") {
    if (!filters.customer_account_id
      || Object.keys(filters).some((key) => !["customer_account_id", "view"].includes(key))) {
      invalidRequest("detail_filter");
    }
  }
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    parameters.set("cursor", pattern(cursor, CURSOR, "cursor", 2048));
  }
  return parameters;
}

function renewalParameters(filters, cursor, limit) {
  plain(filters, "filters");
  const parameters = new URLSearchParams({ limit: String(integer(limit, 1, 100, "limit")) });
  for (const key of Object.keys(filters).sort()) {
    if (!RENEWAL_FILTERS.has(key)) invalidRequest(`filter.${key}`);
    const value = safeString(filters[key], 1, 160, `filter.${key}`);
    if (key === "customer_account_id") pattern(value, ID.account, key);
    if (["renewal_from", "renewal_to"].includes(key)) timestamp(value, key);
    parameters.set(key, value);
  }
  if (cursor !== null && cursor !== undefined && cursor !== "") {
    parameters.set("cursor", pattern(cursor, CURSOR, "cursor", 2048));
  }
  return parameters;
}

function accountSummary(value) {
  plain(value, "account");
  const keys = {
    customer_success: [
      "scope", "customer_account_id", "workspace_id", "display_name", "slug", "kind",
      "region", "operational_status", "activation", "activation_completed_steps", "adoption",
      "health_risk", "renewal_status", "open_support_ticket_count", "plan_count",
      "open_task_count", "revision", "projection_updated_at",
    ],
    support: [
      "scope", "customer_account_id", "workspace_id", "operational_status", "health_risk",
      "open_support_ticket_count", "breached_sla_count", "projection_updated_at",
    ],
    finance: [
      "scope", "customer_account_id", "workspace_id", "region", "subscription_id",
      "plan_version_id", "subscription_status", "renewal_at", "projection_updated_at",
    ],
    owner: [
      "scope", "customer_account_id", "workspace_id", "display_name", "slug", "kind",
      "region", "operational_status", "activation", "activation_completed_steps", "adoption",
      "health_risk", "subscription_id", "plan_version_id", "subscription_status", "renewal_at",
      "open_support_ticket_count", "breached_sla_count", "plan_count", "open_task_count",
      "revision", "projection_updated_at",
    ],
  }[value.scope];
  if (!keys) invalidResponse("account.scope");
  exact(value, keys);
  pattern(value.customer_account_id, ID.account, "customer_account_id");
  pattern(value.workspace_id, ID.workspace, "workspace_id");
  timestamp(value.projection_updated_at, "projection_updated_at");
  if (Object.hasOwn(value, "health_risk")) choice(value.health_risk, RISK, "health_risk");
  for (const key of [
    "activation_completed_steps", "open_support_ticket_count", "breached_sla_count",
    "plan_count", "open_task_count", "revision",
  ]) {
    if (Object.hasOwn(value, key)) integer(value[key], key === "revision" ? 1 : 0, 1e9, key);
  }
  return Object.freeze({ ...value });
}

function accountDetail(value) {
  exact(value, [
    "plans", "plans_count", "plans_truncated", "tasks", "tasks_count", "tasks_truncated",
    "qbr_summaries", "qbr_count", "qbr_truncated", "operation_count", "operations_truncated",
  ]);
  const plans = frozenArray(value.plans, 0, 20, "plans", planSummary);
  const tasks = frozenArray(value.tasks, 0, 50, "tasks", taskSummary);
  const qbrSummaries = frozenArray(value.qbr_summaries, 0, 8, "qbr_summaries", qbrSummary);
  for (const key of ["plans_count", "tasks_count", "qbr_count", "operation_count"]) {
    integer(value[key], 0, 1e9, key);
  }
  for (const key of ["plans_truncated", "tasks_truncated", "qbr_truncated", "operations_truncated"]) {
    boolean(value[key], key);
  }
  return Object.freeze({ ...value, plans, tasks, qbr_summaries: qbrSummaries });
}

function contactSummary(value) {
  exact(value, [
    "customer_contact_id", "contact_role", "status", "account_id", "display_name", "email",
    "phone", "membership_revision", "notification_preference_revision", "preference_summary",
  ]);
  pattern(value.customer_contact_id, ID.contact, "customer_contact_id");
  choice(value.contact_role, ["primary", "billing", "technical", "security"], "contact_role");
  choice(value.status, ["active", "unavailable"], "status");
  safeString(value.account_id, 4, 80, "account_id");
  safeString(value.display_name, 1, 160, "display_name");
  safeString(value.email, 3, 320, "email");
  nullableString(value.phone, 1, 32, "phone");
  integer(value.membership_revision, 1, 1e9, "membership_revision");
  integer(value.notification_preference_revision, 0, 1e9, "notification_preference_revision");
  const preferences = frozenArray(
    value.preference_summary, 0, 100, "preference_summary", preferenceSummary,
  );
  return Object.freeze({ ...value, preference_summary: preferences });
}

function preferenceSummary(value) {
  exact(value, ["category", "channel", "enabled"]);
  safeString(value.category, 1, 80, "category");
  safeString(value.channel, 1, 40, "channel");
  boolean(value.enabled, "enabled");
  return Object.freeze({ ...value });
}

function healthResponse(value, accountId) {
  plain(value, "health");
  const support = value.scope === "support";
  const keys = support
    ? [
        "customer_account_id", "scope", "formula_version", "risk_level", "factors",
        "risk_codes", "action_codes", "freshness", "source_sequence", "evaluated_at",
        "request_id", "trace_id",
      ]
    : [
        "customer_account_id", "scope", "formula_version", "window_start", "window_end",
        "comparison_start", "comparison_end", "score_bps", "risk_level", "factors",
        "risk_codes", "action_codes", "freshness", "source_sequence", "evaluated_at",
        "request_id", "trace_id",
      ];
  if (!support && value.scope !== "customer_success") invalidResponse("health.scope");
  exact(value, keys);
  if (value.customer_account_id !== accountId) invalidResponse("health.owner");
  choice(value.formula_version, ["customer-health-v1", "customer-health-platform-v2"], "formula_version");
  choice(value.risk_level, RISK.slice(1), "risk_level");
  choice(value.freshness, ["fresh", "stale"], "freshness");
  integer(value.source_sequence, 1, Number.MAX_SAFE_INTEGER, "source_sequence");
  timestamp(value.evaluated_at, "evaluated_at");
  if (!support) {
    for (const key of ["window_start", "window_end", "comparison_start", "comparison_end"]) {
      timestamp(value[key], key);
    }
    integer(value.score_bps, 0, 10000, "score_bps");
  }
  const factors = frozenArray(value.factors, 1, 6, "factors", healthFactor);
  const riskCodes = frozenStrings(value.risk_codes, "risk_codes");
  const actionCodes = frozenStrings(value.action_codes, "action_codes");
  return Object.freeze({ ...value, factors, risk_codes: riskCodes, action_codes: actionCodes, ...trace(value) });
}

function healthFactor(value) {
  exact(value, ["code", "score_bps", "contribution_bps", "current_value", "comparison_value"]);
  choice(value.code, [
    "usage_trend", "reliability", "cost_anomaly_health", "budget_readiness",
    "team_activity", "ticket_health",
  ], "factor.code");
  nullableInteger(value.score_bps, 0, 10000, "score_bps");
  integer(value.contribution_bps, 0, 10000, "contribution_bps");
  nullableInteger(value.current_value, 0, Number.MAX_SAFE_INTEGER, "current_value");
  nullableInteger(value.comparison_value, 0, Number.MAX_SAFE_INTEGER, "comparison_value");
  return Object.freeze({ ...value });
}

function planSummary(value) {
  exact(value, [
    "customer_success_plan_id", "customer_account_id", "objective_code", "starts_on", "ends_on",
    "target_health_bps", "target_activation_steps", "target_adoption_bps",
    "source_health_sequence", "status", "revision", "created_by_operator_id",
    "updated_by_operator_id", "created_at", "updated_at", "completed_at",
  ]);
  pattern(value.customer_success_plan_id, ID.plan, "customer_success_plan_id");
  pattern(value.customer_account_id, ID.account, "customer_account_id");
  safeString(value.objective_code, 3, 80, "objective_code");
  date(value.starts_on, "starts_on");
  date(value.ends_on, "ends_on");
  integer(value.target_health_bps, 0, 10000, "target_health_bps");
  integer(value.target_activation_steps, 0, 8, "target_activation_steps");
  integer(value.target_adoption_bps, 0, 10000, "target_adoption_bps");
  nullableInteger(value.source_health_sequence, 1, Number.MAX_SAFE_INTEGER, "source_health_sequence");
  choice(value.status, PLAN_STATUS, "status");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  pattern(value.created_by_operator_id, ID.operator, "created_by_operator_id");
  pattern(value.updated_by_operator_id, ID.operator, "updated_by_operator_id");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.completed_at, "completed_at");
  return Object.freeze({ ...value });
}

function taskSummary(value) {
  exact(value, [
    "customer_task_id", "customer_account_id", "customer_success_plan_id",
    "renewal_opportunity_id", "task_type", "priority", "reason_code",
    "assignee_operator_id", "due_at", "required", "status", "due_generation", "revision",
    "created_at", "updated_at", "completed_at",
  ]);
  pattern(value.customer_task_id, ID.task, "customer_task_id");
  pattern(value.customer_account_id, ID.account, "customer_account_id");
  nullablePattern(value.customer_success_plan_id, ID.plan, "customer_success_plan_id");
  nullablePattern(value.renewal_opportunity_id, ID.renewal, "renewal_opportunity_id");
  safeString(value.task_type, 3, 80, "task_type");
  choice(value.priority, PRIORITY, "priority");
  pattern(value.reason_code, SAFE_CODE, "reason_code");
  pattern(value.assignee_operator_id, ID.operator, "assignee_operator_id");
  timestamp(value.due_at, "due_at");
  boolean(value.required, "required");
  choice(value.status, TASK_STATUS, "status");
  integer(value.due_generation, 1, Number.MAX_SAFE_INTEGER, "due_generation");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  nullableTimestamp(value.completed_at, "completed_at");
  return Object.freeze({ ...value });
}

function qbrSummary(value) {
  exact(value, [
    "qbr_summary_id", "quarter", "health_sequence", "adoption_bps", "support_ticket_count",
    "renewal_risk_count", "outcome_code", "action_code", "source_digest", "generated_at",
  ]);
  pattern(value.qbr_summary_id, ID.qbr, "qbr_summary_id");
  pattern(value.quarter, /^[0-9]{4}-Q[1-4]$/u, "quarter");
  nullableInteger(value.health_sequence, 1, Number.MAX_SAFE_INTEGER, "health_sequence");
  nullableInteger(value.adoption_bps, 0, 10000, "adoption_bps");
  integer(value.support_ticket_count, 0, 1e9, "support_ticket_count");
  integer(value.renewal_risk_count, 0, 1e9, "renewal_risk_count");
  safeString(value.outcome_code, 3, 80, "outcome_code");
  safeString(value.action_code, 3, 80, "action_code");
  pattern(value.source_digest, /^[0-9a-f]{64}$/u, "source_digest");
  timestamp(value.generated_at, "generated_at");
  return Object.freeze({ ...value });
}

function batchSummary(value) {
  exact(value, [
    "customer_task_batch_id", "prepared_by_operator_id", "approved_by_operator_id",
    "account_count", "manifest_sha256", "task_type", "priority", "reason_code",
    "assignee_operator_id", "due_at", "status", "result_count", "revision", "created_at",
    "updated_at",
  ]);
  pattern(value.customer_task_batch_id, ID.batch, "customer_task_batch_id");
  pattern(value.prepared_by_operator_id, ID.operator, "prepared_by_operator_id");
  nullablePattern(value.approved_by_operator_id, ID.operator, "approved_by_operator_id");
  integer(value.account_count, 1, 100, "account_count");
  pattern(value.manifest_sha256, /^[0-9a-f]{64}$/u, "manifest_sha256");
  safeString(value.task_type, 3, 80, "task_type");
  choice(value.priority, PRIORITY, "priority");
  pattern(value.reason_code, SAFE_CODE, "reason_code");
  pattern(value.assignee_operator_id, ID.operator, "assignee_operator_id");
  timestamp(value.due_at, "due_at");
  choice(value.status, ["awaiting_approval", "applied", "rejected"], "status");
  integer(value.result_count, 0, 100, "result_count");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  timestamp(value.created_at, "created_at");
  timestamp(value.updated_at, "updated_at");
  return Object.freeze({ ...value });
}

function renewalSummary(value) {
  plain(value, "renewal");
  const keys = {
    customer_success: [
      "scope", "renewal_opportunity_id", "customer_account_id", "workspace_id", "display_name",
      "renewal_at", "health_risk_code", "adoption_risk_code", "payment_risk_code",
      "invoice_status", "status", "open_plan_count", "open_task_count", "revision",
    ],
    finance: [
      "scope", "renewal_opportunity_id", "customer_account_id", "workspace_id", "subscription_id",
      "plan_version_id", "renewal_at", "currency", "amount_minor", "amount_status",
      "payment_risk_code", "invoice_status", "status", "revision",
    ],
    owner: [
      "scope", "renewal_opportunity_id", "customer_account_id", "workspace_id", "display_name",
      "subscription_id", "plan_version_id", "renewal_at", "currency", "amount_minor",
      "amount_status", "health_risk_code", "adoption_risk_code", "payment_risk_code",
      "invoice_status", "status", "open_plan_count", "open_task_count", "revision",
    ],
  }[value.scope];
  if (!keys) invalidResponse("renewal.scope");
  exact(value, keys);
  pattern(value.renewal_opportunity_id, ID.renewal, "renewal_opportunity_id");
  pattern(value.customer_account_id, ID.account, "customer_account_id");
  pattern(value.workspace_id, ID.workspace, "workspace_id");
  timestamp(value.renewal_at, "renewal_at");
  integer(value.revision, 1, Number.MAX_SAFE_INTEGER, "revision");
  if (Object.hasOwn(value, "health_risk_code")) choice(value.health_risk_code, RISK, "health_risk_code");
  if (Object.hasOwn(value, "amount_minor")) nullableInteger(value.amount_minor, 0, Number.MAX_SAFE_INTEGER, "amount_minor");
  for (const key of ["open_plan_count", "open_task_count"]) {
    if (Object.hasOwn(value, key)) integer(value[key], 0, 1e9, key);
  }
  return Object.freeze({ ...value });
}

function planCommand(value) {
  plain(value, "command");
  const operation = choice(
    value.operation,
    ["create", "activate", "revise_targets", "complete", "cancel"],
    "operation",
  );
  const common = ["operation", "customer_account_id", "expected_revision", "confirmed"];
  const keys = operation === "create"
    ? [...common, "objective_code", "starts_on", "ends_on", "target_health_bps",
      "target_activation_steps", "target_adoption_bps", "source_health_sequence"]
    : operation === "revise_targets"
      ? [...common, "customer_success_plan_id", "ends_on", "target_health_bps",
        "target_activation_steps", "target_adoption_bps", "source_health_sequence"]
      : [...common, "customer_success_plan_id"];
  exact(value, keys, invalidRequest);
  pattern(value.customer_account_id, ID.account, "customer_account_id");
  if (value.confirmed !== true) invalidRequest("confirmed");
  if (operation === "create") {
    integer(value.expected_revision, 0, 0, "expected_revision");
    choice(value.objective_code, [
      "activation", "adoption", "reliability", "renewal", "security_readiness",
    ], "objective_code");
    date(value.starts_on, "starts_on");
  } else {
    integer(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision");
    pattern(value.customer_success_plan_id, ID.plan, "customer_success_plan_id");
  }
  if (["create", "revise_targets"].includes(operation)) {
    date(value.ends_on, "ends_on");
    integer(value.target_health_bps, 0, 10000, "target_health_bps");
    integer(value.target_activation_steps, 0, 8, "target_activation_steps");
    integer(value.target_adoption_bps, 0, 10000, "target_adoption_bps");
    integer(value.source_health_sequence, 1, Number.MAX_SAFE_INTEGER, "source_health_sequence");
  }
  return { ...value };
}

function taskCommand(value) {
  plain(value, "command");
  const operation = choice(
    value.operation,
    ["create_one", "transition_one", "prepare_batch", "approve_batch"],
    "operation",
  );
  const common = ["operation", "reason_code", "expected_revision", "confirmed"];
  const keys = {
    create_one: [
      ...common, "customer_account_id", "customer_success_plan_id", "renewal_opportunity_id",
      "task_type", "priority", "assignee_operator_id", "due_at", "required",
    ],
    transition_one: [...common, "customer_task_id", "target_status"],
    prepare_batch: [
      ...common, "customer_account_ids", "task_type", "priority", "assignee_operator_id", "due_at",
    ],
    approve_batch: [...common, "customer_task_batch_id", "decision"],
  }[operation];
  exact(value, keys, invalidRequest);
  pattern(value.reason_code, SAFE_CODE, "reason_code");
  if (value.confirmed !== true) invalidRequest("confirmed");
  if (operation === "create_one") {
    integer(value.expected_revision, 0, 0, "expected_revision");
    pattern(value.customer_account_id, ID.account, "customer_account_id");
    nullablePattern(value.customer_success_plan_id, ID.plan, "customer_success_plan_id");
    nullablePattern(value.renewal_opportunity_id, ID.renewal, "renewal_opportunity_id");
    taskCreation(value);
    boolean(value.required, "required");
  } else if (operation === "transition_one") {
    integer(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision");
    pattern(value.customer_task_id, ID.task, "customer_task_id");
    choice(value.target_status, TASK_STATUS, "target_status");
  } else if (operation === "prepare_batch") {
    integer(value.expected_revision, 0, 0, "expected_revision");
    array(value.customer_account_ids, 1, 100, "customer_account_ids");
    const ids = value.customer_account_ids.map((item) => pattern(item, ID.account, "customer_account_id"));
    if (new Set(ids).size !== ids.length) invalidRequest("customer_account_ids");
    taskCreation(value);
  } else {
    integer(value.expected_revision, 1, Number.MAX_SAFE_INTEGER, "expected_revision");
    pattern(value.customer_task_batch_id, ID.batch, "customer_task_batch_id");
    choice(value.decision, ["approved", "rejected"], "decision");
  }
  return { ...value, ...(value.customer_account_ids
    ? { customer_account_ids: [...value.customer_account_ids] } : {}) };
}

function taskCreation(value) {
  choice(value.task_type, TASK_TYPES, "task_type");
  choice(value.priority, PRIORITY, "priority");
  pattern(value.assignee_operator_id, ID.operator, "assignee_operator_id");
  timestamp(value.due_at, "due_at");
}

async function requestJson(controlApiUrl, path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = readPlatformCsrfCookie();
    headers["If-Match"] = `"${integer(options.revision, 0, Number.MAX_SAFE_INTEGER, "revision")}"`;
    headers["Idempotency-Key"] = options.idempotencyKey;
  }
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
    if (error?.name === "AbortError") throw error;
    throw new PlatformApiError({
      code: "platform.network_unavailable",
      message: "暂时无法连接客户成功服务。",
    });
  }
  const payload = await responseJson(response);
  if (!response.ok) {
    throw new PlatformApiError({
      status: response.status,
      code: safeStringOr(payload?.code, "platform.request_failed"),
      message: safeStringOr(payload?.message, "客户成功请求未完成。"),
      requestId: safeStringOr(payload?.request_id, "-"),
    });
  }
  return payload;
}

function apiUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest("control_api_url");
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest("control_api_url");
  }
  return new URL(path, base).toString();
}

async function responseJson(response) {
  try {
    const value = await response.json();
    plain(value, "response");
    return value;
  } catch {
    invalidResponse("json");
  }
}

function trace(value) {
  safeString(value.request_id, 1, 160, "request_id");
  safeString(value.trace_id, 1, 160, "trace_id");
  return { request_id: value.request_id, trace_id: value.trace_id };
}

function exact(value, keys, fail = invalidResponse) {
  plain(value, "object", fail);
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail("shape");
  }
  return value;
}

function plain(value, label, fail = invalidResponse) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) fail(label);
  return value;
}

function array(value, minimum, maximum, label) {
  if (!Array.isArray(value) || value.length < minimum || value.length > maximum) {
    invalidResponse(label);
  }
  return value;
}

function frozenArray(value, minimum, maximum, label, mapper) {
  return Object.freeze(array(value, minimum, maximum, label).map(mapper));
}

function frozenStrings(value, label) {
  const items = array(value, 0, 100, label).map((item) => safeString(item, 1, 80, label));
  if (new Set(items).size !== items.length) invalidResponse(label);
  return Object.freeze(items);
}

function safeString(value, minimum, maximum, label) {
  if (typeof value !== "string" || value.length < minimum || value.length > maximum
    || /[\u0000-\u001f\u007f]/u.test(value)) invalidResponse(label);
  return value;
}

function safeStringOr(value, fallback) {
  return typeof value === "string"
    ? value.replace(/[\u0000-\u001f\u007f]/gu, " ").slice(0, 240)
    : fallback;
}

function nullableString(value, minimum, maximum, label) {
  return value === null ? null : safeString(value, minimum, maximum, label);
}

function integer(value, minimum, maximum, label) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) invalidResponse(label);
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

function pattern(value, expression, label, maximum = 160) {
  if (typeof value !== "string" || value.length > maximum || !expression.test(value)) {
    invalidResponse(label);
  }
  return value;
}

function nullablePattern(value, expression, label) {
  return value === null ? null : pattern(value, expression, label);
}

function timestamp(value, label) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/u.test(value)
    || !Number.isFinite(Date.parse(value))) invalidResponse(label);
  return value;
}

function nullableTimestamp(value, label) {
  return value === null ? null : timestamp(value, label);
}

function date(value, label) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/u.test(value)
    || !Number.isFinite(Date.parse(`${value}T00:00:00Z`))) invalidResponse(label);
  return value;
}

function nullableCursor(value) {
  if (value !== null) pattern(value, CURSOR, "next_cursor", 2048);
}

function invalidRequest(label = "request") {
  throw new PlatformApiError({
    code: "platform.request_invalid",
    message: `客户成功请求字段无效：${String(label).slice(0, 80)}`,
  });
}

function requestValue(build) {
  try {
    return build();
  } catch (error) {
    if (error instanceof PlatformApiError
      && error.code === "platform.customer_success_response_invalid") {
      invalidRequest("request");
    }
    throw error;
  }
}

function invalidResponse(label = "response") {
  throw new PlatformApiError({
    status: 502,
    code: "platform.customer_success_response_invalid",
    message: `客户成功响应无效：${String(label).slice(0, 80)}`,
  });
}
