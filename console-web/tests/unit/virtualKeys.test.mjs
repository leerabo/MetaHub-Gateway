// 本单测覆盖 Virtual Key 前端能力，确保创建后一次性密钥展示、筛选和禁用流程稳定。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderVirtualKeyPanel } from "../../src/features/virtual-keys/renderVirtualKeyPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createVirtualKey,
  disableVirtualKey,
  listVirtualKeys,
  updateVirtualKey,
  updateVirtualKeyLimits,
} from "../../src/lib/virtualKeys.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const virtualKeysPayload = {
  virtual_keys: [
    {
      virtual_key_id: "vk_1",
      tenant_id: "tenant_1",
      name: "Dev Key",
      key_prefix: "mhv_dev_abcd",
      environment: "dev",
      description: "local",
      project_id: "prj_1",
      project_name: "账务核心",
      application_id: "app_1",
      application_name: "账务 API",
      status: "active",
      last_used_at: "2026-07-08T10:00:00Z",
      created_at: "2026-07-08T09:00:00Z",
      updated_at: "2026-07-08T09:00:00Z",
      rate_limit_rule: {
        rate_limit_rule_id: "rlr_1",
        rpm_limit: 60,
        tpm_limit: 1200,
        concurrency_limit: 3,
        status: "active",
        updated_at: "2026-07-08T09:30:00Z",
      },
    },
  ],
  request_id: "req_vk",
  trace_id: "trc_vk",
};

test("virtual key client lists creates updates and disables with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_vk"]]),
      async json() {
        if (options.method === "POST") {
          return {
            virtual_key: virtualKeysPayload.virtual_keys[0],
            plain_value: "mhv_dev_secret_once",
            request_id: "req_create_vk",
          };
        }
        if (options.method === "PATCH" && String(url).endsWith("/limits")) {
          return {
            virtual_key: {
              ...virtualKeysPayload.virtual_keys[0],
              rate_limit_rule: {
                rate_limit_rule_id: "rlr_1",
                rpm_limit: 2,
                tpm_limit: null,
                concurrency_limit: 1,
                status: "active",
                updated_at: "2026-07-08T09:31:00Z",
              },
            },
            request_id: "req_patch_limits",
          };
        }
        if (options.method === "PATCH") {
          return {
            virtual_key: { ...virtualKeysPayload.virtual_keys[0], status: "disabled" },
            request_id: "req_patch_vk",
          };
        }
        if (options.method === "DELETE") {
          return {
            virtual_key: { ...virtualKeysPayload.virtual_keys[0], status: "disabled" },
            request_id: "req_delete_vk",
          };
        }
        // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        return virtualKeysPayload;
      },
    };
  };

  const listed = await listVirtualKeys({ controlApiUrl: "http://control.local", token: "jwt-token" });
  const created = await createVirtualKey({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    name: "Dev Key",
    environment: "dev",
    description: "local",
    projectId: "prj_1",
    applicationId: "app_1",
  });
  const updated = await updateVirtualKey({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_1",
    status: "disabled",
  });
  const disabled = await disableVirtualKey({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_1",
  });
  const limited = await updateVirtualKeyLimits({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_1",
    rpmLimit: "2",
    tpmLimit: "",
    concurrencyLimit: "1",
    status: "active",
  });

  assert.equal(listed.state, "ok");
  assert.equal(created.plain_value, "mhv_dev_secret_once");
  assert.equal(updated.virtual_key.status, "disabled");
  assert.equal(disabled.virtual_key.status, "disabled");
  assert.equal(limited.virtual_key.rate_limit_rule.rpm_limit, 2);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "POST");
  assert.equal(JSON.parse(calls[1].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[1].options.body).project_id, "prj_1");
  assert.equal(JSON.parse(calls[1].options.body).application_id, "app_1");
  assert.equal(JSON.parse(calls[2].options.body).status, "disabled");
  assert.equal(calls[3].options.method, "DELETE");
  assert.equal(calls[4].options.method, "PATCH");
  assert.match(calls[4].url, /\/virtual-keys\/vk_1\/limits$/);
  assert.deepEqual(JSON.parse(calls[4].options.body), {
    rpm_limit: 2,
    tpm_limit: null,
    concurrency_limit: 1,
    status: "active",
  });
});

test("virtual key client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_vk_denied",
        trace_id: "trc_vk_denied",
      };
    },
  });

  await assert.rejects(
    () => listVirtualKeys({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_vk_denied");
      return true;
    },
  );
});

test("renderVirtualKeyPanel displays developer controls and one-time secret", () => {
  const html = renderVirtualKeyPanel(
    {
      state: "ok",
      ...virtualKeysPayload,
      created: { plainValue: "mhv_dev_secret_once" },
      virtual_keys: [
        {
          ...virtualKeysPayload.virtual_keys[0],
          name: "<Dev Key>",
        },
      ],
    },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE],
    ),
    {
      state: "ok",
      projects: [{ project_id: "prj_1", project_code: "billing", name: "账务核心", status: "active" }],
      applications: [{ application_id: "app_1", name: "账务 API", project_name: "账务核心", status: "active" }],
    },
  );

  assert.match(html, /create-virtual-key-form/);
  assert.match(html, /data-virtual-key-form/);
  assert.match(html, /data-virtual-key-limits-form/);
  assert.match(html, /保存限制/);
  assert.match(html, /name="projectId"/);
  assert.match(html, /账务核心/);
  assert.match(html, /账务 API/);
  assert.match(html, /value="60"/);
  assert.match(html, /mhv_dev_secret_once/);
  assert.doesNotMatch(html, /<Dev Key>/);
  assert.match(html, /&lt;Dev Key&gt;/);
});

test("renderVirtualKeyPanel hides controls for viewers", () => {
  const html = renderVirtualKeyPanel(
    { state: "ok", ...virtualKeysPayload },
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  );

  assert.equal(html, "");
});

test("renderDeveloperStart renders virtual keys for developer but not viewer", () => {
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
    }, [PERMISSIONS.VIRTUAL_KEY_READ, PERMISSIONS.VIRTUAL_KEY_WRITE]),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "virtual-keys",
        navigation_items: [{
          id: "virtual-keys",
          label: "Virtual Key",
          group: "开发接入",
          target_section: "virtual-keys",
          order: 40,
        }],
      },
    },
    virtualKeysState: { state: "ok", ...virtualKeysPayload },
  });

  const viewerContainer = { innerHTML: "" };
  renderDeveloperStart(viewerContainer, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: {
        display_name: "Viewer",
        email: "viewer@example.com",
        role: "viewer",
        tenant_id: "tenant_1",
      },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-08T18:00:00Z" },
    }, []),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "virtual-keys",
        navigation_items: [{
          id: "virtual-keys",
          label: "Virtual Key",
          group: "开发接入",
          target_section: "virtual-keys",
          order: 40,
        }],
      },
    },
    virtualKeysState: { state: "ok", ...virtualKeysPayload },
  });

  assert.match(developerContainer.innerHTML, /virtual-keys-panel/);
  assert.doesNotMatch(viewerContainer.innerHTML, /virtual-keys-panel/);
});
