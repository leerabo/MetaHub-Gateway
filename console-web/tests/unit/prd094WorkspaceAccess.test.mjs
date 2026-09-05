import assert from "node:assert/strict";
import test from "node:test";

import { createIdentityPageController } from "../../src/features/identity/identityPageState.js";
import { renderIdentitySecurityPage } from "../../src/features/identity/renderIdentityPage.js";
import { IdentityApiError } from "../../src/lib/identity.js";
import {
  createWorkspaceMembershipInvitation,
  getCurrentWorkspaceSession,
  getWorkspaceConsoleNavigation,
  listIdentityWorkspaces,
  listWorkspaceMemberships,
  switchIdentityWorkspace,
  transferWorkspaceOwner,
  updateWorkspaceMembership,
} from "../../src/lib/workspaces.js";

const CONTROL_URL = "http://control.local";
const WSP_ONE = `wsp_${"1".repeat(32)}`;
const WSP_TWO = `wsp_${"2".repeat(32)}`;
const MEMBER_OWNER = `wmb_${"1".repeat(32)}`;
const MEMBER_ADMIN = `wmb_${"2".repeat(32)}`;
const MEMBER_SERVICE = `wmb_${"3".repeat(32)}`;
const INVITATION_TOKEN = "A".repeat(43);
const AUTHORIZATION_FINGERPRINT = "f".repeat(64);

test("PRD-094 clients use exact Global and Workspace cookie contracts", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie: "__Host-metahub-global-csrf=global-csrf; __Host-metahub-workspace-csrf=workspace-csrf",
  };
  const calls = installFetch((url) => responseFor(url));
  let navigation;
  let workspaceSession;
  try {
    await listIdentityWorkspaces({ controlApiUrl: CONTROL_URL });
    await switchIdentityWorkspace({ controlApiUrl: CONTROL_URL, workspaceId: WSP_ONE });
    workspaceSession = await getCurrentWorkspaceSession({ controlApiUrl: CONTROL_URL });
    await listWorkspaceMemberships({ controlApiUrl: CONTROL_URL });
    await updateWorkspaceMembership({
      controlApiUrl: CONTROL_URL,
      membershipId: MEMBER_ADMIN,
      revision: 4,
      role: "auditor",
      status: "active",
    });
    await createWorkspaceMembershipInvitation({
      controlApiUrl: CONTROL_URL,
      email: "Member@Example.com",
      role: "developer",
      expiresInHours: 24,
    });
    await transferWorkspaceOwner({
      controlApiUrl: CONTROL_URL,
      targetMembershipId: MEMBER_ADMIN,
      currentPassword: "PrivateOwnerPassword",
    });
    navigation = await getWorkspaceConsoleNavigation({ controlApiUrl: CONTROL_URL });
  } finally {
    globalThis.document = originalDocument;
  }

  assert.deepEqual(
    calls.map(({ url, options }) => [url, options.method, options.credentials, options.cache]),
    [
      ["http://control.local/identity/workspaces", "GET", "include", "no-store"],
      ["http://control.local/identity/workspace-switches", "POST", "include", "no-store"],
      ["http://control.local/workspace/session", "GET", "include", "no-store"],
      ["http://control.local/memberships", "GET", "include", "no-store"],
      [`http://control.local/memberships/${MEMBER_ADMIN}`, "PATCH", "include", "no-store"],
      ["http://control.local/membership-invitations", "POST", "include", "no-store"],
      ["http://control.local/workspace-owner-transfers", "POST", "include", "no-store"],
      ["http://control.local/workspace/navigation", "GET", "include", "no-store"],
    ],
  );
  assert.equal(calls[0].options.headers["X-CSRF-Token"], undefined);
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "global-csrf");
  assert.equal(calls[4].options.headers["X-CSRF-Token"], "workspace-csrf");
  assert.equal(calls[4].options.headers["If-Match"], '"4"');
  assert.equal(navigation.groups[0].items[0].route_code, "workspace.home.overview");
  assert.equal(navigation.permission_revision, 1);
  assert.equal(navigation.permission_fingerprint, AUTHORIZATION_FINGERPRINT);
  assert.equal("permission_codes" in navigation, false);
  assert.deepEqual(workspaceSession.authorization, {
    permission_codes: [],
    permission_revision: 1,
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    membership_revision: 1,
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), { workspace_id: WSP_ONE });
  assert.deepEqual(JSON.parse(calls[4].options.body), { role: "auditor", status: "active" });
  assert.deepEqual(JSON.parse(calls[5].options.body), {
    email: "member@example.com",
    role: "developer",
    expires_in_hours: 24,
  });
  assert.deepEqual(JSON.parse(calls[6].options.body), {
    target_membership_id: MEMBER_ADMIN,
    current_password: "PrivateOwnerPassword",
  });
  for (const call of calls) {
    assert.equal("Authorization" in call.options.headers, false);
    assert.doesNotMatch(call.options.body ?? "", /tenant_id|account_id|permission|revision/u);
  }
});

