import assert from "node:assert/strict";
import test from "node:test";

import {
  changeWorkspaceSubscription,
  createWorkspaceSubscription,
  getWorkspaceSubscription,
  listPublicSaasPlans,
} from "../../src/lib/saasBilling.js";
import {
  createPlatformSaasPlan,
  listPlatformSaasPlans,
} from "../../src/lib/platform.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";

const CONTROL_URL = "http://control.test";
const PLAN_VERSION_ID = `spv_${"1".repeat(32)}`;
const OTHER_PLAN_VERSION_ID = `spv_${"2".repeat(32)}`;

test("Workspace SaaS client uses only cookies plus CSRF, CAS, and idempotency headers", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf-token" };
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse(String(url).endsWith("/catalog/plans") ? catalogPayload() : subscriptionPayload());
  };
  try {
    await listPublicSaasPlans({ controlApiUrl: CONTROL_URL });
    await getWorkspaceSubscription({ controlApiUrl: CONTROL_URL });
    await createWorkspaceSubscription({
      controlApiUrl: CONTROL_URL,
      planVersionId: PLAN_VERSION_ID,
      startTrial: true,
      idempotencyKey: "subscription:create:0001",
    });
    await changeWorkspaceSubscription({
      controlApiUrl: CONTROL_URL,
      planVersionId: OTHER_PLAN_VERSION_ID,
      revision: 3,
      idempotencyKey: "subscription:change:0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map((call) => new URL(call.url).pathname), [
    "/catalog/plans",
    "/subscriptions/current",
    "/subscriptions",
    "/subscriptions/current/change-plan",
  ]);
  assert.equal(calls[0].options.cache, "default");
  assert.equal(calls[1].options.cache, "no-store");
  assert.equal(calls[2].options.credentials, "include");
  assert.equal(calls[2].options.headers["X-CSRF-Token"], "workspace-csrf-token");
  assert.equal(calls[2].options.headers["Idempotency-Key"], "subscription:create:0001");
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    plan_version_id: PLAN_VERSION_ID,
    start_trial: true,
  });
  assert.equal(calls[3].options.headers["If-Match"], '"3"');
  assert.equal(calls.some((call) => "Authorization" in call.options.headers), false);
  assert.doesNotMatch(JSON.stringify(calls), /tenant_id|account_id/u);
});

