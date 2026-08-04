// 本单测覆盖调用日志前端能力，确保筛选、详情、路由决策和用量展示符合审计需求。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createRequestLogPageMemory,
  renderRequestLogPanel,
  requestLogDateTimeLocalValue,
  requestLogFiltersFromSavedFilter,
  requestLogIdentitySnapshot,
} from "../../src/features/request-logs/renderRequestLogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createSavedRequestLogFilter,
  deleteSavedRequestLogFilter,
  getRequestLog,
  listRequestLogs,
  listSavedRequestLogFilters,
  updateSavedRequestLogFilter,
} from "../../src/lib/requestLogs.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const requestLogPayload = {
  request_logs: [
    {
      request_id: "req_prd016_success",
      trace_id: "trc_prd016_success",
      tenant_id: "tenant_1",
      virtual_key_id: "vk_1",
      virtual_key_prefix: "mhv_dev_****",
      project_id: "prj_1",
      project_name: "账务核心",
      application_id: "app_1",
      application_name: "账务 API",
      endpoint: "/v1/chat/completions",
      request_model: "company-default",
      resolved_model: "deepseek-chat",
      provider_id: "provider_1",
      provider_code: "deepseek",
      snapshot_version: 3,
      policy_snapshot_status: "current",
      policy_decision: "route_to",
      policy_reason_code: "policy.route_to.matched",
      policy_id: "pol_route_1",
      policy_action_type: "route_to",
      policy_target_model_id: "model_target_1",
      status: "success",
      error_code: null,
      latency_ms: 42,
      input_tokens: 11,
      output_tokens: 7,
      total_tokens: 18,
      estimated_cost_amount: 0.123456,
      currency: "CNY",
      price_schema_version: "test-v1",
      created_at: "2026-07-08T09:00:00Z",
      updated_at: "2026-07-08T09:00:01Z",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
  request_id: "req_logs",
  trace_id: "trc_logs",
};

const detailPayload = {
  request_log: {
    log: {
      ...requestLogPayload.request_logs[0],
      budget_status: "exhausted",
      budget_rule_id: "br_budget_1",
      budget_scope_type: "virtual_key",
      budget_scope_id: "vk_1",
      budget_action: "block",
      budget_reason_code: "budget.exhausted.block",
      budget_current_cost_cny: 12.5,
      budget_limit_cny: 10,
    },
    routing_decision: {
      decision_status: "selected",
      decision_reason: "routing.cost_lowest",
      requested_model: "company-default",
      environment: "dev",
      selected_internal_model_id: "deepseek-chat",
      provider_code: "deepseek",
      routing_policy_id: "rpol_1",
      routing_policy_version: 4,
      routing_mode: "cost_first",
      price_as_of: "2026-07-18T09:00:00Z",
      estimated_input_tokens: 11,
      estimated_output_tokens: 7,
      token_estimate_truncated: false,
      routing_policy_event_id: "rpe_1",
      error_code: null,
      candidate_summary: { unsafe_raw_value: "CANDIDATE_SUMMARY_MUST_NOT_RENDER" },
      created_at: "2026-07-08T09:00:00Z",
    },
    routing_candidates: [
      {
        routing_candidate_id: "rcand_1",
        rank: 1,
        candidate_role: "fallback",
        model_id: "model_1",
        internal_model_id: "deepseek-chat",
        provider_id: "provider_1",
        provider_code: "deepseek",
        eligible: true,
        status: "selected",
        health_state: "healthy",
        health_fresh: true,
        keypool_state: "available",
        estimated_cost_amount: "0.000000000000000",
        currency: "CNY",
        reason_code: "routing.cost_lowest",
      },
      {
        routing_candidate_id: "rcand_2",
        rank: 2,
        candidate_role: "default",
        model_id: "model_2",
        internal_model_id: "qwen-plus",
        provider_id: "provider_2",
        provider_code: "qwen",
        eligible: true,
        status: "standby",
        health_state: "unknown",
        health_fresh: false,
        keypool_state: "unknown",
        estimated_cost_amount: null,
        currency: null,
        price_source: "not_evaluated",
        reason_code: "routing.cost_fixed_fallback.price_missing",
      },
    ],
    usage: [
      {
        input_tokens: 11,
        output_tokens: 7,
        total_tokens: 18,
        cost_amount: 0.123456,
        currency: "CNY",
        price_schema_version: "test-v1",
        normalized_usage: { input_tokens: 11, output_tokens: 7, total_tokens: 18 },
        created_at: "2026-07-08T09:00:01Z",
      },
    ],
    routing_attempts: [
      {
        routing_attempt_id: "rta_1",
        attempt_sequence: 1,
        attempt_role: "primary",
        trigger_type: "initial",
        trigger_error_code: null,
        model_id: "model_1",
        resolved_model: "deepseek-chat",
        provider_id: "provider_1",
        provider_code: "deepseek",
        status: "failed",
        error_code: "gateway.upstream_error",
        error_category: "upstream_5xx",
        upstream_http_status: 503,
        retryable: true,
        latency_ms: 42,
        usage_status: "unavailable",
        usage_ledger_id: null,
        estimated_cost_cny: 0.01,
      },
      {
        routing_attempt_id: "rta_2",
        attempt_sequence: 2,
        attempt_role: "fallback",
        trigger_type: "fallback",
        trigger_error_code: "gateway.upstream_error",
        model_id: "model_2",
        resolved_model: "qwen-plus",
        provider_id: "provider_2",
        provider_code: "qwen",
        status: "succeeded",
        error_code: null,
        error_category: null,
        upstream_http_status: null,
        retryable: false,
        latency_ms: 35,
        usage_status: "recorded",
        usage_ledger_id: "usg_1",
        estimated_cost_cny: 0.02,
      },
    ],
    budget_evaluations: [
      {
        budget_rule_id: "br_budget_1",
        scope_type: "virtual_key",
        scope_id: "vk_1",
        budget_date: "2026-07-09",
        spent_before_cny: 9.5,
        estimated_request_cost_cny: 3,
        projected_cost_cny: 12.5,
        budget_cny: 10,
        threshold_percent: 100,
        budget_status: "exhausted",
        action_taken: "block",
        reason_code: "budget.exhausted.block",
        created_at: "2026-07-09T08:00:00Z",
      },
    ],
    error_diagnosis: {
      rule_id: "upstream_rate_limited",
      category: "provider_rate_limit",
      reason: "上游 <script>限流</script>",
      suggestion: "稍后重试",
      action: "检查 Provider KeyPool",
      severity: "warning",
      retryable: true,
    },
  },
  request_id: "req_detail",
  trace_id: "trc_detail",
};

/** 构造包含完整 tenant/user/session 与后端权限 revision 的 RequestLog owner 测试身份。 */
function requestLogAuthContext(
  tenantId,
  userId,
  sessionId,
  revision = 1,
  permissionCodes = [PERMISSIONS.REQUEST_LOG_READ],
) {
  // 日志读取权限由用例精确传入，Developer 名称不能作为缺失快照的回退来源。
  return withPermissionSnapshot({
    user: { tenant_id: tenantId, user_id: userId, role: "developer" },
    tenant: { tenant_id: tenantId },
    session: { session_id: sessionId, session_revision: revision },
  }, permissionCodes, { permissionRevision: revision });
}

/** 把认证上下文转换为生命周期回调使用的完整 owner。 */
function requestLogOwner(authContext, refreshId) {
  return {
    identity: requestLogIdentitySnapshot(authContext),
    tenantId: authContext.user.tenant_id,
    navigation: "request-logs",
    refreshId,
    currentRefreshId: refreshId,
  };
}

/** 为指定租户构造同名 request_id 的列表，验证 tenant 而不是 ID 决定详情归属。 */
function tenantListPayload(tenantId, marker) {
  return {
    ...requestLogPayload,
    request_logs: [{
      ...requestLogPayload.request_logs[0],
      tenant_id: tenantId,
      request_id: "req_shared_across_tenants",
      project_name: marker,
    }],
  };
}

/** 为指定租户构造同名详情，marker 用于断言旧租户内容绝未进入新页面。 */
function tenantDetailPayload(tenantId, marker) {
  const list = tenantListPayload(tenantId, marker);
  return {
    ...detailPayload,
    request_log: {
      ...detailPayload.request_log,
      log: {
        ...detailPayload.request_log.log,
        ...list.request_logs[0],
      },
    },
  };
}

test("request logs client lists and reads details with bearer token and filters", async () => {
  const calls = [];
  const listController = new AbortController();
  const detailController = new AbortController();
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        // 该分支影响安全或治理链路，需要用中文说明处理边界。
        return String(url).includes("/request-logs/req_prd016_success") ? detailPayload : requestLogPayload;
      },
    };
  };

  const listed = await listRequestLogs({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    status: "success",
    requestId: "req_prd016_success",
    model: "company-default",
    providerCode: "deepseek",
    snapshotVersion: "3",
    projectId: "prj_1",
    applicationId: "app_1",
    errorCode: "",
    signal: listController.signal,
  });
  const detail = await getRequestLog({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    requestId: "req_prd016_success",
    signal: detailController.signal,
  });

  assert.equal(listed.state, "ok");
  assert.equal(listed.request_logs[0].request_id, "req_prd016_success");
  assert.equal(detail.request_log.routing_decision.decision_reason, "routing.cost_lowest");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].url, /status=success/);
  assert.match(calls[0].url, /request_id=req_prd016_success/);
  assert.match(calls[0].url, /model=company-default/);
  assert.match(calls[0].url, /snapshot_version=3/);
  assert.match(calls[0].url, /project_id=prj_1/);
  assert.match(calls[0].url, /application_id=app_1/);
  assert.doesNotMatch(calls[0].url, /error_code=/);
  assert.equal(calls[0].options.signal, listController.signal);
  assert.equal(calls[1].options.signal, detailController.signal);
});

