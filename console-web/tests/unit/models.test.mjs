// 本单测覆盖模型目录前端能力，确保价格字段、Provider 关联和权限交互保持稳定。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderModelCatalogPanel } from "../../src/features/models/renderModelCatalogPanel.js";
import { readModelEditValues } from "../../src/features/models/modelEditForm.js";
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
      cache_price_per_1k: 0.0005,
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
    cachePricePer1k: 0.0005,
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
  assert.equal(JSON.parse(calls[1].options.body).cache_price_per_1k, 0.0005);
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
  assert.match(html, /服务：DeepSeek Official/);
  assert.match(html, /模型标识：model\/&lt;bad&gt;/);
  assert.match(html, /data-model-edit-form/);
  assert.match(html, /model-alias-dialog-heading model-edit-dialog-heading/);
  assert.match(html, /model-alias-dialog-kicker">模型目录/);
  assert.match(html, /model-edit-actions model-alias-edit-actions/);
  assert.match(html, /缓存命中价格 \/ 百万 Token/);
  assert.match(html, /data-model-pricing-configured/);
  assert.match(html, /<section class="model-pricing"/);
  assert.match(html, /填写价格或确认暂按 0 保存/);
  assert.match(html, /价格暂按 0 保存/);
  assert.doesNotMatch(html, /<details class="model-pricing"/);
  assert.doesNotMatch(html, /<span>价格说明<\/span>/u);
});

test("model catalog keeps entered prices visible for legacy records without a price version", () => {
  const html = renderModelCatalogPanel(
    {
      state: "ok",
      models: [{
        ...modelsPayload.models[0],
        input_price_per_1k: 0.001,
        output_price_per_1k: 0.002,
        cache_price_per_1k: 0.0005,
        price_schema_version: null,
      }],
    },
    providersState,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE],
    ),
  );

  assert.match(html, /输入 0\.001 \/ 输出 0\.002 \/ 缓存命中 0\.0005 CNY \/ 百万 Token/);
  assert.doesNotMatch(html, /未配置价格/);
});

test("model edit dialog backfills legacy prices without a price version", () => {
  const html = renderModelCatalogPanel(
    {
      state: "ok",
      models: [{
        ...modelsPayload.models[0],
        input_price_per_1k: 1.25,
        output_price_per_1k: 2.5,
        cache_price_per_1k: 0.75,
        price_schema_version: null,
      }],
    },
    providersState,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.MODEL_READ, PERMISSIONS.MODEL_WRITE],
    ),
  );

  assert.match(html, /name="pricingEnabled" checked/);
  assert.match(html, /name="inputPricePerMillion"[^>]*value="1\.25"/);
  assert.match(html, /name="outputPricePerMillion"[^>]*value="2\.5"/);
  assert.match(html, /name="cachePricePerMillion"[^>]*value="0\.75"/);
  assert.doesNotMatch(html, /name="inputPricePerMillion"[^>]*value=""/);
});

test("model edit values keep unknown prices absent and validate configured prices", () => {
  const nameOnly = new FormData();
  nameOnly.set("displayName", "模型别名");
  nameOnly.set("providerModelId", "upstream-model");
  nameOnly.set("description", "");
  assert.deepEqual(readModelEditValues(nameOnly), {
    displayName: "模型别名",
    providerModelId: "upstream-model",
    description: null,
  });

  const priced = new FormData();
  priced.set("displayName", "模型别名");
  priced.set("providerModelId", "upstream-model");
  priced.set("pricingEnabled", "on");
  priced.set("inputPricePerMillion", "0");
  priced.set("outputPricePerMillion", "2");
  priced.set("cachePricePerMillion", "0.5");
  priced.set("priceMultiplier", "1.5");
  priced.set("currency", "CNY");
  const values = readModelEditValues(priced, new Date("2026-10-01T00:00:00.000Z"));
  assert.equal(values.inputPricePer1k, 0);
  assert.equal(values.outputPricePer1k, 3);
  assert.equal(values.cachePricePer1k, 0.75);
  assert.equal(values.priceSchemaVersion, "manual-2026-10-01T00:00:00.000Z");
  assert.throws(() => {
    priced.set("outputPricePerMillion", "");
    readModelEditValues(priced);
  }, /有效的输入、输出和缓存命中价格/);
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
