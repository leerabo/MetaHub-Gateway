import assert from "node:assert/strict";
import test from "node:test";

import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  cancelWorkspaceSubscription,
  consumePaymentClientToken,
  createWorkspacePaymentIntent,
  listPlatformRefunds,
  listWorkspaceBillingOrders,
  requestWorkspaceRefund,
  reviewPlatformRefund,
} from "../../src/lib/paymentCommerce.js";

const CONTROL_URL = "http://control.test";
const ORDER_ID = `ord_${"1".repeat(32)}`;
const SUBSCRIPTION_ID = `sub_${"2".repeat(32)}`;
const PLAN_VERSION_ID = `spv_${"3".repeat(32)}`;
const PAYMENT_INTENT_ID = `pmi_${"4".repeat(32)}`;
const REFUND_ID = `rfd_${"5".repeat(32)}`;
const CLIENT_TOKEN = `ptok_${"6".repeat(64)}`;

test("Workspace payment client binds Cookie CSRF, idempotency, CAS, and server-owned fields", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const csrf = "w".repeat(43);
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${csrf}` };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const path = new URL(url).pathname;
    if (path === "/billing/orders") return jsonResponse(billingOrdersPayload());
    if (path === "/payments/intents") return jsonResponse(paymentIntentPayload(), 201);
    if (path === "/refunds") return jsonResponse(refundPayload(), 201);
    return jsonResponse(subscriptionPayload());
  };
  try {
    await listWorkspaceBillingOrders({ controlApiUrl: CONTROL_URL });
    await createWorkspacePaymentIntent({
      controlApiUrl: CONTROL_URL,
      orderId: ORDER_ID,
      provider: "mockpay",
      idempotencyKey: "prd103-payment-client-0001",
    });
    await requestWorkspaceRefund({
      controlApiUrl: CONTROL_URL,
      orderId: ORDER_ID,
      amountMinor: 4000,
      reasonCode: "customer_request",
      confirmed: true,
      idempotencyKey: "prd103-refund-client-0001",
    });
    await cancelWorkspaceSubscription({
      controlApiUrl: CONTROL_URL,
      subscriptionId: SUBSCRIPTION_ID,
      revision: 3,
      idempotencyKey: "prd103-cancel-client-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map((call) => new URL(call.url).pathname), [
    "/billing/orders",
    "/payments/intents",
    "/refunds",
    `/subscriptions/${SUBSCRIPTION_ID}`,
  ]);
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[1].options.headers["Idempotency-Key"], "prd103-payment-client-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    order_id: ORDER_ID,
    provider: "mockpay",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    order_id: ORDER_ID,
    amount_minor: 4000,
    reason_code: "customer_request",
    confirmed: true,
  });
  assert.equal(calls[3].options.method, "PATCH");
  assert.equal(calls[3].options.headers["If-Match"], '"3"');
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    operation: "cancel",
    plan_version_id: null,
  });
  assert.equal(calls.some((call) => "Authorization" in (call.options.headers ?? {})), false);
});

test("one-time payment token is cleared after both successful and failed consumers", async () => {
  const accepted = paymentIntentPayload();
  let observed = "";
  assert.equal(await consumePaymentClientToken(accepted, async ({ clientToken }) => {
    observed = clientToken;
  }), true);
  assert.equal(observed, CLIENT_TOKEN);
  assert.equal(accepted.client_token, "");

  const failed = paymentIntentPayload();
  await assert.rejects(
    consumePaymentClientToken(failed, async () => { throw new Error("provider unavailable"); }),
    /provider unavailable/u,
  );
  assert.equal(failed.client_token, "");

  const replay = { ...paymentIntentPayload(), client_token: null, idempotent_replay: true };
  assert.equal(await consumePaymentClientToken(replay), false);
  assert.equal(replay.client_token, null);
});

test("Platform refund client binds status, Platform CSRF, revision, and fixed decisions", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const csrf = "p".repeat(43);
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${csrf}` };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse(options.method === "POST" ? refundPayload() : platformRefundPayload());
  };
  try {
    await listPlatformRefunds({ controlApiUrl: CONTROL_URL, status: "requested" });
    await reviewPlatformRefund({
      controlApiUrl: CONTROL_URL,
      refundId: REFUND_ID,
      revision: 1,
      decision: "approve",
      reasonCode: "policy_approved",
      confirmed: true,
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].url, `${CONTROL_URL}/platform/refunds?status=requested`);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[1].options.headers["If-Match"], '"1"');
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    decision: "approve",
    reason_code: "policy_approved",
    confirmed: true,
  });
  assert.throws(
    () => reviewPlatformRefund({
      controlApiUrl: CONTROL_URL,
      refundId: REFUND_ID,
      revision: 1,
      decision: "approve",
      reasonCode: "amount_invalid",
      confirmed: true,
    }),
    (error) => error.code === "payment.request_invalid",
  );
});