test("PRD-094 client rejects credential confusion and over-posted DTOs", async () => {
  const originalDocument = globalThis.document;
  globalThis.document = {
    cookie:
      "__Host-metahub-workspace-csrf=one; __Host-metahub-workspace-csrf=two; __Host-metahub-global-csrf=global",
  };
  let fetchCount = 0;
  installFetch(() => {
    fetchCount += 1;
    return jsonResponse(membershipPayload());
  });
  try {
    await assert.rejects(
      updateWorkspaceMembership({
        controlApiUrl: CONTROL_URL,
        membershipId: MEMBER_ADMIN,
        revision: 1,
        role: "owner",
      }),
      (error) => error.code === "workspace.request_invalid",
    );
    await assert.rejects(
      updateWorkspaceMembership({
        controlApiUrl: CONTROL_URL,
        membershipId: MEMBER_ADMIN,
        revision: 1,
        role: "viewer",
      }),
      (error) => error.code === "workspace_session.csrf_invalid",
    );
  } finally {
    globalThis.document = originalDocument;
  }
  assert.equal(fetchCount, 0);

  installFetch(() =>
    jsonResponse({ ...identityWorkspaceListPayload(), tenant_id: "tenant-must-not-pass" }),
  );
  await assert.rejects(
    listIdentityWorkspaces({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "workspace_session.response_invalid",
  );

  installFetch(() => jsonResponse(workspaceContextPayload()));
  await assert.rejects(
    getCurrentWorkspaceSession({ controlApiUrl: CONTROL_URL }),
    (error) => error.code === "workspace_session.response_invalid",
  );
});

test("PRD-094 controller switches, manages members, and keeps invitation Token out of DOM", async () => {
  const channel = fakeChannel();
  const copied = [];
  const switched = [];
  const updated = [];
  const transferred = [];
  const switchDurations = [];
  const clockValues = [100, 380];
  let activeWorkspace = WSP_ONE;
  let sessionAvailable = true;
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    channelFactory: () => channel,
    confirmAction: () => true,
    consoleUrl: "https://console.example.test",
    copyText: async (value) => copied.push(value),
    monotonicNow: () => clockValues.shift(),
    recordWorkspaceSwitch: (metric) => switchDurations.push(metric.durationMs),
    api: workingApi({
      switchWorkspace: async ({ workspaceId }) => {
        switched.push(workspaceId);
        activeWorkspace = workspaceId;
        sessionAvailable = true;
        return workspaceContextPayload(workspaceId);
      },
      workspaceSession: async () => {
        if (!sessionAvailable) throw workspaceError(401, "workspace_session.revoked");
        return workspaceSessionPayload(activeWorkspace);
      },
      updateMembership: async (input) => {
        updated.push(input);
        return membershipPayload({ role: input.role ?? "admin", revision: 5 });
      },
      inviteMembership: async () => invitationPayload(),
      transferOwner: async (input) => {
        transferred.push(input);
        sessionAvailable = false;
        return ownerTransferPayload();
      },
    }),
  });

  assert.equal(await controller.mount(), true);
  assert.equal(await controller.switchWorkspace(1), true);
  assert.deepEqual(switched, [WSP_TWO]);
  assert.deepEqual(switchDurations, [280]);
  assert.equal(await controller.updateMembership(1, { role: "auditor", status: "active" }), true);
  assert.equal(updated[0].membershipId, MEMBER_ADMIN);
  assert.equal(updated[0].revision, 4);
  assert.equal(await controller.inviteMembership({
    email: "new.member@example.com",
    role: "developer",
    expiresInHours: 72,
  }), true);
  assert.doesNotMatch(root.innerHTML, new RegExp(INVITATION_TOKEN, "u"));
  assert.doesNotMatch(root.innerHTML, /accept-invitation/u);
  assert.equal(await controller.copyInvitationSecret("link"), true);
  assert.match(copied[0], new RegExp(`#accept-invitation/${INVITATION_TOKEN}$`, "u"));
  assert.equal(await controller.transferOwner({
    targetIndex: 1,
    currentPassword: "PrivateOwnerPassword",
    confirmed: true,
  }), true);
  assert.equal(transferred[0].targetMembershipId, MEMBER_ADMIN);
  assert.doesNotMatch(root.innerHTML, /PrivateOwnerPassword/u);
  assert.match(root.innerHTML, /选择一个空间后可查看成员与职责/u);
  assert.ok(channel.messages.length >= 3);
  for (const message of channel.messages) {
    assert.deepEqual(Object.keys(message).sort(), ["generation", "type"]);
    assert.equal(message.type, "context-invalidated");
    assert.doesNotMatch(JSON.stringify(message), /workspace|tenant|account|session|role|token/iu);
  }
  controller.destroy();
  assert.equal(channel.closed, true);
});

