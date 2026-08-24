import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  acknowledgePlatformAlert,
  createServiceLevelObjective,
  listErrorBudgets,
  listObservableServices,
} from "../../src/lib/observability.js";

const CONTROL_URL = "http://control.test";
const NOW = "2026-08-24T08:00:00Z";
const SLO_ID = `slo_${"1".repeat(32)}`;
const ALERT_ID = `alt_${"2".repeat(32)}`;

test("observability clients reject extensions and request-level projections", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse(serviceResponse());
    assert.equal((await listObservableServices({ controlApiUrl: CONTROL_URL })).total, 1);
    globalThis.fetch = async () => jsonResponse({ ...serviceResponse(), tenant_id: "tenant-secret" });
    await assert.rejects(
      listObservableServices({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "observability.response_invalid",
    );
    globalThis.fetch = async () => jsonResponse({
      ...budgetResponse(),
      items: [{ ...budgetResponse().items[0], request_id: "request-secret" }],
    });
    await assert.rejects(
      listErrorBudgets({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "observability.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("SLO and alert writes bind Cookie CSRF CAS and idempotency without bearer", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(calls.length === 1 ? sloWriteResponse() : ackResponse(), 201);
  };
  try {
    await createServiceLevelObjective({
      controlApiUrl: CONTROL_URL,
      slo: sloRequest(),
      idempotencyKey: "prd109-console-slo-0001",
    });
    await acknowledgePlatformAlert({
      controlApiUrl: CONTROL_URL,
      alertId: ALERT_ID,
      revision: 1,
      reasonCode: "investigating",
      idempotencyKey: "prd109-console-ack-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[0].options.headers["If-Match"], '"0"');
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd109-console-slo-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    revision: 1,
    reason_code: "investigating",
    confirmed: true,
  });
});

test("renderer independently prunes SLO and ACK controls for security projection", () => {
  const writable = { innerHTML: "" };
  renderPlatformWorkspace(writable, platformState([
    "platform.observability.read",
    "platform.observability.manage",
    "platform.observability.alert_ack",
  ]));
  assert.match(writable.innerHTML, /服务可观测性/u);
  assert.match(writable.innerHTML, /data-platform-slo-form/u);
  assert.match(writable.innerHTML, /data-platform-alert-ack-form/u);
  assert.match(writable.innerHTML, /预算消耗 20 \/ 10/u);
  assert.doesNotMatch(writable.innerHTML, /request-secret|trace-secret|tenant-secret/u);

  const security = { innerHTML: "" };
  const state = platformState(["platform.observability.security_read"]);
  state.observabilityServices.projection = "security";
  state.serviceLevelObjectives.projection = "security";
  state.errorBudgets.projection = "security";
  renderPlatformWorkspace(security, state);
  assert.match(security.innerHTML, /安全投影/u);
  assert.doesNotMatch(security.innerHTML, /data-platform-slo-form/u);
  assert.doesNotMatch(security.innerHTML, /data-platform-alert-ack-form/u);
});

test("controller enforces independent permissions and refreshes all projections", async () => {
  const calls = [];
  const controller = createPlatformPageController({
    root: fakeRoot(),
    newIdempotencyKey: () => "prd109-controller-write-0001",
    api: controllerApi(calls, [
      "platform.observability.read",
      "platform.observability.manage",
      "platform.observability.alert_ack",
    ]),
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("observability");
  assert.equal(await controller.createServiceLevelObjective({ slo: sloRequest() }), true);
  assert.equal(await controller.acknowledgePlatformAlert({
    alertId: ALERT_ID,
    revision: 1,
    reasonCode: "investigating",
    confirmed: true,
  }), true);
  assert.equal(calls.filter((item) => item === "services").length, 3);
  assert.equal(calls.filter((item) => item === "slos").length, 3);
  assert.equal(calls.filter((item) => item === "budgets").length, 3);

  const deniedCalls = [];
  const denied = createPlatformPageController({
    root: fakeRoot(),
    api: controllerApi(deniedCalls, ["platform.observability.security_read"]),
  });
  assert.equal(await denied.mount(), true);
  assert.equal(await denied.createServiceLevelObjective({ slo: sloRequest() }), false);
  assert.equal(await denied.acknowledgePlatformAlert({
    alertId: ALERT_ID,
    revision: 1,
    reasonCode: "investigating",
    confirmed: true,
  }), false);
  assert.equal(deniedCalls.includes("write-slo"), false);
  assert.equal(deniedCalls.includes("ack"), false);
});

test("observability tables collapse to labeled rows with accessible budget state", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(new URL("../../src/lib/observability.js", import.meta.url), "utf8");
  assert.match(css, /\.platform-observability-table,[\s\S]*\.platform-budget-table \{ min-width: 0;/u);
  assert.match(css, /\.platform-observability-table thead,[\s\S]*\.platform-budget-table thead \{ display: none;/u);
  assert.match(css, /\.platform-observability-table td::before,[\s\S]*\.platform-budget-table td::before \{ content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|attributes|prompt|response_body/iu);
});

function controllerApi(calls, permissions) {
  return {
    session: async () => ({ context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } }),
    navigation: async () => ({ items: [{
      id: "observability",
      label: "Observability",
      href: "/platform.html#observability",
    }] }),
    observabilityServices: async () => { calls.push("services"); return serviceResponse(); },
    serviceLevelObjectives: async () => { calls.push("slos"); return sloListResponse(); },
    errorBudgets: async () => { calls.push("budgets"); return budgetResponse(); },
    createServiceLevelObjective: async () => { calls.push("write-slo"); return sloWriteResponse(); },
    acknowledgePlatformAlert: async () => { calls.push("ack"); return ackResponse(); },
  };
}

function platformState(permissions) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } },
    navigation: [{
      id: "observability",
      label: "Observability",
      href: "/platform.html#observability",
    }],
    view: "observability",
    observabilityServices: serviceResponse(),
    serviceLevelObjectives: sloListResponse(),
    errorBudgets: budgetResponse(),
    loading: false,
  };
}

function serviceResponse() {
  return {
    items: [{
      service_catalog_entry_id: `sce_${"3".repeat(32)}`,
      service_code: "gateway",
      display_name: "Gateway Data Plane",
      owner_team_code: "platform_sre",
      service_tier: 1,
      golden_signals: ["availability", "errors", "latency", "traffic"],
      status: "active",
      revision: 1,
      source_watermark: 7,
      total_count: 100,
      error_rate_bps: 2000,
      latency_p95_ms: 500,
      health_status: "unhealthy",
    }],
    total: 1,
    projection: "operations",
    request_id: "request-prd109-services",
    trace_id: "trace-prd109-services",
  };
}

function slo() {
  return {
    service_level_objective_id: SLO_ID,
    slo_code: "gateway.chat.availability",
    version_number: 1,
    service_code: "gateway",
    display_name: "Gateway availability",
    signal_class: "reliability",
    sli_kind: "availability",
    operation_code: "chat.completions",
    region_code: "global",
    window_minutes: 5,
    target_bps: 9000,
    latency_threshold_ms: null,
    burn_warning_bps: 10000,
    burn_critical_bps: 20000,
    definition_sha256: "a".repeat(64),
    is_current: true,
    created_at: NOW,
  };
}

function sloListResponse() {
  return {
    items: [slo()],
    total: 1,
    limit: 50,
    offset: 0,
    projection: "operations",
    request_id: "request-prd109-slos",
    trace_id: "trace-prd109-slos",
  };
}

function budgetResponse() {
  return {
    items: [{
      error_budget_id: `ebg_${"4".repeat(32)}`,
      service_level_objective_id: SLO_ID,
      slo_code: "gateway.chat.availability",
      slo_version: 1,
      service_code: "gateway",
      signal_class: "reliability",
      region_code: "global",
      window_started_at: NOW,
      window_ended_at: NOW,
      total_count: 100,
      good_count: 80,
      allowed_bad_count: 10,
      consumed_bad_count: 20,
      remaining_bad_count: 0,
      burn_rate_bps: 20000,
      health_status: "exhausted",
      source_watermark: 7,
      source_sha256: "b".repeat(64),
      computed_at: NOW,
    }],
    alerts: [{
      alert_id: ALERT_ID,
      service_level_objective_id: SLO_ID,
      slo_code: "gateway.chat.availability",
      service_code: "gateway",
      signal_class: "reliability",
      region_code: "global",
      severity: "critical",
      state: "firing",
      safe_reason_code: "error_budget_exhausted",
      source_watermark: 7,
      revision: 1,
      first_seen_at: NOW,
      last_seen_at: NOW,
      resolved_at: null,
      latest_acknowledgement_at: null,
    }],
    limit: 50,
    next_cursor: null,
    projection: "operations",
    request_id: "request-prd109-budgets",
    trace_id: "trace-prd109-budgets",
  };
}

function sloRequest() {
  return {
    service_code: "gateway",
    slo_code: "gateway.chat.availability",
    display_name: "Gateway availability",
    signal_class: "reliability",
    sli_kind: "availability",
    operation_code: "chat.completions",
    region_code: "global",
    window_minutes: 5,
    target_bps: 9000,
    latency_threshold_ms: null,
    burn_warning_bps: 10000,
    burn_critical_bps: 20000,
    previous_version: 0,
    reason_code: "slo_create",
    confirmed: true,
  };
}

function sloWriteResponse() {
  return {
    slo: slo(),
    idempotent_replay: false,
    aggregation_job_id: `pjob_${"5".repeat(32)}`,
    request_id: "request-prd109-write",
    trace_id: "trace-prd109-write",
  };
}

function ackResponse() {
  return {
    acknowledgement: {
      acknowledgement_id: `aak_${"6".repeat(32)}`,
      alert_id: ALERT_ID,
      alert_revision: 1,
      reason_code: "investigating",
      acknowledged_at: NOW,
    },
    idempotent_replay: false,
    request_id: "request-prd109-ack",
    trace_id: "trace-prd109-ack",
  };
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    replaceChildren() { this.innerHTML = ""; },
    querySelectorAll() { return []; },
  };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(payload); },
  };
}
