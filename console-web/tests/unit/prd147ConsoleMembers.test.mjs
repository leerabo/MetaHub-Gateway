import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { renderUserManagementPanel } from "../../src/features/users/renderUserManagementPanel.js";
import { listWorkspaceMembers, removeWorkspaceMember, updateWorkspaceMember } from "../../src/lib/users.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";

const member = { membership_id: `wmb_${"1".repeat(32)}`, display_name: "Member <script>",
  role: "developer", status: "active", revision: 4, is_current: false, can_manage: true };
const context = { permission_codes: [PERMISSIONS.USER_MANAGE] };
const state = { kind: "workspace", state: "ok", memberships: [member], role: "owner",
  total: 42, offset: 20, limit: 20 };
const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
function production(name) {
  const start = source.indexOf(`async function ${name}(`);
  const end = start + source.slice(start).search(/\n\}\r?\n/u);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 2);
}

test("Workspace member page renders Membership DTO without local-account creation", () => {
  const html = renderUserManagementPanel(state, context);
  assert.match(html, /data-workspace-member-form/u);
  assert.match(html, /data-membership-revision="4"/u);
  assert.match(html, /Member &lt;script&gt;/u);
  assert.match(html, /邀请后台成员/u);
  assert.doesNotMatch(html, /identity\.html#identity-memberships-title/u);
  assert.match(html, /data-workspace-members-offset="0"/u);
  assert.match(html, /data-workspace-members-offset="40"/u);
  assert.doesNotMatch(html, /create-user-form|data-user-role-form|name="password"|<script>/u);
});

test("Workspace member controls honor server manageability and protected identities", () => {
  for (const item of [{ ...member, can_manage: false }, { ...member, role: "owner" },
    { ...member, role: "service_account" }, { ...member, status: "pending" }]) {
    assert.doesNotMatch(renderUserManagementPanel({ ...state, memberships: [item] }, context),
      /data-workspace-member-form/u);
  }
  assert.equal(renderUserManagementPanel(state, { permission_codes: [] }), "");
});

test("Removed workspace members are absent from the active directory", () => {
  const html = renderUserManagementPanel({
    ...state,
    memberships: [{ ...member, status: "disabled" }],
  }, context);
  assert.doesNotMatch(html, /Member/u);
  assert.match(html, /暂无成员/u);
  assert.doesNotMatch(html, /data-workspace-member-remove/u);
});

test("Workspace member feedback is mutually exclusive with an action error", () => {
  const html = renderUserManagementPanel({
    ...state,
    actionError: { message: "Membership service is temporarily unavailable.", requestId: "req_membership" },
    feedback: "成员已更新",
  }, context);
  assert.match(html, /Membership service is temporarily unavailable/u);
  assert.doesNotMatch(html, /成员已更新/u);
});

test("Workspace empty later page preserves recovery without an inverted result range", () => {
  const html = renderUserManagementPanel({ ...state, memberships: [], total: 2 }, context);
  assert.match(html, /0-0 \/ 2/u);
  assert.doesNotMatch(html, /21-2/u);
  assert.match(html, /data-workspace-members-offset="0"\s+aria-label="上一页" title="上一页">/u);
});

test("Workspace member load uses owner-scoped lifecycle and never legacy Bearer users", async () => {
  const calls = [];
  const scope = { PERMISSIONS, workspaceMembersOffset: 20, workspaceMembersSearch: "", workspaceMembersFeedback: "",
    workspaceMembersActionError: null, authSession: { owner: { role: "owner" } },
    listWorkspaceMembers: async (input) => { calls.push(input); return { ...state }; },
    loadResourceState: async (lane, permissions, request, project) => {
      assert.equal(lane, "users");
      assert.deepEqual([...permissions], [PERMISSIONS.USER_MANAGE]);
      return project(await request({ controlApiUrl: "https://control.example.test", signal: "test-signal" }));
    } };
  runInNewContext(`${production("loadUsersState")}; result = loadUsersState({activeNavigationItemId:'users'});`, scope);
  const result = await scope.result;
  assert.equal(result.kind, "workspace");
  assert.equal(calls[0].offset, 20);
  assert.equal(calls[0].signal, "test-signal");
  assert.equal(Object.hasOwn(calls[0], "token"), false);
});

test("Membership clients preserve cookies, pagination, CSRF and If-Match", async () => {
  const previousFetch = globalThis.fetch, previousDocument = globalThis.document;
  const calls = [];
  globalThis.document = { cookie: "__Host-metahub-workspace-csrf=test-csrf" };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ request_id: "req_test", trace_id: "trace_test",
      ...(options.method === "PATCH" || options.method === "DELETE" ? { membership: member }
        : { memberships: [member], total: 42, offset: 20, limit: 20 }) }), { status: 200 });
  };
  try {
    const input = { controlApiUrl: "https://control.example.test", signal: new AbortController().signal };
    await listWorkspaceMembers({ ...input, offset: 20, limit: 20 });
    await updateWorkspaceMember({ ...input, membershipId: member.membership_id,
      revision: 4, role: "auditor", status: "disabled" });
    await removeWorkspaceMember({ ...input, membershipId: member.membership_id, revision: 4 });
    assert.equal(calls[0].url, "https://control.example.test/memberships?offset=20&limit=20");
    for (const call of calls) {
      assert.equal(call.options.credentials, "include");
      assert.equal(call.options.signal, input.signal);
      assert.equal(call.options.headers.Authorization, undefined);
    }
    assert.equal(calls[1].options.headers["If-Match"], '"4"');
    assert.equal(calls[1].options.headers["X-CSRF-Token"], "test-csrf");
    assert.deepEqual(JSON.parse(calls[1].options.body), { role: "auditor", status: "disabled" });
    await assert.rejects(listWorkspaceMembers({ ...input, offset: -1 }), AuthApiError);
    await assert.rejects(updateWorkspaceMember({ ...input, membershipId: member.membership_id,
      revision: 0, role: "auditor" }), AuthApiError);
    assert.equal(calls[2].url, "https://control.example.test/memberships/wmb_11111111111111111111111111111111");
    assert.equal(calls[2].options.method, "DELETE");
    assert.equal(calls[2].options.headers["If-Match"], '"4"');
    assert.equal(calls.length, 3);
    globalThis.fetch = async () => new Response(JSON.stringify({ code: "workspace_session.expired",
      message: "Expired", request_id: "req_expired" }), { status: 401 });
    await assert.rejects(listWorkspaceMembers(input), (error) => error instanceof AuthApiError
      && error.status === 401 && error.requestId === "req_expired");
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  }
});

test("Membership list retries one transient unavailable response", async () => {
  const previousFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) {
      return new Response(JSON.stringify({
        code: "membership.unavailable",
        message: "Membership service is temporarily unavailable.",
        request_id: "req_membership_unavailable",
      }), { status: 503 });
    }
    return new Response(JSON.stringify({
      memberships: [member], total: 1, offset: 0, limit: 20,
      request_id: "req_membership_ok", trace_id: "trace_membership_ok",
    }), { status: 200 });
  };
  try {
    const result = await listWorkspaceMembers({
      controlApiUrl: "https://control.example.test",
      offset: 0,
      limit: 20,
      signal: new AbortController().signal,
    });
    assert.equal(result.memberships.length, 1);
    assert.equal(calls.length, 2);
  } finally {
    globalThis.fetch = previousFetch;
  }
});
