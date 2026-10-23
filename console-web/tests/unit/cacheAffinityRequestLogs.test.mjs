// 本单测覆盖 PAGE-073 亲和选择展示，确保预计命中与上游实报用量分离且运行时标识不进入 DOM。
import assert from "node:assert/strict";
import test from "node:test";

import { renderRequestLogPanel } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 详情身份和请求字段均为固定测试值，不包含真实 Prompt、Token 或 Provider Key。
const DEVELOPER_CONTEXT = withPermissionSnapshot(
  { user: { role: "developer" } },
  [PERMISSIONS.REQUEST_LOG_READ],
);
const VIEWER_CONTEXT = withPermissionSnapshot({ user: { role: "viewer" } }, []);

/** 构造只包含 RequestLog 渲染所需安全字段的最小详情状态。 */
function requestLogState(selections) {
  const log = {
    request_id: "req_cache_log",
    trace_id: "trc_cache_log",
    virtual_key_prefix: "mhv_dev_****",
    endpoint: "/v1/chat/completions",
    status: "success",
    error_code: null,
    request_model: "company-default",
    resolved_model: "deepseek-chat",
    provider_code: "deepseek",
    policy_snapshot_status: "disabled",
    fallback_triggered: false,
    routing_attempt_count: 1,
    latency_ms: 28,
    estimated_cost_amount: "0.001",
    currency: "CNY",
    created_at: "2026-07-20T10:00:00Z",
    updated_at: "2026-07-20T10:00:01Z",
  };
  return {
    state: "ok",
    request_logs: [],
    total: 0,
    limit: 20,
    offset: 0,
    filters: { limit: 20, offset: 0 },
    detail: {
      request_log: {
        log,
        routing_decision: null,
        routing_candidates: [],
        routing_attempts: [],
        cache_affinity_selections: selections,
        budget_evaluations: [],
        dlp_matches: [],
        usage: [{
          input_tokens: 1200,
          output_tokens: 20,
          total_tokens: 1220,
          cache_tokens: 900,
          cost_amount: "0.001",
          cost_cny: "0.001",
          cost_usd: "0",
          currency: "CNY",
          price_schema_version: "v1",
          price_snapshot_id: null,
          estimated: false,
        }],
        error_diagnosis: null,
      },
      request_id: "req_cache_detail",
      trace_id: "trc_cache_detail",
    },
    detailError: null,
  };
}

test("request log renders safe affinity selections and distinguishes reported cache tokens", () => {
  const html = renderRequestLogPanel(requestLogState([
    {
      cache_affinity_selection_id: `cas_${"1".repeat(32)}`,
      cache_affinity_id: `caf_${"2".repeat(32)}`,
      cache_affinity_version: 3,
      attempt_sequence: 1,
      selection_reason: "binding_warm_reused",
      estimated_hit: true,
      endpoint_preference_applied: true,
      target_kind: "managed",
      model_id: "model_deepseek",
      provider_id: "provider_deepseek",
      provider_code: "deepseek",
      created_at: "2026-07-20T10:00:00Z",
      cache_affinity_binding_id_snapshot: "binding_secret",
      provider_credential_id_snapshot: "credential_secret",
      endpoint_revision_digest_snapshot: "revision_digest_secret",
      affinity_scope_digest: "scope_digest_secret",
      data_class: "classified_secret",
      key_prefix: "key_prefix_secret",
    },
    {
      cache_affinity_version: 3,
      attempt_sequence: 2,
      selection_reason: "binding_rebound",
      estimated_hit: false,
      endpoint_preference_applied: false,
      target_kind: "environment",
      model_id: "<script>fallback</script>",
      provider_code: "<img src=x>",
      created_at: "2026-07-20T10:00:01Z",
    },
  ]), DEVELOPER_CONTEXT);

  assert.match(html, /Prompt Cache 亲和/);
  assert.match(html, /预计复用绑定/);
  assert.match(html, /重新绑定/);
  assert.match(html, /配置版本[\s\S]*v3/);
  assert.match(html, /托管目标|环境目标/);
  assert.match(html, /上游实报缓存 Token[\s\S]*900/);
  assert.match(html, /&lt;script&gt;fallback&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /<script>fallback<\/script>|<img src=x>/);
  assert.doesNotMatch(
    html,
    /binding_secret|credential_secret|revision_digest_secret|scope_digest_secret|classified_secret|key_prefix_secret/,
  );
  assert.doesNotMatch(html, /cas_1111|caf_2222|provider_deepseek|model_deepseek/);
  assert.match(html, /未识别模型/u);
});

test("request log uses a neutral empty affinity state and viewer receives no DOM", () => {
  const developerHtml = renderRequestLogPanel(requestLogState([]), DEVELOPER_CONTEXT);
  const viewerHtml = renderRequestLogPanel(requestLogState([]), VIEWER_CONTEXT);
  assert.match(developerHtml, /本次请求未记录 Prompt Cache 亲和选择/);
  assert.doesNotMatch(developerHtml, /前缀不足|Prompt 长度|data_class|未启用，因为/);
  assert.equal(viewerHtml, "");
});
