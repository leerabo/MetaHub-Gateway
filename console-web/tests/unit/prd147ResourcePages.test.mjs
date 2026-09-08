import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, hasPermission } from "../../src/lib/permissions.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { workspaceConsoleSessionFromShell, sameWorkspaceConsoleSession, hasWorkspaceConsoleSession } from "../../src/lib/workspaceConsoleSession.js";
import { createWorkspaceResourcePage, workspaceResourcePageOwner } from "../../src/lib/workspaceResourcePage.js";
import { createFilePageState, filePageOwner, isFileAbortError } from "../../src/features/files/filePageState.js";
import { bindFileInteractions } from "../../src/features/files/bindFiles.js";
import { renderVirtualKeyPanel } from "../../src/features/virtual-keys/renderVirtualKeyPanel.js";

const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
const allPermissions = Object.values(PERMISSIONS);
const expiry = "2099-01-01T00:00:00Z";
function shell(navigation = "projects", workspace = "1", permissions = allPermissions) {
  const identity = { account: { status: "active" }, current_session: {
    session_id: "public_session", is_current: true, revoke_generation: 0, expires_at: expiry,
  } };
  const context = { workspace_id: `wsp_${workspace.repeat(32)}`, role: "owner", session_status: "active", expires_at: expiry };
  const authorization = { permission_codes: [...permissions], permission_revision: 1,
    membership_revision: 2, permission_fingerprint: "a".repeat(64) };
  const route = WORKSPACE_SOURCE_ROUTE_REGISTRY[navigation];
  return { state: "ok", workspaceNavigationContract: true, activeNavigationItemId: navigation, activeRouteCode: route,
    workspaceIdentity: identity, workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    navigation: { permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64),
      active_navigation_item_id: navigation, active_route_code: route,
      navigation_items: [{ id: navigation, route_code: route, target_section: navigation, visible: true, disabled: false }] } };
}

function production(name) {
  const start = source.indexOf(`async function ${name}(`) >= 0
    ? source.indexOf(`async function ${name}(`) : source.indexOf(`function ${name}(`);
  const end = start + source.slice(start).search(/\n}\r?\n/);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 2);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
class Element {
  constructor() {
    this.isConnected = true;
    this.listeners = new Map();
    this.dataset = { environmentId: "env_sample", providerId: "provider_sample", providerKeyId: "pkey_sample",
      virtualKeyId: "vkey_sample", modelId: "model_sample", entryCode: "sample", providerTab: "providers",
      providerProbe: "provider_sample", providerStatusOffset: "50" };
    this.values = { name: "Sample", status: "disabled", confirmed: "on", entryCode: "sample", version: "2026.09.08.1",
      regionCode: "cn-east", capabilityCodes: "chat", providerCode: "sample", timeoutSeconds: "5" };
    this.button = { disabled: false };
  }
  addEventListener(event, handler) {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), handler]);
  }
  querySelector() { return this.button; }
  closest() { return { replaceChildren: () => { this.cleared = true; } }; }
  fire(event) { return Promise.all((this.listeners.get(event) ?? []).map((handler) => handler({ preventDefault() {}, currentTarget: this }))); }
}

const helpers = ["clearBasicResourceFeedback", "clearBasicResourceSessionState", "bindResourceElement",
  "handleResourceAuthorizationError", "handleResourceMutationError", "resourceMutationRequest", "refreshResourceMutation",
  "loadResourceState", "loadModelsState", "loadEnvironmentState", "loadProjectState", "loadVirtualKeysState", "loadProvidersState",
  "currentFileReadOwner", "currentFileWriteOwner", "bindFilesPageInteractions", "submitFileUpload", "loadFilesState",
  "loadAndRenderActiveNavigation", "loadProviderHealthState", "loadProviderStatusState", "runProviderProbe",
  "ownProviderStatusVisitAfterRender"];
