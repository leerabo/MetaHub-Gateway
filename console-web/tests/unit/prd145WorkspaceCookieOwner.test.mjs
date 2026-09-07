import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { createWorkspaceConsoleExperienceController, workspaceConsoleExperienceOwner } from "../../src/features/workspace-console-experience/workspaceConsoleExperienceController.js";
import { createWorkspaceConsoleExperienceStore } from "../../src/features/workspace-console-experience/workspaceConsoleExperienceStore.js";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { getWorkspaceConsoleOverview, queryWorkspaceConsoleAssistant, listWorkspaceConsoleApprovals, getWorkspaceDeploymentSummary } from "../../src/lib/workspaceConsoleExperience.js";

const FINGERPRINT = "f".repeat(64);
const EXPIRY = "2099-01-01T00:00:00Z";
const PERMISSIONS = ["workspace.console.overview.read", "workspace.console.assistant.query",
  "workspace.console.approval.read", "workspace.deployment_diagnostic.read"];
const VIEWS = ["overview", "system-status", "admin-assistant", "approvals", "deployment-summary"];
const originalFetch = globalThis.fetch;
const originalDocument = globalThis.document;
test.afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalDocument === undefined) delete globalThis.document;
  else globalThis.document = originalDocument;
});

function shell(view = "overview") {
  const id = view === "deployment-summary" ? "config-summary" : view;
  const route = WORKSPACE_SOURCE_ROUTE_REGISTRY[id];
  const workspaceIdentity = { account: { status: "active" },
    current_session: { session_id: "gs_public_1", revoke_generation: 0, expires_at: EXPIRY, is_current: true } };
  const workspaceContext = { workspace_id: `wsp_${"1".repeat(32)}`, role: "owner", session_status: "active", expires_at: EXPIRY };
  const workspaceAuthorization = { permission_codes: [...PERMISSIONS], permission_revision: 2,
    membership_revision: 3, permission_fingerprint: FINGERPRINT };
  return { state: "ok", workspaceNavigationContract: true, workspaceIdentity, workspaceContext, workspaceAuthorization,
    workspaceOwner: workspaceNavigationOwner(workspaceIdentity, { context: workspaceContext, authorization: workspaceAuthorization }),
    activeNavigationItemId: id, activeRouteCode: route,
    navigation: { active_navigation_item_id: id, active_route_code: route,
      navigation_items: [{ id, route_code: route, visible: true, disabled: false }],
      permission_revision: 2, membership_revision: 3, permission_fingerprint: FINGERPRINT,
      navigation_epoch: 17, contract_generation: 1, contract_snapshot_id: `ncs_${"2".repeat(32)}` },
  };
}

function response(overrides = {}) {
  return { domain: "workspace", status: "ready", navigation_epoch: 2,
    permission_revision: 2, permission_fingerprint: FINGERPRINT, next_cursor: null,
    ...overrides };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((resolveValue, rejectValue) => { resolve = resolveValue; reject = rejectValue; });
  return { promise, resolve, reject };
}

function noFacts(snapshot) {
  assert.equal(snapshot.overview, null);
  assert.equal(snapshot.approvals, null);
  assert.equal(snapshot.deploymentSummary, null);
  assert.equal(snapshot.assistant.draft, "");
  assert.equal(snapshot.assistant.response, null);
  assert.equal(snapshot.canNextApprovals, false);
  assert.equal(snapshot.canPreviousApprovals, false);
  assert.equal(snapshot.busy, false);
}

for (const view of VIEWS) {
  test(`PRD-145 ${view} loads from public Cookie ownership without legacy identity`, async () => {
    const calls = [];
    const api = Object.fromEntries(["consoleOverview", "consoleAssistant", "consoleApprovals", "deploymentSummary"].map((name) =>
      [name, async (input) => { calls.push({ name, input }); return response(); }]));
    const controller = createWorkspaceConsoleExperienceController({ api });
    const currentShell = shell(view);
    assert.equal(controller.enter(currentShell, view), true);
    assert.equal(controller.owns(currentShell, view), true);
    if (view === "admin-assistant") {
      assert.equal(await controller.load(), true);
      assert.equal(calls.length, 0);
      assert.equal(controller.setAssistantDraft("Summarize operations"), true);
      assert.equal(await controller.submitAssistant(), true);
    } else assert.equal(await controller.load(), true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].name, ({ overview: "consoleOverview", "system-status": "consoleOverview",
      "admin-assistant": "consoleAssistant", approvals: "consoleApprovals",
      "deployment-summary": "deploymentSummary" })[view]);
    assert.ok(calls[0].input.signal instanceof AbortSignal);
    for (const key of ["token", "accountId", "tenantId", "sessionId", "workspaceId"]) {
      assert.equal(Object.hasOwn(calls[0].input, key), false);
      assert.equal(Object.hasOwn(controller.snapshot(), key), false);
    }
    assert.equal(controller.snapshot().status, "ready");
    controller.leave();
    noFacts(controller.snapshot());
  });
}

