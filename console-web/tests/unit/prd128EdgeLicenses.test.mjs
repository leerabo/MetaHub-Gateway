import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createPrivateGatewayPageState,
  privateGatewayPageOwner,
} from "../../src/features/private-gateways/privateGatewayPageState.js";
import { renderPrivateGateways } from "../../src/features/private-gateways/renderPrivateGateways.js";
import { createPlatformPageController } from "../../src/features/platform/platformPageState.js";
import {
  getWorkspaceLicense,
  issuePlatformLicense,
  listPlatformLicenses,
  refreshWorkspaceLicense,
  revokePlatformLicense,
} from "../../src/lib/edgeLicenses.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PlatformApiError } from "../../src/lib/platform.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const CONTROL_URL = "http://control.local";
const DEPLOYMENT_ID = `edp_${"1".repeat(32)}`;
const LICENSE_ID = `lic_${"2".repeat(32)}`;
const ENTITLEMENT_ID = `let_${"3".repeat(32)}`;
const WORKSPACE_ID = `wsp_${"4".repeat(32)}`;
const ACK_ID = `lak_${"5".repeat(32)}`;
const ORDER_ID = `ord_${"6".repeat(32)}`;
const TENANT_ID = "12345678-1234-4123-8123-123456789abc";
const WORKSPACE_CSRF = "W".repeat(43);
const PLATFORM_CSRF = "P".repeat(43);
const IDEMPOTENCY = "edge-license-test-00000001";
const NOW = Date.now();

test("strict license clients isolate credential domains and enforce safe summaries", async (t) => {
  installCookies(t);
  const calls = installFetch(t, (url, options) => {
    const path = new URL(url).pathname;
    if (path === `/edge/licenses/${DEPLOYMENT_ID}`) {
      return jsonResponse(licenseResponse(), { etag: '"2"' });
    }
    if (path === `/edge/licenses/${LICENSE_ID}/refresh`) {
      return jsonResponse(licenseResponse({ license: license({ revision: 3, generation: 2 }) }),
        { etag: '"3"', status: 201 });
    }
    if (path === "/platform/edge/licenses") return jsonResponse(licenseList());
    if (path === "/edge/licenses" && options.method === "POST") {
      return jsonResponse(licenseResponse(), { etag: '"2"', status: 201 });
    }
    if (path === `/edge/licenses/${LICENSE_ID}/revoke`) {
      return jsonResponse(licenseResponse({
        license: license({
          status: "revoked",
          revision: 3,
          generation: 2,
          revocation_generation: 1,
          revoked_reason_code: "security_revocation",
          revoked_at: iso(0),
        }),
      }), { etag: '"3"' });
    }
    throw new Error(`unexpected route ${path}`);
  });

  await getWorkspaceLicense({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID });
  await refreshWorkspaceLicense({
    controlApiUrl: CONTROL_URL,
    licenseId: LICENSE_ID,
    expectedRevision: 2,
    idempotencyKey: IDEMPOTENCY,
  });
  await listPlatformLicenses({ controlApiUrl: CONTROL_URL });
  await issuePlatformLicense({
    controlApiUrl: CONTROL_URL,
    deploymentId: DEPLOYMENT_ID,
    nodeLimit: 3,
    concurrentRequestLimit: 100,
    features: ["policy_enforcement", "advanced_routing"],
    expiresAt: iso(30),
    graceDays: 7,
    sourceOrderId: ORDER_ID,
    reasonCode: "commercial_activation",
    idempotencyKey: IDEMPOTENCY,
  });
  await revokePlatformLicense({
    controlApiUrl: CONTROL_URL,
    licenseId: LICENSE_ID,
    expectedRevision: 2,
    reasonCode: "security_revocation",
    idempotencyKey: IDEMPOTENCY,
  });

  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], WORKSPACE_CSRF);
  assert.equal(calls[2].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[3].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  assert.equal(calls[4].options.headers["X-CSRF-Token"], PLATFORM_CSRF);
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.redirect, "error");
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.deepEqual(JSON.parse(calls[3].options.body).features,
    ["advanced_routing", "policy_enforcement"]);
  assert.doesNotMatch(JSON.stringify(calls), /signature|public_key|entitlement.*manifest/iu);
});

test("license clients reject unknown sensitive fields and ETag drift", async (t) => {
  installCookies(t);
  installFetch(t, () => jsonResponse(licenseResponse({ signature: "forbidden" }), { etag: '"2"' }));
  await assert.rejects(
    () => getWorkspaceLicense({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error instanceof AuthApiError && error.code === "edge_license.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(licenseList({
    licenses: [license(), license()],
  }));
  await assert.rejects(
    () => listPlatformLicenses({ controlApiUrl: CONTROL_URL }),
    (error) => error instanceof PlatformApiError && error.code === "edge_license.response_invalid",
  );

  globalThis.fetch = async () => jsonResponse(licenseResponse(), { etag: '"999"' });
  await assert.rejects(
    () => getWorkspaceLicense({ controlApiUrl: CONTROL_URL, deploymentId: DEPLOYMENT_ID }),
    (error) => error.code === "edge_license.response_invalid",
  );
});

