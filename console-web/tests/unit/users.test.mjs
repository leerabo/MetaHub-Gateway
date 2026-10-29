/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/tests/unit/users.test.mjs
 */

import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderUserManagementPanel } from "../../src/features/users/renderUserManagementPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createUser, listUsers, updateUserRole } from "../../src/lib/users.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const usersPayload = {
  users: [
    {
      user_id: "user_1",
      tenant_id: "tenant_1",
      email: "dev@example.com",
      display_name: "Dev User",
      role: "developer",
      status: "active",
    },
  ],
  roles: [
    { code: "admin", name: "管理员", description: "管理配置" },
    { code: "developer", name: "开发者", description: "调试调用" },
    { code: "viewer", name: "只读用户", description: "只读" },
  ],
  request_id: "req_users",
  trace_id: "trc_users",
};

test("users client lists creates and updates roles with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_users"]]),
      async json() {
        if (options.method === "POST") {
          // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
          return { user: usersPayload.users[0], request_id: "req_create" };
        }
        if (options.method === "PATCH") {
          // 权限判断必须在后端完成，避免只靠前端隐藏入口造成越权。
          return { user: { ...usersPayload.users[0], role: "viewer" }, request_id: "req_role" };
        }
        return usersPayload;
      },
    };
  };

  const listed = await listUsers({ controlApiUrl: "http://control.local", token: "jwt-token" });
  const created = await createUser({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    email: "dev@example.com",
    displayName: "Dev User",
    password: "LocalDev!2026",
    role: "developer",
  });
  const changed = await updateUserRole({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    userId: "user_1",
    role: "viewer",
  });

  assert.equal(listed.state, "ok");
  assert.equal(created.user.email, "dev@example.com");
  assert.equal(changed.user.role, "viewer");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "POST");
  assert.equal(calls[2].options.method, "PATCH");
  assert.equal(JSON.parse(calls[1].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[1].options.body).display_name, "Dev User");
  assert.deepEqual(JSON.parse(calls[2].options.body), { role: "viewer" });
});

test("users client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_users_denied",
        trace_id: "trc_users_denied",
      };
    },
  });

  await assert.rejects(
    () =>
      listUsers({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
      }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_users_denied");
      return true;
    },
  );
});

test("renderUserManagementPanel displays admin controls and escapes unsafe values", () => {
  const html = renderUserManagementPanel(
    {
      state: "ok",
      ...usersPayload,
      users: [
        {
          ...usersPayload.users[0],
          email: "<dev>@example.com",
          display_name: "<Dev>",
        },
      ],
    },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.USER_MANAGE],
    ),
  );

  assert.match(html, /create-user-form/);
  assert.match(html, /data-user-role-form/);
  assert.match(html, /developer" selected/);
  assert.doesNotMatch(html, /<Dev>/);
  assert.match(html, /&lt;Dev&gt;/);
  assert.match(html, /&lt;dev&gt;@example.com/);
});

test("renderUserManagementPanel hides user management for non-admin roles", () => {
  const developerHtml = renderUserManagementPanel(
    { state: "ok", ...usersPayload },
    withPermissionSnapshot({ user: { role: "developer" } }, []),
  );
  const viewerHtml = renderUserManagementPanel(
    { state: "ok", ...usersPayload },
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  );

  assert.equal(developerHtml, "");
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart does not render user management section for developer", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: {
        display_name: "Dev User",
        email: "dev@example.com",
        role: "developer",
        tenant_id: "tenant_1",
      },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-07T18:00:00Z" },
    }, []),
    usersState: { state: "ok", ...usersPayload },
  });

  assert.doesNotMatch(container.innerHTML, /users-panel/);
  assert.doesNotMatch(container.innerHTML, /create-user-form/);
});
