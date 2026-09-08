import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { IdentityApiError } from "../../src/lib/identity.js";
import { PERMISSIONS, hasPermission } from "../../src/lib/permissions.js";
import * as logs from "../../src/lib/requestLogs.js";
import { listAuditLogs, exportAuditLogs } from "../../src/lib/auditLogs.js";
import { createRequestLogPageMemory } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { createAuditExportLifecycle } from "../../src/features/audit-logs/renderAuditLogPanel.js";
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
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness(navigation = "request-logs") {
  const effects = [], calls = [], button = new Element();
  const scope = { AuthApiError, PERMISSIONS, hasPermission, workspaceResourcePageOwner,
    authSession: null, resourcePageShellState: shell(navigation), refreshSequence: 1, hash: navigation,
    REQUEST_LOG_NAVIGATION_IDS: new Set(["request-logs", "output-safety"]),
    controlApiUrl: "https://control.test", auditLogFilters: {}, auditExportState: { status: "idle" },
    requestLogPageMemory: createRequestLogPageMemory(), auditExportLifecycle: createAuditExportLifecycle(),
    requestFeedbackMutationMemory: { leave() {}, syncDetail() {} },
    document: { querySelector: () => button },
    refresh: async () => effects.push("refresh"),
    handleResourceAuthorizationError: () => { effects.push("auth"); return true; },
    authErrorMessage: () => "Unavailable", isAbortError: (error) => error?.name === "AbortError",
    setAuditExportState: (state) => effects.push(state.status), syncAuditExportDomState: () => effects.push("sync"),
    triggerAuditCsvDownload: () => effects.push("download"),
    createAuditExportRequestId: () => "test_export_147", safeAuditExportErrorCode: () => "audit_export.unavailable",
    safeAuditExportRequestId: () => "test_export_147",
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
    "requestLogInteractionAllowed", "configureCurrentRequestLogState", "safeRequestLogClientError",
    "loadRequestLogsState", "loadResourceState", "loadAuditLogsState", "ownsAuditExportOperation",
    "mutateCurrentSavedRequestLogFilter"];
  Object.assign(scope, runInNewContext(`${names.map(production).join("\n")}\n({${names.join(",")}})`, scope));
  return { scope, effects, calls, button, switchOwner() {
    scope.resourcePageShellState = shell(navigation, "2");
    scope.authSession = workspaceConsoleSessionFromShell(scope.resourcePageShellState);
    scope.resourcePageLifecycle.enter();
  } };
}
const payload = { request_logs: [{ request_id: "req_a", tenant_id: "ten_fact" }], total: 1 };

test("production authentication message distinguishes Workspace recovery from network failure", () => {
  const message = runInNewContext(`${production("authErrorMessage")}\nauthErrorMessage`, { AuthApiError, IdentityApiError });
  assert.match(message(new IdentityApiError({ status: 401, code: "workspace_session.required" })), /Workspace/);
  assert.match(message(new IdentityApiError({ status: 401, code: "workspace_session.expired" })), /Workspace/);
  assert.match(message(new IdentityApiError({ status: 401, code: "identity.session_expired" })), /重新登录/);
  assert.equal(message(new AuthApiError({ status: 401, code: "auth.token_expired" })), "登录状态已失效，请重新登录。");
  assert.equal(message(new Error("private transport detail")), "无法连接控制面认证接口。");
});

test("production Workspace log loader uses real client and memory without transport substitutes", async (t) => {
  const { scope } = harness();
  const calls = [], errors = [];
  Object.assign(scope, logs);
  scope.handleResourceAuthorizationError = (error) => { errors.push(error); return false; };
  t.mock.method(globalThis, "fetch", async (url) => {
    calls.push(new URL(url).pathname);
    return Response.json(String(url).includes("saved-filters") ? { saved_filters: [] } : payload);
  });
  const result = await scope.loadRequestLogsState(scope.resourcePageShellState, 1);
  assert.deepEqual(errors, []);
  assert.equal(result.state, "ok");
  assert.deepEqual(calls.sort(), ["/request-logs", "/request-logs/saved-filters"]);
});

