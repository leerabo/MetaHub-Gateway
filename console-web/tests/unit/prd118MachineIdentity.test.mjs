import assert from "node:assert/strict";
import test from "node:test";

import { IdentityApiError } from "../../src/lib/identity.js";
import {
  createWorkspaceOAuthClient,
  createWorkspaceServiceAccount,
  listWorkspaceServiceAccounts,
  revokeWorkspaceServiceAccount,
  rotateWorkspaceServiceAccountCredential,
} from "../../src/lib/machineIdentity.js";
import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";

const CONTROL_URL = "http://control.local";
const WORKSPACE_ID = `wsp_${"1".repeat(32)}`;
const SERVICE_ACCOUNT_ID = `svc_${"2".repeat(32)}`;
const OAUTH_CLIENT_ID = `ocl_${"3".repeat(32)}`;
const PUBLIC_CLIENT_ID = `moc_${"4".repeat(32)}`;
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);
const IDEMPOTENCY_KEYS = ["service-account-create-0001", "oauth-client-create-000001",
  "oauth-client-rotate-000001", "service-account-revoke-001"];
const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_DOCUMENT = globalThis.document;

test.afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_DOCUMENT === undefined) delete globalThis.document;
  else globalThis.document = ORIGINAL_DOCUMENT;
});

test("PRD-118 strict clients keep Workspace Cookie credentials and CAS boundaries", async () => {
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  let generation = 1;
  const calls = installFetch((url, options) => {
    const path = new URL(String(url)).pathname;
    if (options.method === "GET") return jsonResponse(machineList());
    if (path === "/service-accounts" && options.method === "POST") {
      return jsonResponse(machineAccountResponse(), { status: 201 });
    }
    if (path === "/oauth/clients") {
      return jsonResponse(machineCredentialResponse(generation, "C"), { status: 201 });
    }
    if (path.endsWith("/rotate")) {
      generation += 1;
      return jsonResponse(machineCredentialResponse(generation, "R"));
    }
    return jsonResponse(machineAccountResponse({ status: "revoked", revision: 2 }));
  });

  await listWorkspaceServiceAccounts({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID });
  await createWorkspaceServiceAccount({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    name: "provider-reader",
    displayName: "Provider reader",
    scopeTemplate: "provider_reader",
    scopes: ["provider.read"],
    tokenTtlSeconds: 900,
    ipAllowlist: ["10.0.0.0/24"],
    mtlsFingerprintSha256: "a".repeat(64),
    idempotencyKey: IDEMPOTENCY_KEYS[0],
  });
  const created = await createWorkspaceOAuthClient({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    serviceAccountId: SERVICE_ACCOUNT_ID,
    idempotencyKey: IDEMPOTENCY_KEYS[1],
  });
  await rotateWorkspaceServiceAccountCredential({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    serviceAccountId: SERVICE_ACCOUNT_ID,
    revision: 1,
    idempotencyKey: IDEMPOTENCY_KEYS[2],
  });
  await revokeWorkspaceServiceAccount({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    serviceAccountId: SERVICE_ACCOUNT_ID,
    revision: 1,
    idempotencyKey: IDEMPOTENCY_KEYS[3],
  });

  assert.match(created.client_secret, /^mcs_/u);
  assert.deepEqual(
    calls.map(({ url, options }) => [new URL(url).pathname, options.method]),
    [
      ["/service-accounts", "GET"],
      ["/service-accounts", "POST"],
      ["/oauth/clients", "POST"],
      [`/service-accounts/${SERVICE_ACCOUNT_ID}/rotate`, "POST"],
      [`/service-accounts/${SERVICE_ACCOUNT_ID}`, "DELETE"],
    ],
  );
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    name: "provider-reader",
    display_name: "Provider reader",
    scope_template: "provider_reader",
    scopes: ["provider.read"],
    token_ttl_seconds: 900,
    ip_allowlist: ["10.0.0.0/24"],
    mtls_fingerprint_sha256: "a".repeat(64),
  });
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.deepEqual(calls.slice(1).map((call) => call.options.headers["Idempotency-Key"]),
    IDEMPOTENCY_KEYS);
  assert.equal(calls[3].options.headers["If-Match"], '"1"');
  assert.equal(calls[4].options.headers["If-Match"], '"1"');
  for (const call of calls) {
    assert.equal(call.options.credentials, "include");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.headers.Authorization, undefined);
  }
});