const payload = { state: "ok", providers: [{ provider_id: "provider_sample" }], projects: [], applications: [], models: [],
  virtual_keys: [], files: [], next_cursor: null, file: { file_id: "file_sample" }, plain_value: "synthetic-one-time-value",
  virtual_key: { key_prefix: "synthetic" }, entry_code: "sample" };
const clients = ["createEnvironment", "updateEnvironment", "createProject", "createApplication", "createVirtualKey",
  "disableVirtualKey", "updateVirtualKey", "updateVirtualKeyLimits", "getProviderCatalogCompatibility", "createProviderFromCatalog",
  "preflightCatalogProvider", "createProvider", "updateProvider", "testProviderConnection", "createProviderKey", "updateProviderKey",
  "createModel", "updateModel", "listEnvironments", "listProjects", "listApplications", "listVirtualKeys", "listProviders",
  "listProviderKeys", "listProviderCatalog", "listModels", "listFiles", "uploadFile", "listProviderStatuses", "probeProvider"];

function harness(navigation, selector) {
  const element = new Element();
  const calls = [], effects = [], scheduled = [];
  const pending = deferred();
  const scope = { AuthApiError, DOMException, AbortController, PERMISSIONS, hasPermission, filePageOwner, isFileAbortError,
    hasWorkspaceConsoleSession, gatewayUrl: "https://gateway.example.test", consoleUrl: "https://console.example.test",
    currentI18n: null, gatewayDebugState: {}, policyDryRunState: {},
    workspaceConsoleExperienceView: () => null,
    workspaceConsoleExperienceController: { leave() {}, snapshot: () => ({}) },
    enterpriseChatPageState: { snapshot: () => ({}) }, invitationPageState: { snapshot: () => ({}) },
    publicBrandingDisplay: () => null, renderDeveloperStart: (_app, state) => { effects.push("render"); scope.rendered = state; },
    bindOutputSafetyDetailInteractions() {}, bindAuthenticatedEvents() {},
    navigator: { clipboard: { writeText: (value) => { effects.push("clipboard"); scope.copied = value; return pending.promise; } } },
    filePageState: createFilePageState(), controlApiUrl: "https://control.example.test", refreshSequence: 1, hash: navigation,
    providerStatusPageState: { clear() {} }, providerStatusVisitTracker: { leave() {} },
    workspacePageVisitTracker: { leave() {} }, workspacePageVisitShellState: null,
    providerHealthRequestGeneration: 0, providerHealthController: null,
    RESOURCE_NAVIGATION_IDS: new Set(["environments", "projects", "providers", "models", "virtual-keys", "files", "provider-health"]),
    resourcePageShellState: shell(navigation), authErrorMessage: () => "Fixed error", isAbortError: isFileAbortError,
    createProviderCatalogIdempotencyKey: () => "console:catalog:synthetic:0001",
    optionalNumber: (value, fallback) => value ? Number(value) : fallback,
    optionalText: (value) => value || undefined, readCapabilities: () => ({ chat: true }),
    workspaceProviderEngineeringController: { snapshot: () => ({}), close: () => effects.push("engineering-close") },
    workspaceProviderEngineeringProviders: [],
    refresh: async () => { effects.push("refresh"); },
    clearAuthSession: () => effects.push("clear-auth"),
    renderLogin: () => { effects.push("login"); scope.resourcePageLifecycle.clear(); scope.authSession = {}; },
    queueMicrotask: (fn) => scheduled.push(fn),
    app: { replaceChildren: () => effects.push("clear-dom") },
    document: { querySelector: (value) => value === selector ? element : null,
      querySelectorAll: (value) => value === selector ? [element] : [] },
    FormData: class {
      constructor(form) { effects.push("form-read"); return new URLSearchParams(form.values); }
    },
    bindFileInteractions: (handlers) => { scope.fileHandlers = handlers; },
  };
  scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
  scope.readActiveNavigationHash = () => scope.hash;
  scope.resourcePageLifecycle = createWorkspaceResourcePage({ current: () => ({
    session: scope.authSession, shell: scope.resourcePageShellState, hash: scope.hash, refreshGeneration: scope.refreshSequence,
  }) });
  for (const client of clients) scope[client] = async (input) => { calls.push({ client, input }); return pending.promise; };
  const start = source.indexOf('  const createEnvironmentForm = document.querySelector(');
  const end = source.indexOf('  const createModelAliasForm = document.querySelector(', start);
  const secretStart = source.indexOf('  const dismissSecretButton = document.querySelector(');
  const secretEnd = source.indexOf('  const budgetScopeChainKey = document.querySelector(', secretStart);
  const functions = runInNewContext(`${helpers.map(production).join("\n")}
    function bindActualResourceHandlers() { ${source.slice(start, end)} }
    function bindActualSecretHandlers() { ${source.slice(secretStart, secretEnd)} }
    ({ ${helpers.join(",")}, bindActualResourceHandlers, bindActualSecretHandlers });`, scope);
  Object.assign(scope, functions);
  scope.resourcePageLifecycle.enter();
  functions.clearBasicResourceFeedback();
  scope.resourcePageLifecycle.render();
  function switchOwner() {
    scope.resourcePageShellState = shell(navigation, "2");
    scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
    scope.resourcePageLifecycle.enter();
    functions.clearBasicResourceFeedback();
  }
  return { scope, functions, element, calls, effects, scheduled, pending, switchOwner };
}

