// 本单测覆盖 PRD-020 内置 API 调试台：前端只提交安全元数据，页面不再接收明文 Virtual Key。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderDebugConsolePanel } from "../../src/features/debug-console/renderDebugConsolePanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { sendDebugChatCompletion } from "../../src/lib/debugConsole.js";

const virtualKeysState = {
  state: "ok",
  virtual_keys: [
    {
      virtual_key_id: "vk_1",
      name: "Dev Key",
      key_prefix: "mhv_dev_abcd",
      environment: "dev",
      status: "active",
    },
    {
      virtual_key_id: "vk_disabled",
      name: "Disabled Key",
      key_prefix: "mhv_dev_disabled",
      environment: "dev",
      status: "disabled",
    },
  ],
};

const modelAliasesState = {
  state: "ok",
  model_aliases: [
    {
      model_alias_id: "alias_1",
      alias: "company-default",
      environment: "dev",
      status: "active",
    },
  ],
};

const debugResult = {
  state: "ok",
  tenant_id: "tenant_1",
  debug_request_id: "dbg_1",
  request_id: "req_debug",
  trace_id: "trc_debug",
  status: "success",
  latency_ms: 42,
  virtual_key_id: "vk_1",
  virtual_key_prefix: "mhv_dev_abcd",
  model_alias: "company-default",
  resolved_model: "default-chat",
  provider_code: "local_openai",
  response_preview: "调试响应 <unsafe>",
  usage: { input_tokens: 3, output_tokens: 5, total_tokens: 8, cache_tokens: 0, estimated: false },
  cost: { amount: "0.000016", currency: "CNY" },
};

const diagnosis = {
  rule_id: "upstream_rate_limited",
  category: "provider_rate_limit",
  reason: "上游 <script>限流</script>",
  suggestion: "稍后重试",
  action: "检查 KeyPool",
  severity: "warning",
  retryable: true,
};

test("debug console client sends only metadata and maps success", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_debug"]]),
      async json() {
        return { ...debugResult };
      },
    };
  };

  const result = await sendDebugChatCompletion({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_1",
    modelAlias: "company-default",
    messages: [{ role: "user", content: "hello" }],
    temperature: 0.2,
    maxTokens: 128,
  });

  const body = JSON.parse(calls[0].options.body);
  assert.equal(calls[0].url, "http://control.local/debug/chat-completions");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.equal(body.virtual_key_id, "vk_1");
  assert.equal(body.model_alias, "company-default");
  assert.equal(body.tenant_id, undefined);
  assert.equal(body.virtual_key, undefined);
  assert.equal(body.provider_key, undefined);
  assert.equal(result.request_id, "req_debug");
});

test("debug console client preserves unified error fields", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "debug.request_invalid",
        message: "Debug request payload is invalid.",
        request_id: "req_invalid",
        trace_id: "trc_invalid",
        suggestion: "Choose an active virtual key.",
        diagnosis,
      };
    },
  });

  await assert.rejects(
    () =>
      sendDebugChatCompletion({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        virtualKeyId: "",
        modelAlias: "",
        messages: [],
      }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "debug.request_invalid");
      assert.equal(error.requestId, "req_invalid");
      assert.equal(error.suggestion, "Choose an active virtual key.");
      assert.equal(error.diagnosis.rule_id, "upstream_rate_limited");
      return true;
    },
  );
});

test("renderDebugConsolePanel hides plain key input and escapes result", () => {
  const html = renderDebugConsolePanel(
    {
      virtualKeyId: "vk_1",
      modelAlias: "company-default",
      messagesText: '[{"role":"user","content":"<private>"}]',
      feedback: "请求完成",
      result: debugResult,
    },
    { virtualKeysState, modelAliasesState },
  );

  assert.match(html, /debug-console-form/);
  assert.match(html, /<option value="vk_1" selected>Dev Key \/ dev<\/option>/);
  assert.match(html, /company-default/);
  assert.match(html, /req_debug/);
  assert.match(html, /data-debug-log-request-id="req_debug"/);
  assert.doesNotMatch(html, /type="password"/);
  assert.doesNotMatch(html, /mhv_prd020_virtual_key_secret/);
  assert.doesNotMatch(html, /调试响应 <unsafe>/);
  assert.match(html, /调试响应 &lt;unsafe&gt;/);
});

test("renderDebugConsolePanel shows escaped diagnosis fields", () => {
  const html = renderDebugConsolePanel(
    {
      error: {
        code: "gateway.upstream_rate_limited",
        message: "Upstream provider is rate limited.",
        requestId: "req_error",
        traceId: "trc_error",
        suggestion: "Retry later.",
        diagnosis,
      },
    },
    { virtualKeysState, modelAliasesState },
  );

  assert.match(html, /错误诊断/);
  assert.match(html, /Upstream provider is rate limited/);
  assert.match(html, /上游 &lt;script&gt;限流&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>限流<\/script>/);
});

test("renderDeveloperStart renders debug console active item", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: {
      user: { display_name: "Dev", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "debug-console",
        navigation_items: [
          {
            id: "debug-console",
            label: "API 调试台",
            group: "开发",
            target_section: "debug-console",
            description: "Debug",
            empty_state_hint: "Debug",
            order: 20,
          },
        ],
      },
    },
    virtualKeysState,
    modelAliasesState,
    gatewayDebugState: { result: debugResult },
  });

  assert.match(container.innerHTML, /API 调试台/);
  assert.match(container.innerHTML, /debug-console-form/);
  assert.doesNotMatch(container.innerHTML, /type="password"/);
});
