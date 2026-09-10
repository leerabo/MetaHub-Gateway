import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  commandCapacityChaos,
  createCapacityForecast,
  createCapacityLoadTest,
  getPlatformCapacity,
  getPlatformUnitEconomics,
} from "../../src/lib/capacityManagement.js";

const CONTROL_URL = "http://control.test";
const NOW = "2026-08-25T08:00:00Z";
const FORECAST_ID = `cfc_${"1".repeat(32)}`;
const RUN_ID = `ltr_${"2".repeat(32)}`;
const EXPERIMENT_ID = `che_${"3".repeat(32)}`;
const JOB_ID = `pjob_${"4".repeat(32)}`;

test("chaos stop intent remains pending and cannot be submitted twice", () => {
  const state = platformState(["platform.capacity.read", "platform.capacity.chaos"]);
  state.capacity.chaos_experiments = [{
    ...chaosExperiment(), status: "running", stop_reason_code: "operator_kill", kill_generation: 1,
  }];
  const root = { innerHTML: "" };
  renderPlatformWorkspace(root, state);
  assert.match(root.innerHTML, /Awaiting stop confirmation/u);
  assert.doesNotMatch(root.innerHTML, /name="action" value="kill"/u);
  assert.doesNotMatch(root.innerHTML, />Terminal</u);
});