const forms = [
  ["provider-health", "[data-provider-probe]", "probeProvider", "click"],
  ["environments", "#create-environment-form", "createEnvironment"],
  ["environments", "[data-environment-status-form]", "updateEnvironment"],
  ["projects", "#create-project-form", "createProject"],
  ["projects", "#create-application-form", "createApplication"],
  ["virtual-keys", "#create-virtual-key-form", "createVirtualKey"],
  ["virtual-keys", "[data-virtual-key-form]", "disableVirtualKey"],
  ["virtual-keys", "[data-virtual-key-form]", "updateVirtualKey", "submit", "active"],
  ["virtual-keys", "[data-virtual-key-limits-form]", "updateVirtualKeyLimits"],
  ["providers", "[data-provider-catalog-compatibility]", "getProviderCatalogCompatibility", "click"],
  ["providers", "[data-provider-catalog-create-form]", "createProviderFromCatalog"],
  ["providers", "[data-provider-catalog-preflight-form]", "preflightCatalogProvider"],
  ["providers", "#create-provider-form", "createProvider"],
  ["providers", "[data-provider-status-form]", "updateProvider"],
  ["providers", "[data-provider-test-form]", "testProviderConnection"],
  ["providers", "[data-provider-key-create-form]", "createProviderKey"],
  ["providers", "[data-provider-key-status-form]", "updateProviderKey"],
  ["models", "#create-model-form", "createModel"],
  ["models", "[data-model-status-form]", "updateModel"],
];
for (const [navigation, selector, client, event = "submit", status] of forms) {
  test(`PRD-147 actual ${client} handler uses Cookie, blocks duplicate submission and refreshes once`, async () => {
    const h = harness(navigation, selector);
    if (status) h.element.values.status = status;
    h.functions.bindActualResourceHandlers();
    const operation = h.element.fire(event);
    await h.element.fire(event);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].client, client);
    assert.equal(h.calls[0].input.authentication, "workspace");
    assert.equal(Object.hasOwn(h.calls[0].input, "token"), false);
    assert.ok(h.calls[0].input.signal instanceof AbortSignal);
    h.pending.resolve(payload);
    await operation;
    assert.equal(h.effects.filter((value) => value === "refresh").length, 1);
    assert.equal(h.effects.includes("login"), false);
    assert.equal(h.scope.resourcePageLifecycle.blocksReplacement(false), false);
    if (client === "createVirtualKey") assert.equal(h.scope.virtualKeyCreatedSecret.plainValue, payload.plain_value);
  });
  for (const boundary of ["workspace", "hash", "detached", "rerender"]) {
    test(`PRD-147 actual ${client} rejects ${boundary} old DOM before reading form data`, async () => {
      const h = harness(navigation, selector);
      h.functions.bindActualResourceHandlers();
      if (boundary === "workspace") h.switchOwner();
      if (boundary === "hash") h.scope.hash = "overview";
      if (boundary === "detached") h.element.isConnected = false;
      if (boundary === "rerender") h.scope.resourcePageLifecycle.render();
      await h.element.fire(event);
      assert.equal(h.calls.length, 0);
      assert.deepEqual(h.effects, []);
    });
  }
  for (const outcome of ["success", "401", "403", "503"]) {
    test(`PRD-147 actual ${client} discards late ${outcome} after Workspace switch`, async () => {
      const h = harness(navigation, selector);
      h.functions.bindActualResourceHandlers();
      const operation = h.element.fire(event);
      assert.equal(h.calls.length, 1);
      h.switchOwner();
      const current = h.scope.authSession;
      if (outcome === "success") h.pending.resolve(payload);
      else h.pending.reject(new AuthApiError({ status: Number(outcome), code: "auth.permission_denied" }));
      await operation;
      assert.equal(h.scope.authSession, current);
      assert.equal(h.scope.virtualKeyCreatedSecret, null);
      assert.equal(h.scope.providerCatalogLastCreated, null);
      assert.equal(h.scope.providerLastTestResult, null);
      assert.equal(h.effects.includes("refresh"), false);
      assert.equal(h.effects.includes("login"), false);
      assert.equal(h.scheduled.length, 0);
      assert.equal(h.calls[0].input.signal.aborted, true);
    });
  }
}