test("payment renderers expose commands only inside finance permission boundaries", () => {
  const financeRoot = { innerHTML: "" };
  renderIdentitySecurityPage(financeRoot, identityState("finance_admin"));
  assert.match(financeRoot.innerHTML, /id="identity-payment-commerce-title"/u);
  assert.match(financeRoot.innerHTML, /data-payment-intent-form/u);
  assert.match(financeRoot.innerHTML, /data-refund-request-form/u);
  assert.match(financeRoot.innerHTML, /data-identity-action="cancel-subscription"/u);
  assert.doesNotMatch(financeRoot.innerHTML, new RegExp(CLIENT_TOKEN, "u"));

  const developerRoot = { innerHTML: "" };
  renderIdentitySecurityPage(developerRoot, identityState("developer"));
  assert.doesNotMatch(developerRoot.innerHTML, /identity-payment-commerce-title|cancel-subscription/u);

  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState(["platform.billing.read", "platform.billing.adjust"]));
  assert.match(writable.innerHTML, /退款审核队列/u);
  assert.match(writable.innerHTML, /data-platform-action="refund-approve"/u);
  assert.match(writable.innerHTML, /data-platform-refund-reject-form/u);

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.billing.read"]));
  assert.match(readOnly.innerHTML, /退款审核队列/u);
  assert.doesNotMatch(readOnly.innerHTML, /refund-approve|platform-refund-reject-form/u);
});

function identityState(role) {
  return {
    account: {
      display_name: "Finance",
      email: "finance@example.com",
      status: "active",
      mfa_configured: true,
    },
    currentSession: {
      session_id: "session-a",
      is_current: true,
      device_family: "browser",
      created_at: "2026-08-23T00:00:00Z",
      last_seen_at: "2026-08-23T00:00:00Z",
      expires_at: "2026-08-24T00:00:00Z",
    },
    sessions: [],
    workspaces: [],
    workspaceChoices: [],
    memberships: [],
    navigationItems: [],
    workspaceContext: {
      workspace_id: `wsp_${"7".repeat(32)}`,
      name: "Workspace",
      kind: "organization",
      role,
    },
    onboarding: null,
    saasCatalog: { plans: [] },
    subscription: subscriptionPayload(),
    subscriptionBusy: false,
    billingOrders: billingOrdersPayload(),
    paymentBusy: false,
    platformAccess: false,
  };
}

function platformState(permissions) {
  return {
    session: {
      context: {
        expires_at: "2026-08-24T00:00:00Z",
        permissions,
        operator: { display_name: "Billing", roles: ["platform_billing"] },
      },
    },
    navigation: [{ id: "billing", label: "Billing", href: "/platform.html#billing" }],
    view: "billing",
    plans: null,
    billing: { cycles: [], next_cursor: null },
    refunds: platformRefundPayload(),
    refundStatus: "requested",
    billingStatus: "",
    billingCursorHistory: [],
    loading: false,
  };
}

function billingOrdersPayload() {
  return {
    orders: [{
      order_id: ORDER_ID,
      subscription_id: SUBSCRIPTION_ID,
      plan_version_id: PLAN_VERSION_ID,
      order_kind: "create",
      status: "pending_payment",
      amount_minor: 10000,
      captured_amount_minor: 8000,
      refunded_amount_minor: 0,
      currency: "CNY",
      state_revision: 2,
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:01:00Z",
    }],
    payment_intents: [],
    refunds: [],
    dunning_cases: [],
    request_id: "req-orders",
    trace_id: "trc-orders",
  };
}

function paymentIntentPayload() {
  return {
    payment_intent: {
      payment_intent_id: PAYMENT_INTENT_ID,
      order_id: ORDER_ID,
      provider: "mockpay",
      status: "requires_action",
      amount_minor: 10000,
      currency: "CNY",
      state_revision: 1,
      expires_at: "2026-08-23T00:15:00Z",
      created_at: "2026-08-23T00:00:00Z",
      updated_at: "2026-08-23T00:00:00Z",
    },
    client_token: CLIENT_TOKEN,
    idempotent_replay: false,
    request_id: "req-intent",
    trace_id: "trc-intent",
  };
}

function refundPayload() {
  return {
    refund: {
      refund_id: REFUND_ID,
      order_id: ORDER_ID,
      payment_intent_id: PAYMENT_INTENT_ID,
      amount_minor: 4000,
      currency: "CNY",
      reason_code: "customer_request",
      status: "requested",
      state_revision: 1,
      requested_at: "2026-08-23T00:05:00Z",
      updated_at: "2026-08-23T00:05:00Z",
    },
    idempotent_replay: false,
    request_id: "req-refund",
    trace_id: "trc-refund",
  };
}

function platformRefundPayload() {
  return {
    refunds: [refundPayload().refund],
    request_id: "req-refunds",
    trace_id: "trc-refunds",
  };
}

function subscriptionPayload() {
  return {
    subscription: {
      subscription_id: SUBSCRIPTION_ID,
      plan_version_id: PLAN_VERSION_ID,
      pending_plan_version_id: null,
      status: "active",
      state_revision: 3,
      current_period_ends_at: "2026-09-23T00:00:00Z",
    },
    trial: null,
    entitlement: null,
    latest_order: null,
    idempotent_replay: false,
    request_id: "req-subscription",
    trace_id: "trc-subscription",
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