test("Workspace license state prunes on permission drift and renders capacity without RBAC claims", () => {
  const auth = workspaceContext("license-owner-a", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_LICENSE_READ,
    PERMISSIONS.EDGE_LICENSE_REFRESH,
    PERMISSIONS.EDGE_LICENSE_IMPORT,
  ]);
  const shell = workspaceShell(auth);
  const owner = privateGatewayPageOwner(auth, shell);
  assert.equal(owner.canReadLicenses, true);
  assert.equal(owner.canRefreshLicenses, true);
  assert.equal(owner.canImportLicenses, true);

  const state = createPrivateGatewayPageState();
  state.enter(owner);
  acceptLicenseLoad(state, owner, licenseResponse());
  const html = renderPrivateGateways(state.snapshot(), auth, shell);
  assert.match(html, /License 容量/u);
  assert.match(html, /2 \/ 3/u);
  assert.match(html, /在线刷新/u);
  assert.match(html, /href="#plans"/u);
  assert.doesNotMatch(html, /signature|public_key|permission_codes/iu);

  const operation = state.beginLicenseRefresh(owner, DEPLOYMENT_ID, IDEMPOTENCY);
  assert.ok(operation);
  assert.equal(state.acceptLicenseRefresh(
    operation,
    licenseResponse({ license: license({ revision: 3, generation: 2 }) }),
    owner,
  ), true);
  state.finishLicenseRefresh(operation);
  assert.equal(state.snapshot().licenseStates[DEPLOYMENT_ID].revision, 3);

  const changed = workspaceContext("license-owner-b", [
    PERMISSIONS.EDGE_DEPLOYMENT_READ,
    PERMISSIONS.EDGE_LICENSE_READ,
    PERMISSIONS.EDGE_LICENSE_REFRESH,
  ]);
  state.enter(privateGatewayPageOwner(changed, workspaceShell(changed)));
  assert.equal(state.snapshot().licenseStates[DEPLOYMENT_ID], undefined);

  const deploymentOnly = workspaceContext("deployment-only", [PERMISSIONS.EDGE_DEPLOYMENT_READ]);
  const deploymentOwner = privateGatewayPageOwner(deploymentOnly, workspaceShell(deploymentOnly));
  assert.equal(deploymentOwner.canReadLicenses, false);
  assert.doesNotMatch(
    renderPrivateGateways(state.snapshot(), deploymentOnly, workspaceShell(deploymentOnly)),
    /License 容量/u,
  );
});

test("Platform license view authorizes issue and revoke without retaining commercial input", async (t) => {
  installWindow(t, "#edge-licenses");
  const calls = [];
  const root = fakeRoot();
  const controller = createPlatformPageController({
    root,
    api: platformApi({
      issueEdgeLicense: async (input) => {
        calls.push({ lane: "issue", ...input });
        return licenseResponse({ license: license({ revision: 3, generation: 2 }) });
      },
      revokeEdgeLicense: async (input) => {
        calls.push({ lane: "revoke", ...input });
        return licenseResponse({
          license: license({
            status: "revoked",
            revision: 4,
            generation: 3,
            revocation_generation: 1,
            revoked_reason_code: "security_revocation",
            revoked_at: iso(0),
          }),
        });
      },
    }),
    newIdempotencyKey: () => IDEMPOTENCY,
  });
  assert.equal(await controller.mount(), true);
  assert.match(root.innerHTML, /Gateway licenses/u);
  assert.match(root.innerHTML, /data-platform-edge-license-issue-form/u);
  assert.match(root.innerHTML, /data-platform-edge-license-revoke-form/u);

  assert.equal(await controller.issueEdgeLicense({
    deploymentId: DEPLOYMENT_ID,
    nodeLimit: 5,
    concurrentRequestLimit: 200,
    features: ["advanced_routing"],
    expiresAt: iso(60),
    graceDays: 7,
    sourceOrderId: ORDER_ID,
    expectedRevision: 2,
    reasonCode: "capacity_expansion",
    confirmed: true,
  }), true);
  assert.equal(calls[0].idempotencyKey, IDEMPOTENCY);
  assert.equal(await controller.revokeEdgeLicense({
    licenseId: LICENSE_ID,
    expectedRevision: 3,
    reasonCode: "security_revocation",
    confirmed: true,
  }), true);
  assert.equal(calls[1].idempotencyKey, IDEMPOTENCY);
  assert.doesNotMatch(root.innerHTML, new RegExp(`${ORDER_ID}|signature|public_key`, "iu"));
  controller.destroy();

  const [platformCss, edgeCss, clientSource, stateSource] = await Promise.all([
    readFile(new URL("../../src/styles/platform.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/edge-operations.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/edgeLicenses.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/private-gateways/privateGatewayPageState.js", import.meta.url), "utf8"),
  ]);
  assert.match(platformCss, /@media \(max-width: 760px\)[\s\S]*\.platform-edge-license-table/u);
  assert.match(edgeCss, /@media \(max-width: 760px\)[\s\S]*\.private-gateway-license-facts/u);
  assert.doesNotMatch(`${clientSource}\n${stateSource}`, /localStorage|sessionStorage|indexedDB/iu);
});

