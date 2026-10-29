import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  decidePlatformOfflineBundle,
  getWorkspaceOfflineBundleState,
  listPlatformOfflineBundles,
  requestWorkspaceOfflineBundle,
} from "../../src/lib/edgeOfflineBundles.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const TENANT_ID = "11111111-1111-4111-8111-111111111111";
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const BUNDLE_ID = `obb_${"3".repeat(32)}`;
const REQUEST_ID = `obr_${"4".repeat(32)}`;
const SECOND_REQUEST_ID = `obr_${"7".repeat(32)}`;
const CONFLICT_ID = `ofc_${"5".repeat(32)}`;
const ENTRY_ID = `obe_${"6".repeat(32)}`;
const WORKSPACE_CSRF = "W".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const STEP_UP = "step-up-grant-000000000001";
const IDEMPOTENCY = "offline-bundle-test-000001";
const REQUEST_CHAIN = "req_prd130_test";
const TRACE_CHAIN = "trc_prd130_test";

test("offline clients isolate credential domains and strip signed material from UI state", async (t) => {
  installCookies(t);
  const calls = installFetch(t, (url) => {
    const path = new URL(url).pathname;
    if (path === `/edge/deployments/${DEPLOYMENT_ID}/offline-bundles`) {
      return jsonResponse(stateResponse());
    }
    if (path === "/platform/edge/offline-bundles") {
      return jsonResponse(platformList());
    }
    if (path.endsWith(`/offline-bundles/${REQUEST_ID}/decisions`)) {
      return jsonResponse(decisionResponse());
    }
    throw new Error(`unexpected route ${path}`);
  });

  const workspace = await getWorkspaceOfflineBundleState({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
  });
  const platform = await listPlatformOfflineBundles({ controlApiUrl: CONTROL_URL });
  const decision = await decidePlatformOfflineBundle({
    controlApiUrl: CONTROL_URL,
    requestId: REQUEST_ID,
    action: "approve",
    expectedRevision: 1,
    reasonCode: "dual_control_approved",
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(workspace.asynchronous_management, true);
  assert.equal(platform.items[0].state.deployment_id, DEPLOYMENT_ID);
  assert.deepEqual(Object.keys(decision.bundle).sort(), [
    "bundle_id", "created_at", "deployment_id", "direction", "domains", "entry_count",
    "expires_at", "manifest_sha256", "producer_generation", "tenant_id", "workspace_id",
  ]);
  assert.doesNotMatch(JSON.stringify(decision), /signature|public_key|nonce|key_envelope|wrapped_key/iu);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[2].options.headers["Idempotency-Key"], IDEMPOTENCY);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
  }
});

test("workspace transfer requires CSRF, step-up, exact manifest, and idempotency", async (t) => {
  installCookies(t);
  const calls = installFetch(t, (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), {
      manifest: manifest(),
      reason_code: "scheduled_media_exchange",
    });
    return jsonResponse(stateResponse({ idempotent_replay: true }), { status: 200 });
  });

  const result = await requestWorkspaceOfflineBundle({
    controlApiUrl: CONTROL_URL,
    operation: "export",
    manifest: manifest(),
    reasonCode: "scheduled_media_exchange",
    stepUpGrant: STEP_UP,
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(result.idempotent_replay, true);
  assert.equal(calls[0].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[0].options.headers["X-Step-Up-Grant"], STEP_UP);
  assert.equal(calls[0].options.headers["Idempotency-Key"], IDEMPOTENCY);
});

