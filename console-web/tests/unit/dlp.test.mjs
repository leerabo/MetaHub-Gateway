// 本单测覆盖 PRD-024 DLP 前端能力，确保规则 API、admin 面板、导航接入和日志详情脱敏展示符合安全边界。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderDlpPanel } from "../../src/features/dlp/renderDlpPanel.js";
import { renderRequestLogPanel } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createDlpRule, listDlpRules, updateDlpRule } from "../../src/lib/dlp.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造 DLP 读写权限快照，角色名仅作为展示信息。 */
function dlpManagerContext(baseContext = { user: { role: "security_admin" } }) {
  return withPermissionSnapshot(baseContext, [PERMISSIONS.DLP_READ, PERMISSIONS.DLP_WRITE]);
}

const dlpRulesPayload = {
  dlp_rules: [
    {
      dlp_rule_id: "dlp_1",
      tenant_id: "tenant_1",
      name: "高风险关键词",
      rule_type: "keyword",
      pattern: "敏感词",
      pattern_hash: "hash_rule",
      action: "log_only",
      scan_scope: "input",
      status: "active",
      priority: 100,
      description: "先试运行观察误拦截",
      created_at: "2026-07-10T08:00:00Z",
      updated_at: "2026-07-10T08:00:00Z",
    },
  ],
  request_id: "req_dlp_rules",
  trace_id: "trc_dlp_rules",
};

const dlpLogDetailPayload = {
  request_log: {
    log: {
      request_id: "req_dlp_blocked",
      trace_id: "trc_dlp_blocked",
      tenant_id: "tenant_1",
      virtual_key_id: "vk_1",
      virtual_key_prefix: "mhv_dev_****",
      endpoint: "/v1/chat/completions",
      request_model: "company-default",
      resolved_model: "deepseek-chat",
      provider_id: "provider_1",
      provider_code: "deepseek",
      status: "failed",
      error_code: "gateway.dlp_blocked",
      latency_ms: 3,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      estimated_cost_amount: 0,
      currency: "CNY",
      dlp_status: "blocked",
      dlp_rule_id: "dlp_1",
      dlp_action: "block",
      dlp_match_count: 1,
      dlp_reason_code: "dlp.keyword_matched",
      created_at: "2026-07-10T08:00:00Z",
      updated_at: "2026-07-10T08:00:00Z",
    },
    routing_decision: null,
    usage: [],
    budget_evaluations: [],
    dlp_matches: [
      {
        dlp_match_id: "dm_1",
        dlp_rule_id: "dlp_1",
        rule_name: "高风险关键词",
        rule_type: "keyword",
        action: "block",
        message_index: 0,
        match_text_hash: "hash_only_no_plaintext",
        match_length: 3,
        created_at: "2026-07-10T08:00:00Z",
      },
    ],
    error_diagnosis: null,
  },
  request_id: "req_detail",
  trace_id: "trc_detail",
};

test("dlp client sends safe query create and patch payloads", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_dlp"]]),
      async json() {
        return dlpRulesPayload;
      },
    };
  };

  const listed = await listDlpRules({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    status: "all",
    ruleType: "keyword",
    action: "log_only",
    scanScope: "both",
  });
  await createDlpRule({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    name: "高风险关键词",
    ruleType: "keyword",
    pattern: "敏感词",
    action: "log_only",
    scanScope: "output",
    status: "active",
    priority: 100,
    description: "",
  });
  await updateDlpRule({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    ruleId: "dlp_1",
    action: "block",
    scanScope: "both",
    status: "active",
    priority: 90,
  });

  assert.equal(listed.state, "ok");
  assert.match(calls[0].url, /status=all/);
  assert.match(calls[0].url, /rule_type=keyword/);
  assert.match(calls[0].url, /action=log_only/);
  assert.match(calls[0].url, /scan_scope=both/);
  assert.doesNotMatch(calls[0].url, /tenant_id=/);
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    name: "高风险关键词",
    rule_type: "keyword",
    pattern: "敏感词",
    action: "log_only",
    scan_scope: "output",
    status: "active",
    priority: 100,
  });
  assert.equal(calls[2].options.method, "PATCH");
  assert.match(calls[2].url, /\/dlp\/rules\/dlp_1$/);
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    action: "block",
    scan_scope: "both",
    status: "active",
    priority: 90,
  });
});