function acceptLicenseLoad(state, owner, response) {
  const operation = state.beginLoad(owner, 1);
  assert.ok(operation);
  assert.equal(state.acceptLoad(
    operation,
    { deployments: [deployment()] },
    owner,
    1,
    [],
    [],
    null,
    null,
    [],
    [],
    [{ deploymentId: DEPLOYMENT_ID, response }],
  ), true);
  state.finishLoad(operation);
}

function license(overrides = {}) {
  return {
    license_id: LICENSE_ID,
    workspace_id: WORKSPACE_ID,
    tenant_id: TENANT_ID,
    deployment_id: DEPLOYMENT_ID,
    status: "active",
    current_entitlement_id: ENTITLEMENT_ID,
    capacity: {
      node_limit: 3,
      concurrent_request_limit: 100,
      features: ["advanced_routing", "policy_enforcement"],
    },
    generation: 1,
    refresh_counter: 1,
    revocation_generation: 0,
    manifest_sha256: "a".repeat(64),
    signing_key_id: "edge-license-v1",
    issued_at: iso(-1),
    not_before: iso(-1),
    expires_at: iso(30),
    grace_ends_at: iso(37),
    last_acknowledgement: {
      acknowledgement_id: ACK_ID,
      generation: 1,
      disposition: "applied",
      decision_status: "current",
      reason_code: "edge_license.current",
      acknowledged_at: iso(0),
    },
    latest_usage: {
      active_node_units: 2,
      active_request_units: 40,
      denied_count: 0,
      bucket_started_at: iso(-0.01),
      bucket_ended_at: iso(0),
    },
    revision: 2,
    updated_at: iso(0),
    revoked_reason_code: null,
    revoked_at: null,
    ...overrides,
  };
}

function licenseResponse(overrides = {}) {
  return {
    license: license(),
    idempotent_replay: false,
    request_id: "request-prd128",
    trace_id: "trace-prd128",
    ...overrides,
  };
}

function licenseList(overrides = {}) {
  return {
    licenses: [license()],
    request_id: "request-prd128",
    trace_id: "trace-prd128",
    ...overrides,
  };
}

function deployment() {
  return {
    agent_version: "1.0.0",
    certificate_expires_at: iso(30),
    created_at: iso(-30),
    current_certificate: null,
    deployment_id: DEPLOYMENT_ID,
    display_name: "License Gateway",
    health_code: "healthy",
    identity_generation: 1,
    last_connected_at: iso(0),
    mode: "connected",
    recent_events: [],
    region_code: "cn-test",
    revision: 1,
    revocation_generation: 0,
    revoked_at: null,
    status: "active",
    updated_at: iso(0),
    workspace_id: WORKSPACE_ID,
  };
}

function workspaceContext(fingerprint, permissionCodes) {
  return withPermissionSnapshot({
    user: {
      tenant_id: TENANT_ID,
      account_id: "gac_prd128",
      user_id: "usr_prd128",
      email: "owner@example.invalid",
      role: "admin",
    },
    tenant: { tenant_id: TENANT_ID },
    session: { session_id: "ses_prd128", session_revision: 1 },
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

function platformApi(overrides = {}) {
  return {
    session: async () => ({
      context: {
        operator: { display_name: "Billing", roles: ["platform_billing"] },
        permissions: [
          "platform.edge_license.issue",
          "platform.edge_license.read",
          "platform.edge_license.revoke",
        ],
      },
    }),
    navigation: async () => ({
      items: [{ id: "edge-licenses", label: "Gateway licenses", href: "/platform.html#edge-licenses" }],
    }),
    edgeLicenses: async () => licenseList(),
    ...overrides,
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
  const originalWindow = globalThis.window;
  globalThis.window = {
    location: { hash },
    history: { replaceState(_state, _title, next) { globalThis.window.location.hash = next; } },
    confirm: () => true,
  };
  t.after(() => { globalThis.window = originalWindow; });
}

function installCookies(t) {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: `__Host-metahub-workspace-csrf=${WORKSPACE_CSRF}; __Host-metahub-platform-csrf=${PLATFORM_CSRF}`,
  };
  t.after(() => { globalThis.document = originalDocument; });
}

function installFetch(t, handler) {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return handler(url, options);
  };
  t.after(() => { globalThis.fetch = originalFetch; });
  return calls;
}

function jsonResponse(payload, { status = 200, etag = null } = {}) {
  const headers = new Headers({
    "Cache-Control": "private, no-store",
    "X-Request-ID": payload.request_id ?? "request-prd128",
    "X-Trace-ID": payload.trace_id ?? "trace-prd128",
  });
  if (etag) headers.set("ETag", etag);
  return new Response(JSON.stringify(payload), { status, headers });
}

function iso(days) {
  return new Date(NOW + days * 24 * 60 * 60 * 1000).toISOString();
}
