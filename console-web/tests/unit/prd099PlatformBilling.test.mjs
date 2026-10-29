import assert from "node:assert/strict";
import test from "node:test";

import {
  createPlatformBillingAdjustment,
  downloadWorkspaceBillingExport,
  getWorkspaceBillingSummary,
  listPlatformBillingSummary,
  saveWorkspaceBillingExport,
} from "../../src/lib/platformBilling.js";
import { renderIdentitySecurityPage as renderAccountPage } from "../../src/features/identity/renderIdentityPage.js";
const renderIdentitySecurityPage = (root, state) => renderAccountPage(root, { accountSection: "billing", ...state });
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";

const CONTROL_URL = "http://control.test";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const CYCLE_ID = `pbc_${"2".repeat(32)}`;
const PLAN_VERSION_ID = `spv_${"3".repeat(32)}`;
const SUMMARY_HASH = "a".repeat(64);

test("Workspace billing client uses the Workspace cookie boundary and validates cycle selection", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(workspacePayload());
  };
  try {
    await getWorkspaceBillingSummary({ controlApiUrl: CONTROL_URL });
    await getWorkspaceBillingSummary({ controlApiUrl: CONTROL_URL, cycleId: CYCLE_ID });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(calls.map((call) => call.url), [
    `${CONTROL_URL}/billing/summary`,
    `${CONTROL_URL}/billing/summary?cycle_id=${CYCLE_ID}`,
  ]);
  assert.equal(calls.every((call) => call.options.credentials === "include"), true);
  assert.equal(calls.every((call) => call.options.cache === "no-store"), true);
  assert.equal(calls.some((call) => "Authorization" in call.options.headers), false);
  assert.throws(
    () => getWorkspaceBillingSummary({ controlApiUrl: CONTROL_URL, cycleId: "pbc_bad" }),
    (error) => error.code === "platform_billing.request_invalid",
  );
});

test("Workspace reconciliation export validates headers and revokes its Blob URL", async () => {
  const originalFetch = globalThis.fetch;
  const csv = new Blob(["\ufeffcycle_id,meter\r\n"], { type: "text/csv;charset=utf-8" });
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    headers: headers({
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": 'attachment; filename="metahub-billing-22222222.csv"',
      "Cache-Control": "no-store, private",
      "X-Content-Type-Options": "nosniff",
    }),
    async blob() { return csv; },
  });
  let clicked = 0;
  let removed = 0;
  let revoked = "";
  const link = {
    click() { clicked += 1; },
    remove() { removed += 1; },
  };
  const environment = {
    document: { body: { append(node) { assert.equal(node, link); } }, createElement: () => link },
    URL: { createObjectURL: () => "blob:billing", revokeObjectURL: (value) => { revoked = value; } },
  };
  try {
    const result = await downloadWorkspaceBillingExport({
      controlApiUrl: CONTROL_URL,
      cycleId: CYCLE_ID,
    });
    saveWorkspaceBillingExport(result, environment);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(link.download, "metahub-billing-22222222.csv");
  assert.equal(clicked, 1);
  assert.equal(removed, 1);
  assert.equal(revoked, "blob:billing");
});

test("Platform billing client binds status cursor, CSRF, idempotency, and summary CAS", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const csrf = "p".repeat(43);
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${csrf}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(String(url).endsWith("/adjustments")
      ? adjustmentPayload()
      : platformPayload());
  };
  try {
    await listPlatformBillingSummary({
      controlApiUrl: CONTROL_URL,
      status: "reconciliation_required",
      cursor: "cursor_1",
      limit: 50,
    });
    await createPlatformBillingAdjustment({
      controlApiUrl: CONTROL_URL,
      idempotencyKey: "platform-billing:test-0001",
      adjustment: {
        workspace_id: WORKSPACE_ID,
        billing_cycle_id: CYCLE_ID,
        meter_code: "api_requests",
        reason_code: "manual_credit",
        quantity_delta: -5,
        amount_delta: "-1.00",
        expected_summary_sha256: SUMMARY_HASH,
      },
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].url, `${CONTROL_URL}/platform/billing/summary?limit=50&status=reconciliation_required&cursor=cursor_1`);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], csrf);
  assert.equal(calls[1].options.headers["Idempotency-Key"], "platform-billing:test-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    workspace_id: WORKSPACE_ID,
    billing_cycle_id: CYCLE_ID,
    meter_code: "api_requests",
    reason_code: "manual_credit",
    quantity_delta: -5,
    amount_delta: "-1.00",
    expected_summary_sha256: SUMMARY_HASH,
  });
  assert.equal("Authorization" in calls[1].options.headers, false);
});

