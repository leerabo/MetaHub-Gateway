import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformGrowthController } from "../../src/features/platform-growth/platformGrowthController.js";
import { createPlatformGrowthStore } from "../../src/features/platform-growth/platformGrowthStore.js";
import { renderPlatformGrowth } from "../../src/features/platform-growth/renderPlatformGrowth.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  applyGrowthCampaign,
  applyGrowthExperiment,
  decideGrowthExport,
  downloadGrowthExport,
  getGrowthExport,
  issueGrowthExportReceipt,
  listGrowthAttribution,
  listGrowthCampaigns,
  listGrowthCohorts,
  listGrowthExperiments,
  listGrowthFunnels,
  requestGrowthExport,
} from "../../src/lib/platformGrowth.js";
import { PlatformApiError } from "../../src/lib/platform.js";

const CONTROL_URL = "http://control.local";
const CAMPAIGN_ID = `gca_${"1".repeat(32)}`;
const EXPERIMENT_ID = `gex_${"2".repeat(32)}`;
const EXPORT_ID = `gxp_${"3".repeat(32)}`;
const SOURCE_ID = `acs_${"4".repeat(32)}`;
const OPERATOR_ID = `pto_${"5".repeat(32)}`;
const CHANNEL_ID = `chp_${"6".repeat(32)}`;
const CSRF = "C".repeat(43);
const STEP_UP = "S".repeat(43);
const RECEIPT = "R".repeat(43);
const IDEMPOTENCY = "growth-command-request-0001";
const NOW = "2026-09-01T08:00:00.000Z";

test("PRD-141 strict client covers the Platform Growth routes and exact command headers", async (t) => {
  installDocument(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith("/download")) {
      return new Response("bucket,count\n2026-09-01,20\n", {
        headers: {
          "Content-Type": "text/csv; charset=utf-8",
          "Content-Disposition": 'attachment; filename="growth.csv"',
          "X-Content-Type-Options": "nosniff",
        },
      });
    }
    if (path.endsWith("/receipts")) return jsonResponse(receiptResponse());
    if (path.endsWith("/approvals")) return jsonResponse(exportResponse("approved", 2));
    if (path === "/platform/growth/funnels") return jsonResponse(funnelResponse());
    if (path === "/platform/growth/cohorts") return jsonResponse(cohortResponse());
    if (path === "/platform/growth/campaigns") {
      return jsonResponse(options.method === "POST" ? campaignResponse() : campaignList());
    }
    if (path === "/platform/growth/experiments") {
      return jsonResponse(options.method === "POST" ? experimentResponse() : experimentList());
    }
    if (path === "/platform/growth/attribution") return jsonResponse(attributionResponse());
    if (path === "/platform/growth/exports") return jsonResponse(exportResponse(), 202);
    if (path === `/platform/growth/exports/${EXPORT_ID}`) return jsonResponse(exportResponse());
    throw new Error(`unexpected route ${path}`);
  });

  await listGrowthFunnels({ controlApiUrl: CONTROL_URL, filters: { view: "overview" } });
  await listGrowthCohorts({ controlApiUrl: CONTROL_URL, filters: { age_buckets: "12" } });
  await listGrowthCampaigns({ controlApiUrl: CONTROL_URL });
  await listGrowthExperiments({ controlApiUrl: CONTROL_URL });
  await listGrowthAttribution({ controlApiUrl: CONTROL_URL, filters: { view: "summary" } });
  await applyGrowthCampaign({
    controlApiUrl: CONTROL_URL,
    command: { operation: "pause", campaign_id: CAMPAIGN_ID, expected_revision: 1, confirmed: true },
    idempotencyKey: IDEMPOTENCY,
    stepUpGrant: STEP_UP,
  });
  await applyGrowthExperiment({
    controlApiUrl: CONTROL_URL,
    command: {
      operation: "start",
      experiment_id: EXPERIMENT_ID,
      expected_revision: 1,
      source_watermark: NOW,
      confirmed: true,
    },
    idempotencyKey: IDEMPOTENCY,
    stepUpGrant: STEP_UP,
  });
  await requestGrowthExport({
    controlApiUrl: CONTROL_URL,
    manifest: exportManifest(),
    idempotencyKey: IDEMPOTENCY,
    stepUpGrant: STEP_UP,
  });
  await getGrowthExport({ controlApiUrl: CONTROL_URL, exportId: EXPORT_ID });
  await decideGrowthExport({
    controlApiUrl: CONTROL_URL,
    exportId: EXPORT_ID,
    command: { operation: "approve", expected_revision: 1, reason_code: "policy_approved", confirmed: true },
    idempotencyKey: IDEMPOTENCY,
    stepUpGrant: STEP_UP,
  });
  const receipt = await issueGrowthExportReceipt({
    controlApiUrl: CONTROL_URL,
    exportId: EXPORT_ID,
    stepUpGrant: STEP_UP,
  });
  const file = await downloadGrowthExport({
    controlApiUrl: CONTROL_URL,
    exportId: EXPORT_ID,
    receipt: receipt.receipt,
  });

  assert.equal(file.filename, "growth.csv");
  assert.equal(calls.length, 12);
  calls.forEach(({ options }) => {
    assert.equal(options.credentials, "include");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, undefined);
  });
  const posts = calls.filter(({ options }) => options.method === "POST");
  posts.forEach(({ options }) => assert.equal(options.headers["X-CSRF-Token"], CSRF));
  assert.equal(posts[0].options.headers["If-Match"], '"1"');
  assert.equal(posts[1].options.headers["If-Match"], '"1"');
  assert.equal(posts[2].options.headers["If-Match"], '"0"');
  assert.equal(posts[3].options.headers["If-Match"], '"1"');
  posts.slice(0, 4).forEach(({ options }) => {
    assert.equal(options.headers["Idempotency-Key"], IDEMPOTENCY);
    assert.equal(options.headers["X-Step-Up-Grant"], STEP_UP);
  });
  assert.equal(posts[4].options.headers["Idempotency-Key"], undefined);
  const download = calls.at(-1);
  assert.equal(download.options.headers["X-Growth-Export-Receipt"], RECEIPT);
  assert.equal(new URL(download.url).search, "");
});