test("capacity clients enforce closed projections, CSRF, CAS, and fixed commands", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  const responses = [
    capacityResponse(), economicsResponse(), forecastResponse(), loadResponse(), chaosResponse(),
  ];
  try {
    globalThis.fetch = async (url, options) => {
      calls.push({ url: String(url), options });
      return jsonResponse(responses[calls.length - 1], calls.length > 2 ? 201 : 200);
    };
    assert.equal((await getPlatformCapacity({ controlApiUrl: CONTROL_URL })).services.length, 1);
    assert.equal((await getPlatformUnitEconomics({
      controlApiUrl: CONTROL_URL, billingPeriod: "2026-08", currency: "CNY",
    })).total, 1);
    await createCapacityForecast({
      controlApiUrl: CONTROL_URL,
      serviceCode: "gateway",
      regionCode: "cn-north-1",
      horizonDays: 30,
      expectedRevision: 1,
      confirmed: true,
      idempotencyKey: "prd113-console-forecast-0001",
    });
    await createCapacityLoadTest({
      controlApiUrl: CONTROL_URL,
      scenarioCode: "gateway-release",
      environmentClass: "staging",
      regionCode: "cn-north-1",
      syntheticTenantCount: 100,
      targetRps: 1600,
      durationSeconds: 60,
      confirmed: true,
      idempotencyKey: "prd113-console-load-test-001",
    });
    await commandCapacityChaos({
      controlApiUrl: CONTROL_URL,
      action: "approve",
      experimentId: EXPERIMENT_ID,
      revision: 1,
      reasonCode: "release_validation",
      confirmed: true,
      idempotencyKey: "prd113-console-chaos-approve-1",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(new URL(calls[0].url).pathname, "/platform/capacity");
  assert.equal(new URL(calls[1].url).search, "?billing_period=2026-08&currency=CNY");
  assert.equal(calls[2].options.headers["If-Match"], '"1"');
  assert.equal(calls[2].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[2].options.credentials, "include");
  assert.equal("Authorization" in calls[2].options.headers, false);
  assert.equal("If-Match" in calls[3].options.headers, false);
  assert.equal(calls[4].options.headers["If-Match"], '"1"');
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    action: "approve",
    chaos_experiment_id: EXPERIMENT_ID,
    reason_code: "release_validation",
    confirmed: true,
  });

  const original = globalThis.fetch;
  globalThis.fetch = async () => jsonResponse({ ...capacityResponse(), tenant_id: "private" });
  try {
    await assert.rejects(
      getPlatformCapacity({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "capacity.response_invalid" && error.status === 502,
    );
    assert.throws(
      () => createCapacityLoadTest({
        controlApiUrl: CONTROL_URL,
        scenarioCode: "https://invalid.test/script",
        environmentClass: "staging",
        regionCode: "cn-north-1",
        syntheticTenantCount: 1,
        targetRps: 1,
        durationSeconds: 10,
        confirmed: true,
        idempotencyKey: "prd113-console-invalid-load-1",
      }),
      (error) => error.code === "capacity.request_invalid",
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("renderer prunes capacity, chaos, and economics projections independently", () => {
  const billing = fakeRoot();
  renderPlatformWorkspace(billing, platformState(["platform.unit_economics.read"]));
  assert.match(billing.innerHTML, /Unit economics/u);
  assert.doesNotMatch(billing.innerHTML, /Service headroom|data-platform-capacity-(?:forecast|load|chaos)-form/u);

  const security = fakeRoot();
  renderPlatformWorkspace(security, platformState([
    "platform.capacity.read", "platform.capacity.chaos",
  ]));
  assert.match(security.innerHTML, /Service headroom/u);
  assert.match(security.innerHTML, /value="approve"/u);
  assert.match(security.innerHTML, /value="kill"/u);
  assert.doesNotMatch(security.innerHTML, /data-platform-capacity-forecast-form|data-platform-capacity-load-form|Unit economics/u);

  const ops = fakeRoot();
  renderPlatformWorkspace(ops, platformState([
    "platform.capacity.read", "platform.capacity.test", "platform.capacity.chaos",
  ]));
  assert.match(ops.innerHTML, /data-platform-capacity-forecast-form/u);
  assert.match(ops.innerHTML, /data-platform-capacity-load-form/u);
  assert.match(ops.innerHTML, /Create chaos experiment/u);
  assert.doesNotMatch(ops.innerHTML, /tenant_id|workspace_id|internal_endpoint|secret/iu);
});

test("controller requests only authorized projections and prunes revoked capacity navigation", async () => {
  const calls = [];
  let permissions = ["platform.unit_economics.read"];
  const api = {
    session: async () => ({ context: {
      permissions,
      operator: { display_name: "Billing", roles: ["platform_billing"] },
    } }),
    navigation: async () => ({ items: permissions.length ? [{
      id: "capacity", label: "Capacity and resilience", href: "/platform.html#capacity",
    }] : [] }),
    capacity: async () => { calls.push("capacity"); return capacityResponse(); },
    unitEconomics: async () => { calls.push("economics"); return economicsResponse(); },
  };
  const controller = createPlatformPageController({ root: fakeRoot(), api });
  assert.equal(await controller.mount(), true);
  assert.deepEqual(calls, ["economics"]);
  controller.selectView("capacity");
  assert.equal(await controller.createCapacityForecast({ confirmed: true }), false);
  permissions = [];
  assert.equal(await controller.refresh(), true);
  assert.equal(controller.selectView("capacity"), undefined);
  assert.deepEqual(calls, ["economics"]);
});

test("capacity layout has stable narrow-screen tables and no browser persistence", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(
    new URL("../../src/lib/capacityManagement.js", import.meta.url), "utf8",
  );
  assert.match(css, /\.platform-capacity-table,[\s\S]*\.platform-economics-table \{ min-width: 0; \}/u);
  assert.match(css, /\.platform-chaos-table thead[\s\S]*display: none/u);
  assert.match(css, /\.platform-economics-table td::before[\s\S]*content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|indexedDB|tenant_id["']\s*:/iu);
});

function platformState(permissions) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } },
    navigation: [{
      id: "capacity", label: "Capacity and resilience", href: "#capacity",
    }],
    view: "capacity",
    capacity: permissions.includes("platform.capacity.read") ? capacityResponse() : null,
    unitEconomics: permissions.includes("platform.unit_economics.read")
      ? economicsResponse() : null,
    capacityIntent: null,
    loading: false,
  };
}

function capacityResponse() {
  return {
    services: [{
      service_code: "gateway", region_code: "cn-north-1", demand_rps: 800,
      capacity_rps: 2000, utilization_bps: 4000, headroom_rps: 1200,
      threshold_bps: 8000, threshold_reached: false, slo_p95_ms: 2000,
      recommendation_code: "hold", revision: 1, updated_at: NOW,
    }],
    scenarios: [{
      scenario_code: "gateway-release", service_code: "gateway", run_kind: "load",
      max_tenant_count: 200, max_target_rps: 10000, max_duration_seconds: 3600,
      target_peak_multiplier_bps: 20000, manifest_sha256: "a".repeat(64), status: "active",
    }],
    baselines: [{
      performance_baseline_id: `pbl_${"5".repeat(32)}`, service_code: "gateway",
      scenario_code: "gateway-release", environment_class: "staging", version: 1,
      sample_count: 96000, p50_ms: 35, p95_ms: 70, p99_ms: 87,
      throughput_rps_milli: 1592000, error_rate_bps: 25, created_at: NOW,
    }],
    forecasts: [forecast()],
    load_tests: [loadRun()],
    chaos_faults: [{
      fault_code: "dependency-timeout", service_code: "shared", max_blast_tenants: 25,
      max_duration_seconds: 900, manifest_sha256: "b".repeat(64), status: "active",
    }],
    chaos_experiments: [chaosExperiment()],
    rollout_blocks: [{
      service_code: "gateway", scenario_code: "gateway-release",
      environment_class: "staging", load_test_run_id: RUN_ID,
      status: "active", created_at: NOW, cleared_at: null,
    }],
    request_id: "request-prd113-capacity",
    trace_id: "trace-prd113-capacity",
  };
}

function forecast() {
  return {
    capacity_forecast_id: FORECAST_ID, service_code: "gateway", region_code: "cn-north-1",
    horizon_days: 30, model_code: "linear-v1", predicted_demand_rps: null,
    predicted_capacity_rps: null, confidence_bps: null, error_outcome_bps: null,
    recommendation_code: null, status: "queued", job_id: JOB_ID, revision: 1,
    created_at: NOW, updated_at: NOW,
  };
}

function loadRun() {
  return {
    load_test_run_id: RUN_ID, scenario_code: "gateway-release", service_code: "gateway",
    run_kind: "load", environment_class: "staging", region_code: "cn-north-1",
    synthetic_tenant_count: 100, target_rps: 1600, duration_seconds: 60,
    status: "succeeded", job_id: JOB_ID, sample_count: 96000, p50_ms: 36, p95_ms: 72,
    p99_ms: 90, throughput_rps_milli: 1592000, error_rate_bps: 25,
    open_connection_delta: 0, handle_delta: 0, fairness_jain_millionths: 995000,
    controlled_degradation: true, regression_status: "regressed",
    safe_outcome_code: "runner.load_completed", revision: 3, started_at: NOW,
    completed_at: NOW, created_at: NOW, updated_at: NOW,
  };
}

function chaosExperiment() {
  return {
    chaos_experiment_id: EXPERIMENT_ID, fault_code: "dependency-timeout",
    service_code: "gateway", environment_class: "production", region_code: "cn-north-1",
    blast_tenant_count: 10, duration_seconds: 60, objective_p95_ms: 2000,
    objective_error_rate_bps: 100, stop_error_rate_bps: 500,
    status: "pending_approval", job_id: null, approvals: [], kill_generation: 0,
    observed_p95_ms: null, observed_error_rate_bps: null, objective_met: null,
    stop_reason_code: null, revision: 1, started_at: null, completed_at: null,
    created_at: NOW, updated_at: NOW,
  };
}

function economicsResponse() {
  return {
    items: [{
      unit_economics_snapshot_id: `ues_${"6".repeat(32)}`, billing_period: "2026-08",
      service_code: "gateway", request_count: 1000, input_tokens: 2000,
      output_tokens: 1000, total_tokens: 3000, allocated_cost_micros: 500000,
      cost_per_request_micros: 500, cost_per_million_tokens_micros: 166666667,
      currency: "CNY", usage_watermark: "c".repeat(64), allocation_version: 1,
      created_at: NOW,
    }],
    total: 1,
    request_id: "request-prd113-economics",
    trace_id: "trace-prd113-economics",
  };
}

function forecastResponse() {
  return { forecast: forecast(), idempotent_replay: false, request_id: "req", trace_id: "trc" };
}
function loadResponse() {
  return { run: loadRun(), idempotent_replay: false, request_id: "req", trace_id: "trc" };
}
function chaosResponse() {
  return {
    experiment: chaosExperiment(), approval_recorded: true, idempotent_replay: false,
    request_id: "req", trace_id: "trc",
  };
}
function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}
function jsonResponse(payload, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}