test("request log lifecycle rejects tenant A late detail after logout and tenant B same request id", () => {
  const memory = createRequestLogPageMemory();
  const authA = requestLogAuthContext("tenant_a", "user_a", "session_a", 1);
  const ownerA = requestLogOwner(authA, 11);
  memory.configure({
    ...ownerA,
    nextFilters: { requestId: "req_shared_across_tenants", limit: 20, offset: 0 },
    nextSelectedRequestId: "req_shared_across_tenants",
  });
  const listA = memory.beginList(ownerA);
  assert.equal(memory.acceptList(listA, tenantListPayload("tenant_a", "TENANT_A_LIST"), ownerA), true);
  const detailA = memory.beginDetail(listA, ownerA);
  assert.ok(detailA);

  // logout 必须同步 Abort 两阶段请求并清空 selected/detail/list/feedback；迟到回调仍由 generation 拒绝。
  memory.leave();
  assert.equal(listA.signal.aborted, true);
  assert.equal(detailA.signal.aborted, true);
  assert.equal(memory.snapshot(), null);
  const cleared = memory.query();
  assert.equal(cleared.selectedRequestId, null);
  assert.equal(cleared.filters.requestId, "");
  assert.equal(cleared.filters.traceId, "");
  assert.equal(cleared.filters.selectedSavedFilterId, "");
  assert.equal(cleared.filters.limit, 20);
  assert.equal(cleared.filters.offset, 0);

  const authB = requestLogAuthContext("tenant_b", "user_b", "session_b", 7);
  const ownerB = requestLogOwner(authB, 12);
  memory.configure({
    ...ownerB,
    nextFilters: { requestId: "req_shared_across_tenants", limit: 20, offset: 0 },
    nextSelectedRequestId: "req_shared_across_tenants",
  });
  const listB = memory.beginList(ownerB);
  assert.equal(memory.acceptList(listB, tenantListPayload("tenant_b", "TENANT_B_LIST"), ownerB), true);
  const detailB = memory.beginDetail(listB, ownerB);

  // A 与 B 的 request_id 相同也不能让旧 A 回调通过，tenant/identity/generation 都属于强 owner。
  assert.equal(memory.acceptDetail(
    detailA,
    tenantDetailPayload("tenant_a", "TENANT_A_SECRET_DETAIL"),
    ownerA,
  ), false);
  assert.equal(memory.acceptDetail(
    detailB,
    tenantDetailPayload("tenant_a", "TENANT_A_WRONG_OWNER"),
    ownerB,
  ), false);
  assert.equal(memory.acceptDetail(
    detailB,
    tenantDetailPayload("tenant_b", "TENANT_B_DETAIL"),
    ownerB,
  ), true);

  const html = renderRequestLogPanel(memory.snapshot(), authB);
  assert.match(html, /TENANT_B_LIST|TENANT_B_DETAIL/);
  assert.doesNotMatch(html, /TENANT_A_SECRET_DETAIL|TENANT_A_WRONG_OWNER|TENANT_A_LIST/);
});