test("PRD-145 Cookie owner rejects missing, revoked or mismatched session/navigation evidence", () => {
  const mutations = [
    (s) => { delete s.workspaceIdentity; },
    (s) => { delete s.workspaceOwner; },
    (s) => { s.workspaceIdentity.account.status = "disabled"; },
    (s) => { s.workspaceIdentity.current_session.is_current = false; },
    (s) => { s.workspaceIdentity.current_session.session_id = "other_session"; },
    (s) => { s.workspaceContext.session_status = "revoked"; },
    (s) => { s.workspaceContext.workspace_id = `wsp_${"3".repeat(32)}`; },
    (s) => { s.workspaceContext.expires_at = "2020-01-01T00:00:00Z"; },
    (s) => { s.workspaceAuthorization.permission_codes = []; },
    (s) => { s.workspaceAuthorization.membership_revision += 1; },
    (s) => { s.navigation.permission_revision += 1; },
    (s) => { s.navigation.membership_revision += 1; },
    (s) => { s.navigation.permission_fingerprint = "a".repeat(64); },
    (s) => { s.workspaceNavigationContract = false; },
    (s) => { s.activeRouteCode = "workspace.home.assistant"; },
    (s) => { s.navigation.navigation_items[0].disabled = true; },
    (s) => { s.navigation.navigation_items[0].visible = false; },
    (s) => { s.navigation.contract_snapshot_id = "invalid"; },
    (s) => { s.navigation.navigation_epoch = 0; },
  ];
  for (const mutate of mutations) {
    const currentShell = shell();
    mutate(currentShell);
    assert.equal(workspaceConsoleExperienceOwner(currentShell, "overview"), null);
  }
  const store = createWorkspaceConsoleExperienceStore();
  assert.equal(store.enter({ accountId: "old_account", tenantId: "old_tenant", sessionId: "old_session",
    permissions: PERMISSIONS, permissionRevision: 2, permissionFingerprint: FINGERPRINT }, "overview"), false);
  assert.equal(store.begin("overview"), null);
});

test("PRD-145 every public owner dimension fences old results and clears private state", () => {
  const firstOwner = workspaceConsoleExperienceOwner(shell(), "overview");
  const changes = { workspaceId: `wsp_${"3".repeat(32)}`, role: "admin", globalSessionId: "gs_public_2",
    revokeGeneration: 1, globalExpiresAt: "2099-02-01T00:00:00Z", workspaceExpiresAt: "2099-02-01T00:00:00Z",
    permissionRevision: 3, membershipRevision: 4, permissionFingerprint: "a".repeat(64),
    contractSnapshotId: `ncs_${"4".repeat(32)}`, contractGeneration: 2, navigationEpoch: 18,
    permissions: PERMISSIONS.slice(0, 1) };
  for (const [name, value] of Object.entries(changes)) {
    const store = createWorkspaceConsoleExperienceStore();
    store.enter(firstOwner, "overview");
    store.accept(store.begin("overview"), response(), firstOwner);
    const late = store.begin("overview");
    const nextOwner = { ...firstOwner, [name]: value };
    assert.equal(store.enter(nextOwner, "overview"), true);
    assert.equal(late.signal.aborted, true);
    assert.equal(store.accept(late, response(), nextOwner), false);
    assert.equal(store.reject(late, { status: 401 }, nextOwner), false);
    noFacts(store.snapshot());
    assert.equal(store.owns(nextOwner, "overview"), true);
  }
});