test("PRD-094 owner transfer requires explicit inline confirmation", async () => {
  let transferCount = 0;
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    api: workingApi({
      transferOwner: async () => {
        transferCount += 1;
        return ownerTransferPayload();
      },
    }),
  });

  await controller.mount();
  assert.match(root.innerHTML, /name="confirmTransfer" type="checkbox" required/u);
  assert.equal(await controller.transferOwner({
    targetIndex: 1,
    currentPassword: "PrivateOwnerPassword",
  }), false);
  assert.equal(transferCount, 0);
  assert.doesNotMatch(root.innerHTML, /PrivateOwnerPassword/u);
});

test("PRD-094 BroadcastChannel invalidation aborts and fences a late switch", async () => {
  const channel = fakeChannel();
  const pending = deferred();
  let capturedSignal = null;
  const root = createRoot();
  const controller = createIdentityPageController({
    root,
    channelFactory: () => channel,
    api: workingApi({
      switchWorkspace: ({ signal }) => {
        capturedSignal = signal;
        return pending.promise;
      },
    }),
  });
  await controller.mount();
  const switchResult = controller.switchWorkspace(1);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(capturedSignal.aborted, false);

  channel.onmessage({ data: { type: "context-invalidated", generation: 8 } });
  await Promise.resolve();
  assert.equal(capturedSignal.aborted, true);
  pending.resolve(workspaceContextPayload(WSP_TWO));
  assert.equal(await switchResult, false);
  await waitFor(() => root.innerHTML.includes("Acme AI"));
  assert.match(root.innerHTML, /Acme AI/u);
  assert.doesNotMatch(root.innerHTML, /Late Workspace Result/u);
  controller.destroy();
});

test("PRD-094 context lifecycle never uses localStorage or sessionStorage", async () => {
  const descriptors = new Map(
    ["localStorage", "sessionStorage"].map((name) => [
      name,
      Object.getOwnPropertyDescriptor(globalThis, name),
    ]),
  );
  for (const name of descriptors.keys()) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      get() {
        throw new Error(`${name} must not be accessed`);
      },
    });
  }
  const channel = fakeChannel();
  const controller = createIdentityPageController({
    root: createRoot(),
    channelFactory: () => channel,
    api: workingApi({ switchWorkspace: async () => workspaceContextPayload(WSP_TWO) }),
  });
  try {
    await controller.mount();
    await controller.refreshWorkspaceContext();
    channel.onmessage({ data: { type: "context-invalidated", generation: 2 } });
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    controller.destroy();
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});

test("PRD-094 renderer exposes machine subjects without internal identifiers or login actions", () => {
  const root = createRoot();
  renderIdentitySecurityPage(root, {
    account: accountPayload().account,
    currentSession: accountPayload().current_session,
    sessions: [accountPayload().current_session],
    workspaces: [richWorkspace(WSP_ONE, "Acme AI")],
    workspaceChoices: identityWorkspaceListPayload().workspaces,
    workspaceContext: workspaceContextPayload().context,
    memberships: membershipListPayload().memberships,
    navigationItems: navigationPayload().groups.flatMap((group) => group.items),
    workspaceView: "summary",
  });

  assert.match(root.innerHTML, /机器主体/u);
  assert.match(root.innerHTML, /data-membership-update-form/u);
  assert.match(root.innerHTML, /data-owner-transfer-form/u);
  assert.match(root.innerHTML, /data-membership-invitation-form/u);
  assert.doesNotMatch(
    root.innerHTML,
    new RegExp(`${WSP_ONE}|${WSP_TWO}|${MEMBER_OWNER}|${MEMBER_ADMIN}|${MEMBER_SERVICE}`, "u"),
  );
  assert.doesNotMatch(
    root.innerHTML,
    /tenant_id|workspace_id|membership_id|revision|issuer|audience|matrix|authorization_fingerprint/iu,
  );
  const serviceSection = root.innerHTML.slice(root.innerHTML.indexOf("Automation Runner"));
  assert.doesNotMatch(serviceSection.split("</article>", 1)[0], /switch-workspace|membership-update/u);
});

