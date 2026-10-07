// 本单测覆盖模型别名前端能力，确保创建、更新、权限隐藏和错误反馈符合治理控制台预期。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderModelAliasPanel } from "../../src/features/model-aliases/renderModelAliasPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createModelAlias,
  listModelAliases,
  updateFallbackPolicy,
  updateModelAlias,
} from "../../src/lib/modelAliases.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const aliasPayload = {
  model_aliases: [
    {
      model_alias_id: "alias_1",
      tenant_id: "tenant_1",
      alias: "company-default",
      environment: "dev",
      default_model_id: "model_1",
      default_internal_model_id: "deepseek-chat",
      default_display_name: "DeepSeek Chat",
      default_provider_code: "deepseek",
      default_provider_name: "DeepSeek Official",
      fallback_model_id: "model_2",
      fallback_internal_model_id: "qwen-plus",
      fallback_display_name: "Qwen Plus",
      fallback_provider_code: "qwen",
      fallback_provider_name: "Qwen Cloud",
      fallback_policy: {
        fallback_policy_id: "fbp_1",
        model_alias_id: "alias_1",
        status: "enabled",
        version: 2,
        priority: 100,
        trigger_conditions: ["upstream_timeout", "upstream_5xx"],
        max_attempts: 1,
      },
      routing_policy: {
        routing_policy_id: "rpol_1",
        model_alias_id: "alias_1",
        routing_mode: "fixed",
        status: "enabled",
        version: 5,
        candidates: [],
        warnings: [],
      },
      status: "active",
      description: "default route",
      created_at: "2026-07-08T09:00:00Z",
      updated_at: "2026-07-08T09:30:00Z",
      disabled_at: null,
    },
  ],
  request_id: "req_aliases",
  trace_id: "trc_aliases",
};

const modelsState = {
  state: "ok",
  models: [
    {
      model_id: "model_1",
      internal_model_id: "deepseek-chat",
      provider_code: "deepseek",
      status: "active",
    },
    {
      model_id: "model_2",
      internal_model_id: "qwen-plus",
      provider_code: "qwen",
      status: "active",
    },
  ],
};

test("model aliases client lists creates and updates with bearer token", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_aliases"]]),
      async json() {
        if (options.method === "POST") {
          // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
          return { model_alias: aliasPayload.model_aliases[0], request_id: "req_create_alias" };
        }
        if (options.method === "PATCH" && String(url).endsWith("/fallback")) {
          return {
            model_alias: aliasPayload.model_aliases[0],
            fallback_policy: aliasPayload.model_aliases[0].fallback_policy,
            request_id: "req_patch_fallback",
          };
        }
        if (options.method === "PATCH") {
          return {
            model_alias: { ...aliasPayload.model_aliases[0], status: "disabled" },
            request_id: "req_patch_alias",
          };
        }
        return aliasPayload;
      },
    };
  };

  const listed = await listModelAliases({ controlApiUrl: "http://control.local", token: "jwt-token" });
  const created = await createModelAlias({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    alias: "company-default",
    environment: "dev",
    defaultModelId: "model_1",
    fallbackModelId: "model_2",
    description: "default route",
  });
  const changed = await updateModelAlias({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelAliasId: "alias_1",
    status: "disabled",
    expectedRoutingPolicyVersion: 5,
  });
  const fallbackChanged = await updateFallbackPolicy({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    modelAliasId: "alias_1",
    enabled: true,
    fallbackModelId: "model_2",
    triggerConditions: ["upstream_timeout", "upstream_5xx"],
    expectedRoutingPolicyVersion: 5,
  });

  assert.equal(listed.state, "ok");
  assert.equal(created.model_alias.alias, "company-default");
  assert.equal(changed.model_alias.status, "disabled");
  assert.equal(fallbackChanged.fallback_policy.version, 2);
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(calls[1].options.method, "POST");
  assert.equal(JSON.parse(calls[1].options.body).tenant_id, undefined);
  assert.equal(JSON.parse(calls[1].options.body).default_model_id, "model_1");
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    status: "disabled",
    expected_routing_policy_version: 5,
  });
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    enabled: true,
    fallback_model_id: "model_2",
    trigger_conditions: ["upstream_timeout", "upstream_5xx"],
    expected_routing_policy_version: 5,
  });
});

test("model aliases client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_alias_denied",
        trace_id: "trc_alias_denied",
      };
    },
  });

  await assert.rejects(
    () => listModelAliases({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "auth.permission_denied");
      assert.equal(error.requestId, "req_alias_denied");
      return true;
    },
  );
});

