import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { listEdgeSecretReferences } from "../../src/lib/edgeManagement.js";
import {
  createLocalAdminSession,
  createLocalSecret,
  deleteLocalAdminSession,
  deleteLocalSecret,
  localAdminSessionSnapshot,
  rotateLocalSecret,
  testLocalProviderConnection,
} from "../../src/lib/localSecretManagement.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const LOCAL_URL = "http://localhost:8000";
const TENANT_ID = "tenant_edge_prd123";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const DEPLOYMENT_ID = `edp_${"2".repeat(32)}`;
const REFERENCE_ID = `lsr_${"3".repeat(32)}`;
const PROVIDER_ID = "pcn_edge_prd123";

function context(role = "security_admin") {
  return withPermissionSnapshot({
    tenant: { tenant_id: TENANT_ID, name: "Edge tenant" },
    user: {
      tenant_id: TENANT_ID,
      user_id: "usr_edge_prd123",
      role,
      email: "edge-prd123@example.test",
    },
    session: { session_id: "ses_edge_prd123", session_revision: 1 },
  }, [PERMISSIONS.EDGE_DEPLOYMENT_READ, PERMISSIONS.EDGE_SECRET_REFERENCE_READ], {
    permissionRevision: 33,
    authorizationFingerprint: "edge_prd123_authz",
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
    display_name: "PRD-123 gateway",
    region_code: "cn-east-1",
    mode: "connected",
    status: "active",
    revision: 2,
    revocation_generation: 1,
    agent_version: "1.2.0",
    health_code: "healthy",
    last_connected_at: "2026-08-27T02:10:00Z",
    certificate_expires_at: "2026-09-03T02:00:00Z",
    identity_generation: 1,
    current_certificate: null,
    recent_events: [],
    created_at: "2026-08-27T02:00:00Z",
    updated_at: "2026-08-27T02:10:00Z",
    revoked_at: null,
  };
}

function reference(overrides = {}) {
  return {
    schema_version: "1",
    reference_id: REFERENCE_ID,
    deployment_id: DEPLOYMENT_ID,
    tenant_id: TENANT_ID,
    workspace_id: WORKSPACE_ID,
    provider_connection_id: PROVIDER_ID,
    label: "Primary provider credential",
    kind: "api_key",
    status: "active",
    revision: 1,
    active_version: 1,
    last_test_outcome: "succeeded",
    capabilities: ["chat_completions", "models"],
    rotated_at: "2026-08-27T02:08:00Z",
    updated_at: "2026-08-27T02:08:00Z",
    ...overrides,
  };
}

function desiredEntry() {
  return {
    deploymentId: DEPLOYMENT_ID,
    response: {
      state: null,
      revision: 0,
      idempotent_replay: false,
      request_id: "req_prd123_desired",
      trace_id: "trc_prd123_desired",
    },
  };
}

function jsonResponse(payload, { status = 200, csrf = "" } = {}) {
  const headers = {
    "Content-Type": "application/json",
    "Cache-Control": "no-store, private",
  };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  return new Response(JSON.stringify(payload), { status, headers });
}

test("cloud Secret Reference client accepts only the safe exact projection", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const payload = {
    references: [reference()],
    request_id: "req_prd123_references",
    trace_id: "trc_prd123_references",
  };
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store, private",
        "X-Request-ID": payload.request_id,
        "X-Trace-ID": payload.trace_id,
      },
    });
  };
  const result = await listEdgeSecretReferences({ controlApiUrl: CONTROL_URL });
  assert.equal(result.references[0].reference_id, REFERENCE_ID);
  assert.equal(calls[0].options.credentials, "include");
  assert.equal("X-CSRF-Token" in calls[0].options.headers, false);

  globalThis.fetch = async () => new Response(JSON.stringify({
    ...payload,
    references: [{ ...reference(), ciphertext: "forbidden" }],
  }), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": payload.request_id,
      "X-Trace-ID": payload.trace_id,
    },
  });
  await assert.rejects(
    listEdgeSecretReferences({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "edge.response_invalid",
  );
});

