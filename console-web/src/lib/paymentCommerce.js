import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const ORDER_ID = /^ord_[0-9a-f]{32}$/u;
const PAYMENT_INTENT_ID = /^pmi_[0-9a-f]{32}$/u;
const REFUND_ID = /^rfd_[0-9a-f]{32}$/u;
const SUBSCRIPTION_ID = /^sub_[0-9a-f]{32}$/u;
const DUNNING_ID = /^dun_[0-9a-f]{32}$/u;
const PLAN_VERSION_ID = /^spv_[0-9a-f]{32}$/u;
const CLIENT_TOKEN = /^ptok_[0-9a-f]{64}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const CURRENCIES = new Set(["CNY", "USD"]);
const PROVIDERS = new Set(["mockpay", "cloudpay"]);
const ORDER_STATUSES = new Set([
  "pending_payment", "paid", "fulfilled", "past_due", "canceled", "refunded",
  "partially_refunded",
]);
const INTENT_STATUSES = new Set([
  "requires_action", "processing", "succeeded", "failed", "canceled",
]);
const REFUND_STATUSES = new Set(["requested", "processing", "succeeded", "failed", "rejected"]);
const REFUND_REASONS = new Set(["customer_request", "service_failure", "duplicate_charge"]);
const REVIEW_REASONS = new Set(["policy_approved", "amount_invalid", "evidence_missing"]);

export function listWorkspaceBillingOrders({ controlApiUrl, signal } = {}) {
  return jsonRequest(controlApiUrl, "/billing/orders", { signal }, "workspace");
}

export function createWorkspacePaymentIntent({
  controlApiUrl,
  orderId,
  provider,
  idempotencyKey,
  signal,
} = {}) {
  const selectedProvider = String(provider ?? "");
  if (!PROVIDERS.has(selectedProvider)) invalidRequest("workspace");
  return jsonRequest(controlApiUrl, "/payments/intents", {
    method: "POST",
    headers: workspaceWriteHeaders(idempotencyKey),
    body: JSON.stringify({ order_id: checkedId(ORDER_ID, orderId, "workspace"), provider: selectedProvider }),
    signal,
  }, "workspace");
}

export function requestWorkspaceRefund({
  controlApiUrl,
  orderId,
  amountMinor,
  reasonCode,
  confirmed,
  idempotencyKey,
  signal,
} = {}) {
  const amount = Number(amountMinor);
  const reason = String(reasonCode ?? "");
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > 1_000_000_000_000
    || !REFUND_REASONS.has(reason) || confirmed !== true) invalidRequest("workspace");
  return jsonRequest(controlApiUrl, "/refunds", {
    method: "POST",
    headers: workspaceWriteHeaders(idempotencyKey),
    body: JSON.stringify({
      order_id: checkedId(ORDER_ID, orderId, "workspace"),
      amount_minor: amount,
      reason_code: reason,
      confirmed: true,
    }),
    signal,
  }, "workspace");
}

export function cancelWorkspaceSubscription({
  controlApiUrl,
  subscriptionId,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const expectedRevision = checkedRevision(revision, "workspace");
  return jsonRequest(
    controlApiUrl,
    `/subscriptions/${checkedId(SUBSCRIPTION_ID, subscriptionId, "workspace")}`,
    {
      method: "PATCH",
      headers: { ...workspaceWriteHeaders(idempotencyKey), "If-Match": `"${expectedRevision}"` },
      body: JSON.stringify({ operation: "cancel", plan_version_id: null }),
      signal,
    },
    "workspace",
  );
}

export function listPlatformRefunds({ controlApiUrl, status = "requested", signal } = {}) {
  const selectedStatus = String(status ?? "");
  if (!REFUND_STATUSES.has(selectedStatus)) invalidRequest("platform");
  return jsonRequest(
    controlApiUrl,
    `/platform/refunds?status=${encodeURIComponent(selectedStatus)}`,
    { signal },
    "platform",
  );
}

export function reviewPlatformRefund({
  controlApiUrl,
  refundId,
  revision,
  decision,
  reasonCode,
  confirmed,
  signal,
} = {}) {
  const selectedDecision = String(decision ?? "");
  const selectedReason = String(reasonCode ?? "");
  if (!new Set(["approve", "reject"]).has(selectedDecision)
    || !REVIEW_REASONS.has(selectedReason) || confirmed !== true
    || (selectedDecision === "approve") !== (selectedReason === "policy_approved")) {
    invalidRequest("platform");
  }
  return jsonRequest(
    controlApiUrl,
    `/platform/refunds/${checkedId(REFUND_ID, refundId, "platform")}/review`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": readPlatformCsrfCookie(),
        "If-Match": `"${checkedRevision(revision, "platform")}"`,
      },
      body: JSON.stringify({
        decision: selectedDecision,
        reason_code: selectedReason,
        confirmed: true,
      }),
      signal,
    },
    "platform",
  );
}