test("Platform SaaS client sends the exact catalog payload under Platform CSRF", async () => {
  const originalDocument = globalThis.document;
  const platformCsrf = "p".repeat(43);
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${platformCsrf}` };
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return jsonResponse(catalogPayload());
  };
  try {
    await listPlatformSaasPlans({ controlApiUrl: CONTROL_URL });
    await createPlatformSaasPlan({
      controlApiUrl: CONTROL_URL,
      plan: {
        plan_code: "2team",
        display_name: "Team <safe>",
        display_order: 20,
        billing_period: "monthly",
        currency: "CNY",
        unit_price: "10000000000.00",
        trial_days: 14,
        entitlements: {
          gateway_requests_per_month: 10000,
          workspace_seats: 5,
          model_aliases: 10,
          retention_days: 30,
          priority_support: true,
        },
        publish: true,
      },
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.equal(calls[0].url, `${CONTROL_URL}/platform/plans`);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], platformCsrf);
  const requestBody = JSON.parse(calls[1].options.body);
  assert.equal(requestBody.plan_code, "2team");
  assert.equal(requestBody.unit_price, "10000000000.00");
  assert.equal(requestBody.entitlements.priority_support, true);
  assert.equal("Authorization" in calls[1].options.headers, false);
});

test("Workspace renderer exposes subscription commands only to owner and finance roles", () => {
  const ownerRoot = { innerHTML: "" };
  renderIdentitySecurityPage(ownerRoot, identityState("owner"));
  assert.match(ownerRoot.innerHTML, /id="identity-subscription-title"/u);
  assert.match(ownerRoot.innerHTML, /data-saas-subscription-create-form/u);
  assert.doesNotMatch(ownerRoot.innerHTML, /Team <safe>/u);

  const financeRoot = { innerHTML: "" };
  renderIdentitySecurityPage(financeRoot, identityState("finance_admin", subscriptionPayload()));
  assert.match(financeRoot.innerHTML, /data-saas-subscription-change-form/u);
  assert.match(financeRoot.innerHTML, /当前权益/u);

  const developerRoot = { innerHTML: "" };
  renderIdentitySecurityPage(developerRoot, identityState("developer"));
  assert.doesNotMatch(developerRoot.innerHTML, /identity-subscription-title|data-saas-subscription/u);
});

test("Platform billing view lists immutable versions and prunes publishing without catalog.write", () => {
  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.catalog.read",
    "platform.catalog.write",
  ]));
  assert.match(writable.innerHTML, /data-platform-plan-form/u);
  assert.match(writable.innerHTML, /版本记录/u);
  assert.doesNotMatch(writable.innerHTML, /Team <safe>/u);

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.catalog.read"]));
  assert.doesNotMatch(readOnly.innerHTML, /data-platform-plan-form/u);
  assert.match(readOnly.innerHTML, /platform-plan-table/u);
});

function identityState(role, subscription = { subscription: null }) {
  return {
    account: { display_name: "Owner", email: "owner@example.com", status: "active", mfa_configured: true },
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
    workspaceContext: {
      workspace_id: `wsp_${"3".repeat(32)}`,
      name: "Workspace",
      kind: "organization",
      role,
      session_status: "active",
    },
    onboarding: null,
    saasCatalog: catalogPayload(),
    subscription,
    subscriptionBusy: false,
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
    plans: catalogPayload(),
    loading: false,
  };
}

function catalogPayload() {
  return {
    plans: [{
      plan_catalog_id: `spc_${"4".repeat(32)}`,
      plan_code: "team",
      display_name: "Team <safe>",
      status: "active",
      display_order: 20,
      revision: 1,
      versions: [{
        plan_version_id: PLAN_VERSION_ID,
        version_number: 1,
        status: "published",
        billing_period: "monthly",
        currency: "CNY",
        unit_price: "99.00",
        trial_days: 14,
        entitlements: {
          gateway_requests_per_month: 10000,
          workspace_seats: 5,
          model_aliases: 10,
          retention_days: 30,
          priority_support: true,
        },
        entitlements_sha256: "a".repeat(64),
        published_at: "2026-08-22T00:00:00Z",
        created_at: "2026-08-22T00:00:00Z",
      }],
    }, {
      plan_catalog_id: `spc_${"7".repeat(32)}`,
      plan_code: "scale",
      display_name: "Scale",
      status: "active",
      display_order: 30,
      revision: 1,
      versions: [{
        plan_version_id: OTHER_PLAN_VERSION_ID,
        version_number: 1,
        status: "published",
        billing_period: "monthly",
        currency: "CNY",
        unit_price: "299.00",
        trial_days: 0,
        entitlements: {
          gateway_requests_per_month: 100000,
          workspace_seats: 20,
          model_aliases: 50,
          retention_days: 90,
          priority_support: true,
        },
        entitlements_sha256: "b".repeat(64),
        published_at: "2026-08-22T00:00:00Z",
        created_at: "2026-08-22T00:00:00Z",
      }],
    }],
    request_id: "req-catalog",
    trace_id: "trc-catalog",
  };
}

function subscriptionPayload() {
  return {
    subscription: {
      subscription_id: `sub_${"5".repeat(32)}`,
      workspace_id: `wsp_${"3".repeat(32)}`,
      plan_version_id: PLAN_VERSION_ID,
      pending_plan_version_id: null,
      status: "trialing",
      current_period_started_at: "2026-08-22T00:00:00Z",
      current_period_ends_at: "2026-09-22T00:00:00Z",
      pending_effective_at: null,
      state_revision: 3,
      created_at: "2026-08-22T00:00:00Z",
      updated_at: "2026-08-22T00:00:00Z",
    },
    trial: null,
    entitlement: {
      entitlement_snapshot_id: `ens_${"6".repeat(32)}`,
      plan_version_id: PLAN_VERSION_ID,
      snapshot_version: 1,
      entitlements: catalogPayload().plans[0].versions[0].entitlements,
      entitlements_sha256: "a".repeat(64),
      effective_from: "2026-08-22T00:00:00Z",
      effective_until: null,
    },
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
    headers: { get: () => null },
    async json() {
      return structuredClone(payload);
    },
  };
}
