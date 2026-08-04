// 本单测覆盖模型目录前端能力，确保价格字段、Provider 关联和权限交互保持稳定。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderModelCatalogPanel } from "../../src/features/models/renderModelCatalogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createModel, listModels, updateModel } from "../../src/lib/models.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const modelsPayload = {
  models: [
    {
      model_id: "model_1",
      tenant_id: "tenant_1",
      internal_model_id: "deepseek-chat",
      provider_id: "provider_1",
      provider_code: "deepseek",
      provider_name: "DeepSeek Official",
      provider_model_id: "deepseek-chat-upstream",
      display_name: "DeepSeek Chat",
      capabilities: { chat: true, stream: true, embeddings: false },
      context_window: 64000,
      input_price_per_1k: 0.001,
      output_price_per_1k: 0.002,
      currency: "CNY",
      price_schema_version: "manual-202607",
      status: "active",
      description: "prod candidate",
      created_at: "2026-07-08T09:00:00Z",
      updated_at: "2026-07-08T09:00:00Z",
      disabled_at: null,
    },
  ],
  request_id: "req_models",
  trace_id: "trc_models",
};

const providersState = {
  state: "ok",
  providers: [
    {
      provider_id: "provider_1",
      provider_code: "deepseek",
      name: "DeepSeek Official",
      status: "active",
    },
  ],
};

test("models client lists creates and updates with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_models"]]),
      async json() {
        if (options.method === "POST") {
          // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
          return { model: modelsPayload.models[0], request_id: "req_create_model" };
        }
        if (options.method === "PATCH") {
          return {
            model: { ...modelsPayload.models[0], status: "disabled" },
            request_id: "req_patch_model",
          };
        }
        // 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        return modelsPayload;
      },
    };
  };

  const listed = await listModels({ controlApiUrl: "http://control.local", token: "jwt-token" });
  const created = await createModel({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    internalModelId: "deepseek-chat",
    providerId: "provider_1",
    providerModelId: "deepseek-chat-upstream",
    displayName: "DeepSeek Chat",
    capabilities: { chat: true, stream: true },
    contextWindow: 64000,
    inputPricePer1k: 0.001,
    outputPricePer1k: 0.002,
    currency: "CNY",
    priceSchemaVersion: "manual-202607",
    description: "prod candidate",
  });
  const changed = await updateModel({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelId: "model_1",
    status: "disabled",
  });

  assert.equal(listed.state, "ok");
  assert.equal(created.model.internal_model_id, "deepseek-chat");
  assert.equal(changed.model.status, "disabled");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "POST");
  assert.equal(JSON.parse(calls[1].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[1].options.body).provider_model_id, "deepseek-chat-upstream");
  assert.deepEqual(JSON.parse(calls[2].options.body), { status: "disabled" });
});

test("models client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_models_denied",
        trace_id: "trc_models_denied",
      };
    },
  });

  await assert.rejects(
    () => listModels({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_models_denied");
      return true;
    },
  );
});

test("renderModelCatalogPanel shows admin controls and escapes unsafe values", () => {
  const html = renderModelCatalogPanel(
    {
      state: "ok",
      ...modelsPayload,
      models: [
        {
          ...modelsPayload.models[0],
          display_name: "<DeepSeek>",
          provider_model_id: "model/<bad>",
        },
      ],
    },
    providersState,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE],
    ),
  );

  assert.match(html, /create-model-form/);
  assert.match(html, /data-model-status-form/);
  assert.doesNotMatch(html, /<DeepSeek>/);
  assert.match(html, /&lt;DeepSeek&gt;/);
  assert.match(html, /model\/&lt;bad&gt;/);
});

test("renderModelCatalogPanel is read-only for developer and hidden for viewer", () => {
  const developerHtml = renderModelCatalogPanel(
    { state: "ok", ...modelsPayload },
    providersState,
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.MODEL_READ]),
  );
  const viewerHtml = renderModelCatalogPanel(
    { state: "ok", ...modelsPayload },
    providersState,
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.match(developerHtml, /deepseek-chat/);
  assert.doesNotMatch(developerHtml, /create-model-form/);
  assert.doesNotMatch(developerHtml, /data-model-status-form/);
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart renders model catalog for admin and developer but not viewer", () => {
  const consoleShellState = {
    activeNavigationItemId: "models",
    navigation: {
      active_navigation_item_id: "models",
      navigation_items: [{
        id: "models",
        label: "模型目录",
        group: "配置",
        target_section: "models",
        description: "租户模型目录",
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
    }, [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE]),
    consoleShellState,
    modelsState: { state: "ok", ...modelsPayload },
    providersState,
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
    }, [PERMISSIONS.MODEL_READ]),
    consoleShellState,
    modelsState: { state: "ok", ...modelsPayload },
    providersState: null,
  });

  const viewerContainer = { innerHTML: "" };
  renderDeveloperStart(viewerContainer, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: {
        display_name: "Viewer User",
        email: "viewer@example.com",
        role: "viewer",
        tenant_id: "tenant_1",
      },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-08T18:00:00Z" },
    }, [PERMISSIONS.CONSOLE_BASIC_READ]),
    consoleShellState,
    modelsState: { state: "ok", ...modelsPayload },
    providersState: null,
  });

  assert.match(adminContainer.innerHTML, /models-panel/);
  assert.match(developerContainer.innerHTML, /models-panel/);
  assert.doesNotMatch(viewerContainer.innerHTML, /models-panel/);
});