test("PRD-145 first responses must match the request authorization and later epochs cannot regress", () => {
  const owner = workspaceConsoleExperienceOwner(shell(), "overview");
  for (const mismatch of [{ permission_revision: 1 }, { permission_revision: 3 },
    { permission_fingerprint: "a".repeat(64) }, { permission_fingerprint: "" }]) {
    const store = createWorkspaceConsoleExperienceStore();
    store.enter(owner, "overview");
    assert.equal(store.accept(store.begin("overview"), response(mismatch), owner), false);
    assert.equal(store.snapshot().status, "conflict");
    noFacts(store.snapshot());
    assert.equal(store.begin("overview"), null);
  }
  const store = createWorkspaceConsoleExperienceStore();
  store.enter(owner, "overview");
  assert.equal(store.accept(store.begin("overview"), response({ navigation_epoch: 4 }), owner), true);
  assert.equal(store.accept(store.begin("overview"), response({ navigation_epoch: 3 }), owner), false);
  noFacts(store.snapshot());
});

test("PRD-145 current authentication errors clear assistant text and approvals cursor history", () => {
  for (const status of [401, 403, 409]) {
    const store = createWorkspaceConsoleExperienceStore();
    const owner = workspaceConsoleExperienceOwner(shell("admin-assistant"), "admin-assistant");
    store.enter(owner, "admin-assistant");
    store.setAssistantDraft(owner, "Private test draft");
    store.accept(store.begin("assistant"), response(), owner);
    assert.equal(store.reject(store.begin("assistant"), { status }, owner), true);
    noFacts(store.snapshot());
    assert.equal(store.setAssistantDraft(owner, "Must revalidate"), false);

    const approvalOwner = workspaceConsoleExperienceOwner(shell("approvals"), "approvals");
    store.enter(approvalOwner, "approvals");
    store.accept(store.begin("approvals"), response({ next_cursor: "opaque_cursor" }), approvalOwner);
    store.nextApprovals(approvalOwner);
    assert.equal(store.reject(store.begin("approvals"), { status }, approvalOwner), true);
    noFacts(store.snapshot());
    assert.equal(store.snapshot().approvalPage, 1);
  }
});

test("PRD-145 changing views erases drafts and refuses the previous view's operations", () => {
  const store = createWorkspaceConsoleExperienceStore();
  const owner = workspaceConsoleExperienceOwner(shell("admin-assistant"), "admin-assistant");
  store.enter(owner, "admin-assistant");
  store.setAssistantDraft(owner, "Private test draft");
  const pending = store.begin("assistant");
  store.enter(owner, "approvals");
  assert.equal(pending.signal.aborted, true);
  noFacts(store.snapshot());
  assert.equal(store.begin("assistant"), null);
  assert.equal(store.setAssistantDraft(owner, "Late input"), false);
  assert.equal(store.clearAssistant(owner), false);
  assert.equal(store.accept(pending, response(), owner), false);
  store.enter(owner, "overview");
  assert.equal(store.configureApprovals(owner, { status: "pending" }), false);
  assert.equal(store.nextApprovals(owner), false);
  assert.equal(store.previousApprovals(owner), false);
});

test("PRD-145 a synchronous owner change during error rendering cannot log out the replacement", async () => {
  let controller, reenter = false, invalid = 0;
  controller = createWorkspaceConsoleExperienceController({ api: {
    consoleOverview: async () => { throw { status: 401 }; },
  }, onChange(snapshot) {
    if (reenter && snapshot.status === "error") {
      reenter = false;
      controller.enter(shell(), "overview");
    }
  }, onSessionInvalid: () => { invalid += 1; } });
  controller.enter(shell(), "overview");
  reenter = true;
  assert.equal(await controller.load(), false);
  assert.equal(invalid, 0);
  assert.equal(controller.owns(shell(), "overview"), true);
});

