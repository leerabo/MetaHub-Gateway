import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPlatformWorkspace } from "../../src/features/platform/renderPlatformPage.js";
import {
  cancelPlatformDiagnostic,
  decideWorkspaceDiagnosticGrant,
  getWorkspaceEdgeHealth,
  listPlatformFleet,
  requestPlatformDiagnosticGrant,
  runPlatformDiagnostic,
} from "../../src/lib/edgeDiagnostics.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const DEPLOYMENT_ID = `edp_${"1".repeat(32)}`;
const OTHER_DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"3".repeat(32)}`;
const GRANT_ID = `dgr_${"4".repeat(32)}`;
const COMMAND_ID = `dcm_${"5".repeat(32)}`;
const RESULT_ID = `drs_${"6".repeat(32)}`;
const SNAPSHOT_ID = `ehs_${"7".repeat(32)}`;
const CURSOR = `cursor_${"a".repeat(32)}`;
const NEXT_CURSOR = `cursor_${"b".repeat(32)}`;
const PLATFORM_CSRF = "P".repeat(43);
const WORKSPACE_CSRF = "W".repeat(43);
const STEP_UP = "S".repeat(43);
const IDEMPOTENCY = "platform-edge-diagnostic-0001";
const NOW = "2026-08-28T01:00:00.000Z";

test("Workspace and Platform readers enforce separate Cookie, no-store, trace, and DTO boundaries", async (t) => {
  const calls = installFetch(t, (url) => new URL(url).pathname === "/platform/fleet"
    ? jsonResponse(fleetPayload())
    : jsonResponse(healthPayload()));
  installCookies(t);

  const workspace = await getWorkspaceEdgeHealth({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
  });
  const platform = await listPlatformFleet({
    controlApiUrl: CONTROL_URL,
    filters: { status: "active", region: "CN-EAST-1", health: "healthy" },
    limit: 25,
  });

  assert.equal(workspace.health.deployment_id, DEPLOYMENT_ID);
  assert.equal(platform.deployments[0].deployment_id, DEPLOYMENT_ID);
  assert.equal(calls[0].url, `${CONTROL_URL}/edge/deployments/${DEPLOYMENT_ID}/health`);
  assert.equal(calls[1].url, `${CONTROL_URL}/platform/fleet?limit=25&status=active&health=healthy&region=cn-east-1`);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
    assert.equal("X-CSRF-Token" in call.options.headers, false);
  }

  for (const forbidden of ["workspace_id", "tenant_id", "display_name"]) {
    globalThis.fetch = async () => jsonResponse(fleetPayload({
      deployments: [{ ...fleetDeployment(), [forbidden]: "must-not-cross-domain" }],
    }));
    await assert.rejects(
      () => listPlatformFleet({ controlApiUrl: CONTROL_URL }),
      (error) => error instanceof PlatformApiError
        && error.code === "platform.edge_fleet_response_invalid",
    );
  }

  globalThis.fetch = async () => jsonResponse(healthPayload({
    health: { ...healthSnapshot(), alert_code: "none" },
  }));
  await assert.rejects(
    () => getWorkspaceEdgeHealth({
      controlApiUrl: CONTROL_URL,
      deploymentId: DEPLOYMENT_ID,
    }),
    (error) => error instanceof AuthApiError && error.code === "edge.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(fleetPayload(), { cacheControl: "private" });
  await assert.rejects(
    () => listPlatformFleet({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "platform.edge_fleet_response_invalid",
  );
});

test("diagnostic writes use domain CSRF, exact bodies, ETags, and bounded parameters", async (t) => {
  const calls = installFetch(t, (_url, options) => {
    const body = JSON.parse(options.body);
    if (body.action === "run" || body.action === "cancel") {
      return jsonResponse(commandResponse({
        command: commandSummary({ status: body.action === "cancel" ? "cancelled" : "authorized" }),
      }), { status: body.action === "run" ? 201 : 200, etag: '"1"' });
    }
    return jsonResponse(grantResponse({
      grant: grantSummary({ status: body.action === "approve" ? "active" : "pending_customer" }),
    }), { status: body.action === "request" ? 201 : 200, etag: '"1"' });
  });
  installCookies(t);

  await decideWorkspaceDiagnosticGrant({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    grantId: GRANT_ID,
    action: "approve",
    reasonCode: "customer_approved",
    ttlSeconds: 300,
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await requestPlatformDiagnosticGrant({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    invocation: {
      command_code: "runtime.watermarks",
      watermark_codes: ["desired_generation", "applied_generation", "lkg_generation"],
    },
    reasonCode: "incident_investigation",
    maxRuns: 2,
    idempotencyKey: IDEMPOTENCY,
  });
  await runPlatformDiagnostic({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    grantId: GRANT_ID,
    deadlineSeconds: 60,
    reasonCode: "approved_diagnostic",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });
  await cancelPlatformDiagnostic({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    commandId: COMMAND_ID,
    reasonCode: "operator_cancelled",
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(calls[0].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[0].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[1].options.headers["X-Step-Up-Grant"], undefined);
  assert.equal(calls[2].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[3].options.headers["X-Step-Up-Grant"], undefined);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    action: "approve",
    diagnostic_grant_id: GRANT_ID,
    reason_code: "customer_approved",
    ttl_seconds: 300,
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: "request",
    invocation: {
      command_code: "runtime.watermarks",
      watermark_codes: ["desired_generation", "applied_generation", "lkg_generation"],
    },
    reason_code: "incident_investigation",
    max_runs: 2,
  });

  const callCount = calls.length;
  await assert.rejects(
    () => requestPlatformDiagnosticGrant({
      controlApiUrl: CONTROL_URL,
      deploymentId: DEPLOYMENT_ID,
      invocation: { command_code: "runtime.version", stdout: "forbidden" },
      reasonCode: "incident_investigation",
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error.code === "platform.edge_fleet_request_invalid",
  );
  assert.equal(calls.length, callCount);

  globalThis.fetch = async () => jsonResponse(grantResponse({
    grant: { ...grantSummary(), workspace_id: WORKSPACE_ID },
  }), { etag: '"1"' });
  await assert.rejects(
    () => requestPlatformDiagnosticGrant({
      controlApiUrl: CONTROL_URL,
      deploymentId: DEPLOYMENT_ID,
      invocation: { command_code: "runtime.version" },
      reasonCode: "incident_investigation",
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof PlatformApiError
      && error.code === "platform.edge_fleet_response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(healthPayload({
    diagnostic_results: [diagnosticResult({
      result: { ...runtimeVersionResult(), stdout: "forbidden" },
    })],
  }));
  await assert.rejects(
    () => getWorkspaceEdgeHealth({
      controlApiUrl: CONTROL_URL,
      deploymentId: DEPLOYMENT_ID,
    }),
    (error) => error instanceof AuthApiError && error.code === "edge.response_invalid",
  );
});

test("Workspace diagnostic owner drift aborts the ticket and rejects late decisions", () => {
  const firstContext = workspaceContext("authz-a");
  const firstOwner = privateGatewayPageOwner(firstContext, workspaceShell(firstContext));
  const state = createPrivateGatewayPageState();
  assert.equal(state.enter(firstOwner), true);
  acceptWorkspaceHealth(state, firstOwner, healthPayload({
    diagnostic_grants: [grantSummary()],
  }));
  assert.equal(state.openDiagnosticDecision(firstOwner, DEPLOYMENT_ID, GRANT_ID, "approve"), true);
  const operation = state.beginDiagnosticDecision(firstOwner, 300);
  assert.ok(operation);

  const secondContext = workspaceContext("authz-b");
  const secondOwner = privateGatewayPageOwner(secondContext, workspaceShell(secondContext));
  assert.equal(state.enter(secondOwner), true);
  assert.equal(operation.controller.signal.aborted, true);
  assert.equal(state.acceptDiagnosticDecision(operation, grantResponse({
    grant: grantSummary({ status: "active" }),
  }), secondOwner), false);
  assert.equal(state.snapshot().healthStates[DEPLOYMENT_ID], undefined);
});

test("Platform Fleet normalizes filters and preserves keyset previous/next history", async (t) => {
  installWindow(t, "#edge-fleet");
  const reads = [];
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi({
      edgeFleet: async (input) => {
        reads.push({ filters: { ...input.filters }, cursor: input.cursor });
        return fleetPayload({
          next_cursor: reads.length === 1 ? CURSOR : reads.length === 2 ? NEXT_CURSOR : null,
        });
      },
    }),
    newIdempotencyKey: () => IDEMPOTENCY,
  });

  assert.equal(await controller.mount(), true);
  assert.equal(await controller.filterEdgeFleet({
    status: "active", region: " CN-EAST-1 ", health: "degraded",
  }), true);
  assert.equal(await controller.nextEdgeFleetPage(), true);
  assert.equal(await controller.previousEdgeFleetPage(), true);
  assert.deepEqual(reads, [
    { filters: { status: "", region: "", health: "" }, cursor: null },
    { filters: { status: "active", region: "cn-east-1", health: "degraded" }, cursor: null },
    { filters: { status: "active", region: "cn-east-1", health: "degraded" }, cursor: NEXT_CURSOR },
    { filters: { status: "active", region: "cn-east-1", health: "degraded" }, cursor: null },
  ]);
  assert.match(root.innerHTML, /data-platform-edge-fleet-filter-form/u);
  assert.match(root.innerHTML, /data-platform-action="edge-fleet-previous"/u);
  controller.destroy();
});

test("Platform run keeps global step-up values out of DOM, URL, Storage, and logs", async (t) => {
  installWindow(t, "#edge-fleet");
  installStorageGuards(t);
  const secretCode = "482913";
  const calls = [];
  const logs = [];
  const originalLog = console.log;
  console.log = (...values) => { logs.push(values.join(" ")); };
  t.after(() => { console.log = originalLog; });
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi({
      createIdentityStepUpGrant: async (input) => {
        calls.push({ lane: "step-up", ...input });
        return { grant: STEP_UP };
      },
      runEdgeDiagnostic: async (input) => {
        calls.push({ lane: "run", ...input });
        return commandResponse();
      },
    }),
    newIdempotencyKey: () => IDEMPOTENCY,
  });

  assert.equal(await controller.mount(), true);
  assert.equal(await controller.runEdgeDiagnostic({
    deploymentId: DEPLOYMENT_ID,
    grantId: GRANT_ID,
    deadlineSeconds: 60,
    reasonCode: "approved_diagnostic",
    method: "totp",
    verificationCode: secretCode,
    confirmed: true,
  }), true);
  assert.equal(calls[0].workspaceId, undefined);
  assert.equal(calls[0].actionCode, "edge.diagnostic.run");
  assert.equal(calls[0].resourceType, "edge_deployment");
  assert.equal(calls[0].resourceId, DEPLOYMENT_ID);
  assert.equal(calls[0].code, secretCode);
  assert.equal(calls[1].stepUpGrant, STEP_UP);
  assert.doesNotMatch(root.innerHTML, new RegExp(`${secretCode}|${STEP_UP}`, "u"));
  assert.doesNotMatch(globalThis.window.location.href, new RegExp(`${secretCode}|${STEP_UP}`, "u"));
  assert.doesNotMatch(logs.join("\n"), new RegExp(`${secretCode}|${STEP_UP}`, "u"));
  controller.destroy();
});

test("Platform WebAuthn response is discarded after owner generation changes", async (t) => {
  installWindow(t, "#edge-fleet");
  let releaseAssertion;
  let stepUps = 0;
  let runs = 0;
  const controller = createPlatformPageController({
    root: fakeRoot(),
    api: platformApi({
      beginIdentityStepUp: async () => ({
        challenge_id: `mfc_${"8".repeat(32)}`,
        public_key_options: {},
      }),
      getWebAuthnAssertion: async () => new Promise((resolve) => {
        releaseAssertion = () => resolve({ id: "credential" });
      }),
      createIdentityStepUpGrant: async () => {
        stepUps += 1;
        return { grant: STEP_UP };
      },
      runEdgeDiagnostic: async () => {
        runs += 1;
        return commandResponse();
      },
    }),
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  await controller.mount();
  const pending = controller.runEdgeDiagnostic({
    deploymentId: DEPLOYMENT_ID,
    grantId: GRANT_ID,
    deadlineSeconds: 60,
    reasonCode: "approved_diagnostic",
    method: "webauthn",
    verificationCode: "",
    confirmed: true,
  });
  while (!releaseAssertion) await Promise.resolve();
  controller.selectView("overview");
  releaseAssertion();
  assert.equal(await pending, false);
  assert.equal(stepUps, 0);
  assert.equal(runs, 0);
  controller.destroy();
});

test("Platform rejects unavailable WebAuthn before entering the loading state", async (t) => {
  installWindow(t, "#edge-fleet");
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi(),
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  await controller.mount();

  assert.equal(await controller.runEdgeDiagnostic({
    deploymentId: DEPLOYMENT_ID,
    grantId: GRANT_ID,
    deadlineSeconds: 60,
    reasonCode: "approved_diagnostic",
    method: "webauthn",
    verificationCode: "",
    confirmed: true,
  }), false);
  assert.doesNotMatch(root.innerHTML, /name="verificationCode"[^>]*disabled/u);
  controller.destroy();
});

test("Workspace and Platform stores clear independently and render stable desktop/narrow controls", async (t) => {
  installWindow(t, "#edge-fleet");
  const workspaceState = createPrivateGatewayPageState();
  const context = workspaceContext("authz-isolated");
  const owner = privateGatewayPageOwner(context, workspaceShell(context));
  workspaceState.enter(owner);
  acceptWorkspaceHealth(workspaceState, owner, healthPayload({
    diagnostic_grants: [grantSummary()],
  }));

  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi(),
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  await controller.mount();
  workspaceState.clear();
  assert.match(root.innerHTML, /Private Gateway fleet/u);
  acceptWorkspaceHealth(workspaceState, owner, healthPayload());
  controller.destroy();
  assert.equal(workspaceState.snapshot().healthStates[DEPLOYMENT_ID].health.health_score, 92);

  const workspaceHtml = renderPrivateGateways(
    workspaceState.snapshot(), context, workspaceShell(context),
  );
  assert.match(workspaceHtml, /edge-health-score/u);
  assert.match(workspaceHtml, /edge-health-signals/u);
  assert.doesNotMatch(workspaceHtml, new RegExp(STEP_UP, "u"));

  const platformRoot = fakeRoot();
  renderPlatformWorkspace(platformRoot, {
    loading: false,
    session: platformSession(),
    navigation: platformNavigation().items,
    edgeFleet: fleetPayload(),
    edgeFleetFilters: { status: "", region: "", health: "" },
    edgeFleetCursor: null,
    edgeFleetCursorHistory: [],
    edgeDiagnosticIntent: null,
    edgeDiagnosticGrant: grantSummary(),
    edgeDiagnosticCommand: commandSummary(),
    view: "edge-fleet",
    error: null,
    feedback: null,
    pendingAction: null,
  });
  assert.match(platformRoot.innerHTML, /data-platform-edge-diagnostic-request-form/u);
  assert.match(platformRoot.innerHTML, /runtime\.connectivity_summary/u);
  assert.match(platformRoot.innerHTML, /data-platform-edge-diagnostic-run-form/u);
  assert.doesNotMatch(platformRoot.innerHTML, /workspace_id|tenant_id|display_name/u);

  const [platformCss, sharedCss] = await Promise.all([
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/edge-operations.css", import.meta.url), "utf8"),
  ]);
  assert.match(platformCss, /\.platform-edge-diagnostic-forms[\s\S]*grid-template-columns: repeat\(2/u);
  assert.match(platformCss, /@media \(max-width: 760px\)[\s\S]*\.platform-edge-diagnostic-forms/u);
  assert.match(sharedCss, /\.edge-health-score/u);
  assert.match(sharedCss, /@media \(max-width: 420px\)/u);
});

function healthSnapshot(overrides = {}) {
  return {
    edge_health_snapshot_id: SNAPSHOT_ID,
    deployment_id: DEPLOYMENT_ID,
    generation: 3,
    score_model_version: "edge-health-v1",
    health_score: 92,
    health_code: "healthy",
    certificate_signal: "healthy",
    version_signal: "current",
    capacity_signal: "healthy",
    config_signal: "converged",
    telemetry_signal: "current",
    validation_signal: "passed",
    desired_generation: 4,
    applied_generation: 4,
    lkg_generation: 3,
    telemetry_lag_seconds: 5,
    certificate_expires_at: "2026-09-28T01:00:00.000Z",
    agent_version: "1.0.0",
    observed_at: NOW,
    created_at: NOW,
    ...overrides,
  };
}

function healthPayload(overrides = {}) {
  return {
    health: healthSnapshot(),
    diagnostic_grants: [],
    diagnostic_results: [],
    request_id: "req_health",
    trace_id: "trc_health",
    ...overrides,
  };
}

function fleetDeployment(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    health_code: "healthy",
    health_score: 92,
    alert_code: "none",
    maintenance_mode: false,
    agent_version: "1.0.0",
    desired_generation: 4,
    applied_generation: 4,
    lkg_generation: 3,
    telemetry_lag_seconds: 5,
    certificate_signal: "healthy",
    source_health_generation: 3,
    index_revision: 7,
    updated_at: NOW,
    ...overrides,
  };
}

function fleetPayload(overrides = {}) {
  const deployments = overrides.deployments ?? [fleetDeployment()];
  return {
    deployments,
    next_cursor: null,
    count: deployments.length,
    request_id: "req_fleet",
    trace_id: "trc_fleet",
    ...overrides,
  };
}

function grantSummary(overrides = {}) {
  return {
    diagnostic_grant_id: GRANT_ID,
    deployment_id: DEPLOYMENT_ID,
    status: "pending_customer",
    command_code: "runtime.version",
    invocation: { command_code: "runtime.version" },
    parameters_sha256: "a".repeat(64),
    reason_code: "incident_investigation",
    max_runs: 1,
    run_count: 0,
    revision: 1,
    requested_at: NOW,
    approved_at: null,
    expires_at: null,
    revoked_at: null,
    updated_at: NOW,
    ...overrides,
  };
}

function grantResponse(overrides = {}) {
  return {
    grant: grantSummary(),
    idempotent_replay: false,
    request_id: "req_grant",
    trace_id: "trc_grant",
    ...overrides,
  };
}

function commandSummary(overrides = {}) {
  return {
    diagnostic_command_id: COMMAND_ID,
    diagnostic_grant_id: GRANT_ID,
    deployment_id: DEPLOYMENT_ID,
    generation: 1,
    status: "authorized",
    command_code: "runtime.version",
    envelope_sha256: "b".repeat(64),
    revision: 1,
    issued_at: NOW,
    deadline_at: "2026-08-28T01:01:00.000Z",
    expires_at: "2026-08-28T01:05:00.000Z",
    lease_expires_at: null,
    started_at: null,
    completed_at: null,
    updated_at: NOW,
    ...overrides,
  };
}

function commandResponse(overrides = {}) {
  return {
    command: commandSummary(),
    idempotent_replay: false,
    request_id: "req_command",
    trace_id: "trc_command",
    ...overrides,
  };
}

function runtimeVersionResult() {
  return {
    command_code: "runtime.version",
    agent_version: "1.0.0",
    gateway_schema_version: 118,
    os_family: "linux",
    architecture: "x86_64",
  };
}

function diagnosticResult(overrides = {}) {
  return {
    diagnostic_result_id: RESULT_ID,
    diagnostic_grant_id: GRANT_ID,
    diagnostic_command_id: COMMAND_ID,
    deployment_id: DEPLOYMENT_ID,
    generation: 1,
    status: "completed",
    reason_code: "completed",
    result: runtimeVersionResult(),
    report_sha256: "c".repeat(64),
    result_sha256: "d".repeat(64),
    duration_ms: 10,
    started_at: NOW,
    completed_at: "2026-08-28T01:00:00.010Z",
    received_at: "2026-08-28T01:00:00.020Z",
    ...overrides,
  };
}

function workspaceContext(fingerprint) {
  return withPermissionSnapshot({
    tenant: { tenant_id: "tenant-prd126", name: "PRD-126" },
    user: {
      tenant_id: "tenant-prd126",
      user_id: "usr-prd126",
      role: "admin",
      email: "prd126@example.test",
    },
    session: { session_id: "ses-prd126", session_revision: 1 },
  }, [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_HEALTH_READ,
    PERMISSIONS.EDGE_DIAGNOSTIC_READ,
    PERMISSIONS.EDGE_DIAGNOSTIC_APPROVE,
  ], { permissionRevision: 126, authorizationFingerprint: fingerprint });
}

function workspaceShell(context) {
  return {
    state: "ok",
    activeNavigationItemId: "private-gateways",
    navigation: {
      active_navigation_item_id: "private-gateways",
      permission_codes: context.permission_codes,
      permission_revision: context.permission_revision,
      authorization_fingerprint: context.authorization_fingerprint,
      navigation_items: [{
        id: "private-gateways",
        target_section: "private-gateways",
        label: "Private Gateway",
        group: "configuration",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
      }],
    },
  };
}

function acceptWorkspaceHealth(state, owner, response) {
  assert.equal(state.enter(owner), true);
  const operation = state.beginLoad(owner, 1);
  assert.ok(operation);
  assert.equal(state.acceptLoad(
    operation,
    { deployments: [{
      deployment_id: DEPLOYMENT_ID,
      workspace_id: WORKSPACE_ID,
      display_name: "Gateway",
      region_code: "cn-east-1",
      mode: "connected",
      status: "active",
      revision: 1,
      revocation_generation: 1,
      recent_events: [],
    }], request_id: "req_list", trace_id: "trc_list" },
    owner,
    1,
    [],
    [],
    null,
    null,
    [{ deploymentId: DEPLOYMENT_ID, response }],
  ), true);
  state.finishLoad(operation);
}

function platformSession() {
  return {
    context: {
      operator: {
        operator_id: "pto_prd126",
        display_name: "Platform Ops",
        status: "active",
        roles: ["platform_ops"],
        revision: 1,
      },
      permissions: ["platform.edge_fleet.read", "platform.edge_fleet.manage"],
      expires_at: "2026-08-28T12:00:00.000Z",
    },
    request_id: "req_session",
    trace_id: "trc_session",
  };
}

function platformNavigation() {
  return {
    items: [{
      id: "edge-fleet",
      label: "Private Gateway fleet",
      href: "/platform.html#edge-fleet",
    }],
  };
}

function platformApi(overrides = {}) {
  return {
    session: async () => platformSession(),
    navigation: async () => platformNavigation(),
    edgeFleet: async () => fleetPayload(),
    requestEdgeDiagnosticGrant: async () => grantResponse(),
    runEdgeDiagnostic: async () => commandResponse(),
    cancelEdgeDiagnostic: async () => commandResponse({
      command: commandSummary({ status: "cancelled" }),
    }),
    createIdentityStepUpGrant: async () => ({ grant: STEP_UP }),
    ...overrides,
  };
}

function jsonResponse(payload, {
  status = 200,
  etag,
  cacheControl = "no-store, private",
} = {}) {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": cacheControl,
    "X-Request-ID": payload.request_id,
    "X-Trace-ID": payload.trace_id,
  };
  if (etag) headers.ETag = etag;
  return new Response(JSON.stringify(payload), { status, headers });
}

function installFetch(t, handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(url, options);
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

function installCookies(t) {
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
    },
  });
  t.after(() => {
    restoreProperty("document", documentDescriptor);
  });
}

function installWindow(t, hash) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  let currentHash = hash;
  const location = {
    href: `http://localhost:5173/platform.html${hash}`,
    get hash() { return currentHash; },
  };
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location,
      history: {
        replaceState(_state, _title, nextHash) {
          currentHash = String(nextHash);
          location.href = `http://localhost:5173/platform.html${currentHash}`;
        },
      },
    },
  });
  t.after(() => restoreProperty("window", descriptor));
}

function installStorageGuards(t) {
  for (const name of ["localStorage", "sessionStorage"]) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() { throw new Error(`${name} must not be accessed`); },
    });
    t.after(() => restoreProperty(name, descriptor));
  }
}

function fakeRoot() {
  let html = "";
  return {
    get innerHTML() { return html; },
    set innerHTML(value) { html = String(value); },
    addEventListener() {},
    querySelectorAll() { return []; },
    replaceChildren() { html = ""; },
  };
}

function restoreProperty(name, descriptor) {
  if (descriptor) Object.defineProperty(globalThis, name, descriptor);
  else delete globalThis[name];
}