const loaders = [["environments", "loadEnvironmentState"], ["projects", "loadProjectState"],
  ["providers", "loadProvidersState"], ["models", "loadModelsState"], ["virtual-keys", "loadVirtualKeysState"], ["files", "loadFilesState"],
  ["provider-health", "loadProviderHealthState"]];
for (const [navigation, loader] of loaders) {
  test(`PRD-147 actual ${navigation} dispatcher supplies Workspace reads and renders the accepted state`, async () => {
    const h = harness(navigation);
    const session = h.scope.authSession;
    const operation = h.functions.loadAndRenderActiveNavigation({ consoleShellState: h.scope.resourcePageShellState,
      refreshId: 1, isBackgroundRefresh: false, refreshOwnerCurrent: () => session === h.scope.authSession });
    assert.ok(h.calls.length > 0);
    h.pending.resolve(payload);
    await operation;
    assert.equal(h.effects.filter((value) => value === "render").length, 1);
    for (const call of h.calls) {
      assert.equal(call.input.authentication, "workspace");
      assert.equal(Object.hasOwn(call.input, "token"), false);
    }
    if (navigation === "models") assert.deepEqual(h.calls.map((call) => call.client), ["listModels", "listProviders", "listProviderKeys"]);
    if (navigation === "virtual-keys") assert.deepEqual(h.calls.map((call) => call.client), ["listVirtualKeys", "listProjects", "listApplications", "listEnvironments"]);
    if (navigation === "files") assert.deepEqual(h.calls.map((call) => call.client), ["listFiles"]);
  });
  for (const outcome of ["success", "401"]) {
    test(`PRD-147 production ${loader} discards late ${outcome} and cannot sign out a new owner`, async () => {
      const h = harness(navigation);
      const operation = ["loadFilesState", "loadProviderHealthState"].includes(loader)
        ? h.functions[loader](h.scope.resourcePageShellState, 1) : h.functions[loader]({ includeCatalog: true });
      assert.ok(h.calls.length > 0);
      for (const call of h.calls) {
        assert.equal(call.input.authentication, "workspace");
        assert.equal(Object.hasOwn(call.input, "token"), false);
      }
      const requestCount = h.calls.length;
      h.switchOwner();
      const session = h.scope.authSession;
      if (outcome === "success") h.pending.resolve(payload);
      else h.pending.reject(new AuthApiError({ status: 401 }));
      assert.equal(await operation, null);
      assert.equal(h.scope.authSession, session);
      assert.equal(h.calls.length, requestCount, "no follow-up provider Key reads");
      assert.equal(h.effects.includes("login"), false);
    });
  }
  test(`PRD-147 production ${loader} accepts the current owner response`, async () => {
    const h = harness(navigation);
    const operation = ["loadFilesState", "loadProviderHealthState"].includes(loader)
      ? h.functions[loader](h.scope.resourcePageShellState, 1) : h.functions[loader]({ includeCatalog: true });
    h.pending.resolve(payload);
    assert.equal((await operation).state, "ok");
  });
}

