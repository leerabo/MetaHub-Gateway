import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, hasPermission } from "../../src/lib/permissions.js";
import { getRequestExplanation } from "../../src/lib/requestExplanations.js";
import { createRequestExplanationPageMemory, createRequestExplanationHandoff } from "../../src/features/request-explanations/requestExplanationPageState.js";
import { WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { workspaceConsoleSessionFromShell } from "../../src/lib/workspaceConsoleSession.js";
import { createWorkspaceResourcePage, workspaceResourcePageOwner } from "../../src/lib/workspaceResourcePage.js";

const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
function production(name) {
  const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
  const end = start + source.slice(start).search(/\n}\r?\n/);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 2);
}
function shell(id = "1", navigation = "request-logs") {
  const expires = "2099-01-01T00:00:00Z";
  const identity = { account: { status: "active" }, current_session: {
    session_id: "public-session", is_current: true, revoke_generation: 0, expires_at: expires,
  } };
  const context = { workspace_id: `wsp_${id.repeat(32)}`, role: "owner", session_status: "active", expires_at: expires };
  const authorization = { permission_codes: Object.values(PERMISSIONS), permission_revision: 1,
    membership_revision: 2, permission_fingerprint: "a".repeat(64) };
  const route = WORKSPACE_SOURCE_ROUTE_REGISTRY[navigation];
  return { state: "ok", workspaceNavigationContract: true, activeNavigationItemId: navigation, activeRouteCode: route,
    workspaceIdentity: identity, workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    navigation: { permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64),
      active_navigation_item_id: navigation, active_route_code: route,
      navigation_items: [{ id: navigation, route_code: route, target_section: navigation, visible: true, disabled: false }] } };
}
class Element {
  isConnected = true;
  listeners = new Map();
  addEventListener(type, handler) { this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]); }
  fire(type) { return Promise.all((this.listeners.get(type) ?? []).map((handler) => handler({ preventDefault() {} }))); }
  querySelector() { return null; }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness(navigation = "request-logs") {
  const effects = [], panel = new Element(), clear = new Element();
  const scope = { AuthApiError, PERMISSIONS, hasPermission, workspaceResourcePageOwner,
    authSession: null, resourcePageShellState: shell("1", navigation), refreshSequence: 1, hash: navigation,
    controlApiUrl: "https://control.test", requestExplanationHandoff: createRequestExplanationHandoff(),
    requestExplanationPageMemory: createRequestExplanationPageMemory({ authentication: "workspace" }),
    document: { querySelector: (selector) => selector === "#request-explanation-clear" ? clear
      : selector.endsWith("-panel") ? panel : null, querySelectorAll: () => [] },
    renderCurrentRequestExplanationSnapshot: () => effects.push("render"),
    handleResourceAuthorizationError: () => { effects.push("auth"); return true; },
    isAbortError: (error) => error?.name === "AbortError", restoreRequestExplanationFocus() {},
  };
  scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
  scope.readActiveNavigationHash = scope.currentRenderedNavigation = () => scope.hash;
  scope.resourcePageLifecycle = createWorkspaceResourcePage({ current: () => ({
    session: scope.authSession, shell: scope.resourcePageShellState, hash: scope.hash,
    refreshGeneration: scope.refreshSequence,
  }) });
  scope.resourcePageLifecycle.enter();
  scope.resourcePageLifecycle.render();
  const names = ["bindResourceElement", "currentEvaluationPageIdentity", "currentRequestExplanationOwner",
    "bindRequestExplanationInteractions", "runRequestExplanationRead", "loadRequestExplanationState",
    "safeRequestExplanationClientError"];
  Object.assign(scope, runInNewContext(`${names.map(production).join("\n")}\n({${names.join(",")}})`, scope));
  const binding = scope.bindResourceElement(panel, [PERMISSIONS.REQUEST_EXPLANATION_READ], navigation);
  const run = (id = "req_test") => scope.runRequestExplanationRead(id, "request_logs", binding, scope.resourcePageShellState);
  return { scope, effects, panel, clear, binding, run, memory: scope.requestExplanationPageMemory,
    switchOwner() {
      scope.resourcePageShellState = shell("2", navigation);
      scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
      scope.resourcePageLifecycle.enter();
      scope.requestExplanationPageMemory.leave();
    } };
}
const response = { request_explanation: { source_request_id: "req_test" }, request_id: "operation", trace_id: "trace" };