/** The provider token is never retained in controller state, even when its consumer fails. */
export async function consumePaymentClientToken(response, consumer = () => {}) {
  if (!plainObject(response) || !("client_token" in response)) invalidResponse("workspace");
  const token = response.client_token;
  if (token === null) return false;
  if (!CLIENT_TOKEN.test(String(token))) invalidResponse("workspace");
  try {
    await consumer({
      clientToken: token,
      paymentIntentId: response.payment_intent.payment_intent_id,
      provider: response.payment_intent.provider,
    });
    return true;
  } finally {
    response.client_token = "";
  }
}

async function jsonRequest(controlApiUrl, path, options, domain) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options?.method ?? "GET",
      headers: { Accept: "application/json", ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError(domain, "payment.network_unavailable", "暂时无法连接支付服务。");
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw clientError(
      domain,
      typeof payload.code === "string" ? payload.code : "payment.request_failed",
      typeof payload.message === "string" ? payload.message : "支付请求未完成，请稍后重试。",
      response.status,
      linkId(payload.request_id) ? payload.request_id : "-",
    );
  }
  if (!validResponse(payload, path)) invalidResponse(domain);
  return payload;
}

function validResponse(payload, path) {
  if (!plainObject(payload) || !linkId(payload.request_id) || !linkId(payload.trace_id)) return false;
  if (path === "/billing/orders") {
    return exactKeys(payload, [
      "orders", "payment_intents", "refunds", "dunning_cases", "request_id", "trace_id",
    ]) && boundedArray(payload.orders, validOrder)
      && boundedArray(payload.payment_intents, validPaymentIntent)
      && boundedArray(payload.refunds, validRefund)
      && boundedArray(payload.dunning_cases, validDunning);
  }
  if (path === "/payments/intents") {
    return exactKeys(payload, [
      "payment_intent", "client_token", "idempotent_replay", "request_id", "trace_id",
    ]) && validPaymentIntent(payload.payment_intent)
      && (payload.client_token === null || CLIENT_TOKEN.test(String(payload.client_token)))
      && typeof payload.idempotent_replay === "boolean";
  }
  if (path === "/refunds" || path.includes("/platform/refunds/")
    || path.startsWith("/subscriptions/")) {
    if (path.startsWith("/subscriptions/")) return validSubscriptionResponse(payload);
    return exactKeys(payload, ["refund", "idempotent_replay", "request_id", "trace_id"])
      && validRefund(payload.refund) && typeof payload.idempotent_replay === "boolean";
  }
  if (path.startsWith("/platform/refunds?")) {
    return exactKeys(payload, ["refunds", "request_id", "trace_id"])
      && boundedArray(payload.refunds, validRefund, 100);
  }
  return false;
}

