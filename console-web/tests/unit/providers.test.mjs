// 本单测覆盖 Provider 与 Provider Key 前端能力，确保密钥只展示脱敏信息且权限交互正确。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createProvider,
  createProviderKey,
  listProviderKeys,
  listProviders,
  testProviderConnection,
  updateProvider,
  updateProviderKey,
} from "../../src/lib/providers.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const providersPayload = {
  providers: [
    {
      provider_id: "provider_1",
      tenant_id: "tenant_1",
      provider_code: "deepseek_official",
      name: "DeepSeek Official",
      provider_type: "deepseek",
      base_url: "https://api.deepseek.com/v1",
      auth_type: "bearer",
      status: "active",
      description: "prod candidate",
      health_status: "unknown",
      last_tested_at: null,
      last_test_error: null,
      created_at: "2026-07-08T09:00:00Z",
      updated_at: "2026-07-08T09:00:00Z",
      disabled_at: null,
    },
  ],
  request_id: "req_provider",
  trace_id: "trc_provider",
};

const providerKeysPayload = {
  provider_keys: [
    {
      provider_key_id: "provider_key_1",
      tenant_id: "tenant_1",
      provider_id: "provider_1",
      name: "Primary Key",
      key_prefix: "sk-provider-",
      key_fingerprint: "fingerprint_1",
      weight: 100,
      status: "active",
      last_used_at: null,
      last_error_at: null,
      last_error_code: null,
      last_error_message: null,
      rate_limited_until: null,
      created_at: "2026-07-08T09:10:00Z",
      updated_at: "2026-07-08T09:10:00Z",
      disabled_at: null,
    },
  ],
  request_id: "req_provider_key",
  trace_id: "trc_provider_key",
};

test("providers client lists creates updates and tests with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_provider"]]),
      async json() {
        // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        if (String(url).endsWith("/keys") && options.method !== "POST") {
          // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
          return providerKeysPayload;
        }
        if (String(url).endsWith("/test")) {
          return {
            provider: { ...providersPayload.providers[0], health_status: "healthy" },
            test_result: {
              status: "success",
              http_status: 200,
              latency_ms: 12,
              message: "Endpoint is reachable.",
              checked_at: "2026-07-08T10:00:00Z",
            },
            request_id: "req_test",
          };
        }
        if (options.method === "POST") {
          // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
          if (String(url).includes("/keys")) {
            // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
            // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
            return { provider_key: providerKeysPayload.provider_keys[0], request_id: "req_create_key" };
          }
          // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
          return { provider: providersPayload.providers[0], request_id: "req_create_provider" };
        }
        if (options.method === "PATCH") {
          // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
          // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
          if (String(url).includes("/provider-keys/")) {
            return {
              provider_key: { ...providerKeysPayload.provider_keys[0], status: "disabled" },
              request_id: "req_patch_key",
            };
          }
          return {
            provider: { ...providersPayload.providers[0], status: "disabled" },
            request_id: "req_patch_provider",
          };
        }
        // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        return providersPayload;
      },
    };
  };

  const listed = await listProviders({ controlApiUrl: "http://control.local", token: "jwt-token" });
  const listedKeys = await listProviderKeys({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerId: "provider_1",
  });
  const created = await createProvider({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerCode: "deepseek_official",
    name: "DeepSeek Official",
    providerType: "deepseek",
    baseUrl: "https://api.deepseek.com/v1",
    authType: "bearer",
    description: "prod candidate",
  });
  const changed = await updateProvider({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerId: "provider_1",
    status: "disabled",
  });
  const createdKey = await createProviderKey({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerId: "provider_1",
    name: "Primary Key",
    plainValue: "sk-new-provider-key",
    weight: 100,
  });
  const changedKey = await updateProviderKey({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerKeyId: "provider_key_1",
    status: "disabled",
  });
  const tested = await testProviderConnection({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    providerId: "provider_1",
    timeoutSeconds: 2,
  });

  assert.equal(listed.state, "ok");
  assert.equal(listedKeys.provider_keys[0].key_prefix, "sk-provider-");
  assert.equal(created.provider.provider_code, "deepseek_official");
  assert.equal(changed.provider.status, "disabled");
  assert.equal(createdKey.provider_key.provider_key_id, "provider_key_1");
  assert.equal(changedKey.provider_key.status, "disabled");
  assert.equal(tested.test_result.status, "success");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[2].options.method, "POST");
  assert.equal(JSON.parse(calls[2].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[2].options.body).provider_code, "deepseek_official");
  assert.deepEqual(JSON.parse(calls[3].options.body), { status: "disabled" });
  assert.equal(JSON.parse(calls[4].options.body).plain_value, "sk-new-provider-key");
  assert.deepEqual(JSON.parse(calls[5].options.body), { status: "disabled" });
  assert.equal(JSON.parse(calls[6].options.body).timeout_seconds, 2);
});

