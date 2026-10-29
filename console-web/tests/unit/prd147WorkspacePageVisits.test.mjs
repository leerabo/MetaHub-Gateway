import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";
import { workspaceConsoleSessionFromShell } from "../../src/lib/workspaceConsoleSession.js";
import { recordWorkspacePageVisit, WORKSPACE_SOURCE_ROUTE_REGISTRY } from "../../src/lib/workspaceNavigation.js";
import { createWorkspacePageVisitTracker } from "../../src/lib/workspacePageVisitTracker.js";

const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
const expiry = "2099-01-01T00:00:00Z";
function state(id = "provider-health", workspace = "1") {
  const identity = { account: { status: "active" }, current_session: {
    session_id: "public_session", is_current: true, revoke_generation: 0, expires_at: expiry,
  } };
  const context = { workspace_id: `wsp_${workspace.repeat(32)}`, role: "owner", session_status: "active", expires_at: expiry };
  const authorization = { permission_codes: ["provider.health.read", "request_log.read"],
    permission_revision: 1, membership_revision: 2, permission_fingerprint: "a".repeat(64) };
  const route = WORKSPACE_SOURCE_ROUTE_REGISTRY[id];
  const shell = { state: "ok", workspaceNavigationContract: true, activeNavigationItemId: id, activeRouteCode: route,
    workspaceIdentity: identity, workspaceContext: context, workspaceAuthorization: authorization,
    workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    navigation: { ...authorization, page_visit_session_ref: workspace.repeat(64),
      contract_snapshot_id: "ncs_" + workspace.repeat(32), navigation_epoch: 1,
      active_navigation_item_id: id, active_route_code: route,
      navigation_items: [{ id, route_code: route, target_section: id, visible: true, disabled: false }] } };
  return { session: workspaceConsoleSessionFromShell(shell), shell, hash: id, renderedNavigation: id };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}
async function flushVisitQueue() {
  await new Promise((resolve) => setImmediate(resolve));
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 800));
}
function harness(initial = state()) {
  const box = { value: initial }, calls = [], errors = [], pending = [];
  const tracker = createWorkspacePageVisitTracker({ current: () => box.value,
    recordVisit: (input) => { calls.push(input); const job = deferred(); pending.push(job); return job.promise; },
    createIdempotencyKey: () => `page-visit:${calls.length}:${pending.length}:synthetic`,
    onError: (error) => errors.push(error),
  });
  return { box, calls, errors, pending, tracker };
}

for (const id of ["provider-health", "overview", "projects", "migration-guide", "files", "quickstart"]) {
  test(`Workspace ${id} visits once after render, not after background refresh`, async () => {
    const h = harness(state(id));
    const first = h.tracker.afterRender({ source: "sidebar", viewportClass: "mobile" });
    const second = h.tracker.afterRender();
    await flushVisitQueue();
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].visit.route_code, WORKSPACE_SOURCE_ROUTE_REGISTRY[id]);
    assert.equal(h.calls[0].visit.source, "sidebar");
    assert.equal(h.calls[0].visit.viewport_class, "mobile");
    h.pending[0].resolve({ accepted: true });
    await Promise.all([first, second]);
    await h.tracker.afterRender();
    assert.equal(h.calls.length, 1);
  });
}

test("Leaving before the scheduled visit prevents transmission", async () => {
  const h = harness();
  const first = h.tracker.afterRender();
  h.tracker.leave();
  await first;
  assert.equal(h.calls.length, 0);
});

for (const status of [401, 403, 409, 412, 503]) {
  test(`Late ${status} cannot affect a new Workspace visit or its in-flight state`, async () => {
    const h = harness();
    const first = h.tracker.afterRender();
    await flushVisitQueue();
    h.box.value = state("provider-health", "2");
    const second = h.tracker.afterRender();
    await flushVisitQueue();
    assert.equal(h.calls[0].signal.aborted, true);
    h.pending[0].reject({ status });
    await first;
    assert.deepEqual(h.errors, []);
    const duplicate = h.tracker.afterRender();
    await flushVisitQueue();
    assert.equal(h.calls.length, 2);
    h.pending[1].resolve();
    await Promise.all([second, duplicate]);
  });
}

