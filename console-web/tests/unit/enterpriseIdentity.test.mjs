import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import {
  createEnterpriseDomain,
  createEnterpriseSsoConnection,
  getEnterpriseIdentitySummary,
  testEnterpriseSsoConnection,
  updateEnterpriseSsoPolicy,
  verifyEnterpriseDomain,
} from "../../src/lib/enterpriseIdentity.js";
import { IdentityApiError } from "../../src/lib/identity.js";
import {
  createScimToken,
  getScimDirectorySummary,
  resolveScimConflict,
  rotateScimToken,
  upsertScimRoleMapping,
} from "../../src/lib/scimDirectory.js";

const CONTROL_URL = "http://control.local";
const WSP = `wsp_${"1".repeat(32)}`;
const VDM = `vdm_${"2".repeat(32)}`;
const SSC = `ssc_${"3".repeat(32)}`;
const GAC = `gac_${"4".repeat(32)}`;
const SCT = `sct_${"7".repeat(32)}`;
const SGL = `sgl_${"8".repeat(32)}`;
const SRM = `srm_${"9".repeat(32)}`;
const SSR = `ssr_${"a".repeat(32)}`;
const SCF = `scf_${"b".repeat(32)}`;
const SCIM_BEARER = `mhscim_${"C".repeat(48)}`;
const CLIENT_SECRET = "not-a-real-client-secret-0001";
const CERTIFICATE_PEM = `-----BEGIN CERTIFICATE-----\n${"A".repeat(96)}\n-----END CERTIFICATE-----`;

test("PRD-114 clients send exact Workspace cookie, CSRF, CAS, and idempotency contracts", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=enterprise%2Dcsrf" };
  const calls = installFetch((url) => responseFor(new URL(url).pathname));
  try {
    await getEnterpriseIdentitySummary({ controlApiUrl: CONTROL_URL, workspaceId: WSP });
    await createEnterpriseDomain({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      domain: "example.com",
      idempotencyKey: "enterprise-domain-0001",
    });
    await verifyEnterpriseDomain({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      domainId: VDM,
      revision: 1,
      idempotencyKey: "enterprise-domain-0002",
    });
    await createEnterpriseSsoConnection({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      idempotencyKey: "enterprise-connect-0001",
      input: oidcInput(),
    });
    await testEnterpriseSsoConnection({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      connectionId: SSC,
      revision: 2,
      idempotencyKey: "enterprise-connect-0002",
    });
    await updateEnterpriseSsoPolicy({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      mode: "enforced",
      connectionId: SSC,
      breakGlassAccountId: GAC,
      revision: 0,
      idempotencyKey: "enterprise-policy-0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map(({ url, options }) => [
    new URL(url).pathname,
    options.method,
    options.credentials,
    options.cache,
  ]), [
    [`/workspaces/${WSP}/enterprise-identity`, "GET", "include", "no-store"],
    [`/workspaces/${WSP}/domains`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/domains/${VDM}/verify`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/sso-connections`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/sso/test`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/sso-policy`, "PATCH", "include", "no-store"],
  ]);
  assert.deepEqual(calls[0].options.headers, { Accept: "application/json" });
  for (const call of calls.slice(1)) {
    assert.equal(call.options.headers["X-CSRF-Token"], "enterprise-csrf");
    assert.equal("Authorization" in call.options.headers, false);
  }
  assert.equal(calls[2].options.body, undefined);
  assert.equal(calls[2].options.headers["If-Match"], '"1"');
  assert.equal(calls[4].options.headers["If-Match"], '"2"');
  assert.equal(calls[5].options.headers["If-Match"], '"0"');
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    verified_domain_id: VDM,
    connection_code: "workforce",
    display_name: "Workforce login",
    protocol: "oidc",
    issuer: "https://idp.example.com",
    authorization_endpoint: "https://idp.example.com/authorize",
    token_endpoint: "https://idp.example.com/token",
    client_id: "metahub-console",
    client_secret: CLIENT_SECRET,
    jwks: jwks(),
  });
  assert.deepEqual(JSON.parse(calls[5].options.body), {
    mode: "enforced",
    active_connection_id: SSC,
    break_glass_account_id: GAC,
  });
});

