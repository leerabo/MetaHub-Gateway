/**
 * 模块职责：维护 MetaHub 控制台前端的数据读取、状态转换和用户可见反馈。
 * 维护边界：前端只负责展示与交互提示，权限结论、密钥脱敏和租户隔离必须以后端返回为准。
 * 来源文件：console-web/tests/unit/auth.test.mjs
 */

import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderLoginPage } from "../../src/features/auth/renderLoginPage.js";
import {
  AuthApiError,
  clearAuthSession,
  getCurrentUser,
  loginAdmin,
  readAuthSession,
  saveAuthSession,
} from "../../src/lib/auth.js";

test("loginAdmin posts credentials and getCurrentUser sends bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_auth"]]),
      async json() {
        // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
        if (String(url).endsWith("/auth/login")) {
          // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
          return { access_token: "jwt-token", token_type: "Bearer" };
        }
        return { user: { email: "admin@metahub.local" } };
      },
    };
  };

  const login = await loginAdmin({
    controlApiUrl: "http://control.local",
    tenantSlug: "default",
    email: "admin@metahub.local",
    password: "secret",
  });
  const me = await getCurrentUser({ controlApiUrl: "http://control.local", token: login.access_token });

  assert.equal(login.access_token, "jwt-token");
  assert.equal(me.user.email, "admin@metahub.local");
  assert.equal(calls[0].url, "http://control.local/auth/login");
  assert.equal(calls[1].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(JSON.stringify(calls).includes("Bearer jwt-token"), true);
});

test("auth client returns structured errors without exposing submitted password", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 401,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.login_invalid",
        message: "Login credentials are invalid.",
        request_id: "req_login",
        trace_id: "trc_login",
        suggestion: "Sign in again.",
        diagnosis: {
          rule_id: "unknown_error",
          category: "unknown",
          reason: "未命中特定规则",
          suggestion: "查看 request_id",
          action: "检查日志",
          severity: "warning",
          retryable: false,
        },
      };
    },
  });

  await assert.rejects(
    () =>
      loginAdmin({
        controlApiUrl: "http://control.local",
        tenantSlug: "default",
        email: "admin@metahub.local",
        password: "PasswordShouldNotLeak",
      }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.login_invalid");
      assert.equal(error.requestId, "req_login");
      assert.equal(error.suggestion, "Sign in again.");
      assert.equal(error.diagnosis.rule_id, "unknown_error");
      assert.equal(String(error).includes("PasswordShouldNotLeak"), false);
      return true;
    },
  );
});

test("auth session storage can save, read, and clear context", () => {
  const storage = createMemoryStorage();
  const context = { user: { email: "admin@metahub.local" } };

  saveAuthSession("jwt-token", context, storage);
  assert.deepEqual(readAuthSession(storage), { token: "jwt-token", context });

  clearAuthSession(storage);
  assert.deepEqual(readAuthSession(storage), { token: null, context: null });
});

test("auth session storage rejects empty token or malformed context", () => {
  const storage = createMemoryStorage();
  const context = { user: { email: "admin@metahub.local" } };

  // 模拟迟到刷新在注销后尝试回写；两个认证槽位都必须保持不存在。
  saveAuthSession("jwt-token", context, storage);
  saveAuthSession(null, context, storage);
  assert.deepEqual(readAuthSession(storage), { token: null, context: null });

  saveAuthSession("jwt-token", context, storage);
  saveAuthSession("jwt-token", null, storage);
  assert.deepEqual(readAuthSession(storage), { token: null, context: null });
});

test("login page and user menu escape unsafe text", () => {
  const container = { innerHTML: "" };
  renderLoginPage(container, {
    tenantSlug: "<default>",
    email: "admin@example.com",
    error: "<script>alert(1)</script>",
    requestId: "req_login",
  });

  assert.doesNotMatch(container.innerHTML, /<script>/);
  assert.match(container.innerHTML, /&lt;script&gt;/);
  assert.match(container.innerHTML, /&lt;default&gt;/);

  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: {
      user: {
        display_name: "<Admin>",
        email: "admin@metahub.local",
        role: "admin",
        tenant_id: "tenant_1",
      },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-07T18:00:00Z" },
    },
  });

  assert.doesNotMatch(container.innerHTML, /<Admin>/);
  assert.match(container.innerHTML, /&lt;Admin&gt;/);
  assert.match(container.innerHTML, /logout-button/);
});

/**
 * 说明该脚本函数或测试用例的职责。
 *
 * 修改时需保持可追踪 request_id、权限边界和敏感信息不泄露的覆盖。
 */
function createMemoryStorage() {
  const values = new Map();
  return {
    getItem(key) {
      // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}
