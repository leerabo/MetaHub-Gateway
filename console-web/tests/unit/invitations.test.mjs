/** PRD-060 Console Invitation 客户端、私有状态和公开 bootstrap 专项测试。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  acceptInvitation,
  buildInvitationAcceptLink,
  captureInvitationRoute,
  createInvitation,
  InvitationApiError,
} from "../../src/lib/invitations.js";
import { createInvitationPageState } from "../../src/features/invitations/invitationPageState.js";
import { renderInvitationAcceptancePage } from "../../src/features/invitations/renderInvitationAcceptancePage.js";
import { renderUserManagementPanel } from "../../src/features/users/renderUserManagementPanel.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";

// 固定测试 Token 只验证长度与闭包边界，不对应任何真实邀请或测试数据库事实。
const RAW_TOKEN = "A".repeat(43);
// 第二个固定值用于模拟响应漂移，避免把主流程 Token 复用于负例。
const OTHER_TOKEN = "B".repeat(43);

/** 构造具备完整 Session、租户和授权指纹的 user.manage owner。 */
function authContext(userId = "user_admin") {
  const permissionCodes = [PERMISSIONS.USER_MANAGE];
  return {
    tenant: { tenant_id: "tenant_a" },
    session: { session_id: "session_a", session_revision: 3 },
    permission_codes: permissionCodes,
    permission_revision: 5,
    authorization_fingerprint: "authz_prd060_a",
    user: {
      tenant_id: "tenant_a",
      user_id: userId,
      email: "admin@example.com",
      role: "admin",
      permission_codes: permissionCodes,
      permission_revision: 5,
      authorization_fingerprint: "authz_prd060_a",
    },
  };
}

/** 构造 fetch 使用的最小 JSON 响应。 */
// 响应夹具只返回显式 payload，不从 URL、Header 或全局 Session 推导业务身份。
function jsonResponse(payload, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: new Map(),
    async json() {
      return payload;
    },
  };
}

test("public invitation fragment is scrubbed synchronously before auth bootstrap", async () => {
  const replacements = [];
  const fakeWindow = {
    location: {
      hash: `#accept-invitation/${RAW_TOKEN}`,
      pathname: "/",
      search: "",
    },
    history: {
      state: { safe: true },
      replaceState(state, title, url) {
        replacements.push({ state, title, url });
      },
    },
  };
  const route = captureInvitationRoute(fakeWindow);

  assert.equal(route.isPublicMode(), true);
  assert.equal(route.readToken(), RAW_TOKEN);
  assert.deepEqual(replacements, [{
    state: { safe: true },
    title: "",
    url: "/#accept-invitation",
  }]);
  assert.doesNotMatch(JSON.stringify(route), new RegExp(RAW_TOKEN));

  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const captureIndex = mainSource.indexOf("captureInvitationRoute(globalThis.window)");
  const authReadIndex = mainSource.indexOf("let authSession = { context: null, owner: null };", captureIndex);
  const publicBranchIndex = mainSource.indexOf("if (publicInvitationRoute?.isPublicMode())");
  const navigationBindIndex = mainSource.indexOf("bindNavigationHashRefresh(window, refresh)");
  assert.ok(captureIndex > 0 && captureIndex < authReadIndex);
  assert.doesNotMatch(mainSource, /readAuthSession\(\)/);
  assert.ok(publicBranchIndex > 0 && publicBranchIndex < navigationBindIndex);
});

test("invitation clients keep tenant out of create and bearer out of public accept", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return calls.length === 1
      ? jsonResponse({
          invitation: {
            email: "dev@example.com",
            role: "developer",
            expires_at: "2026-08-03T12:00:00Z",
          },
          accept_token: RAW_TOKEN,
          request_id: "req_create",
        }, { status: 201 })
      : jsonResponse({
          accepted: true,
          user: { email: "dev@example.com", role: "developer" },
          request_id: "req_accept",
        }, { status: 201 });
  };

  await createInvitation({
    controlApiUrl: "http://control.local",
    token: "admin-session",
    email: "dev@example.com",
    role: "developer",
    expiresInHours: 72,
  });
  await acceptInvitation({
    controlApiUrl: "http://control.local",
    token: RAW_TOKEN,
    displayName: "Developer",
    password: "LocalDev!2026",
  });

  const createBody = JSON.parse(calls[0].options.body);
  const acceptBody = JSON.parse(calls[1].options.body);
  assert.deepEqual(createBody, {
    email: "dev@example.com",
    role: "developer",
    expires_in_hours: 72,
  });
  assert.equal(createBody.tenant_id, undefined);
  assert.equal(calls[0].options.headers.Authorization, "Bearer admin-session");
  assert.equal(calls[1].options.credentials, "omit");
  assert.equal(calls[1].options.cache, "no-store");
  assert.equal(calls[1].options.referrerPolicy, "no-referrer");
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.equal(calls[1].options.headers.Cookie, undefined);
  assert.deepEqual(acceptBody, {
    display_name: "Developer",
    password: "LocalDev!2026",
  });
  assert.equal(acceptBody.tenant_id, undefined);
  assert.equal(acceptBody.email, undefined);
  assert.equal(acceptBody.role, undefined);
});