test("PRD-114 strict DTOs reject unknown response fields and invalid IDs before network", async () => {
  const calls = installFetch(() => jsonResponse({ ...summary(), client_secret: "must-reject" }));
  await assert.rejects(
    getEnterpriseIdentitySummary({ controlApiUrl: CONTROL_URL, workspaceId: WSP }),
    (error) => error instanceof IdentityApiError && error.code === "sso.response_invalid",
  );
  await assert.rejects(
    verifyEnterpriseDomain({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      domainId: "vdm_invalid",
      revision: 1,
      idempotencyKey: "enterprise-domain-0002",
    }),
    (error) => error instanceof IdentityApiError && error.code === "sso.request_invalid",
  );
  assert.equal(calls.length, 1);
});

test("PRD-114 renderer omits internal IDs, endpoints, issuer, fingerprints, and secret material", () => {
  const root = createRoot();
  const state = securityState("owner");
  state.enterpriseIdentity = {
    ...state.enterpriseIdentity,
    domains: [{ ...domain(), status: "pending", verified_at: null }],
    connections: [{ ...connection(), status: "draft" }],
  };
  renderIdentitySecurityPage(root, state);

  assert.match(root.innerHTML, /企业身份|example\.com|Workforce login|OIDC signing key/u);
  assert.match(root.innerHTML, /data-enterprise-domain-index="0"/u);
  assert.match(root.innerHTML, /data-enterprise-connection-index="0"/u);
  for (const forbidden of [
    WSP,
    VDM,
    SSC,
    GAC,
    "https://idp.example.com/private-issuer",
    "https://idp.example.com/private-token-endpoint",
    "f".repeat(64),
    CLIENT_SECRET,
    CERTIFICATE_PEM,
  ]) assert.doesNotMatch(root.innerHTML, new RegExp(escapeRegExp(forbidden), "u"));
});

test("PRD-114 role projection keeps auditors read-only and hides enterprise SSO from developers", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, securityState("auditor"));
  assert.match(root.innerHTML, /企业身份|Workforce login/u);
  assert.doesNotMatch(
    root.innerHTML,
    /data-enterprise-(?:domain|connection|policy)-form|verify-enterprise-domain|test-enterprise-connection/u,
  );

  renderIdentitySecurityPage(root, securityState("security_admin"));
  assert.match(root.innerHTML, /data-enterprise-domain-form|data-enterprise-policy-form/u);

  renderIdentitySecurityPage(root, securityState("developer"));
  assert.doesNotMatch(root.innerHTML, /identity-enterprise-title|Enterprise SSO/u);
});

test("PRD-114 OIDC and SAML modes expose only their protocol-specific fields", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, {
    ...securityState("security_admin"),
    enterpriseConnectionProtocol: "oidc",
  });
  assert.match(root.innerHTML, /name="clientSecret"|name="jwks"/u);
  assert.doesNotMatch(root.innerHTML, /name="certificatePem"|name="ssoUrl"/u);

  renderIdentitySecurityPage(root, {
    ...securityState("security_admin"),
    enterpriseConnectionProtocol: "saml",
  });
  assert.match(root.innerHTML, /name="certificatePem"|name="ssoUrl"/u);
  assert.doesNotMatch(root.innerHTML, /name="clientSecret"|name="jwks"/u);
});