test("request log identity switch and page leave abort both stages and reject late callbacks", () => {
  const memory = createRequestLogPageMemory();
  const authA = requestLogAuthContext("tenant_a", "user_a", "session_a", 1);
  const authRoleChanged = {
    ...authA,
    user: { ...authA.user, role: "admin" },
    session: { ...authA.session, session_revision: 2 },
  };
  const ownerA = requestLogOwner(authA, 21);
  memory.configure({
    ...ownerA,
    nextFilters: { limit: 20, offset: 0 },
    nextSelectedRequestId: "req_shared_across_tenants",
  });
  const listA = memory.beginList(ownerA);
  memory.acceptList(listA, tenantListPayload("tenant_a", "A"), ownerA);
  const detailA = memory.beginDetail(listA, ownerA);

  const ownerChanged = requestLogOwner(authRoleChanged, 22);
  const listChanged = memory.beginList(ownerChanged);
  assert.ok(listChanged);
  assert.equal(listA.signal.aborted, true);
  assert.equal(detailA.signal.aborted, true);
  assert.equal(memory.acceptDetail(detailA, tenantDetailPayload("tenant_a", "LATE"), ownerA), false);

  memory.leave();
  assert.equal(listChanged.signal.aborted, true);
  assert.equal(memory.snapshot(), null);
  assert.equal(memory.query().selectedRequestId, null);
});