for (const [selector, event] of [
  ["#provider-health-filters", "submit"],
  ["[data-provider-health-reset]", "click"],
  ["[data-provider-status-offset]", "click"],
]) {
  for (const stale of ["workspace", "hash", "dom"]) {
    test(`PRD-147 status ${selector} rejects stale ${stale} before reading filters`, async () => {
      const h = harness("provider-health", selector);
      h.functions.bindActualResourceHandlers();
      if (stale === "workspace") h.switchOwner();
      if (stale === "hash") h.scope.hash = "projects";
      if (stale === "dom") h.element.isConnected = false;
      const before = JSON.stringify(h.scope.providerStatusFilters);
      await h.element.fire(event);
      assert.equal(JSON.stringify(h.scope.providerStatusFilters), before);
      assert.deepEqual(h.effects, []);
      assert.equal(h.calls.length, 0);
    });
  }
  test(`PRD-147 current status ${selector} cancels old reads before refreshing`, async () => {
    const h = harness("provider-health", selector);
    h.functions.bindActualResourceHandlers();
    const reading = h.functions.loadProviderHealthState(h.scope.resourcePageShellState);
    await h.element.fire(event);
    assert.equal(h.calls[0].input.signal.aborted, true);
    assert.equal(h.effects.includes("refresh"), true);
    h.pending.resolve(payload);
    assert.equal(await reading, null);
  });
}

test("PRD-147 status page requires both health and request-log permissions", async () => {
  for (const missing of [PERMISSIONS.PROVIDER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ]) {
    const h = harness("provider-health");
    h.scope.resourcePageShellState = shell("provider-health", "1", allPermissions.filter((value) => value !== missing));
    h.scope.authSession = workspaceConsoleSessionFromShell(h.scope.resourcePageShellState);
    h.scope.resourcePageLifecycle.enter();
    assert.equal(await h.functions.loadProviderHealthState(h.scope.resourcePageShellState), null);
    assert.equal(h.calls.length, 0);
  }
});

test("PRD-147 Workspace status does not write an unmigrated Legacy page event", async () => {
  const h = harness("provider-health");
  h.scope.providerStatusIdentitySnapshot = () => { throw new Error("Legacy identity was read"); };
  await h.functions.ownProviderStatusVisitAfterRender(h.scope.resourcePageShellState);
  assert.equal(h.calls.length, 0);
});

test("PRD-147 current status errors preserve filters without recreating a Legacy owner", async () => {
  const h = harness("provider-health");
  h.scope.providerStatusFilters = { health_status: "unhealthy", limit: 50, offset: 50 };
  const loading = h.functions.loadProviderHealthState(h.scope.resourcePageShellState);
  h.pending.reject(new AuthApiError({ status: 503, code: "provider_status.query_unavailable" }));
  const result = await loading;
  assert.equal(result.state, "error");
  assert.equal(result.filters.health_status, "unhealthy");
  assert.equal(result.filters.offset, 50);
  assert.equal(h.effects.includes("login"), false);
});

