// 本单测覆盖模型价格客户端、有效期时间线、管理员写入和只读角色展示边界。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderModelPricePanel } from "../../src/features/model-prices/renderModelPricePanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createModelPrice, listModelPrices } from "../../src/lib/modelPrices.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const modelsState = {
  state: "ok",
  models: [
    {
      model_id: "model_1",
      internal_model_id: "deepseek-chat",
      display_name: "DeepSeek Chat",
      provider_code: "deepseek",
      status: "active",
    },
  ],
};

const pricePayload = {
  price_plans: [
    {
      price_plan_id: "prp_1",
      tenant_id: "tenant_1",
      model_id: "model_1",
      internal_model_id: "deepseek-chat",
      display_name: "DeepSeek Chat",
      provider_code: "deepseek",
      input_price_per_1k: "0.001",
      output_price_per_1k: "0.002",
      currency: "CNY",
      price_schema_version: "manual-202607",
      effective_from: "2026-07-01T00:00:00Z",
      effective_to: null,
      effective_status: "current",
      source: "manual",
      created_by: "user_1",
      created_by_name: "Admin",
      created_at: "2026-07-01T00:00:00Z",
    },
  ],
  total: 1,
  limit: 100,
  offset: 0,
  as_of: "2026-07-12T00:00:00Z",
  request_id: "req_prices",
  trace_id: "trc_prices",
};

test("model price client sends decimal strings without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map(),
      async json() {
        // POST 返回单条计划，GET 返回价格时间线，避免测试依赖真实控制面。
        return options.method === "POST" ? { price_plan: pricePayload.price_plans[0] } : pricePayload;
      },
    };
  };

  await listModelPrices({
    controlApiUrl: "http://control.local",
    token: "jwt",
    modelId: "model_1",
    effectiveStatus: "current",
  });
  await createModelPrice({
    controlApiUrl: "http://control.local",
    token: "jwt",
    modelId: "model_1",
    inputPricePer1k: "0.001000000001",
    outputPricePer1k: "0.002",
    currency: "CNY",
    effectiveFrom: "2026-07-15T00:00:00.000Z",
    priceSchemaVersion: "manual-20260715",
  });

  assert.match(calls[0].url, /model_id=model_1/);
  assert.match(calls[0].url, /effective_status=current/);
  assert.doesNotMatch(calls[0].url, /tenant/);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt");
  const body = JSON.parse(calls[1].options.body);
  assert.equal(body.input_price_per_1k, "0.001000000001");
  assert.equal(body.effective_from, "2026-07-15T00:00:00.000Z");
  assert.equal(body.tenant_id, undefined);
});

test("model price client preserves unified permission error", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map(),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_price_denied",
        trace_id: "trc_price_denied",
      };
    },
  });

  await assert.rejects(
    () => listModelPrices({ controlApiUrl: "http://control.local", token: "jwt" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_price_denied");
      return true;
    },
  );
});

test("model price panel renders admin timeline and escapes model data", () => {
  const html = renderModelPricePanel(
    {
      state: "ok",
      ...pricePayload,
      price_plans: [
        {
          ...pricePayload.price_plans[0],
          display_name: "<DeepSeek>",
          price_schema_version: "<manual>",
        },
      ],
      filters: { effectiveStatus: "all" },
    },
    modelsState,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.MODEL_PRICE_READ, PERMISSIONS.MODEL_PRICE_WRITE],
    ),
  );

  assert.match(html, /create-model-price-form/);
  assert.match(html, /model-price-filter-form/);
  assert.match(html, /当前生效/);
  assert.match(html, /&lt;manual&gt;/);
  assert.doesNotMatch(html, /<DeepSeek>/);
});

test("model price panel is readonly for developer and hidden for viewer", () => {
  const developerHtml = renderModelPricePanel(
    { state: "ok", ...pricePayload, filters: {} },
    modelsState,
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.MODEL_PRICE_READ]),
  );
  const viewerHtml = renderModelPricePanel(
    { state: "ok", ...pricePayload, filters: {} },
    modelsState,
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.match(developerHtml, /manual-202607/);
  assert.doesNotMatch(developerHtml, /create-model-price-form/);
  assert.equal(viewerHtml, "");
});

test("developer start renders active model price navigation", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot({
      user: { display_name: "Admin", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
      tenant: { name: "Default Tenant" },
      session: { expires_at: "2026-07-12T12:00:00Z" },
    }, [PERMISSIONS.MODEL_PRICE_READ, PERMISSIONS.MODEL_PRICE_WRITE]),
    consoleShellState: {
      navigation: {
        navigation_items: [
          {
            id: "model-prices",
            label: "模型价格",
            group: "调用与成本",
            target_section: "model-prices",
            description: "价格时间线",
            empty_state_hint: "创建价格",
          },
        ],
        active_navigation_item_id: "model-prices",
      },
      activeNavigationItemId: "model-prices",
    },
    modelsState,
    modelPricesState: { state: "ok", ...pricePayload, filters: {} },
  });

  assert.match(container.innerHTML, /model-prices-panel/);
  assert.match(container.innerHTML, /manual-202607/);
});
