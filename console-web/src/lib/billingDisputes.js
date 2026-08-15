/** PAGE-112 billing dispute Control API client. */

import { AuthApiError } from "./auth.js";

const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const DISPUTE_ID = /^bdsp_[0-9a-f]{32}$/;
const DISPUTE_ITEM_ID = /^bdsi_[0-9a-f]{32}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_SECONDS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const VISIBLE_KEY = /^[\x21-\x7e]{8,128}$/;
const VISIBLE_CURSOR = /^[\x21-\x7e]+$/;
const POSITIVE_AMOUNT = /^(?:0\.(?!000000)[0-9]{6}|[1-9][0-9]*\.[0-9]{6})$/;
const MONEY_AMOUNT = /^(?:0|[1-9][0-9]*)\.[0-9]{6}$/;
const HASH = /^[0-9a-f]{64}$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const MAX_AMOUNT_MICROS = 1_000_000_000n * 1_000_000n;
const MAX_CURSOR_LENGTH = 2048;
const MAX_REASON_BYTES = 2000;
const MAX_TIME_RANGE_MS = 31 * 24 * 60 * 60 * 1000;

const LIST_STATUSES = Object.freeze([
  "submitted", "under_review", "approved", "rejected", "cancelled",
]);
const SCOPE_TYPES = Object.freeze(["request_ids", "time_range"]);
const REVIEW_ACTIONS = Object.freeze(["start_review", "approve", "reject"]);
const TRANSITION_ACTIONS = Object.freeze([...REVIEW_ACTIONS, "cancel"]);

const ACTOR_KEYS = Object.freeze(["user_id", "display_name", "email"]);
const SUMMARY_KEYS = Object.freeze([
  "billing_dispute_id", "tenant_id", "scope_type", "period_start", "period_end", "status",
  "state_revision", "item_count", "reason_sha256", "created_by", "reviewer",
  "disputed_currency_totals", "approved_currency_totals", "active_hold_count",
  "adjustment_count", "event_delivery_status", "review_started_at", "resolved_at",
  "created_at", "updated_at",
]);
const HOLD_KEYS = Object.freeze([
  "billing_settlement_hold_id", "billing_dispute_item_id", "finops_ledger_entry_id", "status",
  "release_reason", "state_revision", "created_transition_id", "released_transition_id",
  "created_at", "released_at", "updated_at",
]);
const LINK_KEYS = Object.freeze([
  "billing_dispute_adjustment_link_id", "billing_dispute_item_id", "finops_adjustment_id",
  "reversal_finops_ledger_entry_id", "amount", "currency", "billing_dispute_transition_id",
  "created_at",
]);
const ITEM_KEYS = Object.freeze([
  "billing_dispute_item_id", "finops_ledger_entry_id", "usage_ledger_id", "source_request_id",
  "source_trace_id", "ledger_sequence", "currency", "amount", "disputable_amount",
  "current_disputable_amount",
  "price_snapshot_id", "price_schema_version", "price_snapshot_source", "price_evidence_status",
  "created_at", "hold", "adjustment_link",
]);
const TRANSITION_KEYS = Object.freeze([
  "billing_dispute_transition_id", "action", "from_status", "to_status", "expected_revision",
  "new_revision", "actor_user_id", "decision_reason_sha256", "approved_item_count",
  "approved_totals_sha256", "operation_request_id", "operation_trace_id", "created_at",
]);
const EVENT_KEYS = Object.freeze([
  "billing_dispute_event_id", "event_type", "delivery_status", "attempt_count",
  "last_error_code", "occurred_at", "succeeded_at",
]);
const DETAIL_KEYS = Object.freeze([
  "summary", "reason", "decision_reason", "create_request_id", "create_trace_id", "items",
  "holds", "adjustment_links", "transitions", "event",
]);
const LIST_RESPONSE_KEYS = Object.freeze([
  "billing_disputes", "limit", "next_cursor", "has_more", "request_id", "trace_id",
]);
const DETAIL_RESPONSE_KEYS = Object.freeze(["dispute", "request_id", "trace_id"]);
const MUTATION_RESPONSE_KEYS = Object.freeze([
  "dispute", "transition", "adjustment_links", "idempotent_replay", "request_id", "trace_id",
]);

