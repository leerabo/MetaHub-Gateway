import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformCommercialAccountingController } from "../../src/features/platform-commercial-accounting/platformCommercialAccountingController.js";
import { createPlatformCommercialAccountingStore } from "../../src/features/platform-commercial-accounting/platformCommercialAccountingStore.js";
import { renderPlatformCommercialAccounting } from "../../src/features/platform-commercial-accounting/renderPlatformCommercialAccounting.js";
import { createWorkspaceCommercialAccountingController } from "../../src/features/workspace-commercial-accounting/workspaceCommercialAccountingController.js";
import { createWorkspaceCommercialAccountingStore } from "../../src/features/workspace-commercial-accounting/workspaceCommercialAccountingStore.js";
import {
  renderWorkspaceInternalFinOps,
  renderWorkspaceSubscription,
} from "../../src/features/workspace-commercial-accounting/renderWorkspaceCommercialAccounting.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import {
  applyPlatformCommercialPlanCommand,
  applyPlatformProviderReferenceRateCommand,
  decidePlatformCommercialDispute,
  listPlatformCommercialPlans,
  listPlatformCommercialSubscriptions,
} from "../../src/lib/platformCommercialAccounting.js";
import {
  applyWorkspaceProviderCostRateCommand,
  createWorkspaceCommercialDispute,
  getWorkspaceCommercialSubscription,
  listWorkspaceProviderCostRates,
} from "../../src/lib/workspaceCommercialAccounting.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const NOW = "2026-09-03T08:00:00.000Z";
const LATER = "2026-10-03T08:00:00.000Z";
const PLATFORM_CSRF = "P".repeat(43);
const WORKSPACE_CSRF = "W".repeat(43);
const STEP_UP = "G".repeat(43);
const PLATFORM_KEY = "commercial-platform-144-0001";
const WORKSPACE_KEY = "commercial-workspace-144-0001";
const PLAN_ID = "spc_" + "1".repeat(32);
const PLAN_RELEASE_ID = "pplr_" + "2".repeat(32);
const PLAN_VERSION_ID = "spv_" + "3".repeat(32);
const SUBSCRIPTION_ID = "sub_" + "4".repeat(32);
const WORKSPACE_REF = "wref_" + "5".repeat(32);
const INVOICE_ID = "pinv_" + "6".repeat(32);
const DISPUTE_ID = "bdc_" + "7".repeat(32);
const REFERENCE_ID = "pprr_" + "8".repeat(32);
const RATE_ID = "pcr_" + "9".repeat(32);
const RATE_VERSION_ID = "pcrv_" + "a".repeat(32);
const WORKSPACE_ID = "wsp_" + "b".repeat(32);
const OPERATOR_ID = "pto_" + "c".repeat(32);
const PROVIDER_ID = "provider_144";
const MODEL_ID = "model_144";
const HASH = "d".repeat(64);

