import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { hasWorkspaceConsoleSession, isConsoleRefreshOwnerCurrent, sameWorkspaceConsoleSession,
  workspaceConsoleSessionFromShell } from "../../src/lib/workspaceConsoleSession.js";
import { isConsoleNavigationOwnerCurrent } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderWorkspaceSessionPrompt } from "../../src/features/auth/renderLoginPage.js";
import { renderConsoleShell } from "../../src/features/console-shell/renderConsoleShell.js";

const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
const EXPIRY = "2099-01-01T00:00:00Z";
function shell() {
  const identity = { account: { status: "active", display_name: "Test account" },
    current_session: { session_id: "public_session", is_current: true, revoke_generation: 0, expires_at: EXPIRY } };
  const context = { workspace_id: `wsp_${"1".repeat(32)}`, name: "Test workspace",
    role: "owner", session_status: "active", expires_at: EXPIRY };
  const authorization = { permission_codes: ["workspace.console.overview.read"],
    permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64) };
  return { state: "ok", workspaceNavigationContract: true, locale: "zh-CN",
    workspaceIdentity: identity, workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    activeNavigationItemId: "overview", activeRouteCode: "workspace.home.overview",
    navigation: { permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64),
      navigation_items: [{ id: "overview", label: "Overview", target_section: "overview",
        route_code: "workspace.home.overview", visible: true, disabled: false }],
      preference: { sidebar_collapsed: false }, groups: [] } };
}
function section(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, startMarker);
  return source.slice(start, end);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

test("PRD-146 Console session contains immutable real authorization without Legacy mirrors", () => {
  const input = shell();
  const session = workspaceConsoleSessionFromShell(input);
  assert.equal(hasWorkspaceConsoleSession(session), true);
  assert.deepEqual(session.context, input.workspaceAuthorization);
  assert.equal(Object.isFrozen(session), true);
  assert.equal(Object.isFrozen(session.owner), true);
  assert.equal(Object.isFrozen(session.context), true);
  assert.equal(Object.isFrozen(session.context.permission_codes), true);
  input.workspaceAuthorization.permission_codes.push("injected.permission");
  assert.deepEqual(session.context.permission_codes, ["workspace.console.overview.read"]);
  for (const field of ["token", "tenant", "user", "session"]) assert.equal(Object.hasOwn(session, field), false);
  assert.equal(sameWorkspaceConsoleSession(session, workspaceConsoleSessionFromShell(shell())), true);
});

const invalidShells = {
  missingIdentity: (s) => { delete s.workspaceIdentity; },
  missingOwner: (s) => { delete s.workspaceOwner; },
  missingAuthorization: (s) => { delete s.workspaceAuthorization; },
  inactiveAccount: (s) => { s.workspaceIdentity.account.status = "disabled"; },
  oldGlobalSession: (s) => { s.workspaceIdentity.current_session.is_current = false; },
  replacedSession: (s) => { s.workspaceIdentity.current_session.session_id = "new_session"; },
  revokedGeneration: (s) => { s.workspaceIdentity.current_session.revoke_generation += 1; },
  expiredGlobal: (s) => { s.workspaceIdentity.current_session.expires_at = "2000-01-01T00:00:00Z"; },
  expiredWorkspace: (s) => { s.workspaceContext.expires_at = "2000-01-01T00:00:00Z"; },
  invalidExpiry: (s) => { s.workspaceContext.expires_at = "invalid"; },
  revokedWorkspace: (s) => { s.workspaceContext.session_status = "revoked"; },
  changedWorkspace: (s) => { s.workspaceContext.workspace_id = `wsp_${"2".repeat(32)}`; },
  changedRole: (s) => { s.workspaceContext.role = "viewer"; },
  permissionRevision: (s) => { s.workspaceAuthorization.permission_revision += 1; },
  membershipRevision: (s) => { s.workspaceAuthorization.membership_revision += 1; },
  permissionFingerprint: (s) => { s.workspaceAuthorization.permission_fingerprint = "b".repeat(64); },
  missingPermissionCodes: (s) => { delete s.workspaceAuthorization.permission_codes; },
  invalidPermissionCodes: (s) => { s.workspaceAuthorization.permission_codes = [null]; },
  navigationRevision: (s) => { s.navigation.permission_revision += 1; },
  navigationMembership: (s) => { s.navigation.membership_revision += 1; },
  navigationFingerprint: (s) => { s.navigation.permission_fingerprint = "b".repeat(64); },
  legacyNavigation: (s) => { s.workspaceNavigationContract = false; },
  errorShell: (s) => { s.state = "error"; },
};
for (const [name, mutate] of Object.entries(invalidShells)) {
  test(`PRD-146 Console entry rejects ${name}`, () => {
    const input = shell();
    mutate(input);
    assert.equal(workspaceConsoleSessionFromShell(input), null);
  });
}