test("main wires RequestLog cleanup to identity logout pagehide navigation and owner-gated loader", async () => {
  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(mainSource, /pagehide[\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /function renderLogin[\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /logoutButton[\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /clearRequestLogStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(mainSource, /!REQUEST_LOG_NAVIGATION_IDS\.has\(consoleShellState\?\.activeNavigationItemId\)[\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /!REQUEST_LOG_NAVIGATION_IDS\.has\(itemId\)[\s\S]*clearRequestLogSessionState\(\)/);
  assert.match(mainSource, /loadRequestLogsState\(consoleShellState, refreshId\)/);
  assert.match(mainSource, /isCurrentList\(ticket, currentOwner\)/);
  assert.match(mainSource, /isCurrentDetail\(detailTicket, detailOwner\)/);
});

test("request logs client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_denied",
        trace_id: "trc_denied",
      };
    },
  });

  await assert.rejects(
    () => listRequestLogs({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 403);
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("renderRequestLogPanel shows logs and details without leaking sensitive samples", () => {
  const html = renderRequestLogPanel(
    {
      state: "ok",
      ...requestLogPayload,
      detail: detailPayload,
      filters: { status: "success", model: "company-default" },
    },
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.REQUEST_LOG_READ],
    ),
  );

  assert.match(html, /request-log-filter-form/);
  assert.match(html, /req_prd016_success/);
  assert.match(html, /deepseek-chat/);
  assert.match(html, /Policy 快照/);
  assert.match(html, /v3/);
  assert.match(html, /当前版本/);
  assert.match(html, /policy.route_to.matched/);
  assert.match(html, /model_target_1/);
  assert.match(html, /账务核心/);
  assert.match(html, /账务 API/);
  assert.match(html, /routing.cost_lowest/);
  assert.match(html, /RoutingPolicy ID/);
  assert.match(html, /rpol_1/);
  assert.match(html, /v4/);
  assert.match(html, /cost_first/);
  assert.match(html, /2026-07-18T09:00:00Z/);
  assert.match(html, /rpe_1/);
  assert.match(html, /备用角色（初始候选）/);
  assert.match(html, /0.000000000000000 CNY/);
  assert.match(html, /healthy \/ fresh/);
  assert.match(html, /unknown \/ stale/);
  assert.match(html, /未评估/);
  assert.match(html, /预算命中/);
  assert.match(html, /budget.exhausted.block/);
  assert.match(html, /12.5 CNY/);
  assert.match(html, /0.123456 CNY/);
  assert.match(html, /错误诊断/);
  assert.match(html, /上游尝试/);
  assert.match(html, /#1 初始调用/);
  assert.match(html, /#2 Fallback 重试/);
  assert.match(html, /上游未返回用量/);
  assert.match(html, /上游 &lt;script&gt;限流&lt;\/script&gt;/);
  assert.doesNotMatch(html, /sk-provider-secret/);
  assert.doesNotMatch(html, /<script>限流<\/script>/);
  assert.doesNotMatch(html, /raw_usage/);
  assert.doesNotMatch(html, /CANDIDATE_SUMMARY_MUST_NOT_RENDER|candidate_summary|unsafe_raw_value/);
});

test("request log renders frozen quality decision and candidate fields without current-score recomputation", () => {
  const qualityDetail = {
    ...detailPayload,
    request_log: {
      ...detailPayload.request_log,
      routing_decision: {
        ...detailPayload.request_log.routing_decision,
        routing_mode: "quality_cost",
        quality_as_of: "2026-07-18T10:00:00Z",
        quality_formula_version: "quality_cost_v1<script>",
        quality_scoring_performed: false,
        quality_weight_bps: 5000,
        cost_weight_bps: 3000,
        latency_weight_bps: 2000,
        quality_floor_bps: 6000,
        minimum_sample_count: 20,
      },
      routing_candidates: detailPayload.request_log.routing_candidates.map((candidate, index) => ({
        ...candidate,
        model_quality_score_id: index === 0 ? "mqs_<unsafe>" : "mqs_2",
        quality_statistics_version: 42 + index,
        quality_feedback_version: 3,
        quality_evidence_state: index === 0 ? "ready" : "cold",
        quality_sample_count: index === 0 ? 42 : 1,
        quality_success_count: index === 0 ? 40 : 0,
        quality_error_count: index === 0 ? 2 : 1,
        manual_quality_bps: index === 0 ? 8500 : null,
        average_latency_ms: index === 0 ? "1000.001" : null,
        quality_component_bps: index === 0 ? 8355 : 4762,
        cost_component_bps: index === 0 ? 7000 : null,
        latency_component_bps: index === 0 ? 6500 : null,
        weighted_score_bps: index === 0 ? 7748 : null,
      })),
    },
  };
  const html = renderRequestLogPanel({
    state: "ok",
    ...requestLogPayload,
    detail: qualityDetail,
  }, withPermissionSnapshot(
    { user: { role: "developer" } },
    [PERMISSIONS.REQUEST_LOG_READ],
  ));

  assert.match(html, /quality_cost_v1&lt;script&gt;/);
  assert.match(html, /质量评分/);
  assert.match(html, /未执行/);
  assert.match(html, /5000 bps/);
  assert.match(html, /6000 bps/);
  assert.match(html, /mqs_&lt;unsafe&gt;/);
  assert.match(html, /统计 v42 \/ 反馈 v3/);
  assert.match(html, /42 \/ 40 \/ 2/);
  assert.match(html, /8500 bps/);
  assert.match(html, /1000.001 ms/);
  assert.match(html, /8355 bps \/ 7000 bps \/ 6500 bps/);
  assert.match(html, /7748 bps/);
  // cold 候选已经加载的 quality_component_bps 必须原样展示，即使 route-level 没有执行完整加权。
  assert.match(html, /4762 bps/);
  assert.doesNotMatch(html, /<script>|current_model_quality|model-quality-scores/);
});

test("request log snapshot rendering escapes policy fields and never invents v0 for null versions", () => {
  const unavailableLog = {
    ...requestLogPayload.request_logs[0],
    snapshot_version: null,
    policy_snapshot_status: "unavailable",
    policy_decision: "deny",
    policy_reason_code: "policy.<unsafe>",
    policy_id: "<script>policy</script>",
    policy_action_type: "deny",
    policy_target_model_id: null,
  };
  const html = renderRequestLogPanel(
    {
      state: "ok",
      ...requestLogPayload,
      request_logs: [unavailableLog],
      detail: {
        ...detailPayload,
        request_log: {
          ...detailPayload.request_log,
          log: unavailableLog,
        },
      },
      filters: { snapshotVersion: "" },
    },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.REQUEST_LOG_READ],
    ),
  );

  assert.match(html, /快照不可用/);
  assert.match(html, /policy.&lt;unsafe&gt;/);
  assert.match(html, /&lt;script&gt;policy&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script>policy<\/script>/);
  assert.doesNotMatch(html, /<strong>v0<|>v0</);
  assert.match(html, /name="snapshotVersion"/);
});

test("renderRequestLogPanel escapes unsafe fields and hides viewer role", () => {
  const unsafeState = {
    state: "ok",
    ...requestLogPayload,
    request_logs: [
      {
        ...requestLogPayload.request_logs[0],
        request_id: "<script>alert(1)</script>",
        error_code: "gateway.<unsafe>",
      },
    ],
  };

  const developerHtml = renderRequestLogPanel(
    unsafeState,
    withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.REQUEST_LOG_READ],
    ),
  );
  const viewerHtml = renderRequestLogPanel(
    unsafeState,
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  );

  assert.match(developerHtml, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(developerHtml, /gateway.&lt;unsafe&gt;/);
  assert.equal(viewerHtml, "");
});

test("request log list marks existing feedback and detail hosts the immutable feedback form", () => {
  const logs = [{ ...requestLogPayload.request_logs[0], has_feedback: true }];
  const feedbackState = {
    sourceRequestId: "req_prd016_success",
    authoritativeFeedback: null,
    feedbackEligible: true,
    feedbackIneligibleReason: "",
    rating: null,
    accepted: null,
    tags: [],
    dirty: false,
    busy: false,
    canSubmit: false,
    actionError: null,
    statusMessage: "",
  };
  const html = renderRequestLogPanel({
    state: "ok",
    ...requestLogPayload,
    request_logs: logs,
    detail: detailPayload,
  }, requestLogAuthContext(
    "tenant_1",
    "user_1",
    "session_1",
    1,
    [PERMISSIONS.FEEDBACK_USE, PERMISSIONS.REQUEST_LOG_READ],
  ), feedbackState);

  assert.match(html, /request-log-feedback-state">已反馈/);
  assert.match(html, /data-request-feedback-form/);
  assert.match(html, /req_prd016_success/);
  assert.doesNotMatch(
    html,
    /name="tenant_id"|name="actor"|provider_key|name="prompt"|raw_prompt|raw_response|response_body/i,
  );
});

test("renderDeveloperStart includes request logs for developer", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: withPermissionSnapshot(
      { user: { role: "developer", display_name: "Dev", email: "dev@example.com" }, tenant: { name: "Default" } },
      [PERMISSIONS.REQUEST_LOG_READ],
    ),
    consoleShellState: {
      activeNavigationItemId: "request-logs",
      navigation: {
        active_navigation_item_id: "request-logs",
        navigation_items: [{
          id: "request-logs",
          label: "调用日志",
          group: "可观测性",
          target_section: "request-logs",
          description: "调用日志",
          order: 70,
        }],
      },
    },
    requestLogsState: { state: "ok", ...requestLogPayload },
  });

  assert.match(container.innerHTML, /request-logs-panel/);
  assert.match(container.innerHTML, /req_prd016_success/);
});