const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有所需的账单争议权限",
  "billing_dispute.query_invalid": "账单争议查询不合法",
  "billing_dispute.request_invalid": "账单争议请求不合法",
  "billing_dispute.not_found": "账单争议不存在",
  "billing_dispute.scope_not_found": "没有可争议的计费记录",
  "billing_dispute.scope_too_large": "争议范围超过限制",
  "billing_dispute.hold_conflict": "计费记录已被其他争议暂缓",
  "billing_dispute.revision_conflict": "争议状态已变化，请重新读取",
  "billing_dispute.transition_invalid": "当前状态不允许该操作",
  "billing_dispute.self_review_forbidden": "申请人不能审核自己的争议",
  "billing_dispute.adjustment_invalid": "批准金额不合法",
  "billing_dispute.adjustment_conflict": "可调整金额已变化",
  "billing_dispute.idempotency_conflict": "幂等键已用于其他争议意图",
  "billing_dispute.integrity_failed": "账单争议数据完整性校验失败",
  "billing_dispute.unavailable": "账单争议服务暂不可用",
  "billing_dispute.response_invalid": "账单争议响应合同不完整",
});

const ERROR_STATUS = Object.freeze({
  "auth.token_missing": 401,
  "auth.token_invalid": 401,
  "auth.token_expired": 401,
  "auth.session_revoked": 401,
  "auth.permission_denied": 403,
  "billing_dispute.query_invalid": 422,
  "billing_dispute.request_invalid": 422,
  "billing_dispute.not_found": 404,
  "billing_dispute.scope_not_found": 404,
  "billing_dispute.scope_too_large": 422,
  "billing_dispute.hold_conflict": 409,
  "billing_dispute.revision_conflict": 409,
  "billing_dispute.transition_invalid": 409,
  "billing_dispute.self_review_forbidden": 403,
  "billing_dispute.adjustment_invalid": 422,
  "billing_dispute.adjustment_conflict": 409,
  "billing_dispute.idempotency_conflict": 409,
  "billing_dispute.integrity_failed": 503,
  "billing_dispute.unavailable": 503,
});

let requestSequence = 0;

/** Read the current tenant's dispute list without sending tenant or actor fields. */
export async function listBillingDisputes({
  controlApiUrl,
  token,
  tenantId,
  filters = {},
  signal,
  requestId = createBillingDisputeRequestId("list"),
}) {
  requireTenant(tenantId);
  const query = normalizeBillingDisputeListFilters(filters);
  const url = new URL(`${controlApiUrl}/billing-disputes`);
  for (const [key, value] of Object.entries(query)) {
    if (value !== "" && value !== null && value !== undefined) url.searchParams.set(key, String(value));
  }
  return requestJson(url.toString(), {
    headers: authorizationHeaders(token, requestId),
    signal,
  }, new Set([200]), (payload) => normalizeBillingDisputeListResponse(payload, tenantId, query.limit));
}

/** Read one current detail. The path ID is validated before URL construction. */
export async function getBillingDispute({
  controlApiUrl,
  token,
  tenantId,
  billingDisputeId,
  signal,
  requestId = createBillingDisputeRequestId("detail"),
}) {
  requireTenant(tenantId);
  const disputeId = requireDisputeId(billingDisputeId);
  return requestJson(`${controlApiUrl}/billing-disputes/${encodeURIComponent(disputeId)}`, {
    headers: authorizationHeaders(token, requestId),
    signal,
  }, new Set([200]), (payload) => normalizeBillingDisputeDetailResponse(payload, tenantId, disputeId));
}

/** Create a dispute with a single private idempotency key. */
export async function createBillingDispute({
  controlApiUrl,
  token,
  tenantId,
  request,
  idempotencyKey,
  signal,
  requestId = createBillingDisputeRequestId("create"),
}) {
  requireTenant(tenantId);
  const body = normalizeBillingDisputeCreateRequest(request);
  return requestJson(`${controlApiUrl}/billing-disputes`, {
    method: "POST",
    headers: writeHeaders(token, requestId, idempotencyKey),
    body: JSON.stringify(body),
    signal,
  }, new Set([200, 201]), (payload) => normalizeBillingDisputeMutationResponse(
    payload,
    tenantId,
    null,
    body,
  ));
}