test("public network failures never propagate a token-bearing fetch error", async () => {
  globalThis.fetch = async (url) => {
    // 故意把 URL 放入底层异常，证明客户端会丢弃可能含 Token 的原始错误对象。
    throw new TypeError(`failed to fetch ${url}`);
  };

  await assert.rejects(
    () => acceptInvitation({
      controlApiUrl: "http://control.local",
      token: RAW_TOKEN,
      displayName: "Developer",
      password: "LocalDev!2026",
    }),
    (error) => {
      assert.equal(error instanceof InvitationApiError, true);
      assert.equal(error.code, "invitation.unavailable");
      assert.doesNotMatch(error.message, new RegExp(RAW_TOKEN));
      assert.doesNotMatch(String(error.stack), new RegExp(RAW_TOKEN));
      return true;
    },
  );
});

test("admin invitation state keeps link private and rejects stale session results", () => {
  const state = createInvitationPageState();
  const owner = {
    authContext: authContext(),
    token: "session-token-a",
    activeNavigation: "users",
  };
  assert.equal(state.openDialog(owner), true);
  state.markDirty();
  assert.equal(state.isBlockingRefresh(), true);
  const ticket = state.beginCreate(owner, { email: "dev@example.com", role: "developer" });
  assert.ok(ticket);
  const result = {
    invitation: {
      tenant_id: "tenant_a",
      email: "dev@example.com",
      role: "developer",
      status: "pending",
      expires_at: "2026-08-03T12:00:00Z",
    },
    accept_token: RAW_TOKEN,
    request_id: "req_create",
  };
  assert.equal(state.resolveCreate(ticket, result, "http://console.local"), true);
  assert.equal(state.readAcceptLink(), buildInvitationAcceptLink("http://console.local", RAW_TOKEN));
  assert.match(state.readMailText(), /dev@example\.com/);
  assert.doesNotMatch(JSON.stringify(state.snapshot()), new RegExp(RAW_TOKEN));
  assert.doesNotMatch(JSON.stringify(state.snapshot()), /accept-invitation/);

  state.syncOwner({ ...owner, token: "session-token-b" });
  assert.equal(state.readAcceptLink(), null);
  assert.equal(state.snapshot().hasSecret, false);
  assert.equal(state.resolveCreate(ticket, result, "http://console.local"), false);

  state.openDialog({ ...owner, token: "session-token-b" });
  const nextTicket = state.beginCreate(
    { ...owner, token: "session-token-b" },
    { email: "dev@example.com", role: "developer" },
  );
  state.syncOwner({
    authContext: authContext("other_admin"),
    token: "session-token-c",
    activeNavigation: "users",
  });
  assert.equal(state.resolveCreate(nextTicket, result, "http://console.local"), false);
  assert.equal(state.readAcceptLink(), null);
});