for (const failed of [false, true]) {
  test(`PRD-147 actual copy handler drops late clipboard ${failed ? "failure" : "success"}`, async () => {
    const h = harness("virtual-keys", "[data-copy-virtual-key]");
    h.scope.virtualKeyCreatedSecret = { plainValue: payload.plain_value };
    h.element.dataset.copyVirtualKey = "untrusted-dom-value";
    h.functions.bindActualSecretHandlers();
    const operation = h.element.fire("click");
    assert.equal(h.scope.copied, payload.plain_value);
    h.switchOwner();
    if (failed) h.pending.reject(new Error("clipboard unavailable"));
    else h.pending.resolve();
    await operation;
    assert.equal(h.scope.virtualKeyFeedback, "");
    assert.equal(h.effects.includes("refresh"), false);
  });
}

for (const selector of ["[data-copy-virtual-key]", "[data-dismiss-virtual-key-secret]"]) {
  test(`PRD-147 ${selector} cannot read or clear a new Workspace secret`, async () => {
    const h = harness("virtual-keys", selector);
    h.functions.bindActualSecretHandlers();
    h.switchOwner();
    h.scope.virtualKeyCreatedSecret = { plainValue: "new-owner-value" };
    await h.element.fire("click");
    assert.equal(h.scope.virtualKeyCreatedSecret.plainValue, "new-owner-value");
    assert.deepEqual(h.effects, []);
  });
}

test("PRD-147 one-time Key renderer does not duplicate plaintext into data attributes", () => {
  const html = renderVirtualKeyPanel({ state: "ok", virtual_keys: [], created: { plainValue: payload.plain_value } },
    workspaceConsoleSessionFromShell(shell("virtual-keys")).context);
  assert.match(html, /data-copy-virtual-key/);
  assert.doesNotMatch(html, /data-copy-virtual-key=/);
  assert.equal(html.split(payload.plain_value).length, 2);
});

test("PRD-147 hide removes visible plaintext before a refresh that may preserve pending forms", async () => {
  const h = harness("virtual-keys", "[data-dismiss-virtual-key-secret]");
  h.scope.virtualKeyCreatedSecret = { plainValue: payload.plain_value };
  h.functions.bindActualSecretHandlers();
  h.scope.refresh = async () => { assert.equal(h.element.cleared, true); };
  await h.element.fire("click");
  assert.equal(h.scope.virtualKeyCreatedSecret, null);
});

for (const selector of ["[data-provider-catalog-create-form]", "[data-provider-catalog-preflight-form]"]) {
  test(`PRD-147 ${selector} releases mutation ownership when secure id generation fails`, async () => {
    const h = harness("providers", selector);
    h.scope.createProviderCatalogIdempotencyKey = () => { throw new AuthApiError({ status: 0, code: "provider_catalog.configuration_invalid" }); };
    h.functions.bindActualResourceHandlers();
    await h.element.fire("submit");
    assert.equal(h.calls.length, 0);
    assert.equal(h.scope.resourcePageLifecycle.blocksReplacement(false), false);
    assert.equal(h.effects.includes("refresh"), true);
  });
}

for (const selector of ["[data-provider-tab]", "[data-provider-catalog-filter-form]"]) {
  test(`PRD-147 ${selector} cannot change a newer page filter or tab`, async () => {
    const h = harness("providers", selector);
    h.functions.bindActualResourceHandlers();
    h.switchOwner();
    await h.element.fire(selector.includes("filter") ? "submit" : "click");
    assert.equal(h.scope.providerCatalogActiveTab, "catalog");
    assert.equal(h.scope.providerCatalogFeedback, "");
    assert.deepEqual(h.effects, []);
  });
}

test("PRD-147 actual file DOM binder rejects stale selection, filter and paging before reading private inputs", async () => {
  const savedDocument = globalThis.document;
  const upload = new Element(), input = new Element(), filter = new Element(), reset = new Element();
  const prev = new Element(), next = new Element();
  const nodes = new Map([["#file-upload-form", upload], ["#file-upload-input", input],
    ["#file-status-filter-form", filter], ["#file-status-filter-reset", reset], ["#file-page-prev", prev], ["#file-page-next", next]]);
  let calls = 0;
  Object.defineProperty(input, "files", { get() { throw new Error("Stale File input read"); } });
  globalThis.document = { querySelector: (selector) => nodes.get(selector) ?? null };
  try {
    bindFileInteractions({ isCurrent: () => false, onUpload: () => calls++, onFilter: () => calls++,
      onResetFilter: () => calls++, onPreviousPage: () => calls++, onNextPage: () => calls++ });
    await input.fire("change");
    await upload.fire("submit");
    await filter.fire("submit");
    await reset.fire("click");
    await prev.fire("click");
    await next.fire("click");
    assert.equal(calls, 0);
  } finally {
    if (savedDocument === undefined) delete globalThis.document;
    else globalThis.document = savedDocument;
  }
});