/** Apply one CAS transition; tenant, actor, status and currency are never client-authored. */
export async function transitionBillingDispute({
  controlApiUrl,
  token,
  tenantId,
  billingDisputeId,
  request,
  idempotencyKey,
  signal,
  requestId = createBillingDisputeRequestId("transition"),
}) {
  requireTenant(tenantId);
  const disputeId = requireDisputeId(billingDisputeId);
  const body = normalizeBillingDisputeTransitionRequest(request);
  return requestJson(`${controlApiUrl}/billing-disputes/${encodeURIComponent(disputeId)}`, {
    method: "PATCH",
    headers: writeHeaders(token, requestId, idempotencyKey),
    body: JSON.stringify(body),
    signal,
  }, new Set([200]), (payload) => normalizeBillingDisputeMutationResponse(
    payload,
    tenantId,
    disputeId,
    body,
  ));
}

/** Normalize the two mutually exclusive POST scopes to exact backend fields. */
export function normalizeBillingDisputeCreateRequest(value) {
  if (!plainObject(value) || !SCOPE_TYPES.includes(value.scope_type)) throw requestError();
  const reason = normalizedReason(value.reason);
  if (!reason) throw requestError();
  if (value.scope_type === "request_ids") {
    requireExact(value, ["scope_type", "request_ids", "reason"]);
    if (!Array.isArray(value.request_ids) || value.request_ids.length < 1 || value.request_ids.length > 100) {
      throw requestError();
    }
    const requestIds = value.request_ids.map((item) => String(item ?? "").trim());
    if (requestIds.some((item) => !SAFE_ID.test(item)) || new Set(requestIds).size !== requestIds.length) {
      throw requestError();
    }
    return Object.freeze({ scope_type: "request_ids", request_ids: [...requestIds].sort(), reason });
  }
  requireExact(value, ["scope_type", "period_start", "period_end", "reason"]);
  if (!utcSeconds(value.period_start) || !utcSeconds(value.period_end)) throw requestError();
  const start = Date.parse(value.period_start);
  const end = Date.parse(value.period_end);
  if (!(start < end) || end - start > MAX_TIME_RANGE_MS) throw requestError();
  return Object.freeze({
    scope_type: "time_range",
    period_start: value.period_start,
    period_end: value.period_end,
    reason,
  });
}

/** Normalize PATCH bodies by action and reject client-authored authority fields. */
export function normalizeBillingDisputeTransitionRequest(value) {
  if (!plainObject(value) || !TRANSITION_ACTIONS.includes(value.action)) throw requestError();
  if (!positiveInteger(value.expected_revision)) throw requestError();
  if (value.action === "start_review") {
    requireExact(value, ["action", "expected_revision"]);
    return Object.freeze({ action: value.action, expected_revision: value.expected_revision });
  }
  const decisionReason = normalizedReason(value.decision_reason);
  if (!decisionReason) throw requestError();
  if (value.action !== "approve") {
    requireExact(value, ["action", "expected_revision", "decision_reason"]);
    return Object.freeze({
      action: value.action,
      expected_revision: value.expected_revision,
      decision_reason: decisionReason,
    });
  }
  requireExact(value, ["action", "expected_revision", "decision_reason", "adjustments"]);
  if (!Array.isArray(value.adjustments) || value.adjustments.length < 1 || value.adjustments.length > 100) {
    throw requestError();
  }
  const adjustments = value.adjustments.map((item) => {
    requireExact(item, ["billing_dispute_item_id", "amount"]);
    const itemId = String(item.billing_dispute_item_id ?? "");
    const amount = String(item.amount ?? "");
    if (!DISPUTE_ITEM_ID.test(itemId) || !positiveAmount(amount)) throw requestError();
    return Object.freeze({ billing_dispute_item_id: itemId, amount });
  }).sort((left, right) => left.billing_dispute_item_id.localeCompare(right.billing_dispute_item_id));
  if (new Set(adjustments.map((item) => item.billing_dispute_item_id)).size !== adjustments.length) {
    throw requestError();
  }
  return Object.freeze({
    action: value.action,
    expected_revision: value.expected_revision,
    decision_reason: decisionReason,
    adjustments: Object.freeze(adjustments),
  });
}