test("Workspace log memory binds the public session without inventing a tenant and rejects tenant drift", () => {
  const { scope, switchOwner } = harness();
  const owner = scope.currentRequestLogOwner();
  assert.equal(owner.tenantId, "");
  assert.ok(owner.identity);
  const memory = scope.requestLogPageMemory;
  let ticket = memory.beginList(owner);
  assert.equal(memory.acceptList(ticket, payload, owner), true);
  assert.equal(memory.acceptList(ticket, { request_logs: [...payload.request_logs,
    { request_id: "req_b", tenant_id: "ten_foreign" }] }, owner), false);
  ticket = memory.beginList(owner);
  assert.equal(memory.acceptList(ticket, { request_logs: [{ request_id: "req_a", tenant_id: "ten_foreign" }] }, owner), false);
  assert.equal(scope.configureCurrentRequestLogState({}, "req_a"), true);
  ticket = memory.beginList(owner);
  assert.equal(memory.acceptList(ticket, payload, owner), true);
  const detail = memory.beginDetail(ticket, owner);
  assert.equal(memory.acceptDetail(detail, { request_log: { log: payload.request_logs[0] } }, owner), true);
  assert.equal(memory.acceptDetail(detail, { request_log: { log: { request_id: "req_a", tenant_id: "ten_foreign" } } }, owner), false);
  switchOwner();
  assert.equal(memory.acceptList(ticket, payload, scope.currentRequestLogOwner()), false);
  const other = scope.currentRequestLogOwner();
  assert.equal(memory.acceptList(memory.beginList(other), { request_logs: [] }, other), true);
});

test("production Workspace log loader accepts current list, saved filters and selected detail", async () => {
  const { scope, calls } = harness();
  scope.configureCurrentRequestLogState({}, "req_a");
  scope.listSavedRequestLogFilters = async (input) => { calls.push(input); return { saved_filters: [] }; };
  scope.listRequestLogs = async (input) => { calls.push(input); return payload; };
  scope.getRequestLog = async (input) => { calls.push(input); return { request_log: { log: payload.request_logs[0] } }; };
  const state = await scope.loadRequestLogsState(scope.resourcePageShellState, 1);
  assert.equal(state.state, "ok");
  assert.equal(state.detail.request_log.log.tenant_id, "ten_fact");
  assert.equal(calls.length, 3);
  for (const call of calls) {
    assert.equal(call.authentication, "workspace");
    assert.equal(call.token, undefined);
    assert.ok(call.signal);
  }
});

for (const rejected of [false, true]) {
  test(`production log loader discards a late ${rejected ? "401" : "success"} after workspace switch`, async () => {
    const { scope, effects, switchOwner } = harness();
    const pending = deferred();
    scope.listSavedRequestLogFilters = async () => ({ saved_filters: [] });
    scope.listRequestLogs = () => pending.promise;
    const result = scope.loadRequestLogsState(scope.resourcePageShellState, 1);
    switchOwner();
    if (rejected) pending.reject(new AuthApiError({ status: 401, code: "workspace_session.expired" }));
    else pending.resolve(payload);
    assert.equal(await result, null);
    assert.deepEqual(effects, []);
  });
}

test("production saved-filter mutation rejects disconnected DOM and late owner success", async () => {
  const { scope, button, effects, switchOwner } = harness();
  const binding = scope.bindResourceElement(button, [PERMISSIONS.REQUEST_LOG_READ], "request-logs");
  const pending = deferred();
  let calls = 0;
  const run = () => scope.mutateCurrentSavedRequestLogFilter(() => { calls += 1; return pending.promise; },
    { activeNavigationItemId: "request-logs", binding });
  button.isConnected = false;
  await run();
  assert.equal(calls, 0);
  button.isConnected = true;
  const result = run();
  assert.equal(calls, 1);
  assert.equal(scope.resourcePageLifecycle.blocksReplacement(true), true);
  switchOwner();
  pending.resolve({});
  await result;
  assert.deepEqual(effects, []);
});

test("a background authorization refresh does not discard a current saved-filter write", async () => {
  const { scope, button, effects } = harness();
  const binding = scope.bindResourceElement(button, [PERMISSIONS.REQUEST_LOG_READ], "request-logs");
  const pending = deferred();
  const result = scope.mutateCurrentSavedRequestLogFilter(() => pending.promise,
    { activeNavigationItemId: "request-logs", binding });
  scope.refreshSequence += 1;
  pending.resolve({});
  await result;
  assert.deepEqual(effects, ["refresh"]);
});