test("renderers expose invitation controls without placing raw token in HTML", () => {
  const context = authContext();
  const adminHtml = renderUserManagementPanel(
    {
      state: "ok",
      users: [],
      roles: [{ code: "developer", name: "开发者" }],
    },
    context,
    {
      open: true,
      busy: false,
      hasSecret: true,
      invitation: {
        email: "dev@example.com",
        role: "developer",
        expiresAt: "2026-08-03T12:00:00Z",
      },
      feedback: { state: "success", message: "邀请链接已生成", requestId: "req_create" },
    },
  );
  const publicHtml = renderInvitationAcceptancePage({ status: "form" });

  assert.match(adminHtml, /invite-user-button/);
  assert.match(adminHtml, /invitation-dialog/);
  assert.match(adminHtml, /create-user-form/);
  assert.match(adminHtml, /name="expiresInHours"/);
  assert.match(adminHtml, /<option value="24">24 小时<\/option>/);
  assert.match(adminHtml, /<option value="72" selected>72 小时<\/option>/);
  assert.match(adminHtml, /<option value="168">168 小时<\/option>/);
  assert.doesNotMatch(adminHtml, /name="expiresInHours" type="number"/);
  assert.match(adminHtml, /data-manual-invitation-link hidden/);
  assert.match(adminHtml, /data-manual-invitation-mail hidden/);
  assert.match(adminHtml, /data-invitation-manual-copy hidden/);
  assert.doesNotMatch(adminHtml, new RegExp(RAW_TOKEN));
  assert.doesNotMatch(adminHtml, /accept-invitation\//);
  assert.doesNotMatch(publicHtml, new RegExp(RAW_TOKEN));
  assert.doesNotMatch(publicHtml, /Authorization/);
  assert.match(publicHtml, /name="confirmPassword"/);

  const busyHtml = renderUserManagementPanel(
    { state: "ok", users: [], roles: [{ code: "developer", name: "开发者" }] },
    context,
    { open: true, busy: true, hasSecret: false },
  );
  assert.match(busyHtml, /data-invitation-close[^>]* disabled/);
  assert.match(busyHtml, /class="invitation-create-button" disabled aria-busy="true"/);
});

test("invalid invitation fragment is scrubbed and never treated as console navigation", () => {
  const replacements = [];
  const route = captureInvitationRoute({
    location: { hash: "#accept-invitation/not-a-token", pathname: "/", search: "?a=1" },
    history: {
      state: null,
      replaceState(_state, _title, url) {
        replacements.push(url);
      },
    },
  });

  assert.equal(route.isPublicMode(), true);
  assert.equal(route.readToken(), null);
  assert.deepEqual(replacements, ["/?a=1#accept-invitation"]);
});

test("leaving public invitation removes the sentinel before normal bootstrap", async () => {
  const replacements = [];
  const route = captureInvitationRoute({
    location: { hash: `#accept-invitation/${RAW_TOKEN}`, pathname: "/console", search: "?lang=zh" },
    history: {
      state: { safe: true },
      replaceState(_state, _title, url) {
        replacements.push(url);
      },
    },
  });

  route.leave();
  assert.equal(route.isPublicMode(), false);
  assert.equal(route.readToken(), null);
  assert.deepEqual(replacements, [
    "/console?lang=zh#accept-invitation",
    "/console?lang=zh",
  ]);

  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const publicBranch = mainSource.slice(
    mainSource.indexOf("if (publicInvitationRoute?.isPublicMode())"),
    mainSource.indexOf("} else {", mainSource.indexOf("if (publicInvitationRoute?.isPublicMode())")),
  );
  assert.match(publicBranch, /addEventListener\("hashchange"/);
  assert.match(publicBranch, /publicInvitationRoute\.leave\(\)/);
});

test("busy invitation creation cannot be closed before its one-time result settles", () => {
  const state = createInvitationPageState();
  const owner = {
    authContext: authContext(),
    token: "session-token-a",
    activeNavigation: "users",
  };
  assert.equal(state.openDialog(owner), true);
  const ticket = state.beginCreate(owner, { email: "dev@example.com", role: "developer" });
  assert.ok(ticket);
  assert.equal(state.close(), false);
  assert.equal(state.snapshot().open, true);
  assert.equal(state.snapshot().busy, true);

  assert.equal(state.rejectCreate(ticket, { message: "创建失败", requestId: "req_failed" }), true);
  assert.equal(state.close(), true);
  assert.equal(state.snapshot().open, false);
});

test("admin state rejects a tenant or intent-drifted create response", () => {
  const state = createInvitationPageState();
  const owner = {
    authContext: authContext(),
    token: "session-token-a",
    activeNavigation: "users",
  };
  state.openDialog(owner);
  const ticket = state.beginCreate(owner, { email: "dev@example.com", role: "developer" });

  assert.throws(
    () => state.resolveCreate(ticket, {
      invitation: {
        tenant_id: "tenant_b",
        email: "dev@example.com",
        role: "developer",
        status: "pending",
      },
      accept_token: OTHER_TOKEN,
    }, "http://console.local"),
    /邀请响应校验失败/,
  );
  assert.equal(state.readAcceptLink(), null);
  assert.equal(state.snapshot().hasSecret, false);
});

test("main source gates user PII loading by active PAGE-086 and checks password confirmation", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(mainSource, /loadUsersState\(consoleShellState\)/);
  assert.match(mainSource, /activeNavigationItemId !== "users"/);
  assert.match(mainSource, /password !== confirmPassword/);
  const copyHelper = mainSource.slice(
    mainSource.indexOf("async function copyInvitationSecret"),
    mainSource.indexOf("function bindAuthenticatedEvents"),
  );
  assert.doesNotMatch(copyHelper, /revealInvitationSecret\(/);
  assert.match(copyHelper, /manualButton\.hidden = false/);
  assert.match(mainSource, /data-manual-invitation-link.*addEventListener/s);
  assert.match(mainSource, /30_000/);
});

test("create 403 clears invitation owner before refreshing authorization", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const handlerStart = mainSource.indexOf('invitationForm?.addEventListener("submit"');
  const handlerEnd = mainSource.indexOf(
    'invitationDialog?.querySelector("[data-copy-invitation-link]")',
    handlerStart,
  );
  const handler = mainSource.slice(handlerStart, handlerEnd);
  const deniedIndex = handler.indexOf("Number(error?.status) === 403");
  const clearIndex = handler.indexOf("invitationPageState.clear();", deniedIndex);
  const refreshIndex = handler.indexOf("await refresh();", deniedIndex);
  const rejectIndex = handler.indexOf("invitationPageState.rejectCreate(ticket, error)");

  assert.ok(handlerStart > 0 && handlerEnd > handlerStart);
  assert.ok(deniedIndex > 0 && deniedIndex < clearIndex);
  assert.ok(clearIndex < refreshIndex && refreshIndex < rejectIndex);
});