test("PRD-055 fresh page defaults to a bounded recent 24-hour UTC window", () => {
  const filters = createRequestLogPageMemory().query().filters;
  const duration = Date.parse(filters.createdTo) - Date.parse(filters.createdFrom);
  assert.equal(duration, 24 * 60 * 60 * 1000);
  assert.match(filters.createdFrom, /Z$/);
  assert.match(filters.createdTo, /Z$/);
});

test("PRD-055 advanced query uses versioned Base64URL filters and stable sort", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => ({
    ok: true, status: 200, headers: new Map(),
    async json() { calls.push({ url: String(url), options }); return requestLogPayload; },
  });
  await listRequestLogs({
    controlApiUrl: "http://control.local", token: "jwt-token",
    advancedFilters: {
      traceId: "trc_055", providerCode: "deepseek",
      createdFrom: "2026-07-27T00:00:00Z", latencyMsMin: "100",
      costAmountMax: "9.000001", terminalStage: "provider",
      tenantId: "TENANT_MUST_NOT_BE_SENT",
      prompt: "PROMPT_MUST_NOT_BE_SENT",
    },
    // 即使旧调用方残留 legacy 参数，客户端也不得制造后端定义为非法的混合筛选来源。
    status: "success", requestId: "req_legacy", model: "legacy-model",
    providerCode: "legacy-provider", environmentId: "env_legacy",
    snapshotVersion: "99", projectId: "prj_legacy", applicationId: "app_legacy",
    errorCode: "legacy.error",
    sortBy: "latency_ms", sortDirection: "asc", limit: 50, offset: 100,
  });
  const url = new URL(calls[0].url);
  const encoded = url.searchParams.get("filters");
  const normalized = encoded.replaceAll("-", "+").replaceAll("_", "/");
  const decoded = JSON.parse(Buffer.from(normalized, "base64").toString("utf8"));
  assert.deepEqual(decoded, {
    v: 1, trace_id: "trc_055", provider_code: "deepseek",
    created_from: "2026-07-27T00:00:00Z", latency_ms_min: "100",
    cost_amount_max: "9.000001", terminal_stage: "provider",
  });
  assert.equal(url.searchParams.get("sort_by"), "latency_ms");
  assert.equal(url.searchParams.get("sort_direction"), "asc");
  assert.equal(url.searchParams.get("offset"), "100");
  for (const legacyName of [
    "status", "request_id", "model", "provider_code", "environment_id",
    "snapshot_version", "project_id", "application_id", "error_code",
  ]) assert.equal(url.searchParams.has(legacyName), false);
  assert.doesNotMatch(calls[0].url, /TENANT_MUST_NOT_BE_SENT|PROMPT_MUST_NOT_BE_SENT|trc_055/);
});

