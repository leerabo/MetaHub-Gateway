import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  getEdgeTelemetryWatermark,
  listPlatformEdgeFleet,
  patchEdgeTelemetryPolicy,
} from "../../src/lib/edgeManagement.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const TENANT_ID = "tenant_edge_prd122";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const POLICY_ID = `etp_${"3".repeat(32)}`;
const CURSOR_ID = `euc_${"4".repeat(32)}`;
const EPOCH_ID = `ete_${"5".repeat(32)}`;
const ALL = [
  PERMISSIONS.EDGE_DEPLOYMENT_READ,
  PERMISSIONS.EDGE_DEPLOYMENT_MANAGE,
  PERMISSIONS.EDGE_TELEMETRY_READ,
  PERMISSIONS.EDGE_TELEMETRY_MANAGE,
  PERMISSIONS.EDGE_USAGE_READ,
];

function context(permissionCodes = ALL) {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "Edge tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_edge_prd122",
      role: "admin",
      email: "edge-prd122@example.test",
    },
    session: { session_id: "ses_edge_prd122", session_revision: 1 },
  }, permissionCodes, {
    permissionRevision: 32,
    authorizationFingerprint: "edge_prd122_authz",
  });
}

function shell(auth = context()) {
  return {
    state: "ok",
    activeNavigationItemId: "private-gateways",
    navigation: {
      active_navigation_item_id: "private-gateways",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [{
        id: "private-gateways",
        target_section: "private-gateways",
        label: "私有 Gateway",
        group: "配置",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
      }],
    },
  };
}

function deployment() {
  return {
    deployment_id: DEPLOYMENT_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "PRD-122 gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 2,
    revocation_generation: 1,
    agent_version: "1.1.0",
    health_code: "healthy",
    last_connected_at: "2026-08-27T01:10:00Z",
    certificate_expires_at: "2026-09-03T01:00:00Z",
    identity_generation: 1,
    current_certificate: null,
    recent_events: [],
    created_at: "2026-08-27T00:55:00Z",
    updated_at: "2026-08-27T01:10:00Z",
    revoked_at: null,
  };
}

function telemetryPolicy(overrides = {}) {
  return {
    policy_id: POLICY_ID,
    deployment_id: DEPLOYMENT_ID,
    telemetry_level: "billing",
    revision: 2,
    updated_at: "2026-08-27T01:08:00Z",
    ...overrides,
  };
}

function watermark(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    policy: telemetryPolicy(),
    cursors: [{
      cursor_id: CURSOR_ID,
      stream: "usage",
      epoch_id: EPOCH_ID,
      contiguous_sequence: 2,
      highest_seen_sequence: 4,
      expected_sequence: 3,
      updated_at: "2026-08-27T01:09:00Z",
    }],
    usage: {
      request_count: 5,
      input_tokens: 50,
      output_tokens: 25,
      cache_tokens: 10,
      cost_cny_micros: 1200,
      cost_usd_micros: 160,
    },
    failures: [{
      component_code: "provider_pool",
      failure_code: "provider_timeout",
      count: 3,
    }],
    last_receipt_at: "2026-08-27T01:09:00Z",
    lag_seconds: 30,
    backlog_seconds: 90,
    request_id: "req_edge_telemetry",
    trace_id: "trc_edge_telemetry",
    ...overrides,
  };
}

function policyResponse(overrides = {}) {
  return {
    policy: telemetryPolicy({ telemetry_level: "minimal", revision: 3 }),
    idempotent_replay: false,
    request_id: "req_edge_policy",
    trace_id: "trc_edge_policy",
    ...overrides,
  };
}

function fleetPayload(overrides = {}) {
  return {
    deployments: [{
      deployment_id: DEPLOYMENT_ID,
      region_code: "cn-east-1",
      mode: "connected",
      status: "active",
      revision: 2,
      revocation_generation: 1,
      agent_version: "1.1.0",
      health_code: "healthy",
      certificate_expires_at: "2026-09-03T01:00:00Z",
      last_connected_at: "2026-08-27T01:10:00Z",
      desired_generation: 2,
      applied_generation: 2,
      lkg_generation: 2,
      desired_state_status: "applied",
      desired_state_reason_code: "applied",
      desired_state_drifted: false,
      telemetry_last_receipt_at: "2026-08-27T01:09:00Z",
      telemetry_lag_seconds: 30,
      telemetry_backlog_seconds: 90,
      telemetry_cpu_bucket: "low",
      telemetry_memory_bucket: "medium",
      telemetry_error_rate_basis_points: 2000,
      telemetry_failure_code: "provider_timeout",
      updated_at: "2026-08-27T01:10:00Z",
    }],
    request_id: "req_edge_fleet_prd122",
    trace_id: "trc_edge_fleet_prd122",
    ...overrides,
  };
}

function response(payload, etag = "2") {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "ETag": `"${etag}"`,
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
}