test("PRD-146 direct session checks reject malformed owners and mixed credentials", () => {
  const session = workspaceConsoleSessionFromShell(shell());
  for (const value of [null, {}, { authentication: "legacy" }, { ...session, token: "legacy" },
    { ...session, owner: { ...session.owner, workspaceId: "legacy-tenant" } },
    { ...session, owner: { ...session.owner, globalSessionId: "" } },
    { ...session, owner: { ...session.owner, revokeGeneration: -1 } },
    { ...session, owner: { ...session.owner, workspaceExpiresAt: "2000-01-01T00:00:00Z" } }]) {
    assert.equal(hasWorkspaceConsoleSession(value), false);
    assert.equal(sameWorkspaceConsoleSession(session, value), false);
  }
  assert.equal(workspaceConsoleSessionFromShell(null), null);
});

test("PRD-146 refresh allows initial validation but navigation requires an authenticated owner", () => {
  const initial = { context: null, owner: null };
  const owner = { generation: 1, session: initial };
  assert.equal(isConsoleRefreshOwnerCurrent(owner, owner), true);
  assert.equal(isConsoleNavigationOwnerCurrent(owner, owner), false);
  assert.equal(isConsoleRefreshOwnerCurrent({ generation: 1 }, { generation: 1 }), false);
  const session = workspaceConsoleSessionFromShell(shell());
  const live = { generation: 2, session };
  assert.equal(isConsoleNavigationOwnerCurrent(live, live), true);
  assert.equal(isConsoleNavigationOwnerCurrent(live, { generation: 3, session }), false);
  assert.equal(isConsoleNavigationOwnerCurrent(live, { generation: 2,
    session: workspaceConsoleSessionFromShell(shell()) }), false);
});

const cleanupFunctions = ["clearDeveloperContractSecurityLoad", "clearEnterpriseChatSessionState",
  "clearPromptTemplateSessionState", "clearPolicyTemplateLibrarySessionState", "clearBudgetSessionState",
  "clearPolicyEditorSessionState", "clearPolicySnapshotSessionState", "clearPolicyDryRunSessionState",
  "clearIntentSecuritySessionState", "clearRoutingPolicySessionState", "clearModelQualityScoreSessionState",
  "clearRequestLogSessionState", "clearRequestExplanationSessionState", "clearRequestFeedbackSummarySessionState",
  "clearUsageExportSessionState", "clearAuditExportSessionState", "clearWebhookSessionState",
  "clearDeveloperContractDownload", "clearPublicBrandingPreview", "closeWorkspaceAdvancedToolsDialog",
  "cancelPendingConsoleNavigationSideEffects"];
const pageStates = ["providerStatusPageState", "finOpsPageState", "costAnomalyPageState", "sessionRiskPageState",
  "filePageState", "fileSecurityPageState", "batchJobPageState", "tenantDataLifecyclePageState", "mcpToolPageState",
  "localEndpointPageState", "privateGatewayPageState", "migrationOperationPageState", "supportTicketPageState",
  "experimentPageState", "responseCachePageState", "quotaManagementPageState", "webhookDeadLetterPageState",
  "rolePermissionPageState", "invitationPageState"];