test("Billing clients reject unexpected sensitive response fields", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ ...workspacePayload(), prompt: "must-not-render" });
  try {
    await assert.rejects(
      getWorkspaceBillingSummary({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "platform_billing.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Billing renderers prune Workspace and Platform adjustment controls by role", () => {
  const ownerRoot = { innerHTML: "" };
  renderIdentitySecurityPage(ownerRoot, identityState("owner"));
  assert.match(ownerRoot.innerHTML, /id="identity-billing-title"/u);
  assert.match(ownerRoot.innerHTML, /data-identity-action="download-billing-export"/u);
  assert.match(ownerRoot.innerHTML, /API 请求/u);

  const developerRoot = { innerHTML: "" };
  renderIdentitySecurityPage(developerRoot, identityState("developer"));
  assert.doesNotMatch(developerRoot.innerHTML, /identity-billing-title|download-billing-export/u);

  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.billing.read",
    "platform.billing.adjust",
  ]));
  assert.match(writable.innerHTML, /周期与差异状态/u);
  assert.match(writable.innerHTML, /data-platform-billing-adjustment-form/u);
  assert.doesNotMatch(writable.innerHTML, new RegExp(SUMMARY_HASH, "u"));

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.billing.read"]));
  assert.match(readOnly.innerHTML, /platform-billing-table/u);
  assert.doesNotMatch(readOnly.innerHTML, /data-platform-billing-adjustment-form/u);
});

function identityState(role) {
  return {
    account: { display_name: "Finance", email: "finance@example.com", status: "active", mfa_configured: true },
    currentSession: {
      session_id: "session-a",
      is_current: true,
      device_family: "browser",
      created_at: "2026-08-22T00:00:00Z",
      last_seen_at: "2026-08-22T00:00:00Z",
      expires_at: "2026-08-23T00:00:00Z",
    },
    sessions: [],
    workspaces: [],
    workspaceChoices: [],
    memberships: [],
    navigationItems: [],
    workspaceContext: { workspace_id: WORKSPACE_ID, name: "Workspace", kind: "organization", role },
    onboarding: null,
    saasCatalog: { plans: [] },
    subscription: { subscription: null },
    billingSummary: workspacePayload(),
    billingBusy: false,
    platformAccess: false,
  };
}

function platformState(permissions) {
  return {
    session: {
      context: {
        expires_at: "2026-08-23T00:00:00Z",
        permissions,
        operator: { display_name: "Billing", roles: ["platform_billing"] },
      },
    },
    navigation: [{ id: "billing", label: "Billing", href: "/platform.html#billing" }],
    view: "billing",
    plans: null,
    billing: platformPayload(),
    billingStatus: "",
    billingCursorHistory: [],
    loading: false,
  };
}

function workspacePayload() {
  return {
    cycle: cyclePayload(),
    available_cycles: [CYCLE_ID],
    request_id: "req-billing",
    trace_id: "trc-billing",
  };
}

function platformPayload() {
  return {
    cycles: [{ ...cyclePayload(), updated_at: "2026-08-22T02:00:00Z" }],
    next_cursor: null,
    request_id: "req-platform-billing",
    trace_id: "trc-platform-billing",
  };
}

function cyclePayload() {
  return {
    billing_cycle_id: CYCLE_ID,
    workspace_id: WORKSPACE_ID,
    status: "reconciliation_required",
    plan_version_id: PLAN_VERSION_ID,
    period_started_at: "2026-08-01T00:00:00Z",
    period_ends_at: "2026-09-01T00:00:00Z",
    currency: "CNY",
    source_watermark: 12,
    revision: 3,
    meters: [
      meter("api_requests", "request", "subscription_base", 120, 100, 20),
      meter("input_tokens", "token", "informational", 200, 0, 0),
      meter("output_tokens", "token", "informational", 50, 0, 0),
      meter("total_tokens", "token", "informational", 250, 0, 0),
    ],
  };
}

function meter(meterCode, unit, billingMode, usage, allocated, overage) {
  return {
    meter_code: meterCode,
    unit,
    billing_mode: billingMode,
    usage_quantity: usage,
    allocated_quantity: allocated,
    overage_quantity: overage,
    adjustment_quantity: 0,
    currency: "CNY",
    amount: billingMode === "subscription_base" ? "99.00" : "0.00",
    adjustment_amount: "0.00",
    effective_amount: billingMode === "subscription_base" ? "99.00" : "0.00",
    summary_revision: 1,
    summary_sha256: SUMMARY_HASH,
  };
}

function adjustmentPayload() {
  return {
    adjustment: {
      billing_adjustment_id: `pba_${"4".repeat(32)}`,
      workspace_id: WORKSPACE_ID,
      billing_cycle_id: CYCLE_ID,
      meter_code: "api_requests",
      reason_code: "manual_credit",
      quantity_delta: -5,
      amount_delta: "-1.00",
      currency: "CNY",
      created_at: "2026-08-22T03:00:00Z",
    },
    idempotent_replay: false,
    request_id: "req-adjustment",
    trace_id: "trc-adjustment",
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: headers({}),
    async json() { return structuredClone(payload); },
  };
}

function headers(values) {
  const normalized = new Map(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]));
  return { get: (name) => normalized.get(String(name).toLowerCase()) ?? null };
}