test("dlp client maps unified errors without exposing pattern", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "dlp.regex_unsafe",
        message: "Regex is unsafe.",
        request_id: "req_regex",
        trace_id: "trc_regex",
      };
    },
  });

  await assert.rejects(
    () => createDlpRule({ controlApiUrl: "http://control.local", token: "jwt-token", pattern: "(a+)+" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.code, "dlp.regex_unsafe");
      assert.equal(error.requestId, "req_regex");
      assert.doesNotMatch(error.message, /\(a\+\)\+/);
      return true;
    },
  );
});

test("renderDlpPanel shows admin form rules and escaped pattern", () => {
  const html = renderDlpPanel(
    {
      state: "ok",
      rules: {
        dlp_rules: [
          {
            ...dlpRulesPayload.dlp_rules[0],
            name: "高风险 <规则>",
            pattern: "<unsafe-pattern>",
            scan_scope: "output",
          },
        ],
      },
      feedback: "DLP 规则已保存",
      scanScopeFilter: "output",
    },
    dlpManagerContext(),
  );

  assert.match(html, /dlp-rule-form/);
  assert.match(html, /高风险 &lt;规则&gt;/);
  assert.match(html, /&lt;unsafe-pattern&gt;/);
  assert.match(html, /<option value="input" selected>输入<\/option>/);
  assert.match(html, /<option value="output" selected>输出<\/option>/);
  assert.match(html, /id="dlp-scan-scope-filter"/);
  assert.match(html, /dlp-scope-output[^>]*>输出<\/mark>/);
  assert.match(html, /试运行记录/);
  assert.match(html, /DLP 规则已保存/);
  assert.doesNotMatch(html, /tenant_id/);
});

test("renderDlpPanel is hidden for developer and viewer", () => {
  const developerHtml = renderDlpPanel(
    { state: "ok", rules: dlpRulesPayload },
    withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );
  const viewerHtml = renderDlpPanel(
    { state: "ok", rules: dlpRulesPayload },
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
  );

  assert.equal(developerHtml, "");
  assert.equal(viewerHtml, "");
});

test("renderDlpPanel shows scope but no mutations for read-only permission", () => {
  const html = renderDlpPanel(
    { state: "ok", rules: dlpRulesPayload, scanScopeFilter: "input" },
    withPermissionSnapshot({ user: { role: "security_auditor" } }, [PERMISSIONS.DLP_READ]),
  );

  assert.match(html, /dlp-scope-input[^>]*>输入<\/mark>/);
  assert.match(html, /id="dlp-scan-scope-filter"/);
  assert.doesNotMatch(html, /id="dlp-rule-form"/);
  assert.doesNotMatch(html, /data-dlp-rule-update/);
});

test("renderDeveloperStart renders dlp active navigation content", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: dlpManagerContext({
      user: { display_name: "Admin", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-10T12:00:00Z" },
    }),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "dlp-rules",
        navigation_items: [
          {
            id: "dlp-rules",
            label: "DLP 规则",
            group: "安全与治理",
            target_section: "dlp-rules",
            description: "DLP",
            empty_state_hint: "Add DLP rule",
            order: 88,
          },
        ],
      },
    },
    dlpState: {
      state: "ok",
      rules: dlpRulesPayload,
    },
  });

  assert.match(container.innerHTML, /dlp-rules-panel/);
  assert.match(container.innerHTML, /高风险关键词/);
});

test("request log detail renders dlp hash only and never plaintext match", () => {
  const html = renderRequestLogPanel(
    {
      state: "ok",
      request_logs: [dlpLogDetailPayload.request_log.log],
      total: 1,
      limit: 20,
      offset: 0,
      detail: dlpLogDetailPayload,
    },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.REQUEST_LOG_READ],
    ),
  );

  assert.match(html, /内容安全检查/);
  assert.match(html, /已阻断|阻断/);
  assert.doesNotMatch(html, /hash_only_no_plaintext/);
  assert.doesNotMatch(html, /dlp.keyword_matched/);
  assert.doesNotMatch(html, /敏感词/);
  assert.doesNotMatch(html, /用户输入明文/);
});
