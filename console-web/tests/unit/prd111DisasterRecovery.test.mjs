import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  getDisasterRecoveryStatus,
  listDisasterRecoveryBackups,
  startDisasterRecoveryDrill,
  startRegionFailover,
} from "../../src/lib/disasterRecovery.js";

const CONTROL_URL = "http://control.test";
const NOW = "2026-08-24T08:00:00Z";
const PLAN_ID = `fpl_${"1".repeat(32)}`;
const NORTH_ID = `reg_${"2".repeat(32)}`;
const EAST_ID = `reg_${"3".repeat(32)}`;
const RUN_ID = `drr_${"4".repeat(32)}`;

test("DR clients reject response extensions and owner-sensitive recovery fields", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => jsonResponse(statusResponse());
    assert.equal((await getDisasterRecoveryStatus({ controlApiUrl: CONTROL_URL })).plans.length, 1);
    globalThis.fetch = async () => jsonResponse({ ...statusResponse(), endpoint: "internal" });
    await assert.rejects(
      getDisasterRecoveryStatus({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "dr.response_invalid",
    );
    globalThis.fetch = async () => jsonResponse({
      ...backupResponse(),
      items: [{ ...recoveryPoint(), tenant_id: "ten_secret" }],
    });
    await assert.rejects(
      listDisasterRecoveryBackups({ controlApiUrl: CONTROL_URL }),
      (error) => error.code === "dr.response_invalid",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("DR writes bind Cookie CSRF and idempotency without bearer credentials", async () => {
  const originalFetch = globalThis.fetch;
  const originalDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: `__Host-metahub-platform-csrf=${"p".repeat(43)}` };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(runResponse(), 201);
  };
  try {
    await startDisasterRecoveryDrill({
      controlApiUrl: CONTROL_URL,
      failoverPlanId: PLAN_ID,
      planVersion: 1,
      reasonCode: "quarterly_drill",
      expectedEpoch: 0,
      idempotencyKey: "prd111-console-drill-0001",
    });
    await startRegionFailover({
      controlApiUrl: CONTROL_URL,
      regionId: NORTH_ID,
      failoverPlanId: PLAN_ID,
      planVersion: 1,
      reasonCode: "regional_outage",
      expectedEpoch: 0,
      idempotencyKey: "prd111-console-failover-0001",
    });
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, `${CONTROL_URL}/platform/dr/drills`);
  assert.equal(calls[1].url, `${CONTROL_URL}/platform/regions/${NORTH_ID}/failovers`);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "p".repeat(43));
  assert.equal(calls[0].options.headers["Idempotency-Key"], "prd111-console-drill-0001");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    failover_plan_id: PLAN_ID,
    plan_version: 1,
    reason_code: "regional_outage",
    expected_epoch: 0,
    confirmed: true,
  });
});

test("renderer independently prunes execution and security review controls", () => {
  const ops = { innerHTML: "" };
  renderPlatformWorkspace(ops, platformState(["platform.dr.read", "platform.dr.execute"]));
  assert.match(ops.innerHTML, /灾难恢复/u);
  assert.match(ops.innerHTML, /发起演练/u);
  assert.match(ops.innerHTML, /发起切换/u);
  assert.doesNotMatch(ops.innerHTML, /安全复核/u);

  const security = { innerHTML: "" };
  const securityState = platformState([
    "platform.dr.read", "platform.dr.approve", "platform.dr.security_review",
  ]);
  securityState.session.context.operator.operator_id = `pto_${"6".repeat(32)}`;
  securityState.disasterRecoveryStatus = statusResponse([run()]);
  renderPlatformWorkspace(security, securityState);
  assert.match(security.innerHTML, /安全复核/u);
  assert.doesNotMatch(security.innerHTML, /发起演练|发起切换/u);
  assert.doesNotMatch(
    security.innerHTML,
    /object_key|kms_key_version|source_watermark|tenant_id|internal_endpoint/iu,
  );

  const initiator = { innerHTML: "" };
  const initiatorState = platformState([
    "platform.dr.read", "platform.dr.execute", "platform.dr.approve",
    "platform.dr.security_review",
  ]);
  initiatorState.session.context.operator.operator_id = `pto_${"5".repeat(32)}`;
  initiatorState.disasterRecoveryStatus = statusResponse([run()]);
  renderPlatformWorkspace(initiator, initiatorState);
  assert.doesNotMatch(initiator.innerHTML, /data-platform-dr-command-form/u);

  const readOnly = { innerHTML: "" };
  renderPlatformWorkspace(readOnly, platformState(["platform.dr.read"]));
  assert.doesNotMatch(readOnly.innerHTML, /data-platform-dr-command-form/u);
});

test("controller refreshes both projections and locks navigation during a DR vote", async () => {
  const calls = [];
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const api = controllerApi(calls, ["platform.dr.read", "platform.dr.execute"]);
  api.startDisasterRecoveryDrill = async () => {
    calls.push("drill");
    await pending;
    return runResponse();
  };
  const controller = createPlatformPageController({
    root: fakeRoot(),
    api,
    newIdempotencyKey: () => "prd111-controller-vote-0001",
  });
  assert.equal(await controller.mount(), true);
  controller.selectView("disaster-recovery");
  const writing = controller.voteDisasterRecovery({
    runKind: "drill",
    voteKind: "execution",
    failoverPlanId: PLAN_ID,
    planVersion: 1,
    reasonCode: "quarterly_drill",
    expectedEpoch: 0,
    confirmed: true,
  });
  assert.equal(controller.selectView("overview"), false);
  assert.equal(await controller.refresh(), false);
  release();
  assert.equal(await writing, true);
  assert.equal(calls.filter((item) => item === "status").length, 2);
  assert.equal(calls.filter((item) => item === "backups").length, 2);
});