function responseFor(url) {
  const path = new URL(url).pathname;
  if (path === "/identity/workspaces") return jsonResponse(identityWorkspaceListPayload());
  if (path === "/identity/workspace-switches") {
    return jsonResponse(workspaceContextPayload());
  }
  if (path === "/workspace/session") return jsonResponse(workspaceSessionPayload());
  if (path === "/memberships") return jsonResponse(membershipListPayload());
  if (path.startsWith("/memberships/")) return jsonResponse(membershipPayload());
  if (path === "/membership-invitations") return jsonResponse(invitationPayload(), 201);
  if (path === "/workspace-owner-transfers") return jsonResponse(ownerTransferPayload());
  if (path === "/workspace/navigation") {
    return jsonResponse(navigationPayload(), 200, {
      "Cache-Control": "no-store, private",
      ETag: '"1.1.1.1"',
      Pragma: "no-cache",
      Vary: "Cookie",
      "X-Permission-Fingerprint": "f".repeat(64),
    });
  }
  throw new Error(`unexpected path ${path}`);
}

function workingApi(overrides = {}) {
  return {
    me: async () => accountPayload(),
    sessions: async () => sessionListPayload(),
    workspaces: async () => richWorkspaceListPayload(),
    identityWorkspaces: async () => identityWorkspaceListPayload(),
    workspaceSession: async () => workspaceSessionPayload(),
    memberships: async () => membershipListPayload(),
    workspaceNavigation: async () => navigationPayload(),
    createWorkspace: async () => ({ workspace: richWorkspace(WSP_ONE, "Acme AI") }),
    revoke: async () => ({ revoked: true }),
    logout: async () => ({ logged_out: true }),
    rotate: async () => accountPayload(),
    ...overrides,
  };
}

function accountPayload() {
  return {
    account: {
      email: "owner@example.com",
      display_name: "Owner",
      status: "active",
      mfa_configured: false,
    },
    current_session: {
      session_id: "gis_private_session_id",
      device_family: "chrome",
      created_at: "2026-08-20T01:00:00Z",
      last_seen_at: "2026-08-20T02:00:00Z",
      expires_at: "2026-08-21T01:00:00Z",
      is_current: true,
    },
    request_id: "req_account",
    trace_id: "trc_account",
  };
}

function sessionListPayload() {
  return {
    sessions: [accountPayload().current_session],
    limit: 20,
    offset: 0,
    total: 1,
    request_id: "req_sessions",
    trace_id: "trc_sessions",
  };
}

function richWorkspace(workspaceId, name) {
  return {
    workspace_id: workspaceId,
    kind: "organization",
    status: "active",
    profile_state: "confirmed",
    name,
    slug: name.toLowerCase().replaceAll(" ", "-"),
    region: "cn-north-1",
    service_mode: "internal_only",
    environment: { code: "dev", status: "active", origin: "system_created" },
    provisioning: { status: "core_committed" },
    revision: 1,
  };
}

function richWorkspaceListPayload() {
  return {
    workspaces: [richWorkspace(WSP_ONE, "Acme AI"), richWorkspace(WSP_TWO, "Research Lab")],
    limit: 20,
    offset: 0,
    total: 2,
    request_id: "req_rich_workspaces",
    trace_id: "trc_rich_workspaces",
  };
}

function identityWorkspaceListPayload() {
  return {
    workspaces: [
      { workspace_id: WSP_ONE, kind: "organization", name: "Acme AI", slug: "acme-ai", role: "owner" },
      { workspace_id: WSP_TWO, kind: "organization", name: "Research Lab", slug: "research-lab", role: "owner" },
    ],
    limit: 20,
    offset: 0,
    total: 2,
    request_id: "req_identity_workspaces",
    trace_id: "trc_identity_workspaces",
  };
}

