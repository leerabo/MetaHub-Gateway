import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createEdgeRegistration,
  listEdgeDeployments,
  listPlatformEdgeFleet,
  revokeEdgeDeployment,
  workspaceCookieApiOrigin,
} from "../../src/lib/edgeManagement.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";

const CONTROL_URL = "http://control.local";
const TENANT_ID = "tenant_edge_a";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const CERTIFICATE_ID = `edc_${"3".repeat(32)}`;
const IDENTITY_ID = `edi_${"4".repeat(32)}`;
const EVENT_ID = `ege_${"5".repeat(32)}`;
const INTENT_ID = `eri_${"6".repeat(32)}`;
const TOKEN = `erg_${"A".repeat(43)}`;
const CHALLENGE = `ech_${"B".repeat(43)}`;
const ALL = [PERMISSIONS.EDGE_DEPLOYMENT_READ, PERMISSIONS.EDGE_DEPLOYMENT_MANAGE];

function currentWorkspaceShell(permissionCodes = ALL) {
  const identity = { account: { status: "active" }, current_session: {
    session_id: "workspace_edge_session", is_current: true, revoke_generation: 1,
    expires_at: "2099-01-01T00:00:00Z",
  } };
  const context = { workspace_id: WORKSPACE_ID, name: "Edge workspace", kind: "organization",
    slug: "edge", role: "admin", session_status: "active", expires_at: "2099-01-01T00:00:00Z" };
  const authorization = { permission_codes: [...permissionCodes].sort(), permission_revision: 4,
    membership_revision: 2, permission_fingerprint: "c".repeat(64) };
  const owner = workspaceNavigationOwner(identity, { context, authorization });
  return { state: "ok", workspaceNavigationContract: true, workspaceIdentity: identity,
    workspaceContext: context, workspaceAuthorization: authorization, workspaceOwner: owner,
    activeNavigationItemId: "private-gateways", activeRouteCode: "workspace.gateway.private-deployments",
    navigation: { ...authorization, active_navigation_item_id: "private-gateways",
      active_route_code: "workspace.gateway.private-deployments", navigation_items: [{
        id: "private-gateways", route_code: "workspace.gateway.private-deployments",
        target_section: "private-gateways", visible: true, disabled: false,
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
      }] },
  };
}

test("current Workspace Device owner uses workspace authorization without legacy tenant/user mirrors", () => {
  const shell = currentWorkspaceShell();
  const owner = privateGatewayPageOwner(shell.workspaceAuthorization, shell);
  assert.equal(owner.workspaceId, WORKSPACE_ID);
  assert.equal(owner.tenantId, "");
  assert.equal(owner.accountId, "");
  assert.equal(owner.canManage, true);
  const state = createPrivateGatewayPageState();
  assert.equal(state.enter(owner), true);
  const load = state.beginLoad(owner, 1);
  assert.ok(load);
  assert.equal(state.acceptLoad(load, {
    deployments: [], request_id: "req_workspace_edge", trace_id: "trc_workspace_edge",
  }, owner, 1, [], [], null, null, [], [], [], [], []), true);
  state.finishLoad(load);
  assert.match(renderPrivateGateways(state.snapshot(), shell.workspaceAuthorization, shell), /私有 Gateway/u);
  assert.match(renderPrivateGateways(state.snapshot(), shell.workspaceAuthorization, shell), /使用右上角“创建部署”/u);
});

test("current Workspace Device owner fails closed on route, permission and session changes", () => {
  const shell = currentWorkspaceShell();
  assert.ok(privateGatewayPageOwner(shell.workspaceAuthorization, shell));
  const noRead = currentWorkspaceShell([PERMISSIONS.EDGE_DEPLOYMENT_MANAGE]);
  assert.equal(privateGatewayPageOwner(noRead.workspaceAuthorization, noRead), null);
  const wrongRoute = currentWorkspaceShell();
  wrongRoute.activeNavigationItemId = "overview";
  assert.equal(privateGatewayPageOwner(wrongRoute.workspaceAuthorization, wrongRoute), null);
  const revoked = currentWorkspaceShell();
  revoked.workspaceContext.session_status = "revoked";
  assert.equal(privateGatewayPageOwner(revoked.workspaceAuthorization, revoked), null);
});

function context(permissionCodes = ALL, fingerprint = "edge_authz_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "Edge tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_edge_a",
      role: "admin",
      email: "edge@example.test",
    },
    session: { session_id: "ses_edge_a", session_revision: 1 },
  }, permissionCodes, { permissionRevision: 31, authorizationFingerprint: fingerprint });
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

