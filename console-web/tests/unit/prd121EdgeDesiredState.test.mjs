import assert from "node:assert/strict";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import {
  decideEdgeDesiredState,
  getEdgeDesiredState,
  listPlatformEdgeFleet,
  publishEdgeDesiredState,
  rollbackEdgeDesiredState,
} from "../../src/lib/edgeManagement.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const TENANT_ID = "tenant_edge_prd121";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const DESIRED_STATE_ID = `edst_${"3".repeat(32)}`;
const CONFIG_SNAPSHOT_ID = `csn_${"4".repeat(32)}`;
const ALL = [PERMISSIONS.EDGE_DEPLOYMENT_READ, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE];

function context(permissionCodes = ALL) {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "Edge tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_edge_prd121",
      role: "admin",
      email: "edge-prd121@example.test",
    },
    session: { session_id: "ses_edge_prd121", session_revision: 1 },
  }, permissionCodes, {
    permissionRevision: 32,
    authorizationFingerprint: "edge_prd121_authz",
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
    display_name: "PRD-121 gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 2,
    revocation_generation: 1,
    agent_version: "1.0.0",
    health_code: "healthy",
    last_connected_at: "2026-08-26T01:10:00Z",
    certificate_expires_at: "2026-09-02T01:00:00Z",
    identity_generation: 1,
    current_certificate: null,
    recent_events: [],
    created_at: "2026-08-26T00:55:00Z",
    updated_at: "2026-08-26T01:10:00Z",
    revoked_at: null,
  };
}

function desiredState(overrides = {}) {
  return {
    desired_state_id: DESIRED_STATE_ID,
    deployment_id: DEPLOYMENT_ID,
    generation: 2,
    base_generation: 1,
    rollback_of_generation: null,
    source_config_snapshot_id: CONFIG_SNAPSHOT_ID,
    source_generation: 2,
    source_revocation_watermark: 1,
    change_class: "high_risk",
    status: "published",
    revision: 2,
    head_revision: 4,
    requested_by_account_id: "account-requester",
    approved_by_account_id: "account-approver",
    desired_generation: 2,
    applied_generation: 1,
    lkg_generation: 1,
    last_ack_status: "failed",
    last_reason_code: "apply_failed",
    drifted: true,
    requested_at: "2026-08-26T01:00:00Z",
    decided_at: "2026-08-26T01:01:00Z",
    published_at: "2026-08-26T01:01:00Z",
    last_ack_at: "2026-08-26T01:02:00Z",
    updated_at: "2026-08-26T01:02:00Z",
    ...overrides,
  };
}

function pendingState() {
  return desiredState({
    generation: 3,
    base_generation: 2,
    status: "approval_pending",
    revision: 1,
    approved_by_account_id: null,
    decided_at: null,
    published_at: null,
  });
}

function rollbackState() {
  return desiredState({
    generation: 3,
    base_generation: 2,
    rollback_of_generation: 1,
    change_class: "standard",
    head_revision: 5,
    desired_generation: 3,
  });
}

function payload(state, requestId = "req_edge_desired") {
  return {
    state,
    idempotent_replay: false,
    request_id: requestId,
    trace_id: `trace_${requestId}`,
  };
}

function response(value, etag = "4") {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "ETag": `"${etag}"`,
      "X-Request-ID": value.request_id,
      "X-Trace-ID": value.trace_id,
    },
  });
}

test("Workspace desired-state client enforces Cookie, CSRF, ETag, and exact commands", async (t) => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  t.after(() => {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  });
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const replies = [
    response(payload(desiredState()), "4"),
    response(payload(desiredState({
      generation: 3,
      base_generation: 2,
      head_revision: 5,
      desired_generation: 3,
    }), "req_publish"), "5"),
    response(payload(desiredState({
      generation: 3,
      base_generation: 2,
      head_revision: 5,
      desired_generation: 3,
    }), "req_approve"), "5"),
    response(payload(desiredState({ status: "rejected" }), "req_reject"), "4"),
    response(payload(rollbackState(), "req_rollback"), "5"),
  ];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return replies.shift();
  };

  const current = await getEdgeDesiredState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
  });
  assert.equal(current.revision, 4);
  assert.equal(current.state.last_reason_code, "apply_failed");
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);

  await publishEdgeDesiredState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    sourceConfigSnapshotId: CONFIG_SNAPSHOT_ID,
    expectedRevision: 4,
    idempotencyKey: "edge-desired-publish-0001",
  });
  await decideEdgeDesiredState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    action: "approve",
    desiredStateId: DESIRED_STATE_ID,
    reasonCode: "approved_change",
    expectedRevision: 1,
    idempotencyKey: "edge-desired-approve-0001",
  });
  await decideEdgeDesiredState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    action: "reject",
    desiredStateId: DESIRED_STATE_ID,
    reasonCode: "risk_unacceptable",
    expectedRevision: 1,
    idempotencyKey: "edge-desired-reject-00001",
  });
  await rollbackEdgeDesiredState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    lkgGeneration: 1,
    reasonCode: "apply_failed_recovery",
    expectedRevision: 4,
    idempotencyKey: "edge-desired-rollback-0001",
  });

  for (const call of calls.slice(1)) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.headers["X-CSRF-Token"], "workspace-csrf");
    assert.match(call.options.headers["Idempotency-Key"], /^edge-desired-/u);
  }
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: "publish",
    source_config_snapshot_id: CONFIG_SNAPSHOT_ID,
  });
  assert.equal(calls[1].options.headers["If-Match"], '"4"');
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    action: "approve",
    desired_state_id: DESIRED_STATE_ID,
    reason_code: "approved_change",
  });
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    lkg_generation: 1,
    reason_code: "apply_failed_recovery",
  });
});