function workspaceContextPayload(workspaceId = WSP_ONE) {
  const choice = identityWorkspaceListPayload().workspaces.find(
    (workspace) => workspace.workspace_id === workspaceId,
  );
  return {
    context: {
      ...choice,
      session_status: "active",
      expires_at: "2026-08-21T03:00:00Z",
    },
    request_id: "req_workspace_context",
    trace_id: "trc_workspace_context",
  };
}

function workspaceSessionPayload(workspaceId = WSP_ONE) {
  return {
    ...workspaceContextPayload(workspaceId),
    authorization: {
      permission_codes: [],
      permission_revision: 1,
      permission_fingerprint: AUTHORIZATION_FINGERPRINT,
      membership_revision: 1,
    },
  };
}

function membershipListPayload() {
  return {
    memberships: [
      membershipSummary({
        membership_id: MEMBER_OWNER,
        display_name: "Owner",
        role: "owner",
        is_current: true,
        can_manage: false,
      }),
      membershipSummary({ membership_id: MEMBER_ADMIN, display_name: "Team Admin" }),
      membershipSummary({
        membership_id: MEMBER_SERVICE,
        display_name: "Automation Runner",
        role: "service_account",
        can_manage: false,
      }),
    ],
    limit: 20,
    offset: 0,
    total: 3,
    request_id: "req_memberships",
    trace_id: "trc_memberships",
  };
}

function membershipSummary(overrides = {}) {
  return {
    membership_id: MEMBER_ADMIN,
    display_name: "Team Admin",
    role: "admin",
    status: "active",
    revision: 4,
    is_current: false,
    can_manage: true,
    ...overrides,
  };
}

function membershipPayload(overrides = {}) {
  return {
    membership: membershipSummary(overrides),
    request_id: "req_membership",
    trace_id: "trc_membership",
  };
}

function invitationPayload() {
  return {
    invitation: {
      invitation_id: `min_${"4".repeat(32)}`,
      role: "developer",
      status: "pending",
      expires_at: "2026-08-24T03:00:00Z",
    },
    accept_token: INVITATION_TOKEN,
    request_id: "req_invitation",
    trace_id: "trc_invitation",
  };
}

function ownerTransferPayload() {
  return {
    transferred: true,
    source: membershipSummary({
      membership_id: MEMBER_OWNER,
      display_name: "Owner",
      role: "admin",
      revision: 2,
      is_current: true,
    }),
    target: membershipSummary({ role: "owner", revision: 5, can_manage: false }),
    request_id: "req_owner_transfer",
    trace_id: "trc_owner_transfer",
  };
}

function navigationPayload() {
  return {
    account_action_codes: ["identity.preferences"],
    account_locale: "en-US",
    advanced_tools_available: false,
    contract_generation: 1,
    contract_snapshot_id: `ncs_${"1".repeat(32)}`,
    contract_version: "workspace-navigation-v1",
    detail_route_codes: [],
    developer_route_codes: [],
    groups: [
      {
        group_code: "home",
        label_key: "navigation.group.home",
        expanded: false,
        items: [
          {
            route_code: "workspace.home.overview",
            label_key: "navigation.overview.label",
            description_key: "navigation.overview.description",
            kind: "primary",
            order: 10,
          },
        ],
      },
    ],
    item_set_digest: "d".repeat(64),
    membership_revision: 1,
    navigation_epoch: 1,
    observed_at: "2026-09-03T08:00:00Z",
    owner_ref: "a".repeat(64),
    permission_fingerprint: AUTHORIZATION_FINGERPRINT,
    permission_revision: 1,
    preference: {
      revision: 0,
      last_primary_route_code: "workspace.home.overview",
      expanded_group_code: null,
      sidebar_collapsed: false,
    },
    projection_generation: 1,
    projection_status: "ready",
    request_id: "req_navigation",
    trace_id: "trc_navigation",
  };
}

function workspaceError(status, code) {
  return new IdentityApiError({
    status,
    code,
    message: "空间上下文已变化。",
    requestId: "req_error",
  });
}

function fakeChannel() {
  return {
    messages: [],
    closed: false,
    onmessage: null,
    postMessage(message) {
      this.messages.push(structuredClone(message));
    },
    close() {
      this.closed = true;
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

function jsonResponse(payload, status = 200, responseHeaders = {}) {
  const headers = Object.fromEntries(
    Object.entries(responseHeaders).map(([key, value]) => [key.toLowerCase(), value]),
  );
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[String(name).toLowerCase()] ?? null },
    async json() {
      return structuredClone(payload);
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  throw new Error("condition was not reached");
}