function certificate() {
  return {
    certificate_id: CERTIFICATE_ID,
    identity_id: IDENTITY_ID,
    generation: 1,
    status: "active",
    certificate_sha256: "a".repeat(64),
    public_key_sha256: "b".repeat(64),
    issuer_key_id: "edge-device-ca-v1",
    not_before: "2026-08-26T01:00:00Z",
    not_after: "2026-09-02T01:00:00Z",
    drain_until: null,
  };
}

function deployment(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    workspace_id: WORKSPACE_ID,
    display_name: "Shanghai private gateway",
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
    current_certificate: certificate(),
    recent_events: [{
      event_id: EVENT_ID,
      event_type: "edge.registered",
      generation: 1,
      outcome_code: "succeeded",
      reason_code: "registered",
      occurred_at: "2026-08-26T01:00:00Z",
    }],
    created_at: "2026-08-26T00:55:00Z",
    updated_at: "2026-08-26T01:10:00Z",
    revoked_at: null,
    ...overrides,
  };
}

function listPayload(overrides = {}) {
  return { deployments: [deployment()], request_id: "req_edge_list", trace_id: "trc_edge_list", ...overrides };
}

function registrationPayload(overrides = {}) {
  return {
    deployment: deployment({ status: "pending", revision: 1, agent_version: null, health_code: "not_registered", last_connected_at: null, certificate_expires_at: null, identity_generation: null, current_certificate: null, recent_events: [] }),
    registration_intent_id: INTENT_ID,
    registration_token: TOKEN,
    challenge: CHALLENGE,
    expires_at: "2026-08-26T01:10:00Z",
    idempotent_replay: false,
    request_id: "req_edge_create",
    trace_id: "trc_edge_create",
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
      agent_version: "1.0.0",
      health_code: "healthy",
      certificate_expires_at: "2026-09-02T01:00:00Z",
      last_connected_at: "2026-08-26T01:10:00Z",
      desired_generation: 0,
      applied_generation: 0,
      lkg_generation: 0,
      desired_state_status: "not_published",
      desired_state_reason_code: "not_published",
      desired_state_drifted: false,
      telemetry_last_receipt_at: null,
      telemetry_lag_seconds: null,
      telemetry_backlog_seconds: null,
      telemetry_cpu_bucket: "unknown",
      telemetry_memory_bucket: "unknown",
      telemetry_error_rate_basis_points: 0,
      telemetry_failure_code: "none",
      updated_at: "2026-08-26T01:10:00Z",
    }],
    request_id: "req_edge_fleet",
    trace_id: "trc_edge_fleet",
    ...overrides,
  };
}

function response(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
      ...extraHeaders,
    },
  });
}

test("PRD-120 owner requires exact Edge read navigation and projects manage independently", () => {
  const manager = context();
  assert.equal(privateGatewayPageOwner(manager, shell(manager)).canManage, true);
  const reader = context([PERMISSIONS.EDGE_DEPLOYMENT_READ], "edge_reader");
  assert.equal(privateGatewayPageOwner(reader, shell(reader)).canManage, false);
  const manageOnly = context([PERMISSIONS.EDGE_DEPLOYMENT_MANAGE], "edge_manage_only");
  assert.equal(privateGatewayPageOwner(manageOnly, shell(manageOnly)), null);
  assert.equal(privateGatewayPageOwner(manager, {
    ...shell(manager), navigation: { ...shell(manager).navigation, permission_revision: 30 },
  }), null);
});

test("Workspace list client uses Cookie-only no-store boundary and rejects header or DTO drift", async (t) => {
  const calls = installFetch(() => response(listPayload()));
  t.after(restoreGlobals);
  const result = await listEdgeDeployments({ controlApiUrl: CONTROL_URL });
  assert.equal(result.deployments[0].workspace_id, WORKSPACE_ID);
  assert.equal(calls[0].url, `${CONTROL_URL}/edge/deployments`);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal("Authorization" in calls[0].options.headers, false);
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);

  globalThis.fetch = async () => response(listPayload(), 200, { "X-Request-ID": "req_drift" });
  await assert.rejects(listEdgeDeployments({ controlApiUrl: CONTROL_URL }), (error) => error instanceof AuthApiError && error.code === "edge.response_invalid");
  globalThis.fetch = async () => response(listPayload({ deployments: [{ ...deployment(), tenant_id: TENANT_ID }] }));
  await assert.rejects(listEdgeDeployments({ controlApiUrl: CONTROL_URL }), (error) => error.code === "edge.response_invalid");
});

test("Workspace Cookie API keeps the current loopback host without changing legacy API traffic", () => {
  assert.equal(
    workspaceCookieApiOrigin("http://127.0.0.1:8001", {
      hostname: "localhost", origin: "http://localhost:5173",
    }),
    "http://localhost:8001",
  );
  assert.equal(
    workspaceCookieApiOrigin("https://control.example.test", {
      hostname: "console.example.test", origin: "https://console.example.test",
    }),
    "https://control.example.test",
  );
  assert.throws(
    () => workspaceCookieApiOrigin("http://user:password@localhost:8001"),
    (error) => error instanceof AuthApiError && error.code === "workspace.configuration_invalid",
  );
});