test("renderModelAliasPanel shows admin controls and escapes unsafe values", () => {
  const html = renderModelAliasPanel(
    {
      state: "ok",
      ...aliasPayload,
      model_aliases: [
        {
          ...aliasPayload.model_aliases[0],
          alias: "company-default",
          description: "<route>",
          default_internal_model_id: "deepseek/<bad>",
        },
      ],
    },
    modelsState,
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE],
    ),
  );

  assert.match(html, /create-model-alias-form/);
  assert.match(html, /data-model-alias-dialog/);
  assert.equal((html.match(/<dialog[^>]*data-model-alias-dialog/g) ?? []).length, 1);
  assert.match(html, /data-model-alias-dialog-view="create"[\s\S]*data-model-alias-dialog-view="edit"/u);
  assert.match(html, /data-model-alias-create-open/);
  assert.match(html, /data-model-alias-status-form/);
  assert.match(html, /data-model-alias-edit-form/);
  assert.match(html, /data-model-alias-edit-open="alias_1"/u);
  assert.doesNotMatch(html, /data-fallback-policy-form|fallback-policy-editor/);
  assert.match(html, /model-alias-routing-mode/);
  assert.match(html, /name="routingMode" value="quality_cost"/);
  assert.match(html, /model-alias-fallback-settings/);
  assert.match(html, /备用模型 1 个/);
  assert.doesNotMatch(html, /model-alias-fallback-separator/);
  assert.match(html, /选择方式/);
  assert.match(html, /请求开始时按你设置的顺序选择；请求出错后，可按故障切换尝试下一候选。/);
  assert.match(html, /data-fallback-picker/);
  assert.match(html, /type="checkbox" name="fallbackModelId"/);
  assert.match(html, /data-fallback-drag/);
  assert.doesNotMatch(html, /name="fallbackModelId" multiple/);
  assert.match(html, /当前模型失败后按顺序尝试下一模型，直到成功或全部失败。/);
  assert.match(html, /data-routing-policy-version="5"/);
  assert.match(html, /已启用 · 失败后继续尝试/);
  assert.doesNotMatch(html, /<route>/);
  assert.match(html, /&lt;route&gt;/);
  // The catalog's readable model name wins over a stale alias summary; a
  // technical model id must never leak into the rendered route row.
  assert.match(html, /deepseek-chat/);
  assert.doesNotMatch(html, /deepseek\/&lt;bad&gt;/);
});

test("route candidate snapshots keep readable names and prices before catalog refresh", () => {
  const html = renderModelAliasPanel(
    {
      state: "ok",
      model_aliases: [{
        ...aliasPayload.model_aliases[0],
        default_model_id: "snapshot-default",
        default_internal_model_id: "snapshot-model",
        default_provider_name: "Snapshot Provider",
        routing_policy: {
          ...aliasPayload.model_aliases[0].routing_policy,
          candidates: [{
            candidate_role: "default",
            configured_priority: 0,
            model_id: "snapshot-default",
            internal_model_id: "snapshot-model",
            provider_model_id: "kimi-k2.7-code",
            display_name: null,
            model_status: "active",
            provider_code: "moonshot",
            provider_name: "Kimi",
            provider_status: "active",
            input_price_per_1k: 6.5,
            output_price_per_1k: 27,
            cache_price_per_1k: 1.3,
            currency: "CNY",
          }],
        },
      }],
    },
    { state: "loading", models: [] },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE],
    ),
  );

  assert.match(html, /kimi-k2\.7-code/);
  assert.match(html, /输入 6\.5 \/ 输出 27 \/ 缓存命中 1\.3 CNY \/ 百万 Token/);
  assert.doesNotMatch(html, /<strong>snapshot-default<\/strong>/);
});

test("model alias empty state uses the canonical model catalog route", () => {
  const html = renderModelAliasPanel(
    { state: "ok", model_aliases: [] },
    { state: "ok", models: [] },
    withPermissionSnapshot({ user: { role: "admin" } }, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE, PERMISSIONS.MODEL_READ]),
    { state: "ok", environments: [{ environment_id: "env_1", status: "active" }] },
  );
  assert.match(html, /data-console-nav="models" data-console-route="workspace\.gateway\.models" data-console-target="workspace\.gateway\.models"/u);
});

test("renderModelAliasPanel is read-only for developer and hidden for viewer", () => {
  const developerHtml = renderModelAliasPanel(
    { state: "ok", ...aliasPayload },
    modelsState,
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.ROUTING_READ]),
  );
  const viewerHtml = renderModelAliasPanel(
    { state: "ok", ...aliasPayload },
    modelsState,
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.match(developerHtml, /company-default/);
  assert.doesNotMatch(developerHtml, /create-model-alias-form/);
  assert.doesNotMatch(developerHtml, /data-model-alias-status-form/);
  assert.doesNotMatch(developerHtml, /data-fallback-policy-form/);
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart renders model aliases for admin and developer but not viewer", () => {
  const consoleShellState = {
    activeNavigationItemId: "model-aliases",
    navigation: {
      active_navigation_item_id: "model-aliases",
      navigation_items: [{
        id: "model-aliases",
        label: "模型别名",
        group: "配置",
        target_section: "model-aliases",
        description: "模型别名与回退",
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
    }, [PERMISSIONS.ROUTING_READ, PERMISSIONS.ROUTING_WRITE]),
    consoleShellState,
    modelsState,
    modelAliasesState: { state: "ok", ...aliasPayload },
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
    }, [PERMISSIONS.ROUTING_READ]),
    consoleShellState,
    modelsState,
    modelAliasesState: { state: "ok", ...aliasPayload },
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
    modelsState,
    modelAliasesState: { state: "ok", ...aliasPayload },
  });

  assert.match(adminContainer.innerHTML, /model-aliases-panel/);
  assert.match(developerContainer.innerHTML, /model-aliases-panel/);
  assert.doesNotMatch(viewerContainer.innerHTML, /model-aliases-panel/);
});