test("PRD-055 SavedFilter CRUD omits tenant owner and pagination", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true, status: options.method === "DELETE" ? 204 : 200, headers: new Map(),
      async json() { return { saved_filters: [], saved_filter: { saved_filter_id: "sf_1" } }; },
    };
  };
  await listSavedRequestLogFilters({ controlApiUrl: "http://control.local", token: "jwt-token" });
  await createSavedRequestLogFilter({
    controlApiUrl: "http://control.local", token: "jwt-token", name: "高延迟",
    filters: { latencyMsMin: "500", terminalStage: "provider", tenantId: "tenant_evil", offset: 9000 },
    sortBy: "latency_ms", sortDirection: "desc",
  });
  await updateSavedRequestLogFilter({
    controlApiUrl: "http://control.local", token: "jwt-token",
    savedFilterId: "sf_1", version: 3, name: "失败",
    filters: { status: "failed" }, sortBy: "created_at", sortDirection: "desc",
  });
  await deleteSavedRequestLogFilter({
    controlApiUrl: "http://control.local", token: "jwt-token",
    savedFilterId: "sf_1", version: 4,
  });
  assert.deepEqual(JSON.parse(calls[1].options.body).filters, {
    v: 1, latency_ms_min: "500", terminal_stage: "provider",
  });
  assert.doesNotMatch(calls[1].options.body, /tenant|owner|offset|limit/);
  assert.equal(JSON.parse(calls[2].options.body).version, 3);
  assert.match(calls[3].url, /saved-filters\/sf_1\?version=4$/);

  await listRequestLogs({
    controlApiUrl: "http://control.local", token: "jwt-token",
    savedFilterId: "sf_1", advancedFilters: { status: "failed" },
    status: "success", requestId: "req_legacy", model: "legacy-model",
    providerCode: "legacy-provider", environmentId: "env_legacy",
    snapshotVersion: "99", projectId: "prj_legacy", applicationId: "app_legacy",
    errorCode: "legacy.error",
    sortBy: "latency_ms", sortDirection: "asc",
  });
  const appliedUrl = new URL(calls[4].url);
  assert.equal(appliedUrl.searchParams.get("saved_filter_id"), "sf_1");
  assert.equal(appliedUrl.searchParams.has("filters"), false);
  assert.equal(appliedUrl.searchParams.has("sort_by"), false);
  assert.equal(appliedUrl.searchParams.has("sort_direction"), false);
  for (const legacyName of [
    "status", "request_id", "model", "provider_code", "environment_id",
    "snapshot_version", "project_id", "application_id", "error_code",
  ]) assert.equal(appliedUrl.searchParams.has(legacyName), false);
});