// Run the complete production functions with controlled I/O, not a rewritten lifecycle model.
function harness() {
  const effects = [];
  let click;
  const scope = {
    authSession: { context: null, owner: null }, refreshSequence: 1, consoleLogoutPending: false,
    providersLoadController: null, consoleSidebarController: null, nextRefreshIsBackground: false,
    expandedConsoleGroupKey: null, activeRefreshCount: 0,
    hasWorkspaceConsoleSession, sameWorkspaceConsoleSession, workspaceConsoleSessionFromShell,
    isConsoleRefreshOwnerCurrent, currentI18n: null, createI18n: () => ({}), DEFAULT_LOCALE: "zh-CN",
    WORKSPACE_PROVIDER_ENGINEERING_NAVIGATION_IDS: new Set(), WORKSPACE_COMMERCIAL_NAVIGATION_IDS: new Set(),
    REQUEST_LOG_NAVIGATION_IDS: new Set(), usageExportState: { status: "idle" }, auditExportState: { status: "idle" },
    workspaceProviderEngineeringController: { snapshot: () => ({}), close() {} },
    workspaceCommercialAccountingController: { close() {} },
    workspaceConsoleExperienceController: { leave: () => effects.push("experience-leave") },
    workspaceNavigationController: { leave: () => effects.push("navigation-leave") },
    deploymentBrandingController: { clear: () => effects.push("branding-clear") },
    gatewayDebugLifecycle: { leave: () => effects.push("debug-leave") },
    migrationGuideVisitTracker: { leave() {} }, providerStatusVisitTracker: { leave() {} },
    readActiveNavigationHash: () => "", publicBrandingGeneration: 0, publicBrandingPreview: null,
    loadPublicBrandingPreview: async () => {},
    app: { replaceChildren: () => effects.push("dom-clear") },
    document: { querySelector: (selector) => selector === "#logout-button"
      ? { addEventListener: (_event, handler) => { click = handler; } } : null },
    clearAuthSession: () => effects.push("storage-clear"),
    clearConsoleSessionState: (options) => effects.push(options?.clearNavigation === false ? "owner-clear" : "all-clear"),
    loadConsoleShellState: async () => shell(),
    loadAndRenderActiveNavigation: async (input) => {
      assert.equal(input.refreshOwnerCurrent(), true);
      assert.equal(hasWorkspaceConsoleSession(scope.authSession), true);
      effects.push("hydrate");
    },
    renderWorkspaceSessionPrompt: (_app, input) => effects.push(input.pending ? "pending-prompt" : "session-prompt"),
    renderLogin: (error) => effects.push(error ? "error-prompt" : "signed-out"),
    authErrorMessage: () => "Unavailable", workspaceControlApiUrl: "https://control.test",
    logoutIdentity: async () => effects.push("logout-request"),
    consoleWorkspaceContextChannel: { postMessage: (message) => {
      assert.deepEqual(Object.keys(message).sort(), ["generation", "type"]);
      assert.equal(message.type, "context-invalidated");
      assert.equal(Number.isSafeInteger(message.generation), true);
      effects.push("broadcast");
    } },
  };
  for (const name of cleanupFunctions) scope[name] = () => effects.push(name);
  for (const name of pageStates) scope[name] = {
    clear: () => effects.push(name), syncOwner: (owner) => { assert.equal(owner.token, null); },
  };
  const lifecycle = section("async function performRefresh(", "async function loadAndRenderActiveNavigation(");
  const refresh = section("async function refresh(", "export function isConsoleWorkspaceContextInvalidation");
  const invalidate = section("function handleConsoleWorkspaceContextInvalidation()", "export function closeConsoleWorkspaceContextInvalidation");
  const broadcast = section("function broadcastConsoleWorkspaceContextInvalidation()", "async function performRefresh(");
  const logout = section('  const logoutButton = document.querySelector("#logout-button");', "  for (const navigationButton");
  const api = runInNewContext(`${lifecycle}\n${refresh}\n${invalidate}\n${broadcast}\n${logout}
    ({ performRefresh, refresh, handleConsoleWorkspaceContextInvalidation });`, scope);
  return { scope, effects, api, logout: () => click() };
}

test("PRD-146 startup authenticates real Cookie shell with no stored credential", async () => {
  const { scope, effects } = harness();
  const start = runInNewContext(`${section("async function start()", "async function refresh(")}\nstart;`, scope);
  await start();
  assert.ok(effects.indexOf("storage-clear") < effects.indexOf("owner-clear"));
  assert.ok(effects.indexOf("owner-clear") < effects.indexOf("hydrate"));
  assert.equal(hasWorkspaceConsoleSession(scope.authSession), true);
  assert.equal(effects.includes("signed-out"), false);
});