test("PRD-141 client rejects response extensions and invalid request shapes before fetch", async (t) => {
  installDocument(t);
  let fetches = 0;
  installFetch(t, () => {
    fetches += 1;
    return jsonResponse({ ...funnelResponse(), workspace_id: `wsp_${"7".repeat(32)}` });
  });
  await assert.rejects(
    listGrowthFunnels({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.code === "platform.growth_response_invalid",
  );
  await assert.rejects(
    listGrowthFunnels({ controlApiUrl: CONTROL_URL, filters: { owner_override: "forbidden" } }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  await assert.rejects(
    applyGrowthCampaign({
      controlApiUrl: CONTROL_URL,
      command: {
        operation: "pause",
        campaign_id: CAMPAIGN_ID,
        expected_revision: 1,
        confirmed: true,
        notes: "forbidden",
      },
      idempotencyKey: IDEMPOTENCY,
      stepUpGrant: STEP_UP,
    }),
    (error) => error instanceof PlatformApiError && error.code === "platform.request_invalid",
  );
  assert.equal(fetches, 1);
});

test("PRD-141 store aborts old owner lanes and exposes exact role tabs", () => {
  const store = createPlatformGrowthStore();
  const navigation = [{ id: "growth" }];
  assert.equal(store.enter(platformContext(), navigation), true);
  assert.deepEqual(store.snapshot().availableTabs, [
    "funnels", "cohorts", "campaigns", "experiments", "attribution",
  ]);
  const old = store.begin("read-funnels");
  assert.equal(store.enter(platformContext({ revision: 2 }), navigation), true);
  assert.equal(old.controller.signal.aborted, true);
  assert.equal(store.accept(old, { data: { funnels: funnelResponse() } }), false);
  assert.equal(store.snapshot().data.funnels, null);
  store.leave();
  assert.equal(store.enter(platformContext({
    roles: ["platform_customer_success"],
    permissions: ["platform.growth.adoption_read"],
  }), navigation), true);
  assert.deepEqual(store.snapshot().availableTabs, ["funnels", "cohorts"]);
  store.leave();
  assert.equal(store.enter(platformContext({
    roles: ["platform_channel"],
    permissions: ["platform.growth.channel_attribution_read"],
  }), navigation), true);
  assert.deepEqual(store.snapshot().availableTabs, ["attribution"]);
});

test("PRD-141 controller clips customer-success and channel queries before API calls", async () => {
  const customerReads = [];
  const customer = createPlatformGrowthController({
    api: {
      growthFunnels: async (input) => {
        customerReads.push(input.filters);
        return funnelResponse();
      },
    },
  });
  customer.enter(platformContext({
    roles: ["platform_customer_success"],
    permissions: ["platform.growth.adoption_read"],
  }), [{ id: "growth" }]);
  await customer.configure("funnels", {
    view: "overview",
    dimension: "source",
    dimension_value: SOURCE_ID,
  });
  assert.deepEqual(customerReads, [{ view: "adoption", dimension: "none" }]);

  const channelReads = [];
  const channel = createPlatformGrowthController({
    api: {
      growthAttribution: async (input) => {
        channelReads.push(input.filters);
        return { ...attributionResponse(), view: "channel_summary" };
      },
    },
  });
  channel.enter(platformContext({
    roles: ["platform_channel"],
    permissions: ["platform.growth.channel_attribution_read"],
  }), [{ id: "growth" }]);
  await channel.configure("attribution", {
    view: "campaign",
    campaign: CAMPAIGN_ID,
    source: SOURCE_ID,
  });
  assert.deepEqual(channelReads, [{ view: "channel_summary" }]);
});

test("PRD-141 controller never persists or renders an export receipt", async () => {
  const downloads = [];
  const saved = [];
  const controller = createPlatformGrowthController({
    api: {
      createIdentityStepUpGrant: async () => ({ grant: STEP_UP }),
      growthExportReceipt: async () => receiptResponse(),
      growthExportDownload: async (input) => {
        downloads.push(input);
        return { blob: new Blob(["safe"]), filename: "growth.csv" };
      },
      growthExport: async () => exportResponse("downloaded", 2),
    },
    saveDownload: (file) => saved.push(file.filename),
  });
  controller.enter(platformContext(), [{ id: "growth" }]);
  assert.equal(await controller.downloadExport(EXPORT_ID, {
    method: "totp",
    verificationCode: "123456",
  }), true);
  assert.equal(downloads[0].receipt, RECEIPT);
  assert.deepEqual(saved, ["growth.csv"]);
  const serialized = JSON.stringify(controller.snapshot());
  assert.doesNotMatch(serialized, new RegExp(RECEIPT, "u"));
  assert.doesNotMatch(renderPlatformGrowth(controller.snapshot(), new Set(
    platformContext().permissions,
  )), new RegExp(RECEIPT, "u"));
});

test("PRD-141 renderer exposes full tabs and clips the channel projection", () => {
  const full = renderPlatformGrowth(workbenchState(), new Set(platformContext().permissions));
  assert.match(full, /data-platform-growth-root/u);
  assert.match(full, /转化漏斗[\s\S]*留存分析[\s\S]*获客活动[\s\S]*增长实验[\s\S]*归因结果/u);
  assert.match(full, /data-platform-growth-campaign-form/u);
  assert.match(full, /data-platform-growth-export-form/u);
  assert.doesNotMatch(full, new RegExp(RECEIPT, "u"));

  const channelState = {
    ...workbenchState(),
    availableTabs: ["attribution"],
    tab: "attribution",
    data: { ...workbenchState().data, attribution: { ...attributionResponse(), view: "channel_summary" } },
  };
  const channel = renderPlatformGrowth(
    channelState,
    new Set(["platform.growth.channel_attribution_read"]),
  );
  assert.match(channel, /渠道汇总/u);
  assert.doesNotMatch(channel, /Campaign ID|Source ID|<th>Value<\/th>/u);
  assert.doesNotMatch(channel, /data-platform-growth-export-form/u);
});

test("PRD-141 active Workspace entrypoints contain no old Product Analytics runtime chain", async () => {
  const [main, developerStart, health, platformState, platformRenderer, app, consoleService] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/customerHealth.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/platformPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/platform/renderPlatformPage.js", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/app.py", import.meta.url), "utf8"),
    readFile(new URL("../../../control-plane-api/src/metahub_control/console/service.py", import.meta.url), "utf8"),
  ]);
  const forbidden = /growth-analytics|productAnalytics|product-analytics|ProductAnalytics/u;
  assert.doesNotMatch(main, forbidden);
  assert.doesNotMatch(developerStart, forbidden);
  assert.doesNotMatch(health, /growth-analytics/u);
  assert.match(health, /restore_gateway_activity:\s*"request-logs"/u);
  assert.match(platformState, /createPlatformGrowthController/u);
  assert.match(platformRenderer, /renderPlatformGrowth/u);
  assert.doesNotMatch(app, /include_router\(product_analytics/u);
  assert.doesNotMatch(consoleService, /"growth-analytics":\s*"console\.page_viewed"/u);
  assert.match(consoleService, /WORKSPACE_NAVIGATION_EXCLUDED_IDS[\s\S]*growth-analytics/u);
});

test("PRD-141 Platform shell loads only the role-clipped Growth model", async (t) => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash: "#growth" },
    history: { replaceState() {} },
  };
  t.after(() => { globalThis.window = originalWindow; });
  const reads = [];
  const api = {
    session: async () => ({ context: platformContext({
      roles: ["platform_customer_success"],
      permissions: ["platform.growth.adoption_read"],
    }) }),
    navigation: async () => ({
      items: [
        { id: "growth", label: "Growth", href: "/platform.html#growth" },
        { id: "overview", label: "Overview", href: "/platform.html#overview" },
      ],
    }),
    growthFunnels: async ({ filters }) => {
      reads.push(filters);
      return { ...funnelResponse(), items: funnelResponse().items.map((item) => ({
        ...item,
        view: "adoption",
        revenue_minor: null,
        cost_minor: null,
        contribution_minor: null,
        currency: null,
      })) };
    },
    workspaces: async () => {
      throw new Error("Workspace directory must not be prefetched");
    },
  };
  const root = fakeRoot();
  const controller = createPlatformPageController({ root, api });
  assert.equal(await controller.mount(), true);
  await waitFor(() => controller.snapshot().growth?.data?.funnels !== null);
  assert.deepEqual(reads, [{ view: "adoption" }]);
  assert.match(root.innerHTML, /data-platform-growth-root/u);
  assert.doesNotMatch(root.innerHTML, /Campaigns|Experiments|Attribution/u);
  controller.selectView("overview");
  assert.equal(controller.snapshot().growth.data.funnels, null);
  controller.destroy();
});