test("Workspace create and revoke emit exact CSRF, idempotency, CAS, and bodies", async (t) => {
  const calls = installFetch((url) => String(url).endsWith("/revoke")
    ? response({
        deployment: deployment({ status: "revoked", health_code: "revoked", revision: 3, revocation_generation: 2, revoked_at: "2026-08-26T01:20:00Z", current_certificate: null, certificate_expires_at: null }),
        revoked_session_count: 1,
        idempotent_replay: false,
        request_id: "req_edge_revoke",
        trace_id: "trc_edge_revoke",
      })
    : response(registrationPayload(), 201));
  t.after(restoreGlobals);
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const created = await createEdgeRegistration({
    controlApiUrl: CONTROL_URL,
    displayName: "Shanghai private gateway",
    regionCode: "cn-east-1",
    mode: "connected",
    tokenTtlSeconds: 600,
    idempotencyKey: "edge-registration-0001",
  });
  assert.equal(created.registration_token, TOKEN);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    display_name: "Shanghai private gateway", region_code: "cn-east-1", mode: "connected", token_ttl_seconds: 600,
  });
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[0].options.headers["Idempotency-Key"], "edge-registration-0001");

  await revokeEdgeDeployment({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    revision: 2,
    reasonCode: "device_lost",
    idempotencyKey: "edge-revoke-command-0001",
  });
  assert.equal(calls[1].options.headers["If-Match"], '"2"');
  assert.deepEqual(JSON.parse(calls[1].options.body), { reason_code: "device_lost" });
});

test("Platform Fleet client sends no Workspace CSRF and rejects tenant-private projections", async (t) => {
  const calls = installFetch(() => response(fleetPayload()));
  t.after(restoreGlobals);
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=must-not-be-read" };
  const fleet = await listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL });
  assert.equal(fleet.deployments[0].deployment_id, DEPLOYMENT_ID);
  assert.equal(calls[0].url, `${CONTROL_URL}/platform/fleet/edge-deployments`);
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);
  assert.equal("Authorization" in calls[0].options.headers, false);

  globalThis.fetch = async () => response(fleetPayload({ deployments: [{ ...fleetPayload().deployments[0], workspace_id: WORKSPACE_ID }] }));
  await assert.rejects(listPlatformEdgeFleet({ controlApiUrl: CONTROL_URL }), (error) => error instanceof PlatformApiError && error.code === "platform.edge_fleet_response_invalid");
});

test("Platform direct Fleet hash survives session refresh and ignores unrelated Status failures", async (t) => {
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  let currentHash = "#edge-fleet";
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: {
        get hash() { return currentHash; },
      },
      history: {
        replaceState(_state, _title, hash) { currentHash = String(hash); },
      },
    },
  });
  t.after(() => restoreProperty("window", windowDescriptor));

  let permissions = [
    "platform.edge_fleet.read",
    "platform.infrastructure.read",
    "platform.status.read",
  ];
  let fleetReads = 0;
  let unrelatedReads = 0;
  let statusReads = 0;
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: {
      session: async () => ({
        context: {
          operator: {
            operator_id: "pto_edge_owner",
            display_name: "Edge owner",
            status: "active",
            roles: ["platform_owner"],
            revision: 1,
          },
          permissions,
          expires_at: "2026-08-26T12:00:00Z",
        },
      }),
      navigation: async () => ({
        items: [
          { id: "edge-fleet", label: "Private Gateway fleet", href: "/platform.html#edge-fleet" },
          { id: "status-incidents", label: "Status", href: "/platform.html#status-incidents" },
        ].filter((item) => item.id !== "edge-fleet"
          || permissions.includes("platform.edge_fleet.read")),
      }),
      edgeFleet: async () => {
        fleetReads += 1;
        return fleetPayload();
      },
      infrastructureStatus: async () => {
        unrelatedReads += 1;
        throw new PlatformApiError({
          status: 503,
          code: "platform.unavailable",
          message: "Platform authorization is temporarily unavailable.",
        });
      },
      statusIncidents: async () => {
        statusReads += 1;
        throw new PlatformApiError({
          status: 503,
          code: "status.unavailable",
          message: "Status communication request failed.",
        });
      },
    },
  });

  assert.equal(await controller.mount(), true);
  assert.equal(currentHash, "#edge-fleet");
  assert.equal(fleetReads, 1);
  assert.equal(unrelatedReads, 0);
  assert.equal(statusReads, 0);
  assert.match(root.innerHTML, /Private Gateway fleet/u);
  assert.doesNotMatch(root.innerHTML, /workspace_id|tenant_id|certificate_id/u);
  assert.doesNotMatch(root.innerHTML, /01\/01/u);

  permissions = ["platform.status.read"];
  assert.equal(await controller.refresh(), false);
  assert.equal(fleetReads, 1);
  assert.equal(statusReads, 1);
  assert.match(root.innerHTML, /class="platform-shell"/u);
  assert.doesNotMatch(root.innerHTML, /Private Gateway fleet/u);
  assert.doesNotMatch(root.innerHTML, /edp_[a-z0-9]+/u);
  controller.destroy();
});