test("PRD-146 equivalent owner refresh keeps page drafts but uses a fresh request owner", async () => {
  const { scope, effects, api } = harness();
  scope.authSession = workspaceConsoleSessionFromShell(shell());
  const oldSession = scope.authSession;
  await api.refresh();
  assert.equal(effects.includes("owner-clear"), false);
  assert.equal(effects.includes("dom-clear"), false);
  assert.notEqual(scope.authSession, oldSession);
  assert.equal(sameWorkspaceConsoleSession(scope.authSession, oldSession), true);
  assert.equal(effects.includes("hydrate"), true);
  assert.equal(scope.activeRefreshCount, 0);
});

for (const late of ["success", "error"]) {
  test(`PRD-146 superseded refresh ${late} cannot replace the newer Cookie owner`, async () => {
    const { scope, effects, api } = harness();
    const pending = deferred();
    scope.loadConsoleShellState = () => pending.promise;
    const old = api.refresh();
    scope.loadConsoleShellState = async () => shell();
    await api.refresh();
    const installed = scope.authSession;
    const savedEffects = [...effects];
    pending.resolve(late === "success" ? shell() : { state: "error", error: { status: 401 } });
    await old;
    assert.equal(scope.authSession, installed);
    assert.deepEqual(effects, savedEffects);
    assert.equal(scope.activeRefreshCount, 0);
  });
}

test("PRD-146 malformed current shell never enters the page dispatcher", async () => {
  const { scope, effects, api } = harness();
  scope.loadConsoleShellState = async () => ({ state: "ok" });
  await api.refresh();
  assert.equal(effects.includes("hydrate"), false);
  assert.equal(effects.includes("signed-out"), true);
});

for (const result of ["success", "401", "503"]) {
  test(`PRD-146 logout ${result} clears before HTTP and suppresses refresh or peer reentry`, async () => {
    const { scope, effects, api, logout } = harness();
    scope.authSession = workspaceConsoleSessionFromShell(shell());
    const pending = deferred();
    let requests = 0;
    scope.logoutIdentity = (input) => {
      assert.equal(input.controlApiUrl, scope.workspaceControlApiUrl);
      assert.equal(Object.hasOwn(input, "token"), false);
      assert.equal(scope.authSession.context, null);
      assert.equal(effects.includes("pending-prompt"), true);
      requests += 1;
      return pending.promise;
    };
    const leaving = logout();
    assert.deepEqual(effects, ["all-clear", "storage-clear", "pending-prompt", "broadcast"]);
    scope.loadConsoleShellState = () => { throw new Error("logout must not reauthenticate"); };
    await api.refresh();
    api.handleConsoleWorkspaceContextInvalidation();
    await logout();
    assert.equal(requests, 1);
    if (result === "success") pending.resolve({});
    else pending.reject({ status: Number(result) });
    await leaving;
    assert.equal(scope.consoleLogoutPending, false);
    assert.deepEqual(effects.slice(-2), ["broadcast", result === "503" ? "error-prompt" : "signed-out"]);
  });
}

for (const rejected of [false, true]) {
  test(`PRD-146 old logout ${rejected ? "failure" : "success"} never redraws a new page owner`, async () => {
    const { scope, effects, logout } = harness();
    const pending = deferred();
    scope.logoutIdentity = () => pending.promise;
    const leaving = logout();
    scope.refreshSequence += 1;
    const newSession = workspaceConsoleSessionFromShell(shell());
    scope.authSession = newSession;
    if (rejected) pending.reject({ status: 503 });
    else pending.resolve({});
    await leaving;
    assert.equal(scope.authSession, newSession);
    assert.equal(effects.includes("signed-out"), false);
    assert.equal(effects.includes("error-prompt"), false);
  });
}