test("PRD-055 SavedFilter shares identity owner and renderer hides viewer", () => {
  const memory = createRequestLogPageMemory();
  const authA = requestLogAuthContext("tenant_a", "user_a", "session_a", 1);
  const ownerA = requestLogOwner(authA, 71);
  memory.configure({ ...ownerA, nextFilters: { limit: 20, offset: 0 } });
  const listA = memory.beginList(ownerA);
  assert.equal(memory.acceptSavedFilters(listA, {
    saved_filters: [{
      saved_filter_id: "sf_a", name: "<script>TENANT_A_FILTER</script>",
      filters: { v: 1, status: "failed", terminal_stage: "provider" }, sort_by: "created_at",
      sort_direction: "desc", version: 1,
    }],
  }, ownerA), true);
  memory.acceptList(listA, tenantListPayload("tenant_a", "A"), ownerA);
  const currentMutation = memory.beginSavedFilterMutation(ownerA);
  assert.equal(memory.acceptSavedFilterMutationError(currentMutation, {
    code: "saved_filter.version_conflict",
    message: '<img src=x onerror="SAVED_FILTER_XSS">',
    requestId: "req_saved_filter_conflict",
  }, ownerA), true);
  assert.equal(memory.finishSavedFilterMutation(currentMutation, ownerA), true);
  const htmlA = renderRequestLogPanel(memory.snapshot(), authA);
  assert.match(htmlA, /&lt;script&gt;TENANT_A_FILTER&lt;\/script&gt;/);
  assert.match(htmlA, /name="traceId"|name="latencyMsMin"|name="costAmountMax"/);
  assert.match(htmlA, /name="terminalStage"/);
  assert.match(htmlA, /&lt;img src=x onerror=&quot;SAVED_FILTER_XSS&quot;&gt;/);
  assert.doesNotMatch(htmlA, /<img|onerror="SAVED_FILTER_XSS"/);
  assert.equal(renderRequestLogPanel(
    memory.snapshot(),
    withPermissionSnapshot({ user: { role: "viewer" } }, []),
  ), "");

  const mutationA = memory.beginSavedFilterMutation(ownerA);
  memory.leave();
  assert.equal(mutationA.signal.aborted, true);
  assert.equal(memory.acceptSavedFilterMutationError(
    mutationA, { message: "LATE_A" }, ownerA,
  ), false);
  assert.equal(memory.snapshot(), null);
});