function validOrder(value) {
  return plainObject(value) && exactKeys(value, [
    "order_id", "subscription_id", "plan_version_id", "order_kind", "status", "amount_minor",
    "captured_amount_minor", "refunded_amount_minor", "currency", "state_revision", "created_at",
    "updated_at",
  ]) && ORDER_ID.test(String(value.order_id)) && SUBSCRIPTION_ID.test(String(value.subscription_id))
    && PLAN_VERSION_ID.test(String(value.plan_version_id))
    && new Set(["create", "upgrade", "downgrade", "renewal"]).has(value.order_kind)
    && ORDER_STATUSES.has(value.status) && validMoneyInteger(value.amount_minor, true)
    && validMoneyInteger(value.captured_amount_minor, true)
    && validMoneyInteger(value.refunded_amount_minor, true)
    && value.refunded_amount_minor <= value.captured_amount_minor
    && value.captured_amount_minor <= value.amount_minor && CURRENCIES.has(value.currency)
    && positiveInteger(value.state_revision) && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validPaymentIntent(value) {
  return plainObject(value) && exactKeys(value, [
    "payment_intent_id", "order_id", "provider", "status", "amount_minor", "currency",
    "state_revision", "expires_at", "created_at", "updated_at",
  ]) && PAYMENT_INTENT_ID.test(String(value.payment_intent_id))
    && ORDER_ID.test(String(value.order_id)) && PROVIDERS.has(value.provider)
    && INTENT_STATUSES.has(value.status) && validMoneyInteger(value.amount_minor, true)
    && CURRENCIES.has(value.currency) && positiveInteger(value.state_revision)
    && timestamp(value.expires_at) && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validRefund(value) {
  return plainObject(value) && exactKeys(value, [
    "refund_id", "order_id", "payment_intent_id", "amount_minor", "currency", "reason_code",
    "status", "state_revision", "requested_at", "updated_at",
  ]) && REFUND_ID.test(String(value.refund_id)) && ORDER_ID.test(String(value.order_id))
    && PAYMENT_INTENT_ID.test(String(value.payment_intent_id))
    && validMoneyInteger(value.amount_minor, false) && CURRENCIES.has(value.currency)
    && REFUND_REASONS.has(value.reason_code) && REFUND_STATUSES.has(value.status)
    && positiveInteger(value.state_revision) && timestamp(value.requested_at)
    && timestamp(value.updated_at);
}

function validDunning(value) {
  return plainObject(value) && exactKeys(value, [
    "dunning_case_id", "order_id", "status", "cycle_number", "template_reference",
    "next_action_at",
  ]) && DUNNING_ID.test(String(value.dunning_case_id)) && ORDER_ID.test(String(value.order_id))
    && new Set(["open", "recovered", "closed"]).has(value.status)
    && positiveInteger(value.cycle_number) && value.cycle_number <= 12
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u.test(String(value.template_reference))
    && (value.next_action_at === null || timestamp(value.next_action_at));
}

function validSubscriptionResponse(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "subscription", "trial", "entitlement", "latest_order", "idempotent_replay", "request_id",
    "trace_id",
  ]) || typeof value.idempotent_replay !== "boolean") return false;
  const subscription = value.subscription;
  return plainObject(subscription) && SUBSCRIPTION_ID.test(String(subscription.subscription_id))
    && positiveInteger(subscription.state_revision);
}

function workspaceWriteHeaders(idempotencyKey) {
  return {
    "Content-Type": "application/json",
    "X-CSRF-Token": readWorkspaceCsrfCookie(),
    "Idempotency-Key": checkedIdempotencyKey(idempotencyKey, "workspace"),
  };
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest(domain);
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain);
  }
  return new URL(path, base).toString();
}

function checkedId(regex, value, domain) {
  const text = String(value ?? "");
  if (!regex.test(text)) invalidRequest(domain);
  return text;
}

function checkedIdempotencyKey(value, domain) {
  return checkedId(IDEMPOTENCY_KEY, value, domain);
}

function checkedRevision(value, domain) {
  const revision = Number(value);
  if (!positiveInteger(revision)) invalidRequest(domain);
  return revision;
}

function validMoneyInteger(value, allowZero) {
  return Number.isSafeInteger(value) && value >= (allowZero ? 0 : 1)
    && value <= 1_000_000_000_000;
}

function boundedArray(value, validator, maximum = 200) {
  return Array.isArray(value) && value.length <= maximum && value.every(validator);
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function linkId(value) {
  return LINK_ID.test(String(value ?? ""));
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value >= 1;
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function clientError(domain, code, message, status = 0, requestId = "-") {
  const ErrorType = domain === "platform" ? PlatformApiError : IdentityApiError;
  return new ErrorType({ status, code, message, requestId });
}

function invalidRequest(domain) {
  throw clientError(domain, "payment.request_invalid", "支付请求字段无效。");
}

function invalidResponse(domain) {
  throw clientError(domain, "payment.response_invalid", "支付响应格式无效。", 502);
}

async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
