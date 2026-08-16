/** PAGE-114 owner, sensitive-state and renderer lifecycle tests. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createMcpToolPageState,
  mcpToolPageOwner,
} from "../../src/features/mcp-tools/mcpToolPageState.js";
import { renderMcpTools } from "../../src/features/mcp-tools/renderMcpTools.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const SERVER_ID = `mcps_${"a".repeat(32)}`;
const NETWORK_POLICY_ID = `mcpnp_${"e".repeat(32)}`;
const TOOL_ID = `mcpt_${"b".repeat(32)}`;
const INVOCATION_ID = `mcpi_${"c".repeat(32)}`;
const HASH = "d".repeat(64);

function context(permissionCodes, fingerprint = "mcp_authz_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: "tenant_mcp_a" },
    user: { tenant_id: "tenant_mcp_a", user_id: "usr_mcp_a", role: "developer" },
    session: { session_id: "ses_mcp_a", session_revision: 1 },
  }, permissionCodes, { permissionRevision: 17, authorizationFingerprint: fingerprint });
}

function shell(authContext) {
  return {
    state: "ok",
    activeNavigationItemId: "mcp-tools",
    navigation: {
      active_navigation_item_id: "mcp-tools",
      permission_codes: authContext.permission_codes,
      permission_revision: authContext.permission_revision,
      authorization_fingerprint: authContext.authorization_fingerprint,
      navigation_items: [{
        id: "mcp-tools",
        target_section: "mcp-tools",
        visible: true,
        disabled: false,
        required_permissions: [PERMISSIONS.MCP_READ],
      }],
    },
  };
}

function server(extra = {}) {
  return {
    mcp_server_id: SERVER_ID,
    name: "Operations",
    endpoint_hint: "https://m***.example.test/[path]",
    endpoint_has_path: true,
    transport: "streamable_http",
    status: "active",
    state_revision: 2,
    protocol_version: "2025-06-18",
    tool_count: 1,
    active_tool_count: 0,
    network_policy: {
      mcp_network_policy_id: NETWORK_POLICY_ID,
      mcp_server_id: SERVER_ID,
      status: "disabled",
      revision: 1,
      egress_mode: "registered_endpoint_only",
      destination_count: 0,
      connect_timeout_seconds: 5,
      probe_timeout_seconds: 10,
      call_timeout_seconds: 30,
      failure_threshold: 3,
      open_seconds: 60,
      circuit_state: "not_initialized",
      blocked_count: 0,
      last_block_reason: null,
      last_blocked_at: null,
      updated_at: "2026-08-16T00:00:00Z",
    },
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:01:00Z",
    endpoint_url: "https://private.example.test/full/path",
    bearer_token: "secret-server-canary",
    ...extra,
  };
}

function tool(extra = {}) {
  return {
    mcp_tool_id: TOOL_ID,
    mcp_server_id: SERVER_ID,
    remote_name: "create_ticket",
    title: "Create ticket",
    schema_revision: 1,
    policy_revision: 2,
    input_schema_sha256: HASH,
    output_schema_sha256: null,
    risk_level: "standard",
    approval_required: false,
    approval_expires_seconds: 3600,
    required_permission_codes: ["project.read"],
    allowed_role_count: 1,
    allowed_user_count: 0,
    result_redaction_count: 1,
    status: "review_required",
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:01:00Z",
    ...extra,
  };
}

test("page owner requires exact mcp.read navigation and mirrors capabilities", () => {
  const auth = context([
    PERMISSIONS.MCP_READ,
    PERMISSIONS.MCP_SERVER_MANAGE,
    PERMISSIONS.MCP_POLICY_MANAGE,
  ]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  assert.ok(owner);
  assert.equal(owner.canManageServers, true);
  assert.equal(owner.canManagePolicies, true);

  const revoked = context([PERMISSIONS.MCP_SERVER_MANAGE], "mcp_authz_revoked");
  assert.equal(mcpToolPageOwner(revoked, shell(revoked)), null);
});

test("server list projection drops full endpoints, credentials and unknown response fields", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_SERVER_MANAGE]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState({ createIdempotencyKey: () => "mcp_register_test_key" });
  state.enter(owner);
  const ticket = state.beginList(owner, "servers", 1);
  assert.equal(state.acceptList(ticket, {
    servers: [server()],
    next_cursor: null,
    request_id: "req_mcp_servers",
    arguments: { private: "argument-canary" },
  }, owner, 1), true);
  state.finishList(ticket);

  const serialized = JSON.stringify(state.snapshot());
  assert.doesNotMatch(serialized, /private\.example|secret-server-canary|argument-canary/);
  assert.match(serialized, /m\*\*\*\.example/);
  const html = renderMcpTools(state.snapshot(), auth, shell(auth));
  assert.match(html, /data-mcp-open-register/);
  assert.doesNotMatch(html, /data-mcp-refresh-server/);
  assert.match(html, /data-mcp-server-status/);

  assert.equal(state.openRegister(owner), true);
  const register = state.beginRegister(owner);
  assert.ok(register.controller instanceof AbortController);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /mcp_register_test_key/);
});

test("Server enable and disable commands are owner-bound CAS operations and discard late results", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_SERVER_MANAGE]);
  const pageShell = shell(auth);
  const owner = mcpToolPageOwner(auth, pageShell);
  const keys = [];
  const state = createMcpToolPageState({
    createIdempotencyKey(kind) {
      keys.push(kind);
      return `mcp_${kind}_contract`;
    },
  });
  state.enter(owner);
  const list = state.beginList(owner, "servers", 1);
  state.acceptList(list, { servers: [server()], next_cursor: null, request_id: "req_servers" }, owner, 1);
  state.finishList(list);

  const disable = state.beginServerStatus(owner, SERVER_ID);
  assert.equal(disable.expectedRevision, 2);
  assert.equal(disable.nextStatus, "disabled");
  assert.ok(disable.controller instanceof AbortController);
  assert.deepEqual(keys, ["disable"]);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /mcp_disable_contract/);
  assert.equal(state.acceptServerStatus(disable, {
    server: server({ status: "disabled", state_revision: 3 }),
    request_id: "req_disable",
  }, owner), true);
  state.finishServerStatus(disable);
  assert.equal(state.snapshot().tabs.servers.items[0].status, "disabled");
  assert.equal(state.snapshot().feedback.code, "mcp.server_disabled");

  const enable = state.beginServerStatus(owner, SERVER_ID);
  assert.equal(enable.expectedRevision, 3);
  assert.equal(enable.nextStatus, "active");
  assert.deepEqual(keys, ["disable", "enable"]);
  state.clear();
  assert.equal(enable.controller.signal.aborted, true);
  assert.equal(state.acceptServerStatus(enable, {
    server: server({ status: "active", state_revision: 4 }),
  }, owner), false);
});

test("next cursor remains private and does not become the current cursor until an explicit page command", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  const first = state.beginList(owner, "servers", 1);
  state.acceptList(first, {
    servers: [server()],
    next_cursor: "cursor_page_two",
    request_id: "req_page_one",
  }, owner, 1);
  state.finishList(first);
  assert.equal(state.listQuery("servers").cursor, null);
  assert.equal(state.snapshot().tabs.servers.canNext, true);

  assert.equal(state.nextPage(owner), true);
  assert.equal(state.listQuery("servers").cursor, "cursor_page_two");
  assert.equal(state.snapshot().tabs.servers.canPrevious, true);
  const second = state.beginList(owner, "servers", 2);
  state.acceptList(second, { servers: [], next_cursor: null, request_id: "req_page_two" }, owner, 2);
  state.finishList(second);
  assert.equal(state.previousPage(owner), true);
  assert.equal(state.listQuery("servers").cursor, null);
});

test("canonical Schema is explicit-detail only, escaped in HTML and erased on close", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_POLICY_MANAGE]);
  const pageShell = shell(auth);
  const owner = mcpToolPageOwner(auth, pageShell);
  const state = createMcpToolPageState();
  state.enter(owner);
  state.switchTab(owner, "tools");
  const list = state.beginList(owner, "tools", 3);
  state.acceptList(list, { tools: [tool({
    description: "list-description-canary",
    input_schema_original: { type: "object", private: "list-schema-canary" },
  })], next_cursor: null, request_id: "req_tools" }, owner, 3);
  state.finishList(list);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /list-description-canary|list-schema-canary/);

  assert.equal(state.openToolDialog(owner, TOOL_ID, "detail"), true);
  const detail = state.beginToolDetail(owner);
  assert.equal(state.acceptToolDetail(detail, { tool: tool({
    description: "<script>schema-canary</script>",
    input_schema_original: { type: "object", properties: { value: { const: "<img src=x>" } } },
    input_schema_effective: { type: "object", additionalProperties: false },
    output_schema_original: null,
    output_schema_effective: null,
    allowed_role_ids: ["role_ops"],
    allowed_user_ids: [],
    result_redaction_pointers: ["/token"],
  }), request_id: "req_tool_detail" }, owner), true);
  state.finishToolDetail(detail);

  const html = renderMcpTools(state.snapshot(), auth, pageShell);
  assert.match(html, /Input · Original/);
  assert.match(html, /&lt;script&gt;schema-canary&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /<script>schema-canary<\/script>|<img src=x>/);

  assert.equal(state.closeToolDialog(owner), true);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /schema-canary|img src/);
});

test("owner loss aborts a pending Tool detail and discards its late Schema response", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  state.switchTab(owner, "tools");
  const list = state.beginList(owner, "tools", 4);
  state.acceptList(list, { tools: [tool()], next_cursor: null, request_id: "req_tools" }, owner, 4);
  state.finishList(list);

  assert.equal(state.openToolDialog(owner, TOOL_ID, "detail"), true);
  const detail = state.beginToolDetail(owner);
  state.clear();
  assert.equal(detail.controller.signal.aborted, true);
  assert.equal(state.acceptToolDetail(detail, { tool: tool({
    description: "late-schema-canary",
    input_schema_original: { type: "object", properties: { secret: { type: "string" } } },
    input_schema_effective: { type: "object", additionalProperties: false },
    output_schema_original: null,
    output_schema_effective: null,
    allowed_role_ids: [],
    allowed_user_ids: [],
    result_redaction_pointers: [],
  }) }, owner), false);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /late-schema-canary|secret/);
  assert.equal(state.snapshot().toolDialog.open, false);
});

test("invocation tab retains only safe tracking summaries and aborts on owner loss", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  state.switchTab(owner, "invocations");
  const ticket = state.beginList(owner, "invocations", 5);
  state.acceptList(ticket, {
    invocations: [{
      mcp_invocation_id: INVOCATION_ID,
      mcp_server_id: SERVER_ID,
      mcp_tool_id: TOOL_ID,
      status: "failed",
      risk_level: "standard",
      approval_required: false,
      approval_task_id: null,
      input_dlp: { status: "clean", match_count: 0, rule_id: null },
      output_dlp: { status: "blocked", match_count: 1, rule_id: "dlp_private" },
      result_redaction_count: 2,
      probe_attempt_count: 1,
      call_attempt_count: 1,
      request_bytes: 120,
      response_bytes: 80,
      duration_ms: 31,
      execution_status: "called",
      invocation_count: 1,
      tool_call_count: 1,
      cost_status: "not_reported",
      error_code: "mcp.output_dlp_blocked",
      created_at: "2026-08-16T00:00:00Z",
      completed_at: "2026-08-16T00:00:01Z",
      arguments: { token: "argument-secret-canary" },
      result: "result-secret-canary",
    }],
    next_cursor: null,
    request_id: "req_invocations",
  }, owner, 5);
  const serialized = JSON.stringify(state.snapshot());
  assert.doesNotMatch(serialized, /argument-secret-canary|result-secret-canary|dlp_private/);
  assert.match(serialized, /mcp\.output_dlp_blocked/);

  const next = state.beginList(owner, "invocations", 6);
  state.clear();
  assert.equal(next.controller.signal.aborted, true);
  assert.equal(state.acceptList(next, { invocations: [] }, owner, 6), false);
});

test("read-only owner sees no registration or policy controls", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const pageShell = shell(auth);
  const owner = mcpToolPageOwner(auth, pageShell);
  const state = createMcpToolPageState();
  state.enter(owner);
  const list = state.beginList(owner, "servers", 1);
  state.acceptList(list, { servers: [server()], next_cursor: null, request_id: "req_servers" }, owner, 1);
  state.finishList(list);
  const html = renderMcpTools(state.snapshot(), auth, pageShell);
  assert.doesNotMatch(html, /data-mcp-open-register|data-mcp-tool-policy|data-mcp-server-status/);
});

test("developer shell dispatches the backend-owned mcp-tools navigation to PAGE-114", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const pageShell = shell(auth);
  const owner = mcpToolPageOwner(auth, pageShell);
  const state = createMcpToolPageState();
  state.enter(owner);
  const list = state.beginList(owner, "servers", 1);
  state.acceptList(list, { servers: [], next_cursor: null, request_id: "req_shell" }, owner, 1);
  state.finishList(list);
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: auth,
    consoleShellState: pageShell,
    mcpToolsState: state.snapshot(),
  });
  assert.match(container.innerHTML, /class="panel mcp-tools-panel"/);
  assert.match(container.innerHTML, /Servers/);
  assert.doesNotMatch(container.innerHTML, /Endpoint URL/);
});

test("main lifecycle clears PAGE-114 before logout await and feature code never uses browser storage", async () => {
  const [main, binding, state, client] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/mcp-tools/bindMcpTools.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/mcp-tools/mcpToolPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/lib/mcpTools.js", import.meta.url), "utf8"),
  ]);
  const logoutStart = main.indexOf('logoutButton?.addEventListener("click"');
  const clearIndex = main.indexOf("mcpToolPageState.clear();", logoutStart);
  const awaitIndex = main.indexOf("await logoutAdmin", logoutStart);
  assert.ok(logoutStart >= 0 && clearIndex > logoutStart && clearIndex < awaitIndex);
  const loginStart = main.indexOf("function renderLogin(");
  const loginClear = main.indexOf("mcpToolPageState.clear();", loginStart);
  const loginRender = main.indexOf("renderLoginPage(app", loginStart);
  assert.ok(loginStart >= 0 && loginClear > loginStart && loginClear < loginRender);
  assert.ok(binding.indexOf("eraseRegistrationInputs(form)") < binding.indexOf("callbacks.onSubmitRegister"));
  assert.doesNotMatch(`${binding}\n${state}\n${client}`, /localStorage|sessionStorage|location\.hash/);
});

test("hashchange and pagehide synchronously destroy PAGE-114 before asynchronous navigation refresh", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const hashAnchor = main.indexOf("adminAssistantPageState.leave();");
  const hashStart = main.lastIndexOf('window.addEventListener("hashchange", () => {', hashAnchor);
  const genericRefresh = main.indexOf("bindNavigationHashRefresh(window, refresh)", hashStart);
  const hashCondition = main.indexOf('if (nextItemId !== "mcp-tools")', hashStart);
  const hashClear = main.indexOf("mcpToolPageState.clear();", hashCondition);
  assert.ok(
    hashStart >= 0
      && hashCondition > hashStart
      && hashClear > hashCondition
      && hashClear < genericRefresh,
  );

  const pagehideStart = main.indexOf('window.addEventListener("pagehide", () => {', genericRefresh);
  const bootstrap = main.indexOf("void start();", pagehideStart);
  const pagehideClear = main.indexOf("mcpToolPageState.clear();", pagehideStart);
  assert.ok(pagehideStart >= 0 && pagehideClear > pagehideStart && pagehideClear < bootstrap);

  const ownerStart = main.indexOf("function currentMcpToolOwner(");
  const ownerEnd = main.indexOf("function renderCurrentMcpToolsPanel(", ownerStart);
  const ownerSource = main.slice(ownerStart, ownerEnd);
  assert.match(ownerSource, /hashNavigation !== "mcp-tools"/);
  assert.doesNotMatch(ownerSource, /hashNavigation &&/);
});

test("stale MCP mutation and detail failures cannot process global 401 or 403", async () => {
  const main = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const lanes = [
    ["submitMcpServerRegistration", "submitMcpServerRefresh", "rejectRegister"],
    ["submitMcpServerRefresh", "submitMcpServerStatus", "rejectServerRefresh"],
    ["submitMcpServerStatus", "loadMcpToolDialog", "rejectServerStatus"],
    ["loadMcpToolDialog", "submitMcpToolPolicy", "rejectToolDetail"],
    ["submitMcpToolPolicy", "handleMcpAuthenticationFailure", "rejectPolicyUpdate"],
  ];
  for (const [functionName, nextFunctionName, rejectName] of lanes) {
    const start = main.indexOf(`async function ${functionName}(`);
    const end = main.indexOf(`function ${nextFunctionName}(`, start + 1);
    const source = main.slice(start, end);
    const currentFirst = source.indexOf(`if (!mcpToolPageState.${rejectName}(`);
    const globalAuth = source.indexOf("if (handleMcpAuthenticationFailure(error))");
    assert.ok(start >= 0 && end > start, `${functionName} source boundary`);
    assert.ok(currentFirst >= 0, `${functionName} checks its current ticket`);
    assert.ok(globalAuth > currentFirst, `${functionName} handles auth only after current-ticket acceptance`);
  }
});
