import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformCustomerSuccessController } from "../../src/features/platform-customer-success/platformCustomerSuccessController.js";
import { createPlatformCustomerSuccessStore } from "../../src/features/platform-customer-success/platformCustomerSuccessStore.js";
import { renderPlatformCustomerSuccess } from "../../src/features/platform-customer-success/renderPlatformCustomerSuccess.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  applyPlatformCustomerSuccessPlan,
  applyPlatformCustomerTask,
  getPlatformCustomerHealth,
  listPlatformCustomerAccounts,
  listPlatformRenewalPipeline,
} from "../../src/lib/platformCustomerSuccess.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const ACCOUNT_ID = `csa_${"1".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"2".repeat(32)}`;
const OPERATOR_ID = `pto_${"3".repeat(32)}`;
const PLAN_ID = `csp_${"4".repeat(32)}`;
const TASK_ID = `ctk_${"5".repeat(32)}`;
const BATCH_ID = `ctb_${"6".repeat(32)}`;
const RENEWAL_ID = `rno_${"7".repeat(32)}`;
const QBR_ID = `qbr_${"8".repeat(32)}`;
const CSRF = "C".repeat(43);
const STEP_UP = "S".repeat(43);
const IDEMPOTENCY = "customer-success-command-0001";
const NOW = "2026-09-01T08:00:00.000Z";

test("PRD-140 strict client uses five Cookie routes and exact command headers", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === "/platform/customer-accounts") return jsonResponse(directoryResponse());
    if (path === `/platform/customer-accounts/${ACCOUNT_ID}/health`) {
      return jsonResponse(healthResponse());
    }
    if (path === "/platform/renewal-pipeline") return jsonResponse(renewalResponse());
    if (path === "/platform/customer-success-plans") {
      return jsonResponse(planResponse(), 201);
    }
    if (path === "/platform/customer-tasks") return jsonResponse(batchResponse(), 201);
    throw new Error(`unexpected route ${path}`);
  });

  const directory = await listPlatformCustomerAccounts({
    controlApiUrl: CONTROL_URL,
    filters: { region: "cn-north", health_risk: "watch" },
  });
  const health = await getPlatformCustomerHealth({
    controlApiUrl: CONTROL_URL,
    customerAccountId: ACCOUNT_ID,
  });
  const renewals = await listPlatformRenewalPipeline({
    controlApiUrl: CONTROL_URL,
    filters: { customer_account_id: ACCOUNT_ID },
  });
  const plan = await applyPlatformCustomerSuccessPlan({
    controlApiUrl: CONTROL_URL,
    idempotencyKey: IDEMPOTENCY,
    command: createPlanCommand(),
  });
  const batch = await applyPlatformCustomerTask({
    controlApiUrl: CONTROL_URL,
    idempotencyKey: IDEMPOTENCY,
    stepUpGrant: STEP_UP,
    command: {
      operation: "approve_batch",
      customer_task_batch_id: BATCH_ID,
      decision: "approved",
      reason_code: "owner.approved",
      expected_revision: 1,
      confirmed: true,
    },
  });

  assert.equal(directory.items[0].customer_account_id, ACCOUNT_ID);
  assert.equal(health.factors.length, 6);
  assert.equal(renewals.items[0].renewal_opportunity_id, RENEWAL_ID);
  assert.equal(plan.plan.customer_success_plan_id, PLAN_ID);
  assert.equal(batch.batch.customer_task_batch_id, BATCH_ID);
  assert.deepEqual([...new Set(calls.map(({ url }) => new URL(url).pathname))], [
    "/platform/customer-accounts",
    `/platform/customer-accounts/${ACCOUNT_ID}/health`,
    "/platform/renewal-pipeline",
    "/platform/customer-success-plans",
    "/platform/customer-tasks",
  ]);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
  });
  const posts = calls.filter(({ options }) => options.method === "POST");
  posts.forEach(({ options }) => {
    assert.equal(options.headers["X-CSRF-Token"], CSRF);
    assert.equal(options.headers["Idempotency-Key"], IDEMPOTENCY);
  });
  assert.equal(posts[0].options.headers["If-Match"], '"0"');
  assert.equal(posts[1].options.headers["If-Match"], '"1"');
  assert.equal(posts[1].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(new URL(calls[0].url).searchParams.get("region"), "cn-north");
});