test("providers client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_provider_denied",
        trace_id: "trc_provider_denied",
      };
    },
  });

  await assert.rejects(
    () => listProviders({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_provider_denied");
      return true;
    },
  );
});

test("renderProviderPanel displays admin controls and escapes unsafe values", () => {
  const html = renderProviderPanel(
    {
      state: "ok",
      ...providersPayload,
      providers: [
        {
          ...providersPayload.providers[0],
          name: "<DeepSeek>",
          base_url: "https://example.com/<v1>",
        },
      ],
      providerKeysByProviderId: {
        provider_1: { state: "ok", ...providerKeysPayload },
      },
      lastTestResult: {
        providerCode: "deepseek_official",
        status: "failed",
        message: "<bad>",
        httpStatus: 401,
        latencyMs: 9,
      },
    },
    withPermissionSnapshot(
      { user: { role: "ops_admin" } },
      [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE],
    ),
  );

  assert.match(html, /create-provider-form/);
  assert.match(html, /data-provider-key-create-form/);
  assert.match(html, /data-provider-key-status-form/);
  assert.match(html, /KeyPool/);
  assert.match(html, /data-provider-test-form/);
  assert.match(html, /data-provider-status-form/);
  assert.doesNotMatch(html, /sk-new-provider-key/);
  assert.doesNotMatch(html, /<DeepSeek>/);
  assert.match(html, /&lt;DeepSeek&gt;/);
  assert.match(html, /&lt;bad&gt;/);
});

test("renderProviderPanel hides provider management for non-admin roles", () => {
  const developerHtml = renderProviderPanel(
    { state: "ok", ...providersPayload },
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );
  const viewerHtml = renderProviderPanel(
    { state: "ok", ...providersPayload },
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.equal(developerHtml, "");
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart renders providers for admin but not developer", () => {
  const consoleShellState = {
    activeNavigationItemId: "providers",
    navigation: {
      active_navigation_item_id: "providers",
      navigation_items: [{
        id: "providers",
        label: "Provider",
        group: "配置",
        target_section: "providers",
        description: "Provider 与 KeyPool",
      }],
    },
  };
  const adminContainer = { innerHTML: "" };
  renderDeveloperStart(adminContainer, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: {
        display_name: "Admin User",
        email: "admin@example.com",
        role: "admin",
        tenant_id: "tenant_1",
      },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-08T18:00:00Z" },
    }, [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE]),
    consoleShellState,
    providersState: { state: "ok", ...providersPayload },
  });

  const developerContainer = { innerHTML: "" };
  renderDeveloperStart(developerContainer, {
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
      session: { expires_at: "2026-07-08T18:00:00Z" },
    }, [PERMISSIONS.CONSOLE_BASIC_READ]),
    consoleShellState,
    providersState: { state: "ok", ...providersPayload },
  });

  assert.match(adminContainer.innerHTML, /providers-panel/);
  assert.doesNotMatch(developerContainer.innerHTML, /providers-panel/);
});