test("PRD-147 list lane replacement cannot be cleared by an older finally", () => {
  const h = harness("projects");
  const previous = h.scope.resourcePageLifecycle.beginRead("projects", [PERMISSIONS.PROJECT_READ]);
  const latest = h.scope.resourcePageLifecycle.beginRead("projects", [PERMISSIONS.PROJECT_READ]);
  h.scope.resourcePageLifecycle.finishRead(previous);
  assert.equal(previous.controller.signal.aborted, true);
  assert.equal(h.scope.resourcePageLifecycle.isReadCurrent(latest), true);
});

test("PRD-147 actual navigation projection removes old secrets before loading a different resource page", async () => {
  const h = harness("virtual-keys");
  h.scope.virtualKeyCreatedSecret = { plainValue: payload.plain_value };
  const next = shell("models");
  h.scope.hash = "models";
  h.scope.listModels = async () => {
    assert.equal(h.effects.includes("clear-dom"), true);
    assert.equal(h.scope.virtualKeyCreatedSecret, null);
    return h.pending.promise;
  };
  const operation = h.functions.loadAndRenderActiveNavigation({ consoleShellState: next,
    refreshId: 1, isBackgroundRefresh: false, refreshOwnerCurrent: () => true });
  h.pending.resolve(payload);
  await operation;
  assert.equal(h.effects.includes("render"), true);
});

test("PRD-147 resource owner rejects route, navigation, permission and hash drift without Legacy mirrors", () => {
  const mutations = [
    (s) => { s.activeRouteCode = "workspace.gateway.models"; },
    (s) => { s.navigation.active_route_code = "workspace.gateway.models"; },
    (s) => { s.navigation.active_navigation_item_id = "models"; },
    (s) => { s.navigation.navigation_items[0].target_section = "models"; },
    (s) => { s.navigation.navigation_items[0].route_code = "workspace.gateway.models"; },
    (s) => { s.navigation.navigation_items[0].visible = false; },
    (s) => { s.navigation.navigation_items[0].disabled = true; },
    (s) => { s.navigation.navigation_items.push(s.navigation.navigation_items[0]); },
    (s) => { s.workspaceAuthorization.permission_codes = []; },
  ];
  for (const mutate of mutations) {
    const input = shell();
    const session = workspaceConsoleSessionFromShell(input);
    mutate(input);
    assert.equal(workspaceResourcePageOwner(session, input, { permissions: [PERMISSIONS.PROJECT_READ] }), null);
  }
  const input = shell();
  const session = workspaceConsoleSessionFromShell(input);
  assert.ok(workspaceResourcePageOwner(session, input));
  assert.equal(workspaceResourcePageOwner(session, input, { hash: "models" }), null);
  assert.equal(Object.hasOwn(session.context, "user"), false);
  assert.equal(Object.hasOwn(session.context, "tenant"), false);
});

