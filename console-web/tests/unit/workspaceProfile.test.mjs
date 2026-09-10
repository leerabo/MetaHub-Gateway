import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { AuthApiError } from "../../src/lib/auth.js";
import { getWorkspaceProfile, updateWorkspaceProfile } from "../../src/lib/tenant.js";
import { renderTenantPanel } from "../../src/features/tenant/renderTenantPanel.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { createWorkspaceResourcePage } from "../../src/lib/workspaceResourcePage.js";
import { workspaceConsoleSessionFromShell } from "../../src/lib/workspaceConsoleSession.js";
import { workspaceNavigationOwner } from "../../src/features/workspace-navigation/workspaceNavigationStore.js";

const workspace = {
  workspace_id: `wsp_${"1".repeat(32)}`, kind: "organization", name: "Workspace Canary",
  slug: "workspace-canary", status: "active", profile_state: "draft", revision: 1,
  region: "cn-east", service_mode: "internal_only",
  environment: { code: "dev", status: "active", origin: "system_created" }, provisioning: { status: "ready" },
};
const response = () => ({ workspace: structuredClone(workspace), request_id: "req_profile", trace_id: "trc_profile" });
const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
function production(name) {
  const start = source.indexOf(`async function ${name}(`) >= 0
    ? source.indexOf(`async function ${name}(`) : source.indexOf(`function ${name}(`);
  const end = start + source.slice(start).search(/\n\}\r?\n/u);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 2);
}

test("Workspace profile uses existing Global Cookie APIs and exact CAS payloads", async () => {
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: "__Host-metahub-global-csrf=global-profile-csrf" };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return { ok: true, headers: new Headers(), json: async () => response() };
  };
  try {
    await getWorkspaceProfile({ controlApiUrl: "https://control.test", workspaceId: workspace.workspace_id });
    await updateWorkspaceProfile({ controlApiUrl: "https://control.test", workspaceId: workspace.workspace_id,
      revision: 1, kind: "organization", name: "Changed", authentication: "workspace", token: "ignored" });
    await updateWorkspaceProfile({ controlApiUrl: "https://control.test", workspaceId: workspace.workspace_id,
      revision: 2, kind: "personal", name: "Personal" });
    assert.ok(calls.every(({ url, options }) => url.endsWith("/workspaces/" + workspace.workspace_id)
      && options.credentials === "include" && options.cache === "no-store" && !options.headers.Authorization));
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "global-profile-csrf");
    assert.equal(calls[1].options.headers["If-Match"], '"1"');
    assert.deepEqual(JSON.parse(calls[1].options.body), { name: "Changed" });
    assert.deepEqual(JSON.parse(calls[2].options.body), { display_label: "Personal" });
  } finally { globalThis.fetch = previousFetch; globalThis.document = previousDocument; }
});

test("profile errors preserve structured denial and cancellation", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 403, headers: new Headers(),
    json: async () => ({ code: "workspace.not_found", message: "Not found", request_id: "req_denied" }) });
  try {
    await assert.rejects(getWorkspaceProfile({ controlApiUrl: "https://control.test", workspaceId: workspace.workspace_id }),
      (error) => error instanceof AuthApiError && error.status === 403 && error.requestId === "req_denied");
    globalThis.fetch = async () => { throw new DOMException("Aborted", "AbortError"); };
    await assert.rejects(getWorkspaceProfile({ controlApiUrl: "https://control.test", workspaceId: workspace.workspace_id }),
      (error) => error.name === "AbortError");
  } finally { globalThis.fetch = previous; }
});