test("local client uses byte bodies, credentialed CSRF, CAS, and zeroes inputs", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const csrf = "c".repeat(43);
  const expiresAt = Math.floor(Date.now() / 1000) + 600;
  const replies = [
    jsonResponse({ deployment_id: DEPLOYMENT_ID, expires_at: expiresAt }, {
      status: 201,
      csrf,
    }),
    jsonResponse(reference(), { status: 201 }),
    jsonResponse({
      test_id: `lpt_${"4".repeat(32)}`,
      reference_id: REFERENCE_ID,
      version: 1,
      outcome: "succeeded",
      capabilities: ["chat_completions", "models"],
      latency_bucket: "lt_100ms",
      created_at: "2026-08-27T02:09:00Z",
    }),
    jsonResponse(reference({ revision: 2, active_version: 2 })),
    new Response(null, { status: 204, headers: { "Cache-Control": "no-store, private" } }),
    new Response(null, { status: 204, headers: { "Cache-Control": "no-store, private" } }),
  ];
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({
      url: String(url),
      headers: { ...options.headers },
      body: options.body instanceof Uint8Array ? new Uint8Array(options.body) : options.body,
      credentials: options.credentials,
    });
    return replies.shift();
  };

  const handoff = new TextEncoder().encode("local-handoff-browser-test-credential-123456789");
  await createLocalAdminSession({
    localApiOrigin: LOCAL_URL,
    deploymentId: DEPLOYMENT_ID,
    accountId: "usr_edge_prd123",
    tenantId: TENANT_ID,
    workspaceId: WORKSPACE_ID,
    handoffCredential: handoff,
  });
  assert.equal(handoff.every((value) => value === 0), true);
  assert.equal(localAdminSessionSnapshot().deployment_id, DEPLOYMENT_ID);

  const first = new TextEncoder().encode("provider-key-one");
  await createLocalSecret({
    deploymentId: DEPLOYMENT_ID,
    providerConnectionId: PROVIDER_ID,
    label: "Primary provider credential",
    kind: "api_key",
    secretBytes: first,
  });
  assert.equal(first.every((value) => value === 0), true);
  const tested = await testLocalProviderConnection({
    deploymentId: DEPLOYMENT_ID,
    providerConnectionId: PROVIDER_ID,
  });
  assert.equal(tested.outcome, "succeeded");

  const second = new TextEncoder().encode("provider-key-two");
  await rotateLocalSecret({
    deploymentId: DEPLOYMENT_ID,
    referenceId: REFERENCE_ID,
    revision: 1,
    secretBytes: second,
  });
  assert.equal(second.every((value) => value === 0), true);
  await deleteLocalSecret({ deploymentId: DEPLOYMENT_ID, referenceId: REFERENCE_ID, revision: 2 });
  assert.equal(await deleteLocalAdminSession({ localApiOrigin: LOCAL_URL }), true);

  assert.equal(calls.every((call) => call.credentials === "include"), true);
  assert.deepEqual([...calls[0].body], [
    ...new TextEncoder().encode("local-handoff-browser-test-credential-123456789"),
  ]);
  assert.equal(calls[1].headers["X-CSRF-Token"], csrf);
  assert.equal(calls[3].headers["If-Match"], "1");
  assert.equal(calls[4].headers["If-Match"], "2");
  assert.equal(JSON.stringify(calls).includes("provider-key-one"), false);
});

test("security_admin state renders safe references and never stores input bytes", () => {
  const auth = context();
  const owner = privateGatewayPageOwner(auth, shell(auth));
  assert.equal(owner.canReadSecrets, true);
  assert.equal(owner.canManageSecrets, true);
  const clearedOrigins = [];
  const state = createPrivateGatewayPageState({
    clearLocalSession: (origin) => clearedOrigins.push(origin),
  });
  state.enter(owner);
  const load = state.beginLoad(owner, 1);
  assert.equal(state.acceptLoad(
    load,
    { deployments: [deployment()] },
    owner,
    1,
    [desiredEntry()],
    [],
    { references: [reference()] },
  ), true);
  state.finishLoad(load);
  let html = renderPrivateGateways(state.snapshot(), auth, shell(auth));
  assert.match(html, /本地 Secret 引用[\s\S]*Primary provider credential/u);
  assert.match(html, /连接本地托管/u);
  assert.doesNotMatch(html, /ciphertext|provider-key|handoff-browser-test/u);

  assert.equal(state.openSecretDialog(owner, DEPLOYMENT_ID, "handoff"), true);
  const sessionOperation = state.beginLocalSession(owner, LOCAL_URL);
  assert.equal(Object.hasOwn(sessionOperation, "handoffCredential"), false);
  assert.equal(state.acceptLocalSession(sessionOperation, {
    local_api_origin: LOCAL_URL,
    deployment_id: DEPLOYMENT_ID,
    expires_at: Math.floor(Date.now() / 1000) + 600,
  }, owner), true);
  state.finishLocalSession(sessionOperation);

  assert.equal(state.openSecretDialog(owner, DEPLOYMENT_ID, "create"), true);
  const mutation = state.beginSecretMutation(owner, "create", {
    deploymentId: DEPLOYMENT_ID,
    providerConnectionId: PROVIDER_ID,
    label: "Secondary credential",
    kind: "api_key",
  });
  assert.equal(Object.hasOwn(mutation, "secretBytes"), false);
  assert.equal(JSON.stringify(state.snapshot()).includes("provider-key"), false);
  state.finishSecretMutation(mutation);
  state.clear();
  assert.deepEqual(clearedOrigins, [LOCAL_URL]);

  const admin = context("admin");
  const adminOwner = privateGatewayPageOwner(admin, shell(admin));
  assert.equal(adminOwner.canReadSecrets, true);
  assert.equal(adminOwner.canManageSecrets, false);
});

test("Secret input paths avoid browser Storage and clear DOM and byte buffers", async () => {
  const [clientSource, bindingSource, stateSource, mainSource] = await Promise.all([
    readFile(new URL("../../src/lib/localSecretManagement.js", import.meta.url), "utf8"),
    readFile(new URL(
      "../../src/features/private-gateways/bindPrivateGateways.js",
      import.meta.url,
    ), "utf8"),
    readFile(new URL(
      "../../src/features/private-gateways/privateGatewayPageState.js",
      import.meta.url,
    ), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(clientSource, /localStorage|sessionStorage|indexedDB/iu);
  assert.doesNotMatch(bindingSource, /localStorage|sessionStorage|indexedDB/iu);
  assert.doesNotMatch(stateSource, /handoffCredential|secretBytes|secret_value/iu);
  assert.match(bindingSource, /input\) input\.value = ""/u);
  assert.match(bindingSource, /bytes\.fill\(0\)/u);
  assert.match(clientSource, /finally \{[\s\S]*zeroBytes\(handoffCredential\)/u);
  assert.match(mainSource, /clearLocalSession\(localApiOrigin\)[\s\S]*deleteLocalAdminSession/u);
});