test("PRD-146 shared cleanup cancels old owners while optionally preserving new navigation", () => {
  for (const preserve of [false, true]) {
    const { scope, effects } = harness();
    scope.providersLoadController = { abort: () => effects.push("providers-abort") };
    scope.consoleSidebarController = { abort: () => effects.push("sidebar-abort") };
    const clear = runInNewContext(`${section("function clearConsoleSessionState(", "function invitationOwnerInput(")}
      clearConsoleSessionState;`, scope);
    clear({ clearNavigation: !preserve });
    assert.equal(effects.includes("navigation-leave"), !preserve);
    for (const name of [...cleanupFunctions, ...pageStates, "experience-leave", "providers-abort", "sidebar-abort"]) {
      assert.equal(effects.includes(name), true, name);
    }
    assert.equal(scope.workspaceConsoleExperienceShellState, null);
    assert.equal(scope.providersLoadController, null);
    assert.equal(scope.consoleSidebarController, null);
  }
});

test("PRD-146 session prompt is escaped, retryable and never exposes a password form", () => {
  const container = {};
  renderWorkspaceSessionPrompt(container, { error: '<img src=x onerror="bad">', requestId: "<request>" });
  assert.match(container.innerHTML, /&lt;img/);
  assert.match(container.innerHTML, /&lt;request&gt;/);
  assert.match(container.innerHTML, /href="\/identity.html"/);
  assert.match(container.innerHTML, /data-console-session-retry/);
  assert.doesNotMatch(container.innerHTML, /<form|<input|undefined|<img/);
  renderWorkspaceSessionPrompt(container, { pending: true });
  assert.doesNotMatch(container.innerHTML, /<a |<button/);
});

test("PRD-146 old Workspace navigation controls cannot act on the current session", async () => {
  for (const [start, end] of [
    ["async function switchConsoleNavigation(", "function cancelPendingConsoleNavigationSideEffects("],
    ["async function toggleConsoleSidebarGroup(", "function applyExpandedConsoleGroup("],
    ["async function toggleConsoleSidebar(", "function applyConsoleSidebarCollapsedState("],
  ]) {
    const { scope, effects } = harness();
    scope.authSession = workspaceConsoleSessionFromShell(shell());
    scope.sidebarGroupSequence = 0;
    const previous = shell();
    previous.workspaceIdentity.current_session.session_id = "old_session";
    previous.workspaceOwner = workspaceNavigationOwner(previous.workspaceIdentity,
      { context: previous.workspaceContext, authorization: previous.workspaceAuthorization });
    const name = start.match(/function (\w+)/)[1];
    const control = runInNewContext(`${section(start, end)}\n${name};`, scope);
    await control({ closest: () => ({}) }, previous);
    assert.deepEqual(effects, []);
    assert.equal(scope.refreshSequence, 1);
  }
});

for (const status of [401, 403, 409, 503]) {
  for (const stale of [false, true]) {
    test(`PRD-146 group preference ${status} ${stale ? "stale" : "current"} response is owner-fenced`, async () => {
      const { scope, effects } = harness();
      scope.authSession = workspaceConsoleSessionFromShell(shell());
      scope.sidebarGroupSequence = 0;
      const pending = deferred();
      scope.workspaceNavigationController.savePreference = () => pending.promise;
      scope.applyExpandedConsoleGroup = (key) => effects.push(`expand:${key}`);
      scope.renderConsoleSidebarPreferenceError = () => effects.push("preference-error");
      scope.handleConsoleWorkspaceContextInvalidation = () => effects.push("invalidate");
      const toggle = runInNewContext(`${section("async function toggleConsoleSidebarGroup(",
        "function applyExpandedConsoleGroup(")}\ntoggleConsoleSidebarGroup;`, scope);
      const operation = toggle({ dataset: { sidebarGroupToggle: "home" },
        closest: () => ({ querySelector: () => ({}) }), getAttribute: () => "false" }, shell());
      if (stale) scope.refreshSequence += 1;
      pending.reject({ status });
      await operation;
      assert.deepEqual(effects, stale ? ["expand:home"] : status === 503
        ? ["expand:home", "expand:null", "preference-error"] : ["expand:home", "invalidate"]);
    });
  }
}

test("PRD-146 shell menu renders the real Workspace account without Legacy user fields", () => {
  const current = shell();
  const html = renderConsoleShell({ consoleShellState: current, contentHtml: "" });
  assert.match(html, /Test account/);
  assert.match(html, /Test workspace/);
  assert.match(html, /id="logout-button"/);
  assert.doesNotMatch(html, /undefined/);
});