/** Normalize the six declared list query fields and nothing else. */
export function normalizeBillingDisputeListFilters(value = {}) {
  if (!plainObject(value)) throw clientError("billing_dispute.query_invalid", 422);
  requireExactSubset(value, ["status", "scope_type", "created_from", "created_to", "limit", "cursor"]);
  const status = String(value.status ?? "").trim();
  const scopeType = String(value.scope_type ?? "").trim();
  const createdFrom = String(value.created_from ?? "").trim();
  const createdTo = String(value.created_to ?? "").trim();
  const cursor = value.cursor === null ? null : String(value.cursor ?? "").trim();
  const limit = value.limit === undefined ? 50 : value.limit;
  if (
    (status && !LIST_STATUSES.includes(status))
    || (scopeType && !SCOPE_TYPES.includes(scopeType))
    || (createdFrom && !utcSeconds(createdFrom))
    || (createdTo && !utcSeconds(createdTo))
    || (createdFrom && createdTo && Date.parse(createdFrom) >= Date.parse(createdTo))
    || !Number.isSafeInteger(limit)
    || limit < 1
    || limit > 100
    || (cursor !== null && cursor !== "" && (!VISIBLE_CURSOR.test(cursor) || cursor.length > MAX_CURSOR_LENGTH))
  ) throw clientError("billing_dispute.query_invalid", 422);
  return Object.freeze({
    status,
    scope_type: scopeType,
    created_from: createdFrom,
    created_to: createdTo,
    limit,
    cursor: cursor || null,
  });
}

/** Deeply validate the exact list wire DTO before any page state can consume it. */
export function normalizeBillingDisputeListResponse(payload, tenantId, expectedLimit = null) {
  if (
    !exactObject(payload, LIST_RESPONSE_KEYS)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || !integer(payload.limit, 1, 100)
    || (expectedLimit !== null && payload.limit !== expectedLimit)
    || typeof payload.has_more !== "boolean"
    || !nullableCursor(payload.next_cursor)
    || payload.has_more !== (payload.next_cursor !== null)
    || !Array.isArray(payload.billing_disputes)
    || payload.billing_disputes.length > payload.limit
  ) return null;
  const disputes = payload.billing_disputes.map((item) => normalizeSummary(item, tenantId));
  if (disputes.some((item) => !item)) return null;
  if (new Set(disputes.map((item) => item.billing_dispute_id)).size !== disputes.length) return null;
  return Object.freeze({ ...payload, billing_disputes: Object.freeze(disputes) });
}

/** Deeply validate one exact detail response and the tenant owner at its summary root. */
export function normalizeBillingDisputeDetailResponse(payload, tenantId, expectedId = null) {
  if (!exactObject(payload, DETAIL_RESPONSE_KEYS) || !chain(payload.request_id) || !chain(payload.trace_id)) {
    return null;
  }
  const dispute = normalizeDetail(payload.dispute, tenantId);
  if (!dispute || (expectedId !== null && dispute.summary.billing_dispute_id !== expectedId)) return null;
  return Object.freeze({ ...payload, dispute });
}

/** Validate create/PATCH wrappers including their out-of-detail transition/link projections. */
export function normalizeBillingDisputeMutationResponse(
  payload,
  tenantId,
  expectedId = null,
  expectedRequest = null,
) {
  if (
    !exactObject(payload, MUTATION_RESPONSE_KEYS)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || typeof payload.idempotent_replay !== "boolean"
    || !Array.isArray(payload.adjustment_links)
    || payload.adjustment_links.length > 100
  ) return null;
  const dispute = normalizeDetail(payload.dispute, tenantId);
  const transition = normalizeTransition(payload.transition);
  const links = payload.adjustment_links.map(normalizeLink);
  if (
    !dispute
    || !transition
    || links.some((item) => !item)
    || (expectedId !== null && dispute.summary.billing_dispute_id !== expectedId)
    || !dispute.transitions.some((item) => (
      item.billing_dispute_transition_id === transition.billing_dispute_transition_id
      && JSON.stringify(item) === JSON.stringify(transition)
    ))
  ) return null;
  const detailLinks = new Map(dispute.adjustment_links.map((item) => [
    item.billing_dispute_adjustment_link_id,
    JSON.stringify(item),
  ]));
  if (links.some((item) => detailLinks.get(item.billing_dispute_adjustment_link_id) !== JSON.stringify(item))) {
    return null;
  }
  if (!mutationMirrorsRequest(dispute, transition, links, expectedRequest)) return null;
  return Object.freeze({
    ...payload,
    dispute,
    transition,
    adjustment_links: Object.freeze(links),
  });
}