test("PRD-144 clients isolate PlatformSession and WorkspaceSession cookies with exact writes", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const parsed = new URL(url);
    const method = options.method ?? "GET";
    if (parsed.pathname === "/platform/plans" && method === "GET") {
      return jsonResponse(platformPlans());
    }
    if (parsed.pathname === "/platform/plans" && method === "POST") {
      return jsonResponse(platformPlanCommand());
    }
    if (parsed.pathname === "/platform/subscriptions" && method === "GET") {
      return jsonResponse(platformSubscriptions());
    }
    if (parsed.pathname === "/platform/billing-disputes/" + DISPUTE_ID + "/decisions") {
      return jsonResponse(disputeCommand({ status: "under_review" }));
    }
    if (parsed.pathname === "/platform/provider-reference-rates/" + PROVIDER_ID + "/commands") {
      return jsonResponse(referenceCommand());
    }
    if (parsed.pathname === "/workspace/subscription") {
      return jsonResponse(workspaceSubscription());
    }
    if (parsed.pathname === "/workspace/billing-disputes") {
      return jsonResponse(disputeCommand());
    }
    if (parsed.pathname === "/workspace/provider-cost-rates" && method === "GET") {
      return jsonResponse(workspaceRates());
    }
    if (parsed.pathname === "/workspace/provider-cost-rates" && method === "PATCH") {
      return jsonResponse(rateCommand());
    }
    throw new Error("unexpected commercial route " + method + " " + parsed.pathname);
  });

  await listPlatformCommercialPlans({
    controlApiUrl: CONTROL_URL,
    filters: { status: "candidate", currency: "CNY" },
    cursor: "plans.cursor",
    limit: 25,
  });
  await applyPlatformCommercialPlanCommand({
    controlApiUrl: CONTROL_URL,
    command: { action: "submit_review", plan_version_id: PLAN_VERSION_ID },
    expectedRevision: 1,
    idempotencyKey: PLATFORM_KEY,
    stepUpGrant: STEP_UP,
  });
  await listPlatformCommercialSubscriptions({
    controlApiUrl: CONTROL_URL,
    filters: { projection_state: "current" },
    limit: 50,
  });
  await decidePlatformCommercialDispute({
    controlApiUrl: CONTROL_URL,
    disputeId: DISPUTE_ID,
    command: { action: "start_review", expected_source_revision: 3 },
    expectedRevision: 1,
    idempotencyKey: PLATFORM_KEY,
    stepUpGrant: STEP_UP,
  });
  await applyPlatformProviderReferenceRateCommand({
    controlApiUrl: CONTROL_URL,
    providerId: PROVIDER_ID,
    command: { action: "retire", provider_reference_rate_release_id: REFERENCE_ID },
    expectedRevision: 1,
    idempotencyKey: PLATFORM_KEY,
    stepUpGrant: STEP_UP,
  });
  await getWorkspaceCommercialSubscription({ controlApiUrl: CONTROL_URL });
  await createWorkspaceCommercialDispute({
    controlApiUrl: CONTROL_URL,
    command: {
      source_kind: "invoice",
      source_id: INVOICE_ID,
      reason_code: "amount_mismatch",
    },
    expectedSourceRevision: 3,
    idempotencyKey: WORKSPACE_KEY,
    stepUpGrant: STEP_UP,
  });
  await listWorkspaceProviderCostRates({
    controlApiUrl: CONTROL_URL,
    filters: { provider_id: PROVIDER_ID, currency: "CNY" },
    cursor: "rates.cursor",
    limit: 20,
  });
  await applyWorkspaceProviderCostRateCommand({
    controlApiUrl: CONTROL_URL,
    command: { action: "disable", workspace_provider_cost_rate_id: RATE_ID },
    expectedHeadRevision: 2,
    idempotencyKey: WORKSPACE_KEY,
    stepUpGrant: STEP_UP,
  });

  assert.equal(calls.length, 9);
  for (const { url, options } of calls) {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
    assert.doesNotMatch(url, /tenant_id|workspace_id|provider_key|secret|token/iu);
  }
  const writes = calls.filter(({ options }) => (options.method ?? "GET") !== "GET");
  assert.deepEqual(writes.map(({ options }) => options.method), [
    "POST", "POST", "POST", "POST", "PATCH",
  ]);
  for (const call of writes.slice(0, 3)) {
    assert.equal(call.options.headers["X-CSRF-Token"], PLATFORM_CSRF);
    assert.equal(call.options.headers["Idempotency-Key"], PLATFORM_KEY);
    assert.equal(call.options.headers["X-Step-Up-Grant"], STEP_UP);
  }
  for (const call of writes.slice(3)) {
    assert.equal(call.options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
    assert.equal(call.options.headers["Idempotency-Key"], WORKSPACE_KEY);
    assert.equal(call.options.headers["X-Step-Up-Grant"], STEP_UP);
  }
  assert.equal(writes[0].options.headers["If-Match"], '"1"');
  assert.equal(writes[3].options.headers["If-Match"], '"3"');
  assert.equal(writes[4].options.headers["If-Match"], '"2"');
  assert.deepEqual(JSON.parse(writes[4].options.body), {
    action: "disable",
    workspace_provider_cost_rate_id: RATE_ID,
  });
});