test("PRD-114 controller hashes retry intent and scrubs secret, JWKS, certificate, and DNS state", async () => {
  const root = createRoot();
  const attempts = [];
  const keys = ["enterprise-retry-key-0001", "enterprise-retry-key-0002"];
  let domainAttempts = 0;
  let capturedConnectionInput = null;
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => keys.shift(),
    api: controllerApi({
      createEnterpriseDomain: async (input) => {
        attempts.push(input.idempotencyKey);
        domainAttempts += 1;
        if (domainAttempts < 3) {
          throw new IdentityApiError({
            status: 0,
            code: "sso.unavailable",
            message: "企业身份服务暂时不可用，请稍后重试。",
          });
        }
        return domainCreateResponse();
      },
      createEnterpriseSsoConnection: async ({ input }) => {
        capturedConnectionInput = input;
        return connectionResponse();
      },
    }),
  });
  await controller.mount();

  assert.equal(await controller.createEnterpriseDomain({ domain: "retry.example.com" }), false);
  assert.equal(await controller.createEnterpriseDomain({ domain: "retry.example.com" }), false);
  assert.equal(await controller.createEnterpriseDomain({ domain: "changed.example.com" }), true);
  assert.deepEqual(attempts, [
    "enterprise-retry-key-0001",
    "enterprise-retry-key-0001",
    "enterprise-retry-key-0002",
  ]);

  const input = { ...oidcInput(), domainIndex: 0, jwksText: JSON.stringify(jwks()) };
  assert.equal(await controller.createEnterpriseSsoConnection(input), true);
  assert.equal(input.clientSecret, "");
  assert.equal(input.jwksText, "");
  assert.equal(input.certificatePem, "");
  assert.equal(input.jwks, null);
  assert.equal(capturedConnectionInput.clientSecret, "");
  assert.equal(capturedConnectionInput.jwks, null);

  controller.destroy();
  assert.equal(root.innerHTML, "");
  assert.equal(JSON.stringify(controller).includes("enterprise-retry-key"), false);
});