function mutationMirrorsRequest(dispute, transition, links, request) {
  if (request === null) return true;
  if (request.scope_type) {
    return transition.action === "create"
      && transition.from_status === null
      && transition.to_status === "submitted"
      && dispute.summary.scope_type === request.scope_type
      && links.length === 0;
  }
  if (
    transition.action !== request.action
    || transition.expected_revision !== request.expected_revision
    || transition.new_revision !== request.expected_revision + 1
  ) return false;
  if (request.action !== "approve") return links.length === 0;
  const expected = new Map(request.adjustments.map((item) => [item.billing_dispute_item_id, item.amount]));
  return links.length === expected.size
    && links.every((item) => (
      item.billing_dispute_transition_id === transition.billing_dispute_transition_id
      && expected.get(item.billing_dispute_item_id) === item.amount
    ));
}

function normalizeDetail(value, tenantId) {
  if (
    !exactObject(value, DETAIL_KEYS)
    || !displayText(value.reason, MAX_REASON_BYTES)
    || !(value.decision_reason === null || displayText(value.decision_reason, MAX_REASON_BYTES))
    || !chain(value.create_request_id)
    || !chain(value.create_trace_id)
    || !Array.isArray(value.items)
    || !Array.isArray(value.holds)
    || !Array.isArray(value.adjustment_links)
    || !Array.isArray(value.transitions)
    || value.items.length > 100
    || value.holds.length > 100
    || value.adjustment_links.length > 100
    || value.transitions.length > 101
  ) return null;
  const summary = normalizeSummary(value.summary, tenantId);
  const items = value.items.map(normalizeItem);
  const holds = value.holds.map(normalizeHold);
  const links = value.adjustment_links.map(normalizeLink);
  const transitions = value.transitions.map(normalizeTransition);
  const event = normalizeEvent(value.event);
  if (
    !summary
    || !event
    || items.some((item) => !item)
    || holds.some((item) => !item)
    || links.some((item) => !item)
    || transitions.some((item) => !item)
    || items.length !== summary.item_count
    || new Set(items.map((item) => item.billing_dispute_item_id)).size !== items.length
    || new Set(holds.map((item) => item.billing_settlement_hold_id)).size !== holds.length
    || new Set(links.map((item) => item.billing_dispute_adjustment_link_id)).size !== links.length
    || new Set(transitions.map((item) => item.billing_dispute_transition_id)).size !== transitions.length
  ) return null;
  const itemIds = new Set(items.map((item) => item.billing_dispute_item_id));
  const holdById = new Map(holds.map((item) => [item.billing_settlement_hold_id, JSON.stringify(item)]));
  const linkById = new Map(links.map((item) => [item.billing_dispute_adjustment_link_id, JSON.stringify(item)]));
  if (
    holds.some((item) => !itemIds.has(item.billing_dispute_item_id))
    || links.some((item) => !itemIds.has(item.billing_dispute_item_id))
    || items.some((item) => !itemIds.has(item.hold.billing_dispute_item_id))
    || items.some((item) => ![...holdById.values()].includes(JSON.stringify(item.hold)))
    || items.some((item) => item.adjustment_link && ![...linkById.values()].includes(JSON.stringify(item.adjustment_link)))
  ) return null;
  return Object.freeze({
    ...value,
    summary,
    items: Object.freeze(items),
    holds: Object.freeze(holds),
    adjustment_links: Object.freeze(links),
    transitions: Object.freeze(transitions),
    event,
  });
}

function normalizeSummary(value, tenantId) {
  if (
    !exactObject(value, SUMMARY_KEYS)
    || !DISPUTE_ID.test(String(value.billing_dispute_id ?? ""))
    || value.tenant_id !== tenantId
    || !SCOPE_TYPES.includes(value.scope_type)
    || !LIST_STATUSES.includes(value.status)
    || !integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !integer(value.item_count, 1, 100)
    || !HASH.test(String(value.reason_sha256 ?? ""))
    || !currencyTotals(value.disputed_currency_totals)
    || !currencyTotals(value.approved_currency_totals)
    || !nonnegative(value.active_hold_count)
    || value.active_hold_count > value.item_count
    || !nonnegative(value.adjustment_count)
    || value.adjustment_count > value.item_count
    || !["pending", "processing", "succeeded", "failed"].includes(value.event_delivery_status)
    || !nullableUtc(value.review_started_at)
    || !nullableUtc(value.resolved_at)
    || !utcTime(value.created_at)
    || !utcTime(value.updated_at)
  ) return null;
  if (
    value.scope_type === "request_ids"
      ? value.period_start !== null || value.period_end !== null
      : !utcTime(value.period_start) || !utcTime(value.period_end) || Date.parse(value.period_start) >= Date.parse(value.period_end)
  ) return null;
  const createdBy = normalizeActor(value.created_by);
  const reviewer = value.reviewer === null ? null : normalizeActor(value.reviewer);
  if (!createdBy || (value.reviewer !== null && !reviewer)) return null;
  return Object.freeze({
    ...value,
    created_by: createdBy,
    reviewer,
    disputed_currency_totals: Object.freeze({ ...value.disputed_currency_totals }),
    approved_currency_totals: Object.freeze({ ...value.approved_currency_totals }),
  });
}