test("PRD-140 client rejects response extensions and invalid request shapes before fetch", async (t) => {
  installDocument(t);
  let fetches = 0;
  installFetch(t, () => {
    fetches += 1;
    return jsonResponse({ ...directoryResponse(), tenant_id: "forbidden" });
  });
  await assert.rejects(
    listPlatformCustomerAccounts({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.code === "platform.customer_success_response_invalid",
  );
  assert.equal(fetches, 1);
  await assert.rejects(
    listPlatformCustomerAccounts({
      controlApiUrl: CONTROL_URL,
      filters: { owner_override: "hidden" },
    }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  await assert.rejects(
    applyPlatformCustomerSuccessPlan({
      controlApiUrl: CONTROL_URL,
      idempotencyKey: IDEMPOTENCY,
      command: { ...createPlanCommand(), notes: "forbidden" },
    }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  assert.equal(fetches, 1);
});

test("PRD-140 store aborts old owner lanes and rejects late facts", () => {
  const snapshots = [];
  const store = createPlatformCustomerSuccessStore({ onChange: (value) => snapshots.push(value) });
  const contextA = platformContext();
  const navigation = [{ id: "customer-success" }];
  assert.equal(store.enter(contextA, navigation), true);
  const old = store.begin("directory");
  const contextB = platformContext({ revision: 2 });
  assert.equal(store.enter(contextB, navigation), true);
  assert.equal(old.controller.signal.aborted, true);
  assert.equal(store.accept(old, { directory: directoryResponse() }), false);
  assert.equal(store.snapshot().directory, null);
  const current = store.begin("directory");
  assert.equal(store.accept(current, { directory: directoryResponse() }), true);
  assert.equal(store.snapshot().phase, "ready");
  store.leave();
  assert.equal(store.snapshot().directory, null);
  assert.equal(store.snapshot().contacts, null);
  assert.ok(snapshots.length >= 4);
});

test("PRD-140 controller keeps contacts explicit, drops late contact data, and reuses retry key", async () => {
  const contactDeferred = deferred();
  const planKeys = [];
  let planAttempts = 0;
  const api = {
    customerAccounts: async ({ filters, signal }) => {
      if (filters.view === "contact") {
        signal.addEventListener("abort", () => contactDeferred.reject(abortError()), { once: true });
        return contactDeferred.promise;
      }
      if (filters.view === "detail") return detailResponse();
      return directoryResponse();
    },
    customerHealth: async () => healthResponse(),
    customerRenewals: async () => renewalResponse(),
    customerPlan: async ({ idempotencyKey }) => {
      planKeys.push(idempotencyKey);
      planAttempts += 1;
      if (planAttempts === 1) throw new PlatformApiError({ status: 503, code: "platform.unavailable" });
      return planResponse();
    },
  };
  const controller = createPlatformCustomerSuccessController({
    api,
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(controller.enter(platformContext(), [{ id: "customer-success" }]), true);
  assert.equal(await controller.loadDirectory(), true);
  assert.equal(controller.snapshot().contacts, null);
  const pendingContact = controller.loadContacts();
  controller.leave();
  assert.equal(await pendingContact, false);
  assert.equal(controller.snapshot().contacts, null);

  controller.enter(platformContext(), [{ id: "customer-success" }]);
  await controller.loadDirectory();
  assert.equal(await controller.applyPlan(createPlanCommand()), false);
  assert.equal(controller.snapshot().phase, "dependency_unavailable");
  assert.equal(await controller.applyPlan(createPlanCommand()), true);
  assert.deepEqual(planKeys, [IDEMPOTENCY, IDEMPOTENCY]);
});

test("PRD-140 renderer exposes dense tabs and never leaks hidden contacts", () => {
  const permissions = new Set([
    "platform.customer_success.read",
    "platform.customer_success.contact.read",
    "platform.customer_success.plan.manage",
    "platform.customer_success.task.manage",
    "platform.customer_success.renewal.read",
  ]);
  const state = workbenchState();
  const html = renderPlatformCustomerSuccess(state, permissions, platformContext());
  assert.match(html, /data-platform-customer-success-root/u);
  assert.match(html, /Health[\s\S]*Plan[\s\S]*Task[\s\S]*Renewal[\s\S]*QBR/u);
  assert.match(html, /data-platform-customer-plan-create-form/u);
  assert.doesNotMatch(html, /owner@example\.invalid|13800138000/u);
  const taskHtml = renderPlatformCustomerSuccess(
    { ...state, tab: "tasks" },
    permissions,
    platformContext(),
  );
  assert.match(taskHtml, /data-platform-customer-task-create-form/u);
  assert.match(taskHtml, /data-platform-customer-batch-prepare-form/u);
  const contacts = renderPlatformCustomerSuccess({
    ...state,
    contactsVisible: true,
    contacts: [contactSummary()],
  }, permissions, platformContext());
  assert.match(contacts, /owner@example\.invalid/u);
  const support = renderPlatformCustomerSuccess({
    ...state,
    directory: directoryResponse({ scope: "support" }),
    detail: null,
    renewals: null,
  }, new Set(["platform.customer_success.support_read"]), platformContext({
    roles: ["platform_support"],
    permissions: ["platform.customer_success.support_read"],
  }));
  assert.doesNotMatch(support, /data-platform-customer-plan-create-form|data-platform-customer-task-create-form/u);
});

test("PRD-140 renewal and QBR tables fit the desktop detail column", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  assert.match(
    css,
    /\.platform-customer-renewal-table,\s*\.platform-customer-qbr-table\s*\{\s*width: 100%; min-width: 0; table-layout: fixed;\s*\}/u,
  );
  assert.doesNotMatch(
    css,
    /\.platform-customer-renewal-table,\s*\.platform-customer-qbr-table\s*\{[^}]*min-width:\s*780px/u,
  );
});

test("PRD-140 active entrypoints contain no legacy customer-health route, import, DOM, or prefetch", async () => {
  const [main, platformState, platformRenderer, consoleService, app] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/app.py", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(main, /customerHealth|customer-health|\/customer-health/u);
  assert.match(platformState, /createPlatformCustomerSuccessController/u);
  assert.match(platformRenderer, /renderPlatformCustomerSuccess/u);
  assert.match(consoleService, /WORKSPACE_NAVIGATION_EXCLUDED_IDS[\s\S]*customer-health/u);
  assert.doesNotMatch(app, /include_router\(customer_health/u);
});

test("PRD-140 Platform shell loads only the dedicated customer-success model", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#customer-success" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  const reads = [];
  const api = {
    session: async () => ({ context: platformContext() }),
    navigation: async () => ({
      items: [{
        id: "customer-success",
        label: "Customer success",
        href: "/platform.html#customer-success",
      }, {
        id: "overview",
        label: "Overview",
        href: "/platform.html#overview",
      }],
    }),
    customerAccounts: async ({ filters }) => {
      reads.push(filters.view ?? "directory");
      return filters.view === "detail" ? detailResponse() : directoryResponse();
    },
    customerHealth: async () => {
      reads.push("health");
      return healthResponse();
    },
    customerRenewals: async () => {
      reads.push("renewals");
      return renewalResponse();
    },
    workspaces: async () => {
      throw new Error("legacy workspace directory must not be prefetched");
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({ root, api });
  assert.equal(await controller.mount(), true);
  await waitFor(() => controller.snapshot().customerSuccess?.detail !== null);
  assert.deepEqual(reads.sort(), ["detail", "directory", "health", "renewals"]);
  assert.match(root.innerHTML, /data-platform-customer-success-root/u);
  controller.selectView("overview");
  assert.equal(controller.snapshot().customerSuccess.directory, null);
  controller.destroy();
});

function platformContext({
  revision = 1,
  roles = ["platform_customer_success"],
  permissions = [
    "platform.customer_success.read",
    "platform.customer_success.contact.read",
    "platform.customer_success.plan.manage",
    "platform.customer_success.task.manage",
    "platform.customer_success.renewal.read",
  ],
} = {}) {
  return {
    operator: {
      operator_id: OPERATOR_ID,
      display_name: "Customer Success",
      status: "active",
      roles,
      revision,
    },
    permissions,
    expires_at: "2026-09-01T10:00:00.000Z",
  };
}

function directoryResponse({ scope = "customer_success", extra = {} } = {}) {
  return {
    items: [accountSummary(scope)],
    limit: 50,
    has_more: false,
    next_cursor: null,
    detail: null,
    contacts: null,
    request_id: "req_prd140_directory",
    trace_id: "trc_prd140_directory",
    ...extra,
  };
}

function detailResponse() {
  return {
    ...directoryResponse(),
    detail: {
      plans: [planSummary()],
      plans_count: 1,
      plans_truncated: false,
      tasks: [taskSummary()],
      tasks_count: 1,
      tasks_truncated: false,
      qbr_summaries: [qbrSummary()],
      qbr_count: 1,
      qbr_truncated: false,
      operation_count: 0,
      operations_truncated: false,
    },
  };
}

function contactResponse() {
  return { ...directoryResponse(), contacts: [contactSummary()] };
}

function accountSummary(scope) {
  const common = {
    scope,
    customer_account_id: ACCOUNT_ID,
    workspace_id: WORKSPACE_ID,
    projection_updated_at: NOW,
  };
  if (scope === "support") {
    return {
      ...common,
      operational_status: "active",
      health_risk: "watch",
      open_support_ticket_count: 2,
      breached_sla_count: 0,
    };
  }
  return {
    ...common,
    display_name: "Example Workspace",
    slug: "example-workspace",
    kind: "organization",
    region: "cn-north",
    operational_status: "active",
    activation: "activated",
    activation_completed_steps: 8,
    adoption: "high",
    health_risk: "watch",
    renewal_status: "active",
    open_support_ticket_count: 2,
    plan_count: 1,
    open_task_count: 1,
    revision: 2,
  };
}

function healthResponse() {
  return {
    customer_account_id: ACCOUNT_ID,
    scope: "customer_success",
    formula_version: "customer-health-platform-v2",
    window_start: "2026-08-25T00:00:00.000Z",
    window_end: "2026-09-01T00:00:00.000Z",
    comparison_start: "2026-08-18T00:00:00.000Z",
    comparison_end: "2026-08-25T00:00:00.000Z",
    score_bps: 7200,
    risk_level: "watch",
    factors: [
      factor("usage_trend", 7000), factor("reliability", 8000),
      factor("cost_anomaly_health", 7000), factor("budget_readiness", 8000),
      factor("team_activity", 6000), factor("ticket_health", 7500),
    ],
    risk_codes: ["usage.watch"],
    action_codes: ["adoption.follow_up"],
    freshness: "fresh",
    source_sequence: 4,
    evaluated_at: NOW,
    request_id: "req_prd140_health",
    trace_id: "trc_prd140_health",
  };
}

function factor(code, score) {
  return {
    code,
    score_bps: score,
    contribution_bps: 1000,
    current_value: 1,
    comparison_value: 1,
  };
}

function planResponse() {
  return {
    plan: planSummary(),
    idempotent_replay: false,
    request_id: "req_prd140_plan",
    trace_id: "trc_prd140_plan",
  };
}

function planSummary() {
  return {
    customer_success_plan_id: PLAN_ID,
    customer_account_id: ACCOUNT_ID,
    objective_code: "adoption",
    starts_on: "2026-09-01",
    ends_on: "2026-12-01",
    target_health_bps: 8000,
    target_activation_steps: 8,
    target_adoption_bps: 7500,
    source_health_sequence: 4,
    status: "draft",
    revision: 1,
    created_by_operator_id: OPERATOR_ID,
    updated_by_operator_id: OPERATOR_ID,
    created_at: NOW,
    updated_at: NOW,
    completed_at: null,
  };
}

function createPlanCommand() {
  return {
    operation: "create",
    customer_account_id: ACCOUNT_ID,
    expected_revision: 0,
    objective_code: "adoption",
    starts_on: "2026-09-01",
    ends_on: "2026-12-01",
    target_health_bps: 8000,
    target_activation_steps: 8,
    target_adoption_bps: 7500,
    source_health_sequence: 4,
    confirmed: true,
  };
}

function taskSummary() {
  return {
    customer_task_id: TASK_ID,
    customer_account_id: ACCOUNT_ID,
    customer_success_plan_id: PLAN_ID,
    renewal_opportunity_id: null,
    task_type: "adoption_follow_up",
    priority: "high",
    reason_code: "adoption.follow_up",
    assignee_operator_id: OPERATOR_ID,
    due_at: "2026-09-15T00:00:00.000Z",
    required: true,
    status: "open",
    due_generation: 1,
    revision: 1,
    created_at: NOW,
    updated_at: NOW,
    completed_at: null,
  };
}

function batchResponse() {
  return {
    task: null,
    batch: {
      customer_task_batch_id: BATCH_ID,
      prepared_by_operator_id: OPERATOR_ID,
      approved_by_operator_id: `pto_${"9".repeat(32)}`,
      account_count: 1,
      manifest_sha256: "a".repeat(64),
      task_type: "health_review",
      priority: "normal",
      reason_code: "owner.approved",
      assignee_operator_id: OPERATOR_ID,
      due_at: "2026-09-15T00:00:00.000Z",
      status: "applied",
      result_count: 1,
      revision: 2,
      created_at: NOW,
      updated_at: NOW,
    },
    idempotent_replay: false,
    request_id: "req_prd140_batch",
    trace_id: "trc_prd140_batch",
  };
}

function renewalResponse() {
  return {
    items: [{
      scope: "customer_success",
      renewal_opportunity_id: RENEWAL_ID,
      customer_account_id: ACCOUNT_ID,
      workspace_id: WORKSPACE_ID,
      display_name: "Example Workspace",
      renewal_at: "2026-11-30T00:00:00.000Z",
      health_risk_code: "watch",
      adoption_risk_code: "low",
      payment_risk_code: "none",
      invoice_status: "open",
      status: "observing",
      open_plan_count: 1,
      open_task_count: 1,
      revision: 1,
    }],
    limit: 50,
    has_more: false,
    next_cursor: null,
    request_id: "req_prd140_renewal",
    trace_id: "trc_prd140_renewal",
  };
}

function qbrSummary() {
  return {
    qbr_summary_id: QBR_ID,
    quarter: "2026-Q3",
    health_sequence: 4,
    adoption_bps: 7500,
    support_ticket_count: 2,
    renewal_risk_count: 1,
    outcome_code: "watch",
    action_code: "follow_up",
    source_digest: "b".repeat(64),
    generated_at: NOW,
  };
}

function contactSummary() {
  return {
    customer_contact_id: `cct_${"a".repeat(32)}`,
    contact_role: "primary",
    status: "active",
    account_id: `gac_${"b".repeat(32)}`,
    display_name: "Owner Contact",
    email: "owner@example.invalid",
    phone: "13800138000",
    membership_revision: 1,
    notification_preference_revision: 0,
    preference_summary: [{ category: "billing", channel: "email", enabled: true }],
  };
}

function workbenchState() {
  return {
    phase: "ready",
    loading: [],
    filters: {},
    directoryCursor: null,
    directoryHistory: [],
    directory: directoryResponse(),
    selectedAccountId: ACCOUNT_ID,
    tab: "plans",
    detail: detailResponse().detail,
    health: healthResponse(),
    renewals: renewalResponse(),
    contacts: null,
    contactsVisible: false,
    batch: null,
    feedback: "",
    error: null,
    intents: { plan: null, task: null, batch: null },
  };
}

function installDocument(t) {
  const original = globalThis.document;
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${CSRF}` };
  t.after(() => { globalThis.document = original; });
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail("condition did not become true");
}

function installFetch(t, responder) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return responder(String(url), options);
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function abortError() {
  return new DOMException("aborted", "AbortError");
}