test("PRD-055 main gates list and mutations with role navigation and owner tickets", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(source, /!hasPermission\(authSession\.context, PERMISSIONS\.REQUEST_LOG_READ\)[\s\S]*!REQUEST_LOG_NAVIGATION_IDS\.has\(activeNavigation\)/);
  assert.match(source, /listSavedRequestLogFilters\([\s\S]*signal: ticket\.signal/);
  assert.match(source, /beginSavedFilterMutation\(owner\)/);
  assert.match(source, /isCurrentSavedFilterMutation\(ticket, currentOwner\)/);
  assert.match(source, /const owner = currentRequestLogOwner\(\);[\s\S]*requestExplanationHandoff\.stage\([\s\S]*source: "request_logs"/);
  assert.match(source, /function requestLogInteractionAllowed[\s\S]*REQUEST_LOG_NAVIGATION_IDS\.has\(consoleShellState\?\.activeNavigationItemId\)[\s\S]*requestLogIdentitySnapshot/);
  assert.match(source, /terminalStage: optionalText\(data\.get\("terminalStage"\)\)/);
  assert.equal((source.match(/currentRequestLogFiltersFromForm\(\)/g) ?? []).length, 3);
  const selectionStart = source.indexOf('savedFilterSelect?.addEventListener("change"');
  const applyStart = source.indexOf('document.querySelector("#request-log-saved-filter-apply")', selectionStart);
  assert.ok(selectionStart >= 0 && applyStart > selectionStart);
  const selectionHandler = source.slice(selectionStart, applyStart);
  assert.doesNotMatch(selectionHandler, /configureCurrentRequestLogState|await refresh|requestLogFiltersFromSavedFilter/);
  const mutationStart = source.indexOf('document.querySelector("#request-log-saved-filter-create")');
  const mutationEnd = source.indexOf('document.querySelector("#request-log-saved-filter-delete")');
  assert.ok(mutationStart >= 0 && mutationEnd > mutationStart);
  const mutationHandlers = source.slice(
    mutationStart,
    mutationEnd,
  );
  assert.doesNotMatch(mutationHandlers, /filters: requestLogPageMemory\.query\(\)\.filters/);
  assert.doesNotMatch(source, /localStorage[^\n]*requestLog|sessionStorage[^\n]*requestLog/i);
});

test("PRD-055 datetime-local round-trip preserves the same UTC instant", () => {
  const utc = "2026-07-27T00:15:00.000Z";
  const localValue = requestLogDateTimeLocalValue(utc);
  assert.match(localValue, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  assert.equal(new Date(localValue).toISOString(), utc);
  assert.equal(requestLogDateTimeLocalValue("not-a-date"), "");
});

test("PRD-055 SavedFilter restore keeps terminal stage and rejects injected stage text", () => {
  const restored = requestLogFiltersFromSavedFilter({
    saved_filter_id: "sf_stage",
    filters: { v: 1, terminal_stage: "provider" },
    sort_by: "latency_ms",
    sort_direction: "asc",
  });
  assert.equal(restored.terminalStage, "provider");
  assert.equal(restored.selectedSavedFilterId, "sf_stage");
  assert.equal(restored.offset, 0);

  const rejected = requestLogFiltersFromSavedFilter({
    saved_filter_id: "sf_unsafe",
    filters: { v: 1, terminal_stage: '\"><script>STAGE_XSS</script>' },
  });
  assert.equal(rejected.terminalStage, "");
  const appliedHtml = renderRequestLogPanel({
    state: "ok", ...requestLogPayload, filters: restored, savedFilters: [],
  }, requestLogAuthContext("tenant_a", "user_a", "session_a", 1));
  assert.match(appliedHtml, /name="terminalStage"[\s\S]*value="provider" selected/);

  const html = renderRequestLogPanel({
    state: "ok", ...requestLogPayload, filters: rejected, savedFilters: [],
  }, requestLogAuthContext("tenant_a", "user_a", "session_a", 1));
  assert.doesNotMatch(html, /STAGE_XSS|<script>/);
});