function platformContext({
  revision = 1,
  roles = ["platform_owner"],
  permissions = [
    "platform.growth.read",
    "platform.growth.campaign.manage",
    "platform.growth.experiment.manage",
    "platform.growth.export.request",
    "platform.growth.export.approve",
    "platform.growth.export.download",
  ],
} = {}) {
  return {
    operator: {
      operator_id: OPERATOR_ID,
      display_name: "Growth Owner",
      status: "active",
      roles,
      revision,
    },
    permissions,
    expires_at: "2026-09-01T12:00:00.000Z",
  };
}

function freshness() {
  return { status: "fresh", watermark: NOW, stale_seconds: 0, coverage_bps: 10000 };
}

function envelope(items, extra) {
  return {
    items,
    limit: 100,
    has_more: false,
    next_cursor: null,
    ...extra,
    source_freshness: freshness(),
    generated_at: NOW,
    request_id: "req-prd141",
    trace_id: "trace-prd141",
  };
}

function funnelResponse() {
  return envelope([{
    snapshot_id: `gfs_${"7".repeat(32)}`,
    bucket_start: "2026-09-01",
    bucket: "day",
    view: "overview",
    dimension: "none",
    dimension_value: null,
    stages: [
      ["registered", 100, 100, null],
      ["activated", 80, 80, 8000],
      ["trial_started", 60, 60, 7500],
      ["paid", 40, 40, 6667],
      ["retained_30d", 25, 25, 6250],
    ].map(([code, subject_count, event_count, conversion_from_previous_bps]) => ({
      code, subject_count, event_count, conversion_from_previous_bps,
    })),
    coverage_bps: 10000,
    suppressed: false,
    suppression_code: null,
    currency: "CNY",
    revenue_minor: 100000,
    cost_minor: 40000,
    contribution_minor: 60000,
    generation: 1,
    source_watermark: NOW,
  }], {
    formula_version: "growth-funnel-v2",
    schema_version: "platform-growth-funnel-v1",
  });
}