test("Workspace state rejects ETag drift and any raw manifest field", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async () => response(payload(desiredState()), "3");
  await assert.rejects(
    getEdgeDesiredState({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge.response_invalid",
  );
  globalThis.fetch = async () => response(payload({ ...desiredState(), manifest: {} }), "4");
  await assert.rejects(
    getEdgeDesiredState({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge.response_invalid",
  );
});

test("Workspace state machine renders drift, approves independently, and recovers from LKG", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [{ deploymentId: DEPLOYMENT_ID, response: {
      ...payload(desiredState()),
      revision: 4,
    } }],
  );
  state.finishLoad(load);
  const html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /期望[\s\S]*2[\s\S]*实际[\s\S]*1[\s\S]*LKG[\s\S]*1/u);
  assert.match(html, /apply_failed|从 LKG 恢复/u);
  assert.doesNotMatch(html, /manifest|tenant_id|source_config_snapshot_id/u);

  assert.equal(state.openStateCommand(owner, DEPLOYMENT_ID, "rollback"), true);
  const rollback = state.beginStateCommand(owner, {
    action: "rollback",
    reasonCode: "apply_failed_recovery",
  });
  assert.equal(rollback.expectedRevision, 4);
  assert.deepEqual(rollback.request, {
    lkgGeneration: 1,
    reasonCode: "apply_failed_recovery",
  });
  assert.equal(state.acceptStateCommand(rollback, {
    ...payload(rollbackState(), "req_rollback"),
    revision: 5,
  }, owner), true);
  state.finishStateCommand(rollback);
  assert.equal(state.snapshot().feedback.code, "edge.desired_state_rolled_back");

  const reload = state.beginLoad(owner, 2);
  state.acceptLoad(
    reload,
    { deployments: [deployment()] },
    owner,
    2,
    [{ deploymentId: DEPLOYMENT_ID, response: {
      ...payload(pendingState()),
      revision: 1,
    } }],
  );
  state.finishLoad(reload);
  assert.equal(state.openStateCommand(owner, DEPLOYMENT_ID, "approve"), true);
  const approval = state.beginStateCommand(owner, { action: "approve" });
  assert.equal(approval.request.desiredStateId, DESIRED_STATE_ID);
  assert.equal(approval.request.reasonCode, "approved_change");
  assert.equal(state.rejectStateCommand(approval, { code: "edge.state_conflict" }, owner), true);
  assert.match(state.snapshot().stateDialog.error.message, /不允许此操作/u);
  state.clear();
  assert.equal(approval.controller.signal.aborted, true);
});

test("Platform Fleet accepts only safe desired-state watermarks and fixed failure codes", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const fleet = {
    deployments: [{
      deployment_id: DEPLOYMENT_ID,
      region_code: "cn-east-1",
      mode: "connected",
      status: "active",
      revision: 2,
      revocation_generation: 1,
      agent_version: "1.0.0",
      health_code: "healthy",
      certificate_expires_at: "2026-09-02T01:00:00Z",
      last_connected_at: "2026-08-26T01:10:00Z",
      desired_generation: 2,
      applied_generation: 1,
      lkg_generation: 1,
      desired_state_status: "failed",
      desired_state_reason_code: "apply_failed",
      desired_state_drifted: true,
      updated_at: "2026-08-26T01:10:00Z",
    }],
    request_id: "req_fleet_prd121",
    trace_id: "trace_fleet_prd121",
  };
  globalThis.fetch = async () => response(fleet, "0");
  const result = await listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL });
  assert.equal(result.deployments[0].lkg_generation, 1);
  assert.equal(result.deployments[0].desired_state_reason_code, "apply_failed");

  globalThis.fetch = async () => response({
    ...fleet,
    deployments: [{ ...fleet.deployments[0], workspace_id: WORKSPACE_ID }],
  }, "0");
  await assert.rejects(
    listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "platform.edge_fleet_response_invalid",
  );
});
