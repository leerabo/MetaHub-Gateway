import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS, hasPermission } from "../../src/lib/permissions.js";
import { createRequestFeedback, getRequestFeedbackSummary } from "../../src/lib/requestFeedback.js";
import { createRequestFeedbackMutationMemory, createRequestFeedbackSummaryMemory } from "../../src/features/request-feedback/renderRequestFeedback.js";
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
function shell(navigation, id = "1") {
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
  querySelectorAll() { return []; }
  querySelector() { return null; }
  reportValidity() { return true; }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const feedback = { request_feedback_id: "rfb_test", request_id: "req_test", model_id: "model_test",
  rating: 5, accepted: true, tags: [], actor_type: "session_user", status: "recorded",
  created_at: "2026-09-08T00:00:00Z" };
function harness(navigation = "request-logs") {
  const effects = [], form = new Element(), panel = new Element();
  const scope = { AuthApiError, PERMISSIONS, hasPermission, workspaceResourcePageOwner,
    authSession: null, resourcePageShellState: shell(navigation), refreshSequence: 1, hash: navigation,
    REQUEST_LOG_NAVIGATION_IDS: new Set(["request-logs", "output-safety"]),
    controlApiUrl: "https://control.test", requestFeedbackSummaryFilters: { limit: 20, offset: 0 },
    requestFeedbackMutationMemory: createRequestFeedbackMutationMemory({ requestIdFactory: () => "operation_test" }),
    requestFeedbackSummaryMemory: createRequestFeedbackSummaryMemory(),
    document: { querySelector: (selector) => selector === "[data-request-feedback-form]" ? form : panel },
    refresh: async () => effects.push("refresh"),
    renderRequestFeedbackPanelFromMemory: () => effects.push("render"),
    handleResourceAuthorizationError: () => { effects.push("auth"); return true; },
    isAbortError: (error) => error?.name === "AbortError",
  };
  scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
  scope.readActiveNavigationHash = scope.currentRenderedNavigation = () => scope.hash;
  scope.resourcePageLifecycle = createWorkspaceResourcePage({ current: () => ({
    session: scope.authSession, shell: scope.resourcePageShellState, hash: scope.hash,
    refreshGeneration: scope.refreshSequence,
  }) });
  scope.resourcePageLifecycle.enter();
  scope.resourcePageLifecycle.render();
  const names = ["bindResourceElement", "currentEvaluationPageIdentity", "currentRequestLogOwner",
    "currentRequestFeedbackOwner", "bindRequestFeedbackInteractions", "requestFeedbackSummaryQuery",
    "currentRequestFeedbackSummaryOwner", "loadRequestFeedbackSummaryState"];
  Object.assign(scope, runInNewContext(`${names.map(production).join("\n")}\n({${names.join(",")}})`, scope));
  const memory = scope.requestFeedbackMutationMemory;
  memory.syncDetail({ identity: scope.currentRequestLogOwner().identity, navigation: "request-logs",
    detail: { request_log: { log: { request_id: "req_test" }, feedback: null, feedback_eligible: true } } });
  memory.selectRating(5);
  memory.selectAccepted(true);
  return { scope, effects, form, panel, memory, switchOwner() {
    scope.resourcePageShellState = shell(navigation, "2");
    scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
    scope.resourcePageLifecycle.enter();
    memory.leave();
  } };
}

test("production feedback preserves its draft when another resource mutation owns the page", async () => {
  const { scope, form, panel, memory } = harness();
  let calls = 0;
  scope.createRequestFeedback = async () => { calls += 1; return { feedback }; };
  const binding = scope.bindResourceElement(panel, [PERMISSIONS.FEEDBACK_USE], "request-logs");
  const other = scope.resourcePageLifecycle.beginMutation(binding);
  scope.bindRequestFeedbackInteractions(scope.resourcePageShellState);
  await form.fire("submit");
  assert.equal(calls, 0);
  assert.equal(memory.snapshot().busy, false);
  assert.equal(memory.snapshot().rating, 5);
  scope.resourcePageLifecycle.finishMutation(other);
  await form.fire("submit");
  assert.equal(calls, 1);
  assert.equal(memory.snapshot().authoritativeFeedback.request_id, "req_test");
});

for (const stale of [false, true]) {
  test(`production feedback ${stale ? "discards stale success" : "accepts one duplicate-click result"}`, async () => {
    const { scope, form, effects, memory, switchOwner } = harness();
    const pending = deferred(), calls = [];
    scope.createRequestFeedback = (input) => { calls.push(input); return pending.promise; };
    scope.bindRequestFeedbackInteractions(scope.resourcePageShellState);
    form.isConnected = false;
    await form.fire("submit");
    assert.equal(calls.length, 0);
    form.isConnected = true;
    const result = form.fire("submit");
    await form.fire("submit");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].authentication, "workspace");
    assert.equal(calls[0].token, undefined);
    assert.equal(scope.resourcePageLifecycle.blocksReplacement(true), true);
    if (stale) switchOwner();
    pending.resolve({ feedback });
    await result;
    assert.equal(Boolean(memory.snapshot().authoritativeFeedback), !stale);
    assert.equal(effects.includes("refresh"), !stale);
    assert.equal(effects.includes("auth"), false);
  });
}

test("production feedback summary uses the real Workspace client and rejects late 401", async (t) => {
  const { scope, effects, switchOwner } = harness("feedback-summary");
  const calls = [];
  scope.getRequestFeedbackSummary = getRequestFeedbackSummary;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json({ scope_type: "tenant", summary: { feedback_count: 1 }, models: [] });
  });
  assert.equal((await scope.loadRequestFeedbackSummaryState(scope.resourcePageShellState, 1)).state, "ok");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(new Headers(calls[0].options.headers).has("Authorization"), false);
  assert.equal(calls[0].url.includes("tenant"), false);
  const pending = deferred();
  scope.getRequestFeedbackSummary = () => pending.promise;
  const result = scope.loadRequestFeedbackSummaryState(scope.resourcePageShellState, 1);
  switchOwner();
  pending.reject(new AuthApiError({ status: 401, code: "workspace_session.expired" }));
  assert.equal(await result, null);
  assert.deepEqual(effects, []);
});

test("feedback Workspace POST carries CSRF with no bearer and rejects mixed credentials", async (t) => {
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true,
    value: { cookie: `__Host-metahub-workspace-csrf=${"a".repeat(64)}` } });
  t.after(() => {
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument);
    else delete globalThis.document;
  });
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push(options);
    return Response.json({ feedback });
  });
  const options = { controlApiUrl: "https://control.test", authentication: "workspace", requestId: "req_test",
    operationRequestId: "operation_test", rating: 5, accepted: true, tags: [] };
  await createRequestFeedback(options);
  assert.equal(calls[0].credentials, "include");
  assert.equal(new Headers(calls[0].headers).get("X-CSRF-Token"), "a".repeat(64));
  assert.equal(new Headers(calls[0].headers).has("Authorization"), false);
  await assert.rejects(createRequestFeedback({ ...options, token: "synthetic-token" }),
    (error) => error.code === "workspace_session.credential_conflict");
  assert.equal(calls.length, 1);
});