for (const changed of [false, true]) {
  test(`production audit click ${changed ? "discards stale CSV" : "downloads once"} with Workspace ownership`, async () => {
    const { scope, button, calls, effects, switchOwner } = harness("audit-logs");
    const pending = deferred();
    scope.exportAuditLogs = (options) => { calls.push(options); return pending.promise; };
    const start = source.indexOf('  const auditLogExportButton = document.querySelector("#audit-log-export-csv")');
    const end = source.indexOf('  for (const copyButton of document.querySelectorAll("[data-copy-audit-id]"))', start);
    assert.ok(start > 0 && end > start);
    runInNewContext(source.slice(start, end), scope);
    const result = button.fire("click");
    await button.fire("click");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].authentication, "workspace");
    if (changed) switchOwner();
    pending.resolve({});
    await result;
    assert.equal(effects.filter((effect) => effect === "download").length, changed ? 0 : 1);
    assert.equal(effects.includes("refresh"), !changed);
    assert.equal(effects.includes("auth"), false);
    if (changed) {
      await button.fire("click");
      assert.equal(calls.length, 1);
    }
  });
}

test("production audit list rejects mixed tenants and late authorization errors", async () => {
  const { scope, effects, switchOwner } = harness("audit-logs");
  scope.listAuditLogs = async () => ({ audit_logs: [{ tenant_id: "ten_a" }, { tenant_id: "ten_b" }] });
  scope.handleResourceAuthorizationError = () => false;
  assert.equal((await scope.loadAuditLogsState(scope.resourcePageShellState, 1)).state, "error");
  const pending = deferred();
  scope.listAuditLogs = () => pending.promise;
  const response = scope.loadAuditLogsState(scope.resourcePageShellState, 1);
  switchOwner();
  pending.reject(new AuthApiError({ status: 401 }));
  assert.equal(await response, null);
  assert.deepEqual(effects, []);
});

test("all log transports select explicit credentials; side-effect GET includes CSRF", async (t) => {
  const csrf = "c".repeat(43), calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: new URL(url), options });
    if (String(url).includes("/export")) return new Response("a,b\n", { headers: {
      "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": 'attachment; filename="audit.csv"',
      "X-MetaHub-Audit-Export-ID": `aex_${"a".repeat(32)}`, "X-MetaHub-Audit-Export-Row-Count": "1",
      "ETag": `"${"a".repeat(64)}"`, "X-Request-ID": "req_test", "X-Trace-ID": "trace_test",
    } });
    return Response.json({ request_log: { log: { tenant_id: "ten_fact" }, response_cache: null } });
  });
  const prior = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: `__Host-metahub-workspace-csrf=${csrf}` } });
  t.after(() => { if (prior) Object.defineProperty(globalThis, "document", prior); else delete globalThis.document; });
  const operations = [logs.listRequestLogs, logs.listSavedRequestLogFilters, logs.createSavedRequestLogFilter,
    logs.updateSavedRequestLogFilter, logs.deleteSavedRequestLogFilter, logs.getRequestLog, listAuditLogs, exportAuditLogs];
  for (const operation of operations) {
    for (const authentication of ["workspace", "legacy"]) {
      await operation({ controlApiUrl: "https://control.test", authentication,
        ...(authentication === "legacy" ? { token: "synthetic-session" } : {}), requestId: "req_a",
        savedFilterId: "sf_test", version: 1, name: "Test", operationRequestId: "req_export_test" });
      const { url, options } = calls.at(-1), headers = new Headers(options.headers);
      assert.equal(options.credentials, authentication === "workspace" ? "include" : "omit");
      assert.equal(headers.has("authorization"), authentication === "legacy");
      assert.equal(headers.has("x-csrf-token"), authentication === "workspace"
        && (options.method !== "GET" || url.pathname.endsWith("/export")));
      assert.equal(options.csrfRequired, undefined);
      assert.equal(options.cache, "no-store");
    }
    const count = calls.length;
    await assert.rejects(() => operation({ controlApiUrl: "https://control.test", authentication: "workspace",
      token: "synthetic-conflict", requestId: "req_a", savedFilterId: "sf_test" }));
    assert.equal(calls.length, count);
  }
  globalThis.document.cookie = "";
  const count = calls.length;
  await assert.rejects(() => exportAuditLogs({ controlApiUrl: "https://control.test", authentication: "workspace" }),
    (error) => error.code === "workspace_session.csrf_invalid");
  assert.equal(calls.length, count);
});