function normalizeActor(value) {
  if (
    !exactObject(value, ACTOR_KEYS)
    || !SAFE_ID.test(String(value.user_id ?? ""))
    || !(value.display_name === null || shortText(value.display_name, 200))
    || !shortText(value.email, 320)
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeHold(value) {
  if (
    !exactObject(value, HOLD_KEYS)
    || !publicObjectId(value.billing_settlement_hold_id)
    || !DISPUTE_ITEM_ID.test(String(value.billing_dispute_item_id ?? ""))
    || !publicObjectId(value.finops_ledger_entry_id)
    || !["active", "released"].includes(value.status)
    || !(value.release_reason === null || ["approved", "rejected", "cancelled"].includes(value.release_reason))
    || !integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !publicObjectId(value.created_transition_id)
    || !(value.released_transition_id === null || publicObjectId(value.released_transition_id))
    || !utcTime(value.created_at)
    || !nullableUtc(value.released_at)
    || !utcTime(value.updated_at)
  ) return null;
  if (value.status === "active" && (value.release_reason !== null || value.released_transition_id !== null || value.released_at !== null)) {
    return null;
  }
  if (value.status === "released" && (!value.release_reason || !value.released_transition_id || !value.released_at)) {
    return null;
  }
  return Object.freeze({ ...value });
}

function normalizeLink(value) {
  if (
    !exactObject(value, LINK_KEYS)
    || !publicObjectId(value.billing_dispute_adjustment_link_id)
    || !DISPUTE_ITEM_ID.test(String(value.billing_dispute_item_id ?? ""))
    || !publicObjectId(value.finops_adjustment_id)
    || !publicObjectId(value.reversal_finops_ledger_entry_id)
    || !positiveWireAmount(value.amount)
    || !["CNY", "USD"].includes(value.currency)
    || !publicObjectId(value.billing_dispute_transition_id)
    || !utcTime(value.created_at)
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeItem(value) {
  if (
    !exactObject(value, ITEM_KEYS)
    || !DISPUTE_ITEM_ID.test(String(value.billing_dispute_item_id ?? ""))
    || !publicObjectId(value.finops_ledger_entry_id)
    || !SAFE_ID.test(String(value.usage_ledger_id ?? ""))
    || !SAFE_ID.test(String(value.source_request_id ?? ""))
    || !chain(value.source_trace_id)
    || !integer(value.ledger_sequence, 1, Number.MAX_SAFE_INTEGER)
    || !["CNY", "USD"].includes(value.currency)
    || !positiveWireAmount(value.amount)
    || !positiveWireAmount(value.disputable_amount)
    || !nonnegativeWireAmount(value.current_disputable_amount)
    || !(value.price_snapshot_id === null || SAFE_ID.test(String(value.price_snapshot_id)))
    || !(value.price_schema_version === null || shortText(value.price_schema_version, 80))
    || !(value.price_snapshot_source === null || shortText(value.price_snapshot_source, 160))
    || !shortText(value.price_evidence_status, 80)
    || !utcTime(value.created_at)
  ) return null;
  const hold = normalizeHold(value.hold);
  const adjustmentLink = value.adjustment_link === null ? null : normalizeLink(value.adjustment_link);
  if (
    !hold
    || hold.billing_dispute_item_id !== value.billing_dispute_item_id
    || (value.adjustment_link !== null && !adjustmentLink)
    || (adjustmentLink && adjustmentLink.billing_dispute_item_id !== value.billing_dispute_item_id)
  ) return null;
  return Object.freeze({ ...value, hold, adjustment_link: adjustmentLink });
}

function normalizeTransition(value) {
  if (
    !exactObject(value, TRANSITION_KEYS)
    || !publicObjectId(value.billing_dispute_transition_id)
    || !["create", "start_review", "approve", "reject", "cancel"].includes(value.action)
    || !(value.from_status === null || LIST_STATUSES.includes(value.from_status))
    || !LIST_STATUSES.includes(value.to_status)
    || !nonnegative(value.expected_revision)
    || !integer(value.new_revision, 1, Number.MAX_SAFE_INTEGER)
    || !SAFE_ID.test(String(value.actor_user_id ?? ""))
    || !(value.decision_reason_sha256 === null || HASH.test(String(value.decision_reason_sha256)))
    || !integer(value.approved_item_count, 0, 100)
    || !(value.approved_totals_sha256 === null || HASH.test(String(value.approved_totals_sha256)))
    || !chain(value.operation_request_id)
    || !chain(value.operation_trace_id)
    || !utcTime(value.created_at)
  ) return null;
  if (
    (value.action === "create") !== (value.from_status === null)
    || value.new_revision !== value.expected_revision + 1
  ) return null;
  return Object.freeze({ ...value });
}

function normalizeEvent(value) {
  if (
    !exactObject(value, EVENT_KEYS)
    || !publicObjectId(value.billing_dispute_event_id)
    || value.event_type !== "billing_dispute.created"
    || !["pending", "processing", "succeeded", "failed"].includes(value.delivery_status)
    || !nonnegative(value.attempt_count)
    || !(value.last_error_code === null || ERROR_CODE.test(String(value.last_error_code)))
    || !utcTime(value.occurred_at)
    || !nullableUtc(value.succeeded_at)
  ) return null;
  return Object.freeze({ ...value });
}

/** Generate a unique request ID without embedding tenant, dispute IDs or free text. */
export function createBillingDisputeRequestId(lane = "request") {
  requestSequence += 1;
  const safeLane = /^(?:list|detail|create|transition|request)$/.test(lane) ? lane : "request";
  return `console_billing_dispute_${safeLane}_${requestSequence}_${secureUuid().replaceAll("-", "")}`;
}

/** Generate a write key that remains inside the page operation closure. */
export function createBillingDisputeIdempotencyKey() {
  return `billing-dispute-${secureUuid()}`;
}

export function isBillingDisputeAbortError(error) {
  return error?.name === "AbortError";
}

async function requestJson(url, init, successStatuses, normalizeSuccess) {
  let response;
  try {
    response = await fetch(url, { ...init, cache: "no-store", credentials: "omit" });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("billing_dispute.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw clientError("billing_dispute.response_invalid", 502);
  }
  if (!validResponseBoundary(response, payload)) {
    throw clientError("billing_dispute.response_invalid", 502);
  }
  if (!response.ok) {
    if (!validErrorPayload(payload) || ERROR_STATUS[payload.code] !== response.status) {
      throw clientError("billing_dispute.response_invalid", 502, payload?.request_id, payload?.trace_id);
    }
    throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
  }
  if (!successStatuses.has(response.status) || !plainObject(payload)) {
    throw clientError("billing_dispute.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  const normalized = normalizeSuccess?.(payload);
  if (!normalized) {
    throw clientError("billing_dispute.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

function authorizationHeaders(token, requestId) {
  if (typeof token !== "string" || !token || /\s/u.test(token) || !CHAIN_ID.test(String(requestId ?? ""))) {
    throw clientError("auth.token_missing", 401);
  }
  return { Authorization: `Bearer ${token}`, Accept: "application/json", "X-Request-ID": requestId };
}

function writeHeaders(token, requestId, idempotencyKey) {
  const key = String(idempotencyKey ?? "");
  if (!VISIBLE_KEY.test(key) || key.includes(",")) throw requestError();
  return {
    ...authorizationHeaders(token, requestId),
    "Content-Type": "application/json",
    "Idempotency-Key": key,
  };
}

function validResponseBoundary(response, payload) {
  const cacheControl = header(response?.headers, "Cache-Control");
  const requestId = header(response?.headers, "X-Request-ID");
  const traceId = header(response?.headers, "X-Trace-ID");
  return cacheControl === "no-store, private"
    && CHAIN_ID.test(requestId)
    && CHAIN_ID.test(traceId)
    && payload?.request_id === requestId
    && payload?.trace_id === traceId;
}

function validErrorPayload(value) {
  return plainObject(value)
    && ERROR_CODE.test(String(value.code ?? ""))
    && typeof value.message === "string"
    && CHAIN_ID.test(String(value.request_id ?? ""))
    && CHAIN_ID.test(String(value.trace_id ?? ""));
}

function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "billing_dispute.unavailable";
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "账单争议请求失败",
    requestId: CHAIN_ID.test(String(requestId ?? "")) ? requestId : "-",
    traceId: CHAIN_ID.test(String(traceId ?? "")) ? traceId : "-",
  });
}

function requestError() {
  return clientError("billing_dispute.request_invalid", 422);
}

function requireTenant(value) {
  if (!SAFE_ID.test(String(value ?? ""))) throw requestError();
}

function requireDisputeId(value) {
  const id = String(value ?? "");
  if (!DISPUTE_ID.test(id)) throw requestError();
  return id;
}

function normalizedReason(value) {
  if (typeof value !== "string") return "";
  const normalized = value.normalize("NFKC").trim();
  if (!normalized || /[\u0000-\u001f\u007f-\u009f]/u.test(normalized)) return "";
  return new TextEncoder().encode(normalized).byteLength <= MAX_REASON_BYTES ? normalized : "";
}

function positiveAmount(value) {
  if (!POSITIVE_AMOUNT.test(value)) return false;
  const [whole, fraction] = value.split(".");
  return BigInt(whole) * 1_000_000n + BigInt(fraction) <= MAX_AMOUNT_MICROS;
}

function positiveWireAmount(value) {
  return typeof value === "string" && POSITIVE_AMOUNT.test(value);
}

function nonnegativeWireAmount(value) {
  return typeof value === "string" && MONEY_AMOUNT.test(value);
}

function currencyTotals(value) {
  return plainObject(value)
    && Object.keys(value).length <= 2
    && Object.keys(value).every((currency) => ["CNY", "USD"].includes(currency))
    && Object.values(value).every((amount) => typeof amount === "string" && MONEY_AMOUNT.test(amount));
}

function utcSeconds(value) {
  if (typeof value !== "string" || !UTC_SECONDS.test(value)) return false;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) && new Date(milliseconds).toISOString().replace(".000Z", "Z") === value;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function nonnegative(value) {
  return integer(value, 0, Number.MAX_SAFE_INTEGER);
}

function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

function publicObjectId(value) {
  return typeof value === "string" && /^[a-z][a-z0-9]{1,31}_[0-9a-f]{32}$/.test(value);
}

function shortText(value, maximum) {
  return typeof value === "string"
    && value.length >= 1
    && [...value].length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);
}

function displayText(value, maximumBytes) {
  return shortText(value, 2000) && new TextEncoder().encode(value).byteLength <= maximumBytes;
}

function utcTime(value) {
  if (typeof value !== "string" || !UTC_TIME.test(value)) return false;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return false;
  const canonical = new Date(milliseconds).toISOString();
  const normalizedInput = value.includes(".") ? value.replace(/0+Z$/, "Z").replace(/\.Z$/, "Z") : value;
  const normalizedCanonical = canonical.replace(".000Z", "Z");
  return normalizedInput === normalizedCanonical || Date.parse(normalizedInput) === milliseconds;
}

function nullableUtc(value) {
  return value === null || utcTime(value);
}

function nullableCursor(value) {
  return value === null || (
    typeof value === "string"
    && value.length >= 1
    && value.length <= MAX_CURSOR_LENGTH
    && VISIBLE_CURSOR.test(value)
  );
}

function plainObject(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function exactObject(value, keys) {
  return plainObject(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort());
}

function requireExact(value, keys) {
  if (!plainObject(value) || JSON.stringify(Object.keys(value).sort()) !== JSON.stringify([...keys].sort())) {
    throw requestError();
  }
}

function requireExactSubset(value, keys) {
  if (Object.keys(value).some((key) => !keys.includes(key))) {
    throw clientError("billing_dispute.query_invalid", 422);
  }
}

function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

function secureUuid() {
  const value = globalThis.crypto?.randomUUID?.()?.toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(String(value ?? ""))) {
    throw clientError("billing_dispute.unavailable", 503);
  }
  return value;
}

export const BILLING_DISPUTE_REVIEW_ACTIONS = REVIEW_ACTIONS;