test("PRD-147 same-owner refresh keeps draft and mutation bindings, but read generations advance", async () => {
  const h = harness("virtual-keys", "#create-virtual-key-form");
  h.functions.bindActualResourceHandlers();
  await h.element.fire("input");
  assert.equal(h.scope.resourcePageLifecycle.blocksReplacement(true), true);
  const read = h.scope.resourcePageLifecycle.beginRead("keys", [PERMISSIONS.VIRTUAL_KEY_READ]);
  const operation = h.element.fire("submit");
  h.scope.refreshSequence += 1;
  h.scope.resourcePageLifecycle.cancelReads();
  h.scope.resourcePageShellState = shell("virtual-keys");
  assert.equal(h.scope.resourcePageLifecycle.enter().changed, false);
  assert.equal(h.scope.resourcePageLifecycle.blocksReplacement(false), true);
  assert.equal(h.scope.resourcePageLifecycle.isReadCurrent(read), false);
  assert.equal(read.controller.signal.aborted, true);
  h.pending.resolve(payload);
  await operation;
  assert.equal(h.scope.virtualKeyCreatedSecret.plainValue, payload.plain_value);
  assert.equal(h.effects.includes("refresh"), true);
});

test("PRD-147 current permission rejection clears secrets and fences its scheduled refresh", async () => {
  const h = harness("models", "#create-model-form");
  h.functions.bindActualResourceHandlers();
  const operation = h.element.fire("submit");
  h.pending.reject(new AuthApiError({ status: 403, code: "auth.permission_denied" }));
  await operation;
  assert.equal(h.scheduled.length, 1);
  assert.equal(h.effects.includes("clear-dom"), true);
  h.switchOwner();
  h.scheduled[0]();
  assert.equal(h.effects.includes("refresh"), false);
});

test("PRD-147 changing permissions with the same public fingerprint cannot reuse a session", () => {
  const left = workspaceConsoleSessionFromShell(shell());
  const right = workspaceConsoleSessionFromShell(shell("projects", "1", [PERMISSIONS.PROJECT_READ]));
  assert.equal(sameWorkspaceConsoleSession(left, right), false);
});

test("PRD-147 File Workspace owner preserves private cursors without inventing a tenant identity", () => {
  const input = shell("files");
  const session = workspaceConsoleSessionFromShell(input);
  const owner = filePageOwner(session.context, input, { session, requireWrite: true });
  assert.ok(owner);
  assert.equal(owner.workspaceId, session.owner.workspaceId);
  assert.equal(Object.hasOwn(owner, "tenantId"), false);
  const state = createFilePageState();
  assert.equal(state.enter(owner), true);
  const read = state.beginListOperation(owner, 1);
  assert.equal(state.acceptPage(read, { ...payload, next_cursor: "synthetic-private-cursor" }, owner, 1), true);
  state.finishListOperation(read);
  assert.equal(state.nextPage(), true);
  assert.equal(state.query().cursor, "synthetic-private-cursor");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /synthetic-private-cursor|workspaceId|tenantId|identity/);
  const other = shell("files", "2");
  const otherSession = workspaceConsoleSessionFromShell(other);
  state.enter(filePageOwner(otherSession.context, other, { session: otherSession }));
  assert.equal(state.query().cursor, null);
});

for (const outcome of ["success", "401", "403", "503"]) {
  test(`PRD-147 production upload fences late ${outcome} and clears the File ticket`, async () => {
    const h = harness("files", "#file-upload-form");
    const owner = h.functions.currentFileReadOwner(h.scope.resourcePageShellState);
    h.scope.filePageState.enter(owner);
    const binding = h.functions.bindResourceElement(h.element, [PERMISSIONS.FILE_READ, PERMISSIONS.FILE_WRITE], "files");
    const operation = h.functions.submitFileUpload({ name: "synthetic.txt" }, h.scope.resourcePageShellState, binding);
    assert.equal(h.calls[0].client, "uploadFile");
    assert.equal(h.calls[0].input.authentication, "workspace");
    h.switchOwner();
    const session = h.scope.authSession;
    if (outcome === "success") h.pending.resolve(payload);
    else h.pending.reject(new AuthApiError({ status: Number(outcome), code: "auth.permission_denied" }));
    await operation;
    assert.equal(h.scope.authSession, session);
    assert.equal(h.scope.filePageState.snapshot().feedback, null);
    assert.equal(h.scope.filePageState.isUploadBusy(), false);
    assert.equal(h.effects.includes("refresh"), false);
    assert.equal(h.effects.includes("login"), false);
  });
}