test("strict commercial DTOs reject unknown owner fields and invalid commands before fetch", async (t) => {
  installDocument(t);
  let calls = 0;
  installFetch(t, () => {
    calls += 1;
    return jsonResponse({ ...platformPlans(), tenant_id: "forbidden" });
  });
  await assert.rejects(
    listPlatformCommercialPlans({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.status === 502
      && error.code === "platform.commercial_accounting_response_invalid",
  );
  globalThis.fetch = async () => {
    calls += 1;
    return jsonResponse({ ...workspaceRates(), workspace_id: WORKSPACE_ID });
  };
  await assert.rejects(
    listWorkspaceProviderCostRates({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof AuthApiError
      && error.status === 502
      && error.code === "workspace.commercial_accounting_response_invalid",
  );
  const beforeInvalid = calls;
  await assert.rejects(
    listPlatformCommercialSubscriptions({
      controlApiUrl: CONTROL_URL,
      filters: { tenant_id: "forbidden" },
    }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  await assert.rejects(
    applyWorkspaceProviderCostRateCommand({
      controlApiUrl: CONTROL_URL,
      command: {
        action: "disable",
        workspace_provider_cost_rate_id: RATE_ID,
        tenant_id: "forbidden",
      },
      expectedHeadRevision: 2,
      idempotencyKey: WORKSPACE_KEY,
      stepUpGrant: STEP_UP,
    }),
    (error) => error instanceof AuthApiError && error.code === "commercial.input_invalid",
  );
  assert.equal(calls, beforeInvalid);
});

test("Platform and Workspace stores use independent owners and discard all facts on leave", () => {
  const platform = createPlatformCommercialAccountingStore();
  const workspace = createWorkspaceCommercialAccountingStore();
  assert.equal(platform.enter(platformContext(), [{ id: "commercial-accounting" }]), true);
  assert.equal(workspace.enter({
    authContext: workspaceAuthContext(),
    workspaceSession: workspaceSession(),
    activeNavigationItemId: "internal-finops",
  }), true);
  const platformTicket = platform.begin("plans");
  const workspaceTicket = workspace.begin("rates");
  platform.setIntent("plan", { fingerprint: "platform", idempotencyKey: PLATFORM_KEY });
  workspace.setIntent("rate", { fingerprint: "workspace", idempotencyKey: WORKSPACE_KEY });
  workspace.setDraft("rate", { provider_id: PROVIDER_ID });
  assert.equal(platform.accept(platformTicket, { plans: platformPlans() }), true);
  assert.equal(workspace.accept(workspaceTicket, { rates: workspaceRates() }), true);
  const late = workspace.begin("write-rate");
  workspace.leave();
  assert.equal(late.controller.signal.aborted, true);
  assert.equal(workspace.accept(late, { rates: workspaceRates() }), false);
  assert.equal(workspace.snapshot().rates, null);
  assert.equal(workspace.snapshot().intents.rate, null);
  assert.deepEqual(workspace.snapshot().drafts.rate, {});
  assert.equal(platform.snapshot().plans.plans[0].plan_code, "enterprise");
});

test("controllers discard late responses across Platform leave and Workspace navigation switch", async () => {
  const platformLate = deferred();
  const platform = createPlatformCommercialAccountingController({
    api: {
      commercialPlans: () => platformLate.promise,
      commercialSubscriptions: async () => platformSubscriptions(),
    },
  });
  platform.enter(platformContext(), [{ id: "commercial-accounting" }]);
  const platformPending = platform.loadPlans();
  platform.leave();
  platformLate.resolve(platformPlans());
  assert.equal(await platformPending, false);
  assert.equal(platform.snapshot().plans, null);

  const workspaceLate = deferred();
  const firstReadStarted = deferred();
  const workspace = createWorkspaceCommercialAccountingController({
    api: {
      workspaceSession: async () => workspaceSession(),
      workspaceSubscription: () => {
        firstReadStarted.resolve();
        return workspaceLate.promise;
      },
      providerCostRates: async () => workspaceRates(),
    },
  });
  const first = workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "workspace-subscription",
  });
  await firstReadStarted.promise;
  const second = workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "internal-finops",
  });
  assert.equal(await second, true);
  workspaceLate.resolve(workspaceSubscription());
  assert.equal(await first, false);
  assert.equal(workspace.snapshot().navigation, "internal-finops");
  assert.equal(workspace.snapshot().subscription, null);
  assert.equal(workspace.snapshot().rates.rates[0].provider_id, PROVIDER_ID);
});

test("failed intents reuse idempotency keys while step-up grants remain one-shot", async () => {
  const platformKeys = [];
  const platformGrants = [];
  const platformStepUps = [];
  let platformCommandCount = 0;
  let platformKeyCount = 0;
  const platform = createPlatformCommercialAccountingController({
    newIdempotencyKey: () => `commercial-platform-144-${++platformKeyCount}-0000`,
    api: {
      commercialPlans: async () => platformPlans(),
      commercialSubscriptions: async () => platformSubscriptions(),
      createIdentityStepUpGrant: async (input) => {
        platformStepUps.push(input);
        return { grant: String(++platformCommandCount).repeat(43) };
      },
      commandCommercialPlan: async ({ idempotencyKey, stepUpGrant }) => {
        platformKeys.push(idempotencyKey);
        platformGrants.push(stepUpGrant);
        if (platformKeys.length === 1) {
          throw new PlatformApiError({
            status: 503,
            code: "platform.dependency_unavailable",
          });
        }
        return platformPlanCommand();
      },
    },
  });
  platform.enter(platformContext([
    "platform.commercial.plan.read",
    "platform.commercial.subscription.read",
    "platform.commercial.plan.manage",
  ]), [{ id: "commercial-accounting" }]);
  const planInput = {
    command: { action: "submit_review", plan_version_id: PLAN_VERSION_ID },
    expectedRevision: 2,
    resourceId: PLAN_RELEASE_ID,
  };
  assert.equal(await platform.applyPlan(planInput, {
    method: "totp", verificationCode: "123456",
  }), false);
  assert.equal(await platform.applyPlan(planInput, {
    method: "totp", verificationCode: "123456",
  }), true);
  assert.deepEqual(platformKeys, [platformKeys[0], platformKeys[0]]);
  assert.notEqual(platformGrants[0], platformGrants[1]);
  assert.equal(platformKeyCount, 1);
  assert.deepEqual(platformStepUps.map(({ actionCode, resourceType, resourceId }) => ({
    actionCode, resourceType, resourceId,
  })), [
    {
      actionCode: "commercial_plan.submit_review",
      resourceType: "platform_plan_catalog_release",
      resourceId: PLAN_RELEASE_ID,
    },
    {
      actionCode: "commercial_plan.submit_review",
      resourceType: "platform_plan_catalog_release",
      resourceId: PLAN_RELEASE_ID,
    },
  ]);

  const workspaceKeys = [];
  const workspaceGrants = [];
  const workspaceStepUps = [];
  let workspaceCommandCount = 0;
  let workspaceKeyCount = 0;
  const workspace = createWorkspaceCommercialAccountingController({
    newIdempotencyKey: () => `commercial-workspace-144-${++workspaceKeyCount}-0000`,
    api: {
      workspaceSession: async () => workspaceSession(),
      providerCostRates: async () => workspaceRates(),
      createIdentityStepUpGrant: async (input) => {
        workspaceStepUps.push(input);
        return { grant: String(++workspaceCommandCount + 4).repeat(43) };
      },
      commandProviderCostRate: async ({ idempotencyKey, stepUpGrant }) => {
        workspaceKeys.push(idempotencyKey);
        workspaceGrants.push(stepUpGrant);
        if (workspaceKeys.length === 1) {
          throw new AuthApiError({
            status: 503,
            code: "workspace.dependency_unavailable",
          });
        }
        return rateCommand();
      },
    },
  });
  await workspace.open({
    authContext: workspaceAuthContext(),
    activeNavigationItemId: "internal-finops",
  });
  const rateInput = {
    command: { action: "disable", workspace_provider_cost_rate_id: RATE_ID },
    expectedHeadRevision: 2,
    resourceId: RATE_ID,
  };
  assert.equal(await workspace.applyRate(rateInput, {
    method: "totp", verificationCode: "654321",
  }), false);
  assert.equal(await workspace.applyRate(rateInput, {
    method: "totp", verificationCode: "654321",
  }), true);
  assert.deepEqual(workspaceKeys, [workspaceKeys[0], workspaceKeys[0]]);
  assert.notEqual(workspaceGrants[0], workspaceGrants[1]);
  assert.equal(workspaceKeyCount, 1);
  assert.deepEqual(workspaceStepUps.map(({
    workspaceId, actionCode, resourceType, resourceId,
  }) => ({ workspaceId, actionCode, resourceType, resourceId })), [
    {
      workspaceId: WORKSPACE_ID,
      actionCode: "provider_cost_rate.disable",
      resourceType: "workspace_provider_cost_rate",
      resourceId: RATE_ID,
    },
    {
      workspaceId: WORKSPACE_ID,
      actionCode: "provider_cost_rate.disable",
      resourceType: "workspace_provider_cost_rate",
      resourceId: RATE_ID,
    },
  ]);
  assert.doesNotMatch(
    JSON.stringify({ platform: platform.snapshot(), workspace: workspace.snapshot() }),
    /123456|654321|commercial-platform-144|commercial-workspace-144/u,
  );
});

test("renderers escape dynamic facts and preserve Platform versus Workspace boundaries", () => {
  const unsafe = "unsafe<script>alert(1)</script>";
  const platformHtml = renderPlatformCommercialAccounting({
    phase: "ready",
    loading: [],
    planFilters: {},
    subscriptionFilters: {},
    planHistory: [],
    subscriptionHistory: [],
    plans: platformPlans({
      plans: [plan({ display_name: unsafe })],
      reference_rates: [referenceRate({ source_version: unsafe })],
    }),
    subscriptions: platformSubscriptions({
      subscriptions: [subscription({
        display_name: unsafe,
        invoices: [invoice({ invoice_number_masked: unsafe })],
        disputes: [dispute({ source_ref: unsafe })],
      })],
    }),
  }, new Set([
    "platform.commercial.plan.read",
    "platform.commercial.subscription.read",
  ]));
  assert.match(platformHtml, /unsafe&lt;script&gt;alert\(1\)&lt;\/script&gt;/u);
  assert.doesNotMatch(platformHtml, /<script>|tenant_id|workspace_id|provider_key/iu);
  assert.doesNotMatch(
    platformHtml,
    /permission_fingerprint|entitlements_sha256|contract_sha256|source_sha256|artifact_manifest_sha256/u,
  );

  const workspaceState = {
    ...createWorkspaceCommercialAccountingStore().snapshot(),
    phase: "ready",
    canSubmitDispute: true,
    subscription: workspaceSubscription({
      subscription: subscription({
        display_name: unsafe,
        invoices: [invoice({ invoice_number_masked: unsafe })],
        disputes: [dispute({ source_ref: unsafe })],
      }),
    }),
  };
  const workspaceHtml = renderWorkspaceSubscription(workspaceState);
  assert.match(workspaceHtml, /unsafe&lt;script&gt;alert\(1\)&lt;\/script&gt;/u);
  assert.doesNotMatch(workspaceHtml, /<script>|workspace_customer_ref|plan_version_id/iu);
  assert.doesNotMatch(
    workspaceHtml,
    /platform_plan_catalog_release_id|permission_fingerprint|entitlement_sha256|source_sha256/u,
  );

  const finOpsHtml = renderWorkspaceInternalFinOps({
    ...createWorkspaceCommercialAccountingStore().snapshot(),
    phase: "ready",
    canManageRates: true,
    rates: workspaceRates({ rates: [rate({ provider_name: unsafe })] }),
  }, [{ id: "usage" }, { id: "budgets" }, { id: "private-admin", visible: false }]);
  assert.match(finOpsHtml, /unsafe&lt;script&gt;alert\(1\)&lt;\/script&gt;/u);
  assert.match(finOpsHtml, /href="#usage"/u);
  assert.match(finOpsHtml, /href="#budgets"/u);
  assert.doesNotMatch(finOpsHtml, /private-admin|content_sha256|permission_fingerprint/u);
});

test("commercial accounting routes, lifecycle cleanup, and responsive runtime wiring are active", async () => {
  const [
    entry, developerRenderer, platformMain, platformState, platformRenderer,
    platformClient, workspaceClient, consoleService, css,
  ] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL(
      "../../src/features/developer-start/renderDeveloperStart.js", import.meta.url,
    ), "utf8"),
    readFile(new URL("../../src/features/platform/main.js", import.meta.url), "utf8"),
    readFile(new URL(
      "../../src/features/platform/platformPageState.js", import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../src/features/platform/renderPlatformPage.js", import.meta.url,
    ), "utf8"),
    readFile(new URL("../../src/lib/platformCommercialAccounting.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/workspaceCommercialAccounting.js", import.meta.url), "utf8"),
    readFile(new URL(
      "../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url,
    ), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);
  assert.match(
    entry,
    /WORKSPACE_COMMERCIAL_NAVIGATION_IDS[\s\S]*workspace-subscription[\s\S]*internal-finops/u,
  );
  assert.match(
    entry,
    /case "workspace-subscription":[\s\S]*case "internal-finops":[\s\S]*workspaceCommercialAccountingController\.open/u,
  );
  assert.match(
    entry,
    /function takeWorkspaceCommercialStepUp[\s\S]*node\.value = ""[\s\S]*verificationCode = ""/u,
  );
  assert.match(
    developerRenderer,
    /case "workspace-subscription"[\s\S]*renderWorkspaceSubscription/u,
  );
  assert.match(
    developerRenderer,
    /case "internal-finops"[\s\S]*renderWorkspaceInternalFinOps/u,
  );
  assert.match(
    platformMain,
    /commercialPlans:[\s\S]*commandCommercialPlan:[\s\S]*commercialSubscriptions:/u,
  );
  assert.match(platformState, /createPlatformCommercialAccountingController/u);
  assert.match(
    platformState,
    /takeCommercialAccountingStepUp[\s\S]*node\.value = ""/u,
  );
  assert.match(platformRenderer, /state\.view === "commercial-accounting"/u);
  assert.match(
    consoleService,
    /"workspace-subscription"[\s\S]*workspace\.commercial\.subscription\.read/u,
  );
  assert.match(
    consoleService,
    /"internal-finops"[\s\S]*workspace\.provider_cost\.read/u,
  );
  assert.match(css, /\.workspace-commercial-accounting[\s\S]*@media \(max-width: 720px\)/u);
  assert.match(css, /\.platform-commercial-accounting/u);
  assert.doesNotMatch(
    [platformClient, workspaceClient].join("\n"),
    /localStorage|sessionStorage|console\.log/u,
  );
});

function plan(overrides = {}) {
  return {
    platform_plan_catalog_release_id: PLAN_RELEASE_ID,
    plan_catalog_id: PLAN_ID,
    plan_version_id: PLAN_VERSION_ID,
    plan_code: "enterprise",
    display_name: "Enterprise",
    billing_period: "monthly",
    currency: "CNY",
    unit_price_minor: 128_000,
    entitlements_sha256: "1".repeat(64),
    contract_sha256: "2".repeat(64),
    status: "candidate",
    revision: 2,
    release_generation: 0,
    head_revision: 2,
    is_head: true,
    readiness: "candidate",
    actions: ["submit_review"],
    created_at: NOW,
    updated_at: NOW,
    released_at: null,
    ...overrides,
  };
}

function referenceRate(overrides = {}) {
  return {
    provider_reference_rate_release_id: REFERENCE_ID,
    provider_id: PROVIDER_ID,
    model_id: MODEL_ID,
    currency: "CNY",
    input_rate_micros_per_1k: 15_000,
    output_rate_micros_per_1k: 30_000,
    effective_from: NOW,
    effective_until: LATER,
    source_version: "provider-price-2026-09",
    content_sha256: "3".repeat(64),
    status: "released",
    revision: 2,
    release_generation: 1,
    head_revision: 2,
    is_head: true,
    actions: ["retire"],
    created_at: NOW,
    updated_at: NOW,
    released_at: NOW,
    ...overrides,
  };
}

function platformPlans(overrides = {}) {
  return {
    plans: [plan()],
    reference_rates: [referenceRate()],
    next_cursor: "plans.next",
    limit: 50,
    permission_fingerprint: HASH,
    request_id: "req_platform_commercial_plans",
    trace_id: "trc_platform_commercial_plans",
    ...overrides,
  };
}

function invoice(overrides = {}) {
  return {
    platform_invoice_id: INVOICE_ID,
    currency: "CNY",
    subtotal_minor: 100_000,
    tax_rate_basis_points: 600,
    tax_minor: 6_000,
    total_minor: 106_000,
    status: "issued",
    invoice_number_masked: "INV-2026-***-144",
    artifact_manifest_sha256: "4".repeat(64),
    source_revision: 3,
    generation: 1,
    updated_at: NOW,
    ...overrides,
  };
}

function dispute(overrides = {}) {
  return {
    billing_dispute_case_id: DISPUTE_ID,
    source_kind: "invoice",
    source_ref: INVOICE_ID,
    currency: "CNY",
    disputed_amount_minor: 6_000,
    reason_code: "amount_mismatch",
    status: "submitted",
    source_revision: 3,
    state_revision: 1,
    decision_code: null,
    credited_amount_minor: 0,
    actions: [],
    created_at: NOW,
    updated_at: NOW,
    resolved_at: null,
    ...overrides,
  };
}

function subscription(overrides = {}) {
  return {
    workspace_customer_ref: WORKSPACE_REF,
    subscription_id: SUBSCRIPTION_ID,
    plan_catalog_id: PLAN_ID,
    plan_version_id: PLAN_VERSION_ID,
    platform_plan_catalog_release_id: PLAN_RELEASE_ID,
    plan_code: "enterprise",
    display_name: "Enterprise",
    subscription_status: "active",
    current_period_started_at: NOW,
    current_period_ends_at: LATER,
    entitlement_sha256: "5".repeat(64),
    currency: "CNY",
    current_due_minor: 128_000,
    paid_minor: 128_000,
    refunded_minor: 0,
    invoiced_minor: 106_000,
    order_status: "paid",
    payment_status: "succeeded",
    invoice_status: "issued",
    dispute_status: "submitted",
    projection_state: "current",
    source_watermark: 12,
    source_sha256: "6".repeat(64),
    revision: 3,
    projection_generation: 1,
    invoices: [invoice()],
    disputes: [dispute()],
    actions: [],
    projected_at: NOW,
    ...overrides,
  };
}

function platformSubscriptions(overrides = {}) {
  return {
    subscriptions: [subscription()],
    next_cursor: "subscriptions.next",
    limit: 50,
    permission_fingerprint: HASH,
    request_id: "req_platform_commercial_subscriptions",
    trace_id: "trc_platform_commercial_subscriptions",
    ...overrides,
  };
}

function workspaceSubscription(overrides = {}) {
  return {
    subscription: subscription(),
    permission_revision: 9,
    permission_fingerprint: HASH,
    request_id: "req_workspace_commercial_subscription",
    trace_id: "trc_workspace_commercial_subscription",
    ...overrides,
  };
}

function rate(overrides = {}) {
  return {
    workspace_provider_cost_rate_id: RATE_ID,
    workspace_provider_cost_rate_version_id: RATE_VERSION_ID,
    provider_id: PROVIDER_ID,
    provider_code: "provider-144",
    provider_name: "Provider 144",
    model_id: MODEL_ID,
    model_alias: "Model 144",
    currency: "CNY",
    version_number: 2,
    input_rate_micros_per_1k: 12_000,
    output_rate_micros_per_1k: 24_000,
    effective_from: NOW,
    effective_until: LATER,
    source_type: "manual",
    source_ref: "workspace-rate-144",
    status: "active",
    revision: 2,
    head_revision: 2,
    adoption_state: "active",
    content_sha256: "7".repeat(64),
    actions: ["create_version", "adopt_reference", "disable"],
    created_at: NOW,
    ...overrides,
  };
}

function workspaceRates(overrides = {}) {
  return {
    rates: [rate()],
    next_cursor: "rates.next",
    limit: 50,
    permission_revision: 9,
    permission_fingerprint: HASH,
    request_id: "req_workspace_provider_cost_rates",
    trace_id: "trc_workspace_provider_cost_rates",
    ...overrides,
  };
}

function platformPlanCommand(overrides = {}) {
  return {
    plan: plan({ status: "review_required", readiness: "review" }),
    command_id: "platform-commercial-plan-command-144",
    idempotent_replay: false,
    permission_fingerprint: HASH,
    request_id: "req_platform_commercial_plan_command",
    trace_id: "trc_platform_commercial_plan_command",
    ...overrides,
  };
}

function referenceCommand(overrides = {}) {
  return {
    rate: referenceRate({ status: "retired", actions: [] }),
    command_id: "platform-reference-rate-command-144",
    idempotent_replay: false,
    permission_fingerprint: HASH,
    request_id: "req_platform_reference_rate_command",
    trace_id: "trc_platform_reference_rate_command",
    ...overrides,
  };
}

function disputeCommand(disputeOverrides = {}, overrides = {}) {
  return {
    dispute: dispute(disputeOverrides),
    command_id: "commercial-dispute-command-144",
    adjustment_id: null,
    idempotent_replay: false,
    permission_revision: 9,
    permission_fingerprint: HASH,
    request_id: "req_commercial_dispute_command",
    trace_id: "trc_commercial_dispute_command",
    ...overrides,
  };
}

function rateCommand(overrides = {}) {
  return {
    rate: rate({ status: "disabled", adoption_state: "disabled", actions: [] }),
    command_id: "workspace-provider-cost-rate-command-144",
    idempotent_replay: false,
    permission_revision: 9,
    permission_fingerprint: HASH,
    request_id: "req_workspace_provider_cost_rate_command",
    trace_id: "trc_workspace_provider_cost_rate_command",
    ...overrides,
  };
}

function platformContext(permissions = [
  "platform.commercial.plan.read",
  "platform.commercial.subscription.read",
]) {
  return {
    operator: {
      operator_id: OPERATOR_ID,
      revision: 7,
      roles: ["platform_finance_operator"],
    },
    permissions,
    expires_at: "2026-09-03T12:00:00.000Z",
  };
}

function workspaceAuthContext(permissions = [
  PERMISSIONS.WORKSPACE_COMMERCIAL_SUBSCRIPTION_READ,
  PERMISSIONS.WORKSPACE_COMMERCIAL_DISPUTE_SUBMIT,
  PERMISSIONS.WORKSPACE_PROVIDER_COST_READ,
  PERMISSIONS.WORKSPACE_PROVIDER_COST_MANAGE,
  PERMISSIONS.WORKSPACE_INTERNAL_FINOPS_READ,
]) {
  return withPermissionSnapshot({
    user: {
      tenant_id: "tenant_prd144",
      user_id: "usr_prd144",
      email: "finance@example.invalid",
      role: "finance_admin",
    },
    session: { session_id: "ses_prd144", session_revision: 3 },
  }, permissions, {
    permissionRevision: 8,
    authorizationFingerprint: "workspace-commercial-accounting-fingerprint",
  });
}

function workspaceSession() {
  return {
    context: {
      workspace_id: WORKSPACE_ID,
      role: "finance_admin",
      session_status: "active",
      expires_at: "2026-09-03T12:00:00.000Z",
    },
  };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
      "X-Request-ID": payload.request_id ?? "req_prd144",
      "X-Trace-ID": payload.trace_id ?? "trc_prd144",
    },
  });
}

function installDocument(t) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      cookie: `__Host-metahub-platform-csrf=${PLATFORM_CSRF}; __Host-metahub-workspace-csrf=${WORKSPACE_CSRF}`,
    },
  });
  t.after(() => {
    if (descriptor) Object.defineProperty(globalThis, "document", descriptor);
    else delete globalThis.document;
  });
}

function installFetch(t, responder) {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(String(url), options);
  };
  t.after(() => { globalThis.fetch = previous; });
  return calls;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}