for (const outcome of ["success", "401", "403", "409"]) {
  test(`PRD-145 old ${outcome} callback cannot affect a replacement Cookie owner`, async () => {
    const old = deferred();
    let calls = 0, invalid = 0, revoked = 0;
    const controller = createWorkspaceConsoleExperienceController({ api: {
      consoleOverview: async () => ++calls === 1 ? old.promise : response(),
    }, onSessionInvalid: () => { invalid += 1; }, onPermissionRevoked: () => { revoked += 1; } });
    const first = shell();
    controller.enter(first, "overview");
    const pending = controller.load();
    const second = shell();
    second.workspaceIdentity.current_session.session_id = "gs_public_2";
    second.workspaceOwner = workspaceNavigationOwner(second.workspaceIdentity, {
      context: second.workspaceContext, authorization: second.workspaceAuthorization,
    });
    controller.enter(second, "overview");
    assert.equal(await controller.load(), true);
    if (outcome === "success") old.resolve(response({ marker: "old" }));
    else old.reject({ status: Number(outcome) });
    assert.equal(await pending, false);
    assert.equal(controller.snapshot().overview.marker, undefined);
    assert.equal(controller.owns(second, "overview"), true);
    assert.equal(invalid + revoked, 0);
  });
}

test("PRD-145 expiration prevents both a new request and a late response", () => {
  const originalNow = Date.now;
  try {
    Date.now = () => Date.parse("2098-12-31T23:59:59Z");
    const owner = workspaceConsoleExperienceOwner(shell(), "overview");
    const store = createWorkspaceConsoleExperienceStore();
    store.enter(owner, "overview");
    const pending = store.begin("overview");
    Date.now = () => Date.parse(EXPIRY);
    assert.equal(store.accept(pending, response(), owner), false);
    assert.equal(pending.signal.aborted, true);
    noFacts(store.snapshot());
    assert.equal(store.begin("overview"), null);
    assert.equal(workspaceConsoleExperienceOwner(shell(), "overview"), null);
  } finally { Date.now = originalNow; }
});

test("PRD-145 all Cookie clients reject redirects and preserve response body cancellation", async () => {
  globalThis.document = { cookie: `__Host-metahub-workspace-csrf=${"w".repeat(43)}` };
  for (const client of [getWorkspaceConsoleOverview, queryWorkspaceConsoleAssistant,
    listWorkspaceConsoleApprovals, getWorkspaceDeploymentSummary]) {
    const ready = deferred();
    const controller = new AbortController();
    globalThis.fetch = async (_url, options) => {
      assert.equal(options.credentials, "include");
      assert.equal(options.redirect, "error");
      assert.equal(new Headers(options.headers).has("authorization"), false);
      return { ok: true, status: 200, json: () => new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        ready.resolve();
      }) };
    };
    const pending = client({ controlApiUrl: "https://console.invalid", question: "Summarize operations", signal: controller.signal });
    const check = assert.rejects(pending, (error) => error.name === "AbortError");
    await ready.promise;
    controller.abort();
    await check;
  }
});

test("PRD-145 main binds all Cookie origins and clears experience state on peer invalidation", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  for (const name of ["getWorkspaceConsoleOverview", "queryWorkspaceConsoleAssistant",
    "listWorkspaceConsoleApprovals", "getWorkspaceDeploymentSummary"]) {
    assert.match(main, new RegExp(`${name}\\(\\{ controlApiUrl: workspaceControlApiUrl, \\.\\.\\.input \\}\\)`));
  }
  assert.match(main, /workspaceConsoleExperienceController\.enter\(consoleShellState, view\)/u);
  assert.match(main, /workspaceConsoleExperienceController\.owns\(consoleShellState, view\)/u);
  const start = main.indexOf("function handleConsoleWorkspaceContextInvalidation()");
  const end = main.indexOf("export function closeConsoleWorkspaceContextInvalidation", start);
  const invalidation = main.slice(start, end);
  assert.match(invalidation, /clearConsoleSessionState\(\);/u);
  const cleanupStart = main.indexOf("function clearConsoleSessionState(");
  const cleanupEnd = main.indexOf("function invitationOwnerInput(", cleanupStart);
  assert.ok(cleanupStart > 0 && cleanupEnd > cleanupStart);
  const cleanup = main.slice(cleanupStart, cleanupEnd);
  assert.match(cleanup, /workspaceConsoleExperienceShellState = null;/u);
  assert.match(cleanup, /workspaceConsoleExperienceController\.leave\(\);/u);
  const renderStart = main.indexOf("function renderCurrentWorkspaceConsoleExperience()");
  const renderEnd = main.indexOf("function bindWorkspaceConsoleExperiencePageInteractions", renderStart);
  assert.match(main.slice(renderStart, renderEnd), /bindWorkspaceConsoleExperience\(panel, \{ onRefresh: \(\) => refresh\(\) \}\)/u);
});
