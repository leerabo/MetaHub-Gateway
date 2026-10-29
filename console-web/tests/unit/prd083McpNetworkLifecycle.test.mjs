/** PAGE-115 network policy owner, projection, Dialog, and conflict lifecycle tests. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { mcpNetworkPolicyFromFormData } from "../../src/features/mcp-tools/bindMcpTools.js";
import {
  createMcpToolPageState,
  mcpToolPageOwner,
} from "../../src/features/mcp-tools/mcpToolPageState.js";
import { renderMcpTools } from "../../src/features/mcp-tools/renderMcpTools.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const SERVER_ID = `mcps_${"a".repeat(32)}`;
const POLICY_ID = `mcpnp_${"b".repeat(32)}`;

function context(permissionCodes, fingerprint = "mcp_network_authz_a") {
  return withPermissionSnapshot({
    tenant: { tenant_id: "tenant_mcp_network_a" },
    user: { tenant_id: "tenant_mcp_network_a", user_id: "usr_network_a", role: "ops_admin" },
    session: { session_id: "ses_network_a", session_revision: 1 },
  }, permissionCodes, { permissionRevision: 18, authorizationFingerprint: fingerprint });
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

function networkPolicy(extra = {}) {
  return {
    mcp_network_policy_id: POLICY_ID,
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
    ...extra,
  };
}

function server(extra = {}) {
  return {
    mcp_server_id: SERVER_ID,
    name: "Security MCP",
    endpoint_hint: "https://s***.example.test/[path]",
    endpoint_has_path: true,
    transport: "streamable_http",
    status: "active",
    state_revision: 2,
    protocol_version: "2025-06-18",
    tool_count: 1,
    active_tool_count: 1,
    review_required_tool_count: 0,
    network_policy: networkPolicy(),
    created_at: "2026-08-16T00:00:00Z",
    updated_at: "2026-08-16T00:01:00Z",
    ...extra,
  };
}

function acceptServers(state, owner, tab, generation, value = server()) {
  const ticket = state.beginList(owner, tab, generation);
  assert.ok(ticket);
  assert.equal(state.acceptList(ticket, {
    servers: [value],
    next_cursor: null,
    request_id: `req_${tab}_${generation}`,
  }, owner, generation), true);
  state.finishList(ticket);
}

function request(extra = {}) {
  return {
    expected_revision: 1,
    status: "active",
    egress_mode: "registered_endpoint_only",
    connect_timeout_seconds: 4,
    probe_timeout_seconds: 9,
    call_timeout_seconds: 25,
    failure_threshold: 4,
    open_seconds: 90,
    ...extra,
  };
}

test("PAGE-115 requires mcp.read and mirrors only the explicit network capability", () => {
  const manager = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_NETWORK_MANAGE]);
  const managerOwner = mcpToolPageOwner(manager, shell(manager));
  assert.ok(managerOwner);
  assert.equal(managerOwner.canManageNetwork, true);
  assert.equal(managerOwner.canManageServers, false);

  const reader = context([PERMISSIONS.MCP_READ], "mcp_network_reader");
  assert.equal(mcpToolPageOwner(reader, shell(reader)).canManageNetwork, false);

  const manageOnly = context([PERMISSIONS.MCP_NETWORK_MANAGE], "mcp_network_no_read");
  assert.equal(mcpToolPageOwner(manageOnly, shell(manageOnly)), null);
});

test("network list retains only the fixed summary and read-only owners get no edit command", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_NETWORK_MANAGE]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  assert.equal(state.switchTab(owner, "network"), true);
  acceptServers(state, owner, "network", 1, server({
    endpoint_url: "https://private.example.test/full/path",
    bearer_token: "network-token-canary",
    network_policy: networkPolicy({
      endpoint: "https://network-secret.example.test",
      hostname: "internal.example.test",
      resolved_ip: "192.0.2.9",
      deployment_cidr: "192.0.2.9/32",
      third_party_error: "network-third-party-canary",
    }),
  }));

  const serialized = JSON.stringify(state.snapshot());
  assert.doesNotMatch(
    serialized,
    /private\.example|network-token-canary|network-secret|internal\.example|192\.0\.2\.9|third-party-canary/,
  );
  assert.match(serialized, /registered_endpoint_only/);
  const html = renderMcpTools(state.snapshot(), auth, shell(auth));
  assert.match(html, /data-mcp-tab="network"/);
  assert.match(html, /class="mcp-table mcp-network-table"/);
  assert.match(html, /data-mcp-network-policy/);
  assert.doesNotMatch(html, /Endpoint URL|hostname|CIDR|192\.0\.2\.9|network-token-canary/);

  const reader = context([PERMISSIONS.MCP_READ], "mcp_network_reader_render");
  assert.doesNotMatch(
    renderMcpTools(state.snapshot(), reader, shell(reader)),
    /data-mcp-network-policy/,
  );
});

test("network summary rejects type coercion and inconsistent destination counts", () => {
  const auth = context([PERMISSIONS.MCP_READ]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  for (const invalidPolicy of [
    networkPolicy({ call_timeout_seconds: "30" }),
    networkPolicy({ status: "active", destination_count: 0 }),
    networkPolicy({ last_block_reason: "address_denied", last_blocked_at: null }),
  ]) {
    const state = createMcpToolPageState();
    state.enter(owner);
    state.switchTab(owner, "network");
    const ticket = state.beginList(owner, "network", 1);
    assert.equal(state.acceptList(ticket, {
      servers: [server({ network_policy: invalidPolicy })],
      next_cursor: null,
      request_id: "req_invalid_network_summary",
    }, owner, 1), true);
    assert.equal(state.snapshot().tabs.network.state, "error");
    assert.equal(state.snapshot().tabs.network.error.code, "mcp.response_invalid");
  }
});

test("network policy mutation is owner-bound CAS and updates both accepted Server projections", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_NETWORK_MANAGE]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  acceptServers(state, owner, "servers", 1);
  state.switchTab(owner, "network");
  acceptServers(state, owner, "network", 2);

  assert.equal(state.openNetworkDialog(owner, SERVER_ID), true);
  const ticket = state.beginNetworkPolicyUpdate(owner, request());
  assert.ok(ticket.controller instanceof AbortController);
  assert.equal(ticket.expectedRevision, 1);
  assert.equal(state.acceptNetworkPolicyUpdate(ticket, {
    network_policy: networkPolicy({
      status: "active",
      revision: 2,
      destination_count: 1,
      connect_timeout_seconds: 4,
      probe_timeout_seconds: 9,
      call_timeout_seconds: 25,
      failure_threshold: 4,
      open_seconds: 90,
    }),
    request_id: "req_network_update",
  }, owner), true);
  state.finishNetworkPolicyUpdate(ticket);

  const snapshot = state.snapshot();
  assert.equal(snapshot.networkDialog.open, false);
  assert.equal(snapshot.feedback.code, "mcp.network_policy_updated");
  assert.equal(snapshot.tabs.network.items[0].network_policy.revision, 2);
  assert.equal(snapshot.tabs.servers.items[0].network_policy.revision, 2);
});

test("409 keeps a safe draft, rebases only its revision, and never auto-submits", () => {
  const auth = context([PERMISSIONS.MCP_READ, PERMISSIONS.MCP_NETWORK_MANAGE]);
  const owner = mcpToolPageOwner(auth, shell(auth));
  const state = createMcpToolPageState();
  state.enter(owner);
  state.switchTab(owner, "network");
  acceptServers(state, owner, "network", 1);
  state.openNetworkDialog(owner, SERVER_ID);

  const stale = state.beginNetworkPolicyUpdate(owner, request());
  assert.equal(state.rejectNetworkPolicyUpdate(stale, {
    code: "mcp.state_conflict",
    requestId: "req_network_conflict",
    message: "third-party-error-must-not-survive",
  }, owner), true);
  state.finishNetworkPolicyUpdate(stale);
  assert.equal(state.snapshot().networkDialog.state, "conflict");
  assert.equal(state.snapshot().networkDialog.draft.call_timeout_seconds, 25);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /third-party-error-must-not-survive/);

  acceptServers(state, owner, "network", 2, server({
    network_policy: networkPolicy({
      status: "active",
      revision: 2,
      destination_count: 1,
      call_timeout_seconds: 18,
    }),
  }));
  const rebased = state.snapshot().networkDialog;
  assert.equal(rebased.state, "conflict");
  assert.equal(rebased.policy.revision, 2);
  assert.equal(rebased.draft.expected_revision, 2);
  assert.equal(rebased.draft.call_timeout_seconds, 25);

  const html = renderMcpTools(state.snapshot(), auth, shell(auth));
  assert.match(html, /dialog class="mcp-dialog mcp-network-dialog"/);
  assert.match(html, /mcp\.state_conflict/);
  assert.match(html, /安全草稿尚未提交/);
  assert.match(html, /name="expected_revision" value="2"/);
  assert.match(html, /name="call_timeout_seconds"[^>]+value="25"/);

  const retry = state.beginNetworkPolicyUpdate(owner, request({ expected_revision: 2 }));
  assert.ok(retry);
  state.clear();
  assert.equal(retry.controller.signal.aborted, true);
  assert.equal(state.acceptNetworkPolicyUpdate(retry, {
    network_policy: networkPolicy({ status: "active", revision: 3, destination_count: 1 }),
  }, owner), false);
  assert.equal(state.snapshot().networkDialog.open, false);
});

test("network Dialog form emits only the exact bounded policy body", () => {
  const data = new FormData();
  data.set("expected_revision", "7");
  data.set("status", "active");
  data.set("egress_mode", "registered_endpoint_only");
  data.set("connect_timeout_seconds", "5");
  data.set("probe_timeout_seconds", "10");
  data.set("call_timeout_seconds", "30");
  data.set("failure_threshold", "3");
  data.set("open_seconds", "60");
  data.set("hostname", "must-not-be-read.example.test");
  assert.deepEqual(mcpNetworkPolicyFromFormData(data), {
    expected_revision: 7,
    status: "active",
    egress_mode: "registered_endpoint_only",
    connect_timeout_seconds: 5,
    probe_timeout_seconds: 10,
    call_timeout_seconds: 30,
    failure_threshold: 3,
    open_seconds: 60,
  });
  data.set("open_seconds", "9");
  assert.throws(() => mcpNetworkPolicyFromFormData(data));
});

test("main wires network GET and checks the current ticket before global auth handling", async () => {
  const [main, state, binding] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/mcp-tools/mcpToolPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/mcp-tools/bindMcpTools.js", import.meta.url), "utf8"),
  ]);
  const start = main.indexOf("async function submitMcpNetworkPolicy(");
  const end = main.indexOf("function handleMcpAuthenticationFailure(", start);
  const source = main.slice(start, end);
  assert.ok(start >= 0 && end > start);
  assert.ok(source.indexOf("rejectNetworkPolicyUpdate") < source.indexOf("handleMcpAuthenticationFailure"));
  assert.match(main, /tab === "servers" \|\| tab === "network"/);
  assert.doesNotMatch(`${state}\n${binding}`, /localStorage|sessionStorage/);
});