for (const navigation of ["request-logs", "request-explanations"]) {
  test(`production explanation accepts one action and same-owner refresh in ${navigation}`, async () => {
    const { scope, panel, run, memory } = harness(navigation);
    const pending = deferred(), calls = [];
    scope.getRequestExplanation = (input) => { calls.push(input); return pending.promise; };
    panel.isConnected = false;
    await run();
    assert.equal(calls.length, 0);
    panel.isConnected = true;
    const result = run();
    await run("req_duplicate");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].authentication, "workspace");
    assert.equal(calls[0].token, undefined);
    assert.equal(memory.query().requestId, "req_test");
    assert.equal(scope.currentRequestExplanationOwner().tenantId, "");
    scope.refreshSequence += 1;
    pending.resolve(response);
    await result;
    assert.equal(memory.snapshot().state, "ok");
    assert.equal(scope.resourcePageLifecycle.blocksReplacement(false), false);
    assert.equal(await scope.loadRequestExplanationState(scope.resourcePageShellState, 2), null);
    assert.equal(calls.length, 1);
  });
}

for (const failure of [false, true]) {
  test(`production explanation discards stale ${failure ? "401" : "success"}`, async () => {
    const { scope, effects, run, memory, switchOwner } = harness();
    const pending = deferred();
    scope.getRequestExplanation = () => pending.promise;
    const result = run();
    switchOwner();
    if (failure) pending.reject(new AuthApiError({ status: 401, code: "workspace_session.expired" }));
    else pending.resolve(response);
    await result;
    assert.equal(memory.snapshot().state, "idle");
    assert.deepEqual(effects, ["render"]);
  });
}

test("production explanation clear accepts dirty drafts but not a pending mutation", async () => {
  const { scope, clear, memory, binding, run } = harness();
  scope.bindRequestExplanationInteractions(scope.resourcePageShellState);
  memory.setDraft({ ...scope.currentRequestExplanationOwner(), requestId: "req_draft" });
  scope.resourcePageLifecycle.markDirty(binding);
  const other = scope.resourcePageLifecycle.beginMutation(binding);
  await run();
  await clear.fire("click");
  assert.equal(memory.query().draftRequestId, "req_draft");
  assert.equal(memory.query().requestId, null);
  scope.resourcePageLifecycle.finishMutation(other);
  await clear.fire("click");
  assert.equal(memory.query().draftRequestId, "");
  assert.equal(memory.snapshot().state, "idle");
});

test("Workspace explanation GET carries CSRF and rejects mixed bearer credentials", async (t) => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true,
    value: { cookie: `__Host-metahub-workspace-csrf=${"a".repeat(64)}` } });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "document", previous);
    else delete globalThis.document;
  });
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json(response);
  });
  const input = { controlApiUrl: "https://control.test", authentication: "workspace", requestId: "req_test" };
  await getRequestExplanation(input);
  assert.equal(calls[0].options.credentials, "include");
  const headers = new Headers(calls[0].options.headers);
  assert.equal(headers.get("X-CSRF-Token"), "a".repeat(64));
  assert.equal(headers.has("Authorization"), false);
  assert.equal(calls[0].url.includes("tenant"), false);
  await assert.rejects(getRequestExplanation({ ...input, token: "synthetic-token" }),
    (error) => error.code === "workspace_session.credential_conflict");
  assert.equal(calls.length, 1);
});