test("offline clients reject unknown response fields and malformed request manifests", async (t) => {
  installCookies(t);
  installFetch(t, () => jsonResponse(stateResponse({ object_url: "forbidden" })));
  await assert.rejects(
    () => getWorkspaceOfflineBundleState({
      controlApiUrl: CONTROL_URL,
      deploymentId: DEPLOYMENT_ID,
    }),
    (error) => error instanceof AuthApiError
      && error.code === "edge_offline_bundle.response_invalid",
  );

  await assert.rejects(
    () => requestWorkspaceOfflineBundle({
      controlApiUrl: CONTROL_URL,
      operation: "export",
      manifest: { ...manifest(), storage_path: "forbidden" },
      reasonCode: "scheduled_media_exchange",
      stepUpGrant: STEP_UP,
      idempotencyKey: IDEMPOTENCY,
    }),
    (error) => error instanceof AuthApiError
      && error.code === "edge_offline_bundle.request_invalid",
  );

  globalThis.fetch = async () => jsonResponse(platformList({
    items: [platformItem(), platformItem()],
  }));
  await assert.rejects(
    () => listPlatformOfflineBundles({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError
      && error.code === "edge_offline_bundle.response_invalid",
  );
});

test("offline client source has no browser persistence or bearer path", async () => {
  const source = await readFile(
    new URL("../../src/lib/edgeOfflineBundles.js", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /localStorage|sessionStorage|Authorization|Bearer/iu);
  assert.match(source, /credentials:\s*"include"/u);
  assert.match(source, /cache:\s*"no-store"/u);
});

test("Workspace offline exchange view is owner-bound and prunes facts on permission drift", () => {
  const auth = workspaceContext("offline-owner-a", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_OFFLINE_BUNDLE_READ,
    PERMISSIONS.EDGE_OFFLINE_BUNDLE_TRANSFER,
    PERMISSIONS.EDGE_OFFLINE_CONFLICT_RESOLVE,
  ]);
  const shell = workspaceShell(auth);
  const owner = privateGatewayPageOwner(auth, shell);
  assert.equal(owner.canReadOfflineBundles, true);
  assert.equal(owner.canTransferOfflineBundles, true);
  assert.equal(owner.canResolveOfflineConflicts, true);

  const state = createPrivateGatewayPageState();
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  assert.ok(load);
  assert.equal(state.acceptLoad(
    load, { deployments: [deployment()] }, owner, 1,
    [], [], null, null, [], [], [], [],
    [{ deploymentId: DEPLOYMENT_ID, response: stateResponse() }],
  ), true);
  state.finishLoad(load);

  const html = renderPrivateGateways(state.snapshot(), auth, shell);
  assert.match(html, /离线介质交换/u);
  assert.match(html, /云端 → 设备/u);
  assert.match(html, /1 项冲突/u);
  assert.match(html, /待决策/u);
  assert.doesNotMatch(
    html,
    /signature|public_key|nonce|key_envelope|wrapped_key|storage_path|object_url|Secret/iu,
  );

  const changed = workspaceContext("offline-owner-b", [PERMISSIONS.EDGE_DEPLOYMENT_READ]);
  state.enter(privateGatewayPageOwner(changed, workspaceShell(changed)));
  assert.deepEqual(state.snapshot().offlineBundleStates, {});
  assert.equal(state.snapshot().canReadOfflineBundles, false);
});

test("Platform offline exchange loads, retries one intent, approves and rejects without signed facts", async (t) => {
  installWindow(t, "#offline-bundles");
  const root = fakeRoot();
  const decisionCalls = [];
  let keySequence = 0;
  let firstApproveAttempt = true;
  let navigationEnabled = true;
  const api = {
    session: async () => ({
      context: {
        operator: { display_name: "Offline Ops", roles: ["platform_ops"] },
        permissions: navigationEnabled
          ? ["platform.edge_offline_bundle.read", "platform.edge_offline_bundle.manage"]
          : [],
      },
    }),
    navigation: async () => ({
      items: navigationEnabled ? [{
        id: "offline-bundles",
        label: "Offline exchange",
        href: "/platform.html#offline-bundles",
      }] : [],
    }),
    offlineBundles: async () => platformList({
      items: [{
        workspace_id: WORKSPACE_ID,
        tenant_id: TENANT_ID,
        state: stateResponse({
          requests: [requestSummary(), requestSummary({ request_id: SECOND_REQUEST_ID })],
        }),
      }],
    }),
    decideOfflineBundle: async (input) => {
      decisionCalls.push(input);
      if (input.requestId === REQUEST_ID && firstApproveAttempt) {
        firstApproveAttempt = false;
        throw new PlatformApiError({
          status: 503,
          code: "edge_offline_bundle.unavailable",
          message: "Decision outcome is uncertain",
        });
      }
      return decisionSummary(input.requestId, input.action);
    },
  };
  const controller = createPlatformPageController({
    root,
    api,
    newIdempotencyKey: () => `offline-ui-intent-${String(++keySequence).padStart(4, "0")}`,
  });

  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /Offline exchange/u);
  assert.match(root.innerHTML, /2 条待处理/u);
  assert.match(root.innerHTML, /data-platform-offline-decision-form/u);
  assert.doesNotMatch(
    root.innerHTML,
    /signature|public_key|nonce|key_envelope|wrapped_key|storage_path|object_url|Secret/iu,
  );

  const approve = {
    requestId: REQUEST_ID,
    action: "approve",
    expectedRevision: 1,
    reasonCode: "dual_control_approved",
    confirmed: true,
  };
  assert.equal(await controller.decideOfflineBundle(approve), false);
  assert.equal(await controller.decideOfflineBundle(approve), true);
  assert.equal(decisionCalls[0].idempotencyKey, decisionCalls[1].idempotencyKey);
  assert.equal(keySequence, 1);
  assert.match(root.innerHTML, /1 条待处理/u);

  assert.equal(await controller.decideOfflineBundle({
    requestId: SECOND_REQUEST_ID,
    action: "reject",
    expectedRevision: 1,
    reasonCode: "security_review_rejected",
    confirmed: true,
  }), true);
  assert.notEqual(decisionCalls[1].idempotencyKey, decisionCalls[2].idempotencyKey);
  assert.equal(keySequence, 2);
  assert.match(root.innerHTML, /暂无待处理的离线交换请求/u);
  assert.doesNotMatch(root.innerHTML, /data-platform-offline-decision-form/u);

  navigationEnabled = false;
  assert.equal(await controller.refresh(), true);
  assert.doesNotMatch(root.innerHTML, /Offline exchange|manifest_sha256|producer_generation/iu);
  assert.equal(await controller.decideOfflineBundle(approve), false);
  controller.destroy();
});

test("offline page sources avoid browser persistence and include responsive tables", async () => {
  const [platformCss, edgeCss, platformState, workspaceState] = await Promise.all([
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/edge-operations.css", import.meta.url), "utf8"),
    readFile(new URL(
      "../../src/features/platform/platformPageState.js", import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url,
    ), "utf8"),
  ]);
  assert.match(platformCss, /@media \(max-width: 760px\)[\s\S]*\.platform-offline-table/u);
  assert.match(edgeCss, /@media \(max-width: 760px\)[\s\S]*\.private-gateway-offline-facts/u);
  assert.doesNotMatch(
    `${platformState}\n${workspaceState}`,
    /localStorage|sessionStorage|indexedDB/iu,
  );
});

function stateResponse(overrides = {}) {
  return {
    deployment_id: DEPLOYMENT_ID,
    asynchronous_management: true,
    requests: [requestSummary()],
    bundles: [bundleSummary()],
    conflicts: [conflictSummary()],
    request_id: REQUEST_CHAIN,
    trace_id: TRACE_CHAIN,
    idempotent_replay: false,
    ...overrides,
  };
}

function requestSummary(overrides = {}) {
  return {
    request_id: REQUEST_ID,
    deployment_id: DEPLOYMENT_ID,
    operation: "export",
    status: "pending_platform",
    reason_code: "scheduled_media_exchange",
    target_bundle_id: BUNDLE_ID,
    target_conflict_id: null,
    revision: 1,
    requested_at: "2026-08-28T08:00:00.000Z",
    decided_at: null,
    ...overrides,
  };
}

function bundleSummary() {
  return {
    bundle_id: BUNDLE_ID,
    deployment_id: DEPLOYMENT_ID,
    direction: "cloud_to_edge",
    producer_generation: 1,
    domains: ["desired_state"],
    entry_count: 1,
    encrypted_size_bytes: 128,
    transfer_status: "prepared",
    next_sequence: 0,
    chunk_count: 1,
    manifest_sha256: "7".repeat(64),
    created_at: "2026-08-28T08:00:00.000Z",
    expires_at: "2026-08-29T08:00:00.000Z",
  };
}

function conflictSummary() {
  return {
    conflict_id: CONFLICT_ID,
    bundle_id: BUNDLE_ID,
    deployment_id: DEPLOYMENT_ID,
    domain: "desired_state",
    base_generation: 1,
    base_sha256: "8".repeat(64),
    cloud_generation: 2,
    cloud_sha256: "9".repeat(64),
    local_generation: 2,
    local_sha256: "a".repeat(64),
    resolution: null,
    new_generation: null,
    detected_at: "2026-08-28T09:00:00.000Z",
    resolved_at: null,
  };
}

function platformItem() {
  return { workspace_id: WORKSPACE_ID, tenant_id: TENANT_ID, state: stateResponse() };
}

function platformList(overrides = {}) {
  return {
    items: [platformItem()],
    request_id: REQUEST_CHAIN,
    trace_id: TRACE_CHAIN,
    ...overrides,
  };
}

function decisionResponse() {
  return {
    request: requestSummary({ status: "approved", revision: 2,
      decided_at: "2026-08-28T09:30:00.000Z" }),
    signed_bundle: {
      manifest: manifest(),
      manifest_sha256: "b".repeat(64),
      signature: {
        algorithm: "Ed25519",
        key_id: "edge-offline-test-v1",
        public_key: "A".repeat(43),
        value: "B".repeat(86),
      },
    },
    request_id: REQUEST_CHAIN,
    trace_id: TRACE_CHAIN,
    idempotent_replay: false,
  };
}

function decisionSummary(requestId, action) {
  return {
    request: requestSummary({
      request_id: requestId,
      status: action === "approve" ? "approved" : "rejected",
      revision: 2,
      decided_at: "2026-08-28T09:30:00.000Z",
    }),
    bundle: action === "approve" ? {
      bundle_id: BUNDLE_ID,
      created_at: "2026-08-28T08:00:00.000Z",
      deployment_id: DEPLOYMENT_ID,
      direction: "cloud_to_edge",
      domains: ["desired_state"],
      entry_count: 1,
      expires_at: "2026-08-29T08:00:00.000Z",
      manifest_sha256: "b".repeat(64),
      producer_generation: 1,
      tenant_id: TENANT_ID,
      workspace_id: WORKSPACE_ID,
    } : null,
    idempotent_replay: false,
    request_id: REQUEST_CHAIN,
    trace_id: TRACE_CHAIN,
  };
}

function manifest() {
  return {
    schema_version: "metahub.edge-offline-bundle/v1",
    signature_purpose: "metahub.edge-offline-bundle.manifest/v1",
    bundle_id: BUNDLE_ID,
    direction: "cloud_to_edge",
    workspace_id: WORKSPACE_ID,
    tenant_id: TENANT_ID,
    deployment_id: DEPLOYMENT_ID,
    producer_generation: 1,
    version_vector: [{
      domain: "desired_state",
      base_generation: 0,
      base_sha256: null,
      generation: 1,
      value_sha256: "c".repeat(64),
    }],
    entries: [{
      entry_id: ENTRY_ID,
      domain: "desired_state",
      sequence: 0,
      media_type: "application/json",
      plaintext_sha256: "d".repeat(64),
      ciphertext_sha256: "e".repeat(64),
      plaintext_size: 16,
      ciphertext_size: 32,
      nonce: "A".repeat(16),
    }],
    entry_merkle_root: "f".repeat(64),
    key_envelope_sha256: "1".repeat(64),
    kms_key_id_sha256: "2".repeat(64),
    encryption_key_version: 1,
    created_at: "2026-08-28T08:00:00.000Z",
    expires_at: "2026-08-29T08:00:00.000Z",
  };
}

function deployment() {
  return {
    agent_version: "1.0.0",
    certificate_expires_at: "2026-09-27T08:00:00.000Z",
    created_at: "2026-07-28T08:00:00.000Z",
    current_certificate: null,
    deployment_id: DEPLOYMENT_ID,
    display_name: "Air-gapped Gateway",
    health_code: "healthy",
    identity_generation: 1,
    last_connected_at: "2026-08-28T08:00:00.000Z",
    mode: "air_gapped",
    recent_events: [],
    region_code: "cn-test",
    revision: 1,
    revocation_generation: 0,
    revoked_at: null,
    status: "active",
    updated_at: "2026-08-28T08:00:00.000Z",
    workspace_id: WORKSPACE_ID,
  };
}

function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    user: {
      tenant_id: TENANT_ID,
      account_id: "gac_current",
      user_id: "usr_prd130",
      email: "owner@example.invalid",
      role: "security_admin",
    },
    tenant: { tenant_id: TENANT_ID },
    session: { session_id: "ses_prd130", session_revision: 1 },
  }, permissionCodes, { authorizationFingerprint: fingerprint });
}

function workspaceShell(auth) {
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
        required_permissions: [PERMISSIONS.EDGE_DEPLOYMENT_READ],
        visible: true,
        disabled: false,
      }],
    },
  };
}

function fakeRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installWindow(t, hash) {
  const original = globalThis.window;
  globalThis.window = {
    location: { hash },
    history: {
      replaceState(_state, _title, value) { globalThis.window.location.hash = value; },
    },
  };
  t.after(() => { globalThis.window = original; });
}

function installCookies(t) {
  const original = globalThis.document;
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; `
      + `__Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  t.after(() => { globalThis.document = original; });
}

function installFetch(t, responder) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  t.after(() => { globalThis.fetch = original; });
  return calls;
}

function jsonResponse(payload, { status = 200 } = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "private, no-store",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
}