test("PRD-114 source keeps DNS and SSO secrets out of browser storage and defines narrow layouts", async () => {
  const [stateSource, clientSource, css] = await Promise.all([
    readFile(new URL("../../src/features/identity/identityPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/enterpriseIdentity.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/identity.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(clientSource, /localStorage|sessionStorage|indexedDB/u);
  assert.doesNotMatch(stateSource, /localStorage|sessionStorage|indexedDB/u);
  assert.match(stateSource, /secureEnterpriseFingerprint/u);
  assert.match(css, /@media \(max-width: 520px\)[\s\S]*\.identity-enterprise-row/u);
});

test("PRD-115 SCIM clients use Workspace Cookie, CSRF, CAS, and idempotency contracts", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=scim%2Dcsrf" };
  const calls = installFetch((url) => scimResponseFor(new URL(url).pathname));
  try {
    await getScimDirectorySummary({ controlApiUrl: CONTROL_URL, workspaceId: WSP });
    await createScimToken({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      expiresInDays: 90,
      idempotencyKey: "scim-token-create-0001",
    });
    await rotateScimToken({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      tokenId: SCT,
      revision: 1,
      expiresInDays: 180,
      idempotencyKey: "scim-token-rotate-0001",
    });
    await upsertScimRoleMapping({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      groupId: SGL,
      roleCode: "developer",
      revision: 0,
      idempotencyKey: "scim-mapping-save-0001",
    });
    await resolveScimConflict({
      controlApiUrl: CONTROL_URL,
      workspaceId: WSP,
      conflictId: SCF,
      resolutionCode: "keep_local",
      revision: 1,
      idempotencyKey: "scim-conflict-fix-0001",
    });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(calls.map(({ url, options }) => [
    new URL(url).pathname,
    options.method,
    options.credentials,
    options.cache,
  ]), [
    [`/workspaces/${WSP}/scim-directory`, "GET", "include", "no-store"],
    [`/workspaces/${WSP}/scim-tokens`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/scim-tokens/${SCT}/rotate`, "POST", "include", "no-store"],
    [`/workspaces/${WSP}/scim-role-mappings`, "PUT", "include", "no-store"],
    [`/workspaces/${WSP}/scim-conflicts/${SCF}/resolve`, "POST", "include", "no-store"],
  ]);
  for (const call of calls.slice(1)) {
    assert.equal(call.options.headers["X-CSRF-Token"], "scim-csrf");
    assert.equal("Authorization" in call.options.headers, false);
    assert.match(call.options.headers["Idempotency-Key"], /^scim-/u);
  }
  assert.equal(calls[2].options.headers["If-Match"], '"1"');
  assert.equal(calls[3].options.headers["If-Match"], '"0"');
  assert.equal(calls[4].options.headers["If-Match"], '"1"');
});

test("PRD-115 renderer projects roles and omits internal SCIM identifiers", () => {
  const root = createRoot();
  const state = securityState("owner");
  Object.assign(state, {
    scimDirectory: scimSummary(),
    scimDirectoryBusy: false,
    scimDirectoryError: null,
    scimBearerToken: null,
  });
  renderIdentitySecurityPage(root, state);
  assert.match(root.innerHTML, /SCIM 目录|Engineering Directory|Owner 保护/u);
  assert.match(root.innerHTML, /data-scim-token-form|data-scim-mapping-form/u);
  for (const identifier of [SCT, SGL, SRM, SSR, SCF]) {
    assert.doesNotMatch(root.innerHTML, new RegExp(identifier, "u"));
  }

  renderIdentitySecurityPage(root, { ...state, workspaceContext: workspaceContextResponse("viewer").context });
  assert.match(root.innerHTML, /SCIM 目录|Engineering Directory/u);
  assert.doesNotMatch(root.innerHTML, /data-scim-token-form|data-scim-mapping-form|rotate-scim-token/u);

  renderIdentitySecurityPage(root, { ...state, workspaceContext: workspaceContextResponse("developer").context });
  assert.doesNotMatch(root.innerHTML, /identity-scim-title/u);
});

test("PRD-115 controller removes one-time Bearer from DOM and state on close", async () => {
  const root = createRoot();
  const copied = [];
  const controller = createIdentityPageController({
    root,
    newIdempotencyKey: () => "scim-controller-key-0001",
    copyText: async (value) => copied.push(value),
    api: controllerApi({
      scimDirectory: async () => scimSummary(),
      createScimToken: async () => scimTokenResponse(),
    }),
  });
  await controller.mount();
  assert.equal(await controller.createScimToken({ expiresInDays: 90 }), true);
  assert.match(root.innerHTML, new RegExp(escapeRegExp(SCIM_BEARER), "u"));
  assert.equal(await controller.copyScimBearerToken(), true);
  assert.deepEqual(copied, [SCIM_BEARER]);
  assert.equal(controller.dismissScimBearerToken(), true);
  assert.doesNotMatch(root.innerHTML, new RegExp(escapeRegExp(SCIM_BEARER), "u"));
  assert.equal(JSON.stringify(controller).includes(SCIM_BEARER), false);
  controller.destroy();
});

function controllerApi(overrides = {}) {
  return {
    me: async () => accountResponse(),
    sessions: async () => sessionListResponse(),
    workspaces: async () => workspaceListResponse(),
    identityWorkspaces: async () => identityWorkspaceListResponse("owner"),
    workspaceSession: async () => workspaceContextResponse("owner"),
    memberships: async () => ({ memberships: [] }),
    workspaceNavigation: async () => ({ navigation_items: [] }),
    enterpriseIdentity: async () => summary(),
    ...overrides,
  };
}

function responseFor(path) {
  if (path.endsWith("/enterprise-identity")) return jsonResponse(summary());
  if (path.endsWith("/domains")) return jsonResponse(domainCreateResponse(), 201);
  if (path.endsWith("/verify")) return jsonResponse(domainResponse());
  if (path.endsWith("/sso-connections")) return jsonResponse(connectionResponse(), 201);
  if (path.endsWith("/sso/test")) return jsonResponse(testResponse());
  if (path.endsWith("/sso-policy")) return jsonResponse(policyResponse());
  throw new Error(`unexpected enterprise identity path: ${path}`);
}

function scimResponseFor(path) {
  if (path.endsWith("/scim-directory")) return jsonResponse(scimSummary());
  if (path.endsWith("/scim-tokens")) return jsonResponse(scimTokenResponse(), 201);
  if (path.endsWith("/rotate")) return jsonResponse(scimTokenResponse(), 201);
  if (path.endsWith("/scim-role-mappings")) return jsonResponse(scimMappingResponse());
  if (path.endsWith("/resolve")) return jsonResponse(scimConflictResponse());
  throw new Error(`unexpected SCIM directory path: ${path}`);
}

function summary() {
  return {
    workspace_id: WSP,
    domains: [domain()],
    connections: [connection()],
    policy: policy(),
    break_glass_candidates: [candidate()],
    request_id: "req_enterprise",
    trace_id: "trc_enterprise",
  };
}

function domain() {
  return {
    verified_domain_id: VDM,
    domain: "example.com",
    status: "verified",
    revision: 1,
    challenge_expires_at: "2026-08-25T12:00:00Z",
    verified_at: "2026-08-25T11:00:00Z",
    updated_at: "2026-08-25T11:00:00Z",
  };
}

function connection() {
  return {
    sso_connection_id: SSC,
    verified_domain_id: VDM,
    connection_code: "workforce",
    display_name: "Workforce login",
    protocol: "oidc",
    issuer: "https://idp.example.com/private-issuer",
    status: "active",
    revision: 2,
    tested_at: "2026-08-25T11:10:00Z",
    updated_at: "2026-08-25T11:10:00Z",
    certificates: [{
      sso_certificate_id: `crt_${"5".repeat(32)}`,
      purpose: "oidc_jwk",
      fingerprint_sha256: "f".repeat(64),
      status: "active",
      not_before: "2026-08-25T10:00:00Z",
      not_after: "2026-11-25T10:00:00Z",
    }],
  };
}

function policy() {
  return {
    mode: "optional",
    active_connection_id: SSC,
    break_glass_configured: true,
    revision: 1,
    updated_at: "2026-08-25T11:15:00Z",
  };
}

function candidate() {
  return {
    account_id: GAC,
    display_name: "Recovery Owner",
    password_configured: true,
    mfa_configured: true,
  };
}

function domainCreateResponse() {
  return {
    domain: domain(),
    verification_record: "metahub-domain-verification=memory-only-record",
    idempotent_replay: false,
    request_id: "req_domain_create",
    trace_id: "trc_domain_create",
  };
}

function domainResponse() {
  return {
    domain: domain(),
    idempotent_replay: false,
    request_id: "req_domain",
    trace_id: "trc_domain",
  };
}

function connectionResponse() {
  return {
    connection: connection(),
    idempotent_replay: false,
    request_id: "req_connection",
    trace_id: "trc_connection",
  };
}

function testResponse() {
  return {
    connection: connection(),
    outcome: "passed",
    idempotent_replay: false,
    request_id: "req_test",
    trace_id: "trc_test",
  };
}

function policyResponse() {
  return {
    policy: policy(),
    idempotent_replay: false,
    request_id: "req_policy",
    trace_id: "trc_policy",
  };
}

function scimSummary() {
  return {
    workspace_id: WSP,
    tokens: [scimToken()],
    groups: [{
      scim_group_link_id: SGL,
      display_name: "Engineering Directory",
      state: "active",
      revision: 1,
      updated_at: "2026-08-25T11:20:00Z",
    }],
    role_mappings: [scimMapping()],
    sync_runs: [{
      scim_sync_run_id: SSR,
      status: "completed",
      created_count: 3,
      updated_count: 2,
      disabled_count: 1,
      conflict_count: 1,
      safe_outcome_code: "sync.completed",
      started_at: "2026-08-25T11:20:00Z",
      completed_at: "2026-08-25T11:21:00Z",
    }],
    conflicts: [scimConflict()],
    pending_invitation_count: 1,
    linked_user_count: 12,
    linked_group_count: 1,
    request_id: "req_scim_summary",
    trace_id: "trc_scim_summary",
  };
}

function scimToken() {
  return {
    scim_token_id: SCT,
    token_prefix: "mhscim_CCCCCCC",
    status: "active",
    revision: 1,
    expires_at: "2026-11-25T11:20:00Z",
    grace_expires_at: null,
    last_used_at: "2026-08-25T11:21:00Z",
  };
}

function scimMapping() {
  return {
    scim_role_mapping_id: SRM,
    scim_group_link_id: SGL,
    group_display_name: "Engineering Directory",
    role_code: "developer",
    status: "active",
    revision: 1,
    updated_at: "2026-08-25T11:22:00Z",
  };
}

function scimConflict() {
  return {
    scim_conflict_id: SCF,
    subject_type: "membership",
    reason_code: "owner_protected",
    status: "open",
    revision: 1,
    created_at: "2026-08-25T11:22:00Z",
    resolved_at: null,
  };
}

function scimTokenResponse() {
  return {
    token: scimToken(),
    bearer_token: SCIM_BEARER,
    idempotent_replay: false,
    request_id: "req_scim_token",
    trace_id: "trc_scim_token",
  };
}

function scimMappingResponse() {
  return {
    mapping: scimMapping(),
    idempotent_replay: false,
    request_id: "req_scim_mapping",
    trace_id: "trc_scim_mapping",
  };
}

function scimConflictResponse() {
  return {
    conflict: scimConflict(),
    idempotent_replay: false,
    request_id: "req_scim_conflict",
    trace_id: "trc_scim_conflict",
  };
}

function oidcInput() {
  return {
    verifiedDomainId: VDM,
    connectionCode: "workforce",
    displayName: "Workforce login",
    protocol: "oidc",
    issuer: "https://idp.example.com",
    authorizationEndpoint: "https://idp.example.com/authorize",
    tokenEndpoint: "https://idp.example.com/token",
    clientId: "metahub-console",
    clientSecret: CLIENT_SECRET,
    jwks: jwks(),
    certificatePem: CERTIFICATE_PEM,
  };
}

function jwks() {
  return {
    keys: [{
      kty: "RSA",
      kid: "workforce-2026",
      use: "sig",
      alg: "RS256",
      n: "AQAB",
      e: "AQAB",
    }],
  };
}

function securityState(role) {
  return {
    account: accountResponse().account,
    currentSession: accountResponse().current_session,
    sessions: [accountResponse().current_session],
    workspaces: workspaceListResponse().workspaces,
    workspaceChoices: identityWorkspaceListResponse(role).workspaces,
    workspaceContext: workspaceContextResponse(role).context,
    memberships: [],
    navigationItems: [],
    enterpriseIdentity: summary(),
    enterpriseIdentityBusy: false,
    enterpriseVerificationRecord: null,
  };
}

function accountResponse() {
  return {
    account: {
      email: "owner@example.com",
      display_name: "Owner",
      status: "active",
      mfa_configured: true,
    },
    current_session: {
      session_id: `gis_${"6".repeat(32)}`,
      device_family: "chrome",
      created_at: "2026-08-25T09:00:00Z",
      last_seen_at: "2026-08-25T11:00:00Z",
      expires_at: "2026-08-26T09:00:00Z",
      is_current: true,
    },
  };
}

function sessionListResponse() {
  return { sessions: [accountResponse().current_session] };
}

function workspaceListResponse() {
  return {
    workspaces: [{
      workspace_id: WSP,
      kind: "organization",
      status: "active",
      profile_state: "confirmed",
      name: "Acme AI",
      slug: "acme-ai",
      region: "cn-north-1",
      service_mode: "internal_only",
      environment: { code: "dev", status: "active", origin: "system_created" },
      provisioning: { status: "core_committed" },
      revision: 1,
    }],
  };
}

function identityWorkspaceListResponse(role) {
  return {
    workspaces: [{
      workspace_id: WSP,
      kind: "organization",
      name: "Acme AI",
      slug: "acme-ai",
      role,
    }],
  };
}

function workspaceContextResponse(role) {
  return {
    context: {
      ...identityWorkspaceListResponse(role).workspaces[0],
      session_status: "active",
      expires_at: "2026-08-26T09:00:00Z",
    },
  };
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() {
      return true;
    },
    querySelectorAll() {
      return [];
    },
  };
}

function installFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return handler(String(url), options);
  };
  return calls;
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() {
      return structuredClone(payload);
    },
  };
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
}