test("Workspace profile renders without legacy User and only Owner can edit", () => {
  const authorization = { permission_codes: [PERMISSIONS.TENANT_SETTINGS_READ] };
  for (const role of ["owner", "admin"]) {
    const html = renderTenantPanel({ state: "ok", workspace: { ...workspace, name: '<Canary "name">' } },
      authorization, { role, kind: "organization" });
    assert.match(html, /id="workspace-profile-form"/u);
    assert.match(html, /&lt;Canary &quot;name&quot;&gt;/u);
    assert.doesNotMatch(html, /name="serviceMode"|租户 ID|等待登录|Workspace Canary/u);
    assert.equal(html.includes('type="submit"'), role === "owner");
  }
  assert.equal(renderTenantPanel({ workspace }, { permission_codes: [] }, { role: "owner" }), "");
  assert.match(renderTenantPanel({ workspace: { ...workspace, kind: "personal" } }, authorization,
    { role: "owner", kind: "personal" }), /maxlength="120"/u);
  assert.doesNotMatch(renderTenantPanel({ workspace: { ...workspace, status: "migration_conflict" } },
    authorization, { role: "owner", kind: "organization" }), /type="submit"/u);
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function fixture({ save, reenter, role = "owner" } = {}) {
  const expiry = "2099-01-01T00:00:00Z";
  const identity = { account: { status: "active" }, current_session: {
    session_id: "profile_session", is_current: true, revoke_generation: 0, expires_at: expiry,
  } };
  const context = { workspace_id: workspace.workspace_id, role, session_status: "active", expires_at: expiry };
  const authorization = { permission_codes: [PERMISSIONS.TENANT_SETTINGS_READ], permission_revision: 1,
    membership_revision: 1, permission_fingerprint: "a".repeat(64) };
  const shell = { state: "ok", workspaceNavigationContract: true, activeNavigationItemId: "tenant",
    activeRouteCode: "workspace.settings.profile", workspaceIdentity: identity, workspaceContext: context,
    workspaceAuthorization: authorization, workspaceOwner: workspaceNavigationOwner(identity, { context, authorization }),
    navigation: { ...authorization, active_navigation_item_id: "tenant", active_route_code: "workspace.settings.profile",
      navigation_items: [{ id: "tenant", route_code: "workspace.settings.profile", target_section: "tenant", visible: true, disabled: false }] } };
  const session = workspaceConsoleSessionFromShell(shell);
  assert.ok(session);
  const state = { session, shell, hash: "tenant", refreshGeneration: 1 };
  const lifecycle = createWorkspaceResourcePage({ current: () => state });
  lifecycle.enter();
  lifecycle.render();
  const listeners = new Map();
  const fields = { button: { disabled: false }, name: { value: "Changed", disabled: false }, feedback: { textContent: "" } };
  const form = { isConnected: true, addEventListener: (name, handler) => listeners.set(name, handler),
    querySelector: (selector) => selector === 'button[type="submit"]' ? fields.button
      : selector === '[name="workspaceName"]' ? fields.name : fields.feedback };
  const calls = [];
  const scope = { document: { querySelector: () => form }, PERMISSIONS, authSession: session,
    workspaceProfileState: { workspace }, workspaceProfileMutation: null, workspaceProfileRecovery: null,
    resourcePageLifecycle: lifecycle, controlApiUrl: "https://control.test", DOMException,
    bindResourceElement: (element, permissions, navigation) => lifecycle.bind(element, permissions, navigation),
    updateWorkspaceProfile: async (input) => { calls.push({ kind: "save", input }); return save ? save(input) : response(); },
    reenterWorkspaceProfile: async (input) => { calls.push({ kind: "reenter", input }); return reenter ? reenter(input) : {}; },
    isAbortError: (error) => error?.name === "AbortError", authErrorMessage: (error) => error.message,
    handleResourceMutationError: () => false,
    refresh: async () => calls.push({ kind: "refresh" }),
  };
  runInNewContext(["resourceMutationRequest", "refreshResourceMutation", "bindWorkspaceProfileInteractions"].map(production).join("\n"), scope);
  scope.bindWorkspaceProfileInteractions();
  return { scope, fields, form, calls, state, lifecycle, submit: () => listeners.get("submit")?.({ preventDefault() {} }) };
}

test("profile save reenters the same workspace before refreshing", async () => {
  const state = fixture();
  await state.submit();
  assert.deepEqual(state.calls.map(({ kind }) => kind), ["save", "reenter", "refresh"]);
  assert.equal(state.calls[0].input.revision, 1);
  assert.ok(state.calls.slice(0, 2).every(({ input }) => input.workspaceId === workspace.workspace_id));
  assert.equal(state.scope.workspaceProfileRecovery, null);
});

test("profile reentry retry does not repeat an already successful save", async () => {
  let attempts = 0;
  const state = fixture({ reenter: async () => { if (++attempts === 1) throw new Error("Unavailable"); } });
  await state.submit();
  assert.match(state.fields.feedback.textContent, /资料已保存/u);
  assert.equal(state.fields.button.disabled, false);
  assert.equal(state.fields.name.disabled, true);
  await state.submit();
  assert.deepEqual(state.calls.map(({ kind }) => kind), ["save", "reenter", "reenter", "refresh"]);
});

for (const change of ["leave", "navigation", "workspace", "permission"]) {
  test(`late profile save cannot reenter after ${change}`, async () => {
    const saved = deferred();
    const state = fixture({ save: () => saved.promise });
    const submit = state.submit();
    await Promise.resolve();
    if (change === "leave") { state.form.isConnected = false; state.lifecycle.clear(); }
    else if (change === "navigation") state.state.hash = "projects";
    else if (change === "workspace") {
      const shell = state.state.shell;
      const context = { ...shell.workspaceContext, workspace_id: `wsp_${"2".repeat(32)}` };
      state.state.shell = { ...shell, workspaceContext: context,
        workspaceOwner: workspaceNavigationOwner(shell.workspaceIdentity,
          { context, authorization: shell.workspaceAuthorization }) };
      state.state.session = workspaceConsoleSessionFromShell(state.state.shell);
      assert.ok(state.state.session);
    }
    else state.state.session = null;
    state.lifecycle.enter();
    saved.resolve(response());
    await submit;
    assert.deepEqual(state.calls.map(({ kind }) => kind), ["save"]);
    assert.equal(state.calls[0].input.signal.aborted, true);
  });
}

test("profile submit prevents duplicate writes and readonly admin writes", async () => {
  const saved = deferred();
  const state = fixture({ save: () => saved.promise });
  const pending = state.submit();
  await state.submit();
  assert.equal(state.calls.length, 1);
  saved.resolve(response());
  await pending;
  const readonly = fixture({ role: "admin" });
  await readonly.submit();
  assert.equal(readonly.calls.length, 0);
});

test("profile CAS conflict leaves the form retryable without changing session", async () => {
  const state = fixture({ save: () => { throw new AuthApiError({ status: 412,
    code: "workspace.revision_conflict", message: "Changed elsewhere", requestId: "req_conflict" }); } });
  await state.submit();
  assert.deepEqual(state.calls.map(({ kind }) => kind), ["save"]);
  assert.equal(state.fields.button.disabled, false);
  assert.equal(state.fields.name.disabled, false);
  assert.match(state.fields.feedback.textContent, /req_conflict/u);
});