test("PRD-118 client rejects response extension and cross-Workspace projection", async () => {
  globalThis.fetch = async () => jsonResponse({
    ...machineList(),
    client_secret: machineSecret("X"),
  });
  await assert.rejects(
    listWorkspaceServiceAccounts({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
    (error) => error.code === "machine_identity.response_invalid" && error.status === 502,
  );

  globalThis.fetch = async () => jsonResponse(machineList({
    workspaceId: `wsp_${"9".repeat(32)}`,
  }));
  await assert.rejects(
    listWorkspaceServiceAccounts({ controlApiUrl: CONTROL_URL, workspaceId: WORKSPACE_ID }),
    (error) => error.code === "machine_identity.response_invalid" && error.status === 502,
  );
});

test("PRD-135 compliance collector template binds its closed machine scope", async () => {
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=workspace-csrf" };
  const calls = installFetch(() => jsonResponse(machineAccountResponse()));
  await createWorkspaceServiceAccount({
    controlApiUrl: CONTROL_URL,
    workspaceId: WORKSPACE_ID,
    name: "compliance-collector",
    displayName: "Compliance collector",
    scopeTemplate: "compliance_collector",
    scopes: ["compliance.collect"],
    tokenTtlSeconds: 900,
    ipAllowlist: [],
    mtlsFingerprintSha256: null,
    idempotencyKey: "compliance-collector-create-0001",
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    name: "compliance-collector",
    display_name: "Compliance collector",
    scope_template: "compliance_collector",
    scopes: ["compliance.collect"],
    token_ttl_seconds: 900,
    ip_allowlist: [],
    mtls_fingerprint_sha256: null,
  });
});

test("PRD-118 renderer projects exact read/manage permissions and no Platform entrance", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, identityState({
    machineIdentityVisible: true,
    machineIdentityCanManage: true,
    machineIdentity: machineList(),
  }));
  assert.match(root.innerHTML, /机器身份/u);
  assert.match(root.innerHTML, /data-machine-account-form/u);
  assert.match(root.innerHTML, /data-identity-action="rotate-machine-credential"/u);
  assert.match(root.innerHTML, /Generation 1/u);
  assert.match(root.innerHTML, /mcs_CCCCCCCC/u);
  assert.doesNotMatch(root.innerHTML, new RegExp(machineSecret("C"), "u"));

  renderIdentitySecurityPage(root, identityState({
    machineIdentityVisible: true,
    machineIdentityCanManage: false,
    machineIdentity: machineList(),
  }));
  assert.match(root.innerHTML, /当前职责仅可查看机器身份/u);
  assert.doesNotMatch(root.innerHTML, /data-machine-account-form|rotate-machine-credential|revoke-machine-account/u);

  renderIdentitySecurityPage(root, identityState({
    machineIdentityVisible: false,
    machineIdentityCanManage: false,
    machineIdentity: machineList(),
    platformAccess: true,
  }));
  assert.doesNotMatch(root.innerHTML, /identity-machine-title|data-machine-account-form/u);
});

test("PRD-118 controller scrubs one-time secret on close, navigation, error, and destroy", async () => {
  const root = createRoot();
  const copied = [];
  let rotation = 1;
  let idempotencySequence = 0;
  const rotationKeys = [];
  const controller = createIdentityPageController({
    root,
    confirmAction: () => true,
    copyText: async (value) => copied.push(value),
    newIdempotencyKey: () => `workspace-machine-command-${++idempotencySequence}`,
    api: identityApi({
      machineIdentity: async () => machineList(),
      rotateServiceAccountCredential: async ({ idempotencyKey }) => {
        rotationKeys.push(idempotencyKey);
        rotation += 1;
        if (rotation === 4) {
          throw new IdentityApiError({
            status: 503,
            code: "machine_identity.unavailable",
            message: "unavailable",
          });
        }
        return machineCredentialResponse(rotation, rotation === 2 ? "R" : "N");
      },
      notifications: async () => ({
        notifications: [], unread_count: 0, next_cursor: null,
      }),
    }),
  });

  assert.equal(await controller.mount(), true);
  assert.equal(await controller.rotateServiceAccountCredential(0), true);
  const firstSecret = machineSecret("R");
  assert.match(root.innerHTML, new RegExp(firstSecret, "u"));
  assert.equal(await controller.copyMachineCredential("client_secret"), true);
  assert.deepEqual(copied, [firstSecret]);
  assert.equal(controller.dismissMachineCredential(), true);
  assert.doesNotMatch(root.innerHTML, new RegExp(firstSecret, "u"));

  assert.equal(await controller.rotateServiceAccountCredential(0), true);
  const secondSecret = machineSecret("N");
  assert.match(root.innerHTML, new RegExp(secondSecret, "u"));
  assert.equal(await controller.openNotifications(), true);
  assert.doesNotMatch(root.innerHTML, new RegExp(secondSecret, "u"));
  controller.closeNotifications();

  assert.equal(await controller.rotateServiceAccountCredential(0), false);
  assert.doesNotMatch(root.innerHTML, /mcs_[A-Za-z0-9_-]{40,80}/u);
  assert.match(root.innerHTML, /unavailable/u);
  assert.equal(await controller.rotateServiceAccountCredential(0), true);
  assert.equal(rotationKeys.at(-1), rotationKeys.at(-2));
  controller.destroy();
  assert.equal(root.innerHTML, "");
});

function machineList({ workspaceId = WORKSPACE_ID } = {}) {
  return {
    service_accounts: [serviceAccount({ workspaceId })],
    request_id: "req_machine_list",
    trace_id: "trc_machine_list",
  };
}