test("controller requires both approval permissions for the security vote", async () => {
  const deniedCalls = [];
  const denied = createPlatformPageController({
    root: fakeRoot(),
    api: controllerApi(deniedCalls, ["platform.dr.read", "platform.dr.approve"]),
  });
  assert.equal(await denied.mount(), true);
  assert.equal(await denied.voteDisasterRecovery({
    runKind: "drill",
    voteKind: "security_review",
    failoverPlanId: PLAN_ID,
    planVersion: 1,
    reasonCode: "quarterly_drill",
    expectedEpoch: 0,
    confirmed: true,
  }), false);
  assert.equal(deniedCalls.includes("drill"), false);
});

test("DR tables collapse to labeled rows and client avoids browser storage", async () => {
  const css = await readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8");
  const client = await readFile(new URL("../../src/lib/disasterRecovery.js", import.meta.url), "utf8");
  assert.match(css, /\.platform-dr-region-table thead,[\s\S]*\.platform-dr-backup-table thead \{ display: none;/u);
  assert.match(css, /\.platform-dr-region-table td::before,[\s\S]*\.platform-dr-backup-table td::before \{ content: attr\(data-label\)/u);
  assert.doesNotMatch(client, /localStorage|sessionStorage|object_key|kms_key_version|source_watermark|tenant_id/iu);
});

function controllerApi(calls, permissions) {
  return {
    session: async () => ({ context: {
      permissions,
      operator: {
        operator_id: `pto_${"8".repeat(32)}`,
        display_name: "Operator",
        roles: ["platform_ops"],
      },
    } }),
    navigation: async () => ({ items: [{
      id: "disaster-recovery",
      label: "Disaster Recovery",
      href: "/platform.html#disaster-recovery",
    }] }),
    disasterRecoveryStatus: async () => { calls.push("status"); return statusResponse(); },
    disasterRecoveryBackups: async () => { calls.push("backups"); return backupResponse(); },
    startDisasterRecoveryDrill: async () => { calls.push("drill"); return runResponse(); },
    startRegionFailover: async () => { calls.push("failover"); return runResponse(); },
  };
}

function platformState(permissions) {
  return {
    session: { context: {
      permissions,
      operator: { display_name: "Operator", roles: ["platform_ops"] },
    } },
    navigation: [{
      id: "disaster-recovery",
      label: "Disaster Recovery",
      href: "/platform.html#disaster-recovery",
    }],
    view: "disaster-recovery",
    disasterRecoveryStatus: statusResponse(),
    disasterRecoveryBackups: backupResponse(),
    disasterRecoveryIntent: null,
    loading: false,
  };
}

function statusResponse(runs = []) {
  return {
    regions: [region(NORTH_ID, "cn-north-1", "active"), region(EAST_ID, "cn-east-1", "standby")],
    plans: [plan()],
    recent_runs: runs,
    objectives: {
      control_rpo_seconds: 900,
      control_rto_seconds: 7200,
      gateway_rto_seconds: 600,
      latest_recovery_age_seconds: 30,
      recovery_ready: true,
    },
    request_id: "request-prd111-status",
    trace_id: "trace-prd111-status",
  };
}

function region(regionId, regionCode, writerStatus) {
  return {
    region_id: regionId,
    region_code: regionCode,
    residency_group: "cn-main",
    health_status: "healthy",
    writer_status: writerStatus,
    failover_epoch: 0,
    revision: 1,
    updated_at: NOW,
  };
}

function plan() {
  return {
    failover_plan_id: PLAN_ID,
    plan_code: "control-north-to-east",
    version: 1,
    source_region_code: "cn-north-1",
    target_region_code: "cn-east-1",
    residency_group: "cn-main",
    component_profile: "control-full",
    rpo_seconds: 900,
    control_rto_seconds: 7200,
    gateway_rto_seconds: 600,
    stop_condition_code: "split-brain-or-integrity-failure",
    manifest_sha256: "a".repeat(64),
    status: "active",
  };
}

function run() {
  return {
    disaster_recovery_run_id: RUN_ID,
    run_kind: "drill",
    failover_plan_id: PLAN_ID,
    plan_version: 1,
    source_region_code: "cn-north-1",
    target_region_code: "cn-east-1",
    expected_epoch: 0,
    fencing_epoch: 1,
    reason_code: "quarterly_drill",
    status: "planned",
    checkpoint: "approval",
    platform_job_id: null,
    approvals: [{
      approval_kind: "execution",
      operator_id: `pto_${"5".repeat(32)}`,
      approved_at: NOW,
    }],
    restore_evidence: [],
    safe_outcome_code: null,
    revision: 1,
    started_at: null,
    completed_at: null,
    created_at: NOW,
    updated_at: NOW,
  };
}

function recoveryPoint() {
  return {
    recovery_point_id: `rcp_${"6".repeat(32)}`,
    backup_id: `bkp_${"7".repeat(32)}`,
    scope_kind: "platform",
    workspace_id: null,
    schema_head: "0103",
    integrity_status: "verified",
    manifest_sha256: "b".repeat(64),
    source_created_at: NOW,
    age_seconds: 30,
  };
}

function backupResponse() {
  return {
    items: [recoveryPoint()],
    total: 1,
    request_id: "request-prd111-backups",
    trace_id: "trace-prd111-backups",
  };
}

function runResponse() {
  return {
    run: run(),
    approval_recorded: true,
    idempotent_replay: false,
    request_id: "request-prd111-run",
    trace_id: "trace-prd111-run",
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