test("state keeps one-use material only until successful copy, close, refresh, or owner loss", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  let clears = 0;
  const state = createPrivateGatewayPageState({ clearDom: () => { clears += 1; } });
  assert.equal(state.enter(owner), true);
  const load = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(load, listPayload(), owner, 1), true);
  state.finishLoad(load);
  assert.equal(state.openCreate(owner), true);
  const create = state.beginCreate(owner);
  assert.equal(state.acceptCreate(create, registrationPayload(), owner), true);
  state.finishCreate(create);
  assert.match(JSON.stringify(state.snapshot()), new RegExp(TOKEN, "u"));
  const copy = state.beginCopy(owner);
  assert.match(copy.text, new RegExp(CHALLENGE, "u"));
  assert.equal(state.acceptCopy(copy, owner), true);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), new RegExp(`${TOKEN}|${CHALLENGE}`, "u"));

  state.openCreate(owner);
  const createAgain = state.beginCreate(owner);
  state.acceptCreate(createAgain, registrationPayload(), owner);
  state.finishCreate(createAgain);
  const refresh = state.beginLoad(owner, 2);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), new RegExp(`${TOKEN}|${CHALLENGE}`, "u"));
  const reduced = context([], "edge_revoked");
  state.enter(privateGatewayPageOwner(reduced, shell(reduced)));
  assert.equal(refresh.controller.signal.aborted, true);
  assert.ok(clears >= 1);
});

test("renderer escapes display text and wiring avoids Storage while Platform loads Fleet only on active view", async () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  state.acceptLoad(load, listPayload({ deployments: [deployment({ display_name: '<img src=x onerror="alert(1)">' })] }), owner, 1);
  state.finishLoad(load);
  const html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /data-private-gateway-root/u);
  assert.doesNotMatch(html, /<img src=x/u);
  assert.doesNotMatch(html, /tenant_id|registration_token|challenge/u);

  const sources = await Promise.all([
    "../../src/lib/edgeManagement.js",
    "../../src/features/private-gateways/privateGatewayPageState.js",
    "../../src/features/private-gateways/bindPrivateGateways.js",
    "../../src/app/main.js",
    "../../src/features/platform/platformPageState.js",
  ].map((path) => readFile(new URL(path, import.meta.url), "utf8")));
  assert.doesNotMatch(sources.slice(0, 3).join("\n"), /localStorage|sessionStorage/u);
  assert.match(sources[3], /loadPrivateGatewaysState\(consoleShellState, refreshId\)/u);
  assert.match(sources[3], /privateGatewayPageState\.clear\(\)/u);
  assert.match(sources[4], /state\.view === "edge-fleet"[\s\S]{0,160}api\.edgeFleet/u);
  assert.match(sources[4], /state\.view === "edge-fleet"[\s\S]{0,120}state\.edgeFleet = null/u);
});

test("renderer keeps Private Gateway first-use facts separate from low-frequency operations", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  state.acceptLoad(load, listPayload(), owner, 1);
  state.finishLoad(load);

  const html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /data-private-gateway-operation-group="runtime"/u);
  assert.match(html, /data-private-gateway-operation-group="maintenance"/u);
  assert.match(html, /运行与交付/u);
  assert.match(html, /维护与治理/u);
  assert.doesNotMatch(html, /data-private-gateway-operation-group="runtime" open/u);
  assert.doesNotMatch(html, /data-private-gateway-operation-group="maintenance" open/u);
  assert.match(html, /data-private-gateway-revoke/u);
});

test("installer keeps the active configuration visible while requirements remain progressive", async () => {
  const source = await readFile(
    new URL("../../src/features/private-gateways/renderPrivateGateways.js", import.meta.url),
    "utf8",
  );
  assert.match(source, /data-private-gateway-installer-environment/u);
  assert.match(source, /data-private-gateway-installer-requirements/u);
  assert.match(source, /<details class="private-gateway-installer-requirements"/u);
  assert.doesNotMatch(source, /private-gateway-installer-requirements" open/u);
});

let previousFetch;
let previousDocument;

function installFetch(responder) {
  previousFetch = globalThis.fetch;
  previousDocument = globalThis.document;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function restoreGlobals() {
  globalThis.fetch = previousFetch;
  globalThis.document = previousDocument;
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