test("Current failure is reported once without retry; return to the page is a new visit", async () => {
  const h = harness();
  const first = h.tracker.afterRender();
  await flushVisitQueue();
  const error = { status: 503 };
  h.pending[0].reject(error);
  await first;
  await h.tracker.afterRender();
  assert.deepEqual(h.errors, [error]);
  assert.equal(h.calls.length, 1);
  h.tracker.leave();
  const second = h.tracker.afterRender();
  await flushVisitQueue();
  assert.equal(h.calls.length, 2);
  h.pending[1].resolve();
  await second;
});

test("Missing DOM, retired routes and mismatched real hash cannot emit a visit", async () => {
  for (const change of [
    (s) => { s.renderedNavigation = null; },
    (s) => { s.hash = "projects"; },
    (s) => { s.shell.navigation.navigation_items[0].disabled = true; },
    (s) => { s.shell.navigation.page_visit_session_ref = "invalid"; },
    (s) => { s.session = { token: "legacy", context: {} }; },
  ]) {
    const current = state();
    change(current);
    const h = harness(current);
    await h.tracker.afterRender();
    assert.equal(h.calls.length, 0);
  }
});

test("Production render owner passes actual shell, source and viewport to common tracker", async () => {
  const start = main.indexOf("function ownWorkspacePageVisitAfterRender(");
  const end = main.indexOf("\n}", start) + 2;
  const calls = [];
  const scope = { workspacePageVisitShellState: null, workspacePageVisitSource: "sidebar",
    window: { innerWidth: 390 }, workspacePageVisitTracker: { afterRender: (input) => calls.push(input) } };
  runInNewContext(main.slice(start, end), scope);
  const shell = state().shell;
  scope.ownWorkspacePageVisitAfterRender(shell);
  assert.equal(scope.workspacePageVisitShellState, shell);
  assert.equal(calls[0].source, "sidebar");
  assert.equal(calls[0].viewportClass, "mobile");
  assert.match(main, /void ownWorkspacePageVisitAfterRender\(consoleShellState\)/u);
});

test("Visit client sends only Cookie/CSRF and strict DTO; body abort propagates", async () => {
  const originalFetch = globalThis.fetch, originalDocument = globalThis.document;
  const s = state(), calls = [];
  const visit = { ...Object.fromEntries(["page_visit_session_ref", "contract_snapshot_id",
    "permission_fingerprint", "navigation_epoch"].map((key) => [key, s.shell.navigation[key]])),
    route_code: s.shell.activeRouteCode, source: "direct_hash", viewport_class: "desktop" };
  const response = { accepted: true, idempotent_replay: false, event_id: "cpe_" + "c".repeat(64),
    permission_fingerprint: visit.permission_fingerprint, request_id: "req_visit_test", trace_id: "trc_visit_test" };
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=" + "w".repeat(43) };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify(response), { headers: {
      "Cache-Control": "private, no-store", Pragma: "no-cache", Vary: "Cookie",
      "X-Permission-Fingerprint": visit.permission_fingerprint, ETag: `"${response.event_id}"`,
    } });
  };
  try {
    const input = { controlApiUrl: "https://control.test", visit, idempotencyKey: "page-visit:synthetic:0001" };
    assert.equal((await recordWorkspacePageVisit(input)).accepted, true);
    assert.equal(calls[0].url, "https://control.test/workspace/navigation/page-views");
    assert.equal(calls[0].options.credentials, "include");
    assert.equal(calls[0].options.headers.Authorization, undefined);
    assert.deepEqual(JSON.parse(calls[0].options.body), visit);
    assert.throws(() => recordWorkspacePageVisit({ ...input, visit: { ...visit, token: "private" } }));
    const aborted = new DOMException("Cancelled", "AbortError");
    globalThis.fetch = async () => ({ json: async () => { throw aborted; } });
    await assert.rejects(recordWorkspacePageVisit(input), (error) => error === aborted);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.document = originalDocument;
  }
});

test("Workspace visit writers stay serialized across a rapid route transition", async () => {
  const h = harness();
  const first = h.tracker.afterRender();
  await flushVisitQueue();
  h.box.value = state("provider-health", "2");
  const second = h.tracker.afterRender();
  await flushVisitQueue();

  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].visit.page_visit_session_ref, "1".repeat(64));
  assert.equal(h.calls[0].signal.aborted, true);

  h.pending[0].resolve({ accepted: true });
  await flushVisitQueue();
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].visit.page_visit_session_ref, "2".repeat(64));
  h.pending[1].resolve({ accepted: true });
  await Promise.all([first, second]);
});