function cohortResponse() {
  return envelope([{
    snapshot_id: `gco_${"8".repeat(32)}`,
    cohort_kind: "signup_week",
    cohort_bucket: "2026-08-24",
    age_bucket: 1,
    view: "retention",
    dimension: "none",
    dimension_value: null,
    eligible_count: 100,
    retained_count: 70,
    churned_count: 30,
    retention_bps: 7000,
    coverage_bps: 10000,
    suppressed: false,
    suppression_code: null,
    generation: 1,
    supersedes_snapshot_id: null,
    source_watermark: NOW,
  }], {
    formula_version: "growth-cohort-v2",
    schema_version: "platform-growth-cohort-v1",
  });
}

function campaignSummary() {
  return {
    campaign_id: CAMPAIGN_ID,
    display_name: "September activation",
    acquisition_source_id: SOURCE_ID,
    channel_code: "direct",
    target_stage: "activated",
    starts_at: NOW,
    ends_at: "2026-09-30T08:00:00.000Z",
    budget_minor: 100000,
    currency: "CNY",
    status: "active",
    revision: 1,
    conversion_count: 25,
    spend_minor: 40000,
    outcome_bps: 2500,
    action_capabilities: ["pause", "complete"],
    updated_at: NOW,
  };
}

function campaignList() {
  return envelope([campaignSummary()], { schema_version: "platform-growth-campaign-v1" });
}