test("Workspace telemetry client enforces exact watermark, Cookie, CSRF, and policy CAS", async (t) => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  t.after(() => {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const replies = [response(watermark(), "2"), response(policyResponse(), "3")];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return replies.shift();
  };

  const current = await getEdgeTelemetryWatermark({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
  });
  assert.equal(current.revision, 2);
  assert.equal(current.cursors[0].contiguous_sequence, 2);
  assert.equal(current.usage.request_count, 5);
  const updated = await patchEdgeTelemetryPolicy({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    telemetryLevel: "minimal",
    expectedRevision: 2,
    idempotencyKey: "edge-telemetry-policy-0001",
  });
  assert.equal(updated.policy.telemetry_level, "minimal");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[1].options.headers["If-Match"], '"2"');
  assert.deepEqual(JSON.parse(calls[1].options.body), { telemetry_level: "minimal" });

  globalThis.fetch = async () => response({ ...watermark(), provider_id: "forbidden" }, "2");
  await assert.rejects(
    getEdgeTelemetryWatermark({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge.response_invalid",
  );
  globalThis.fetch = async () => response(watermark(), "1");
  await assert.rejects(
    getEdgeTelemetryWatermark({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge.response_invalid",
  );
});

test("Workspace state renders bounded sync, usage, failures, and policy updates", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  assert.equal(owner.canReadTelemetry, true);
  assert.equal(owner.canManageTelemetry, true);
  assert.equal(owner.canReadUsage, true);
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  const telemetry = { ...watermark(), revision: 2 };
  assert.equal(state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [{ deploymentId: DEPLOYMENT_ID, response: {
      state: null,
      revision: 0,
      idempotent_replay: false,
      request_id: "req_desired_empty",
      trace_id: "trc_desired_empty",
    } }],
    [{ deploymentId: DEPLOYMENT_ID, response: telemetry }],
  ), true);
  state.finishLoad(load);
  const html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /遥测同步[\s\S]*Billing[\s\S]*ACK 2[\s\S]*next 3/u);
  assert.match(html, /provider_timeout[\s\S]*3/u);
  assert.match(html, /请求[\s\S]*5/u);
  assert.doesNotMatch(html, /model_id|provider_id|raw batch|tags|attributes/u);

  const operation = state.beginTelemetryPolicy(owner, DEPLOYMENT_ID, "minimal");
  assert.equal(operation.expectedRevision, 2);
  assert.equal(operation.telemetryLevel, "minimal");
  assert.equal(state.acceptTelemetryPolicy(operation, {
    ...policyResponse(),
    revision: 3,
  }, owner), true);
  state.finishTelemetryPolicy(operation);
  assert.equal(
    state.snapshot().telemetryStates[DEPLOYMENT_ID].policy.telemetry_level,
    "minimal",
  );
  assert.equal(state.snapshot().feedback.code, "edge.telemetry_policy_updated");

  const revoked = context([
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_TELEMETRY_READ,
  ]);
  state.enter(privateGatewayPageOwner(revoked, shell(revoked)));
  assert.deepEqual(state.snapshot().telemetryStates, {});
});

test("legacy Fleet client stays safe while the Platform view uses the PRD-126 projection", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => response(fleetPayload(), "0");
  const fleet = await listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL });
  assert.equal(fleet.deployments[0].telemetry_cpu_bucket, "low");
  assert.equal(fleet.deployments[0].telemetry_failure_code, "provider_timeout");

  const root = { innerHTML: "" };
  const platformProjection = {
    deployments: fleet.deployments.map((item) => ({
      deployment_id: item.deployment_id,
      region_code: item.region_code,
      mode: item.mode,
      status: item.status,
      health_code: item.health_code,
      health_score: 86,
      alert_code: "telemetry",
      maintenance_mode: false,
      agent_version: item.agent_version,
      desired_generation: item.desired_generation,
      applied_generation: item.applied_generation,
      lkg_generation: item.lkg_generation,
      telemetry_lag_seconds: item.telemetry_lag_seconds,
      certificate_signal: "healthy",
      source_health_generation: 2,
      index_revision: 3,
      updated_at: item.updated_at,
    })),
    next_cursor: null,
    count: fleet.deployments.length,
    request_id: fleet.request_id,
    trace_id: fleet.trace_id,
  };
  renderPlatformWorkspace(root, {
    session: {
      context: {
        operator: { display_name: "Fleet operator", roles: ["platform_ops"] },
        permissions: ["platform.edge_fleet.read"],
      },
    },
    navigation: [],
    view: "edge-fleet",
    edgeFleet: platformProjection,
    loading: false,
    error: null,
  });
  assert.match(root.innerHTML, /健康分 86[\s\S]*telemetry[\s\S]*30 s/u);
  assert.doesNotMatch(root.innerHTML, /backlog|CPU low|Memory medium|provider_timeout/u);
  assert.doesNotMatch(root.innerHTML, /workspace_id|tenant_id|model_id|provider_id/u);

  globalThis.fetch = async () => response({
    ...fleetPayload(),
    deployments: [{ ...fleetPayload().deployments[0], tenant_id: TENANT_ID }],
  }, "0");
  await assert.rejects(
    listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "platform.edge_fleet_response_invalid",
  );
});

test("narrow telemetry timing grid spans its odd final item without changing desired state", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(
    css,
    /@media \(max-width: 720px\)[\s\S]*\.private-gateway-telemetry \.private-gateway-state-diff > div:last-child:nth-child\(odd\) \{ grid-column: 1 \/ -1; \}/u,
  );
  assert.doesNotMatch(
    css,
    /(?<!\.private-gateway-telemetry )\.private-gateway-state-diff > div:last-child:nth-child\(odd\)/u,
  );
});