function machineAccountResponse({ status = "active", revision = 1 } = {}) {
  return {
    service_account: serviceAccount({ status, revision }),
    request_id: "req_machine_account",
    trace_id: "trc_machine_account",
  };
}

function machineCredentialResponse(generation, marker) {
  const client = oauthClient({ generation, revision: generation, marker });
  return {
    service_account: serviceAccount({
      revision: generation,
      constraintGeneration: generation,
      clientProjection: client,
    }),
    oauth_client: client,
    client_secret: machineSecret(marker),
    request_id: "req_machine_credential",
    trace_id: "trc_machine_credential",
  };
}

function serviceAccount({
  workspaceId = WORKSPACE_ID,
  status = "active",
  revision = 1,
  constraintGeneration = 1,
  clientProjection = oauthClient(),
} = {}) {
  return {
    service_account_id: SERVICE_ACCOUNT_ID,
    workspace_id: workspaceId,
    name: "provider-reader",
    display_name: "Provider reader",
    status,
    scope_template: "provider_reader",
    scopes: ["provider.read"],
    token_ttl_seconds: 900,
    ip_allowlist: ["10.0.0.0/24"],
    mtls_required: false,
    constraint_generation: constraintGeneration,
    revision,
    last_used_at: null,
    revoked_at: status === "revoked" ? "2026-08-26T01:00:00Z" : null,
    created_at: "2026-08-26T00:00:00Z",
    updated_at: "2026-08-26T01:00:00Z",
    oauth_client: clientProjection,
  };
}

function oauthClient({ generation = 1, revision = generation, marker = "C" } = {}) {
  return {
    oauth_client_id: OAUTH_CLIENT_ID,
    client_id: PUBLIC_CLIENT_ID,
    audience: "metahub-control",
    status: "active",
    credential_generation: generation,
    revision,
    credential_prefix: `mcs_${marker.repeat(8)}`,
    credential_expires_at: "2027-08-26T00:00:00Z",
    last_used_at: null,
    created_at: "2026-08-26T00:00:00Z",
    updated_at: "2026-08-26T01:00:00Z",
  };
}

function machineSecret(marker) {
  return `mcs_${marker.repeat(43)}`;
}

function identityState(overrides = {}) {
  return {
    account: {
      email: "owner@example.com",
      display_name: "Owner",
      status: "active",
      mfa_configured: true,
    },
    currentSession: identitySession(),
    sessions: [identitySession()],
    workspaces: [],
    workspaceChoices: [workspaceChoice()],
    workspaceContext: workspaceContext(),
    memberships: [],
    navigationItems: [],
    platformAccess: false,
    machineIdentityVisible: false,
    machineIdentityCanManage: false,
    machineIdentity: null,
    machineIdentityBusy: false,
    machineIdentityError: null,
    machineCredential: null,
    ...overrides,
  };
}

function identityApi(overrides = {}) {
  const permissionCodes = ["machine_identity.read", "machine_identity.manage"];
  return {
    me: async () => ({
      account: identityState().account,
      current_session: identitySession(),
    }),
    sessions: async () => ({ sessions: [identitySession()] }),
    identityWorkspaces: async () => ({ workspaces: [workspaceChoice()] }),
    workspaceSession: async () => ({
      context: workspaceSessionContext(),
      authorization: workspaceAuthorization(permissionCodes),
    }),
    workspaceNavigation: async () => ({
      groups: [],
      membership_revision: 1,
      permission_revision: 1,
      permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    }),
    logout: async () => ({ logged_out: true }),
    ...overrides,
  };
}

function identitySession() {
  return {
    session_id: `gis_${"6".repeat(32)}`,
    device_family: "chrome",
    created_at: "2026-08-25T00:00:00Z",
    last_seen_at: "2026-08-26T00:00:00Z",
    expires_at: "2026-08-27T00:00:00Z",
    is_current: true,
    assurance_level: "mfa",
    auth_method: "totp",
    trusted_device_id: null,
    revoke_generation: 1,
  };
}

function workspaceContext() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
    permission_codes: ["machine_identity.read", "machine_identity.manage"],
  };
}

function workspaceChoice() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
  };
}

function workspaceSessionContext() {
  return {
    workspace_id: WORKSPACE_ID,
    kind: "organization",
    name: "Acme",
    slug: "acme",
    role: "owner",
    session_status: "active",
    expires_at: "2026-08-27T00:00:00Z",
  };
}

function workspaceAuthorization(permissionCodes) {
  return {
    permission_codes: [...permissionCodes],
    permission_revision: 1,
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    membership_revision: 1,
  };
}

function createRoot() {
  return {
    innerHTML: "",
    addEventListener() {},
    contains() { return true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceChildren() { this.innerHTML = ""; },
  };
}

function installFetch(responder) {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return responder(url, options);
  };
  return calls;
}

function jsonResponse(payload, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    async json() { return structuredClone(payload); },
  };
}