function campaignResponse() {
  return { campaign: campaignSummary(), idempotent_replay: false, request_id: "req-prd141", trace_id: "trace-prd141" };
}

function experimentSummary() {
  return {
    experiment_id: EXPERIMENT_ID,
    hypothesis_code: "activation.copy.v1",
    surface_code: "onboarding",
    primary_metric: "activation_rate",
    control_code: "control",
    variant_code: "variant",
    control_allocation_bps: 5000,
    variant_allocation_bps: 5000,
    eligibility_policy_version: "growth-eligibility-v2",
    consent_version: "analytics-v1",
    sample_cap: 1000,
    starts_at: NOW,
    ends_at: "2026-09-30T08:00:00.000Z",
    guardrail_code: "safety.default",
    decision_policy_version: "fixed-horizon-v1",
    status: "scheduled",
    decision: null,
    assigned_count: 0,
    control_count: 0,
    variant_count: 0,
    revision: 1,
    action_capabilities: ["update", "start"],
    updated_at: NOW,
  };
}

function experimentList() {
  return envelope([experimentSummary()], { schema_version: "platform-growth-experiment-v1" });
}

function experimentResponse() {
  return { experiment: experimentSummary(), idempotent_replay: false, request_id: "req-prd141", trace_id: "trace-prd141" };
}

function attributionResponse() {
  return envelope([{
    safe_id: "safe-channel-01",
    source_id: SOURCE_ID,
    campaign_id: CAMPAIGN_ID,
    channel_partner_id: CHANNEL_ID,
    conversion_stage: "paid",
    conversion_count: 25,
    value_minor: 100000,
    cost_minor: null,
    currency: "CNY",
    conversion_bps: 2500,
    coverage_bps: 10000,
    suppressed: false,
    suppression_code: null,
    source_watermark: NOW,
  }], {
    view: "summary",
    formula_version: "last-eligible-touch-30d-v1",
    schema_version: "platform-growth-attribution-v1",
  });
}

function exportManifest() {
  return {
    query_kind: "funnels",
    view: "overview",
    from_date: "2026-08-01",
    to_date: "2026-09-01",
    bucket: "day",
    dimension: "none",
    formula_version: "growth-funnel-v2",
    sensitivity: "aggregate",
    confirmed: true,
  };
}

function exportResponse(status = "ready", revision = 1) {
  return {
    export: {
      export_id: EXPORT_ID,
      query_kind: "funnels",
      sensitivity: "aggregate",
      status,
      revision,
      row_count: 1,
      cell_count: 12,
      expires_at: "2026-09-02T08:00:00.000Z",
      download_count: status === "downloaded" ? 1 : 0,
      created_at: NOW,
      updated_at: NOW,
    },
    idempotent_replay: false,
    request_id: "req-prd141",
    trace_id: "trace-prd141",
  };
}

function receiptResponse() {
  return {
    export_id: EXPORT_ID,
    receipt: RECEIPT,
    expires_at: "2026-09-01T08:05:00.000Z",
    request_id: "req-prd141",
    trace_id: "trace-prd141",
  };
}

function workbenchState() {
  return {
    phase: "ready",
    loading: [],
    availableTabs: ["funnels", "cohorts", "campaigns", "experiments", "attribution"],
    tab: "campaigns",
    filters: { funnels: {}, cohorts: {}, campaigns: {}, experiments: {}, attribution: {} },
    cursors: { funnels: null, cohorts: null, campaigns: null, experiments: null, attribution: null },
    histories: { funnels: [], cohorts: [], campaigns: [], experiments: [], attribution: [] },
    data: {
      funnels: funnelResponse(),
      cohorts: cohortResponse(),
      campaigns: campaignList(),
      experiments: experimentList(),
      attribution: attributionResponse(),
    },
    export: exportResponse().export,
    feedback: "",
    error: null,
    intents: { campaign: null, experiment: null, export: null, approval: null },
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
