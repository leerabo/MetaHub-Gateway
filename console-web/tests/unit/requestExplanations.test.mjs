/**
 * PRD-054 请求解释中心专项测试。
 * 覆盖 API wire、完整 owner、一次性 handoff、迟到响应、重绘 UI 状态、字段白名单和主应用接线。
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { AuthApiError } from "../../src/lib/auth.js";
import { getRequestExplanation } from "../../src/lib/requestExplanations.js";
import {
  createRequestExplanationHandoff,
  createRequestExplanationPageMemory,
  normalizeRequestExplanationId,
  requestExplanationIdentitySnapshot,
} from "../../src/features/request-explanations/requestExplanationPageState.js";
import { renderRequestExplanationPanel } from "../../src/features/request-explanations/renderRequestExplanation.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 决策阶段顺序与后端 STAGE_ORDER 完全一致，测试禁止客户端自行重排。
const STAGES = [
  "authentication",
  "rate_limit",
  "budget",
  "dlp",
  "policy",
  "routing",
  "keypool",
  "provider",
  "usage_and_finops",
  "logs_and_webhooks",
];

// 详情区块顺序与后端 SECTION_ORDER 完全一致，用于冻结十一区块页面合同。
const SECTIONS = [
  "authentication",
  "rate_limit",
  "budget",
  "dlp",
  "policy",
  "routing",
  "keypool",
  "provider_response",
  "usage_and_finops",
  "logs",
  "webhooks",
];

/** 构造带完整 Session revision 的授权身份，避免测试只覆盖 tenant+role 的弱 owner。 */
function authContext({
  tenantId = "tenant_a",
  userId = "user_a",
  role = "admin",
  sessionId = "session_a",
  revision = 3,
  permissionCodes = [PERMISSIONS.REQUEST_EXPLANATION_READ],
} = {}) {
  // 请求解释权限由参数显式给出，admin/developer 角色名不参与授权推导。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, name: "测试租户" },
    user: {
      tenant_id: tenantId,
      user_id: userId,
      email: userId + "@example.com",
      display_name: "测试用户",
      role,
    },
    session: { session_id: sessionId, session_revision: revision },
  }, permissionCodes, { permissionRevision: revision });
}

/** 从认证上下文生成与主应用相同的读取 owner。 */
function owner(context = authContext(), refreshId = 1) {
  return {
    identity: requestExplanationIdentitySnapshot(context),
    tenantId: context.user.tenant_id,
    navigation: "request-explanations",
    refreshId,
    currentRefreshId: refreshId,
  };
}

/** 构造冻结的区块外壳；facts 只在需要验证字段时覆盖。 */
function section(evidenceState = "complete", facts = [], overrides = {}) {
  return {
    evidence_state: evidenceState,
    reason_code: "request_explanation.recorded",
    facts,
    truncated: false,
    ...overrides,
  };
}

/** 构造与后端 RequestExplanationResponse 一致的最小完整 fixture。 */
function responsePayload({
  sourceRequestId = "req_prd054_frontend",
  explanationStatus = "complete",
  missingSections = [],
  truncatedSections = [],
} = {}) {
  const explanation = {
    request_explanation_id: "rxe_0123456789abcdef0123456789abcdef",
    source_request_id: sourceRequestId,
    source_trace_id: "trc_prd054_source",
    status: explanationStatus,
    evidence_version: "request_explanation_v1",
    captured_at: "2026-07-28T01:02:03Z",
    view_event_status: "pending",
    summary: {
      source_status: "failed",
      error_code: "gateway.upstream_error",
      request_model: "company-default",
      resolved_model: "deepseek-chat<script>",
      provider_code: "deepseek",
      environment_id: "env_a",
      environment_name: "生产环境",
      project_id: "project_a",
      project_name: "账务核心",
      application_id: "app_a",
      application_name: "账务 API",
      snapshot_version: 7,
      latency_ms: 1234,
      input_tokens: 12,
      output_tokens: 34,
      total_tokens: 46,
      estimated_cost_amount: "0.000000123456789",
      currency: "CNY",
      fallback_triggered: true,
      created_at: "2026-07-28T01:01:00Z",
      updated_at: "2026-07-28T01:02:00Z",
    },
    decision_chain: STAGES.map((stage, index) => ({
      stage,
      status: stage === "provider" ? "failed" : "passed",
      evidence_state: "complete",
      reason_code: "request_explanation.stage_recorded",
      occurred_at: "2026-07-28T01:01:" + String(index).padStart(2, "0") + "Z",
      evidence_refs: ["fact_" + index],
    })),
    missing_sections: missingSections,
    truncated_sections: truncatedSections,
  };
  for (const name of SECTIONS) {
    explanation[name] = section();
  }
  explanation.authentication = section("complete", [{
    kind: "authentication",
    fact_id: "auth_fact_1",
    occurred_at: "2026-07-28T01:01:00Z",
    values: {
      virtual_key_id: "vk_a",
      virtual_key_prefix: "mh_live_****",
      environment_id: "env_a",
      environment_name: "生产环境",
      project_id: "project_a",
      project_name: "账务核心",
      application_id: "app_a",
      application_name: "账务 API",
      snapshot_version: 7,
      policy_snapshot_status: "active",
      prompt: "PROMPT_MUST_NOT_RENDER",
    },
  }]);
  explanation.usage_and_finops = section("complete", [{
    kind: "finops_entry",
    fact_id: "finops_fact_1",
    occurred_at: "2026-07-28T01:02:00Z",
    values: {
      ledger_sequence: "900719925474099312345",
      event_id: "fnevt_a",
      entry_type: "usage",
      direction: "debit",
      amount_micros: "900719925474099312345",
      currency: "CNY",
      normalized_cny_micros: "900719925474099312345",
      cny_normalization_status: "native",
      price_evidence_status: "complete",
      price_snapshot_source: "request_snapshot",
      posting_source: "gateway",
      quota_type: "cost",
      price_snapshot_id: "price_a",
      price_schema_version: "pricing_v1",
      input_tokens: 12,
      output_tokens: 34,
      total_tokens: 46,
      cache_tokens: 0,
      estimated: false,
      status: "posted",
      raw_usage: "RAW_USAGE_MUST_NOT_RENDER",
    },
  }]);
  explanation.webhooks = section("complete", [{
    kind: "webhook_delivery",
    fact_id: "whd_a",
    occurred_at: "2026-07-28T01:02:00Z",
    values: {
      event_id: "evt_a",
      event_type: "request.failed",
      webhook_endpoint_id: "whe_a",
      endpoint_name: "告警回调<script>",
      status: "failed",
      attempt_count: 2,
      max_attempts: 10,
      http_status: 503,
      duration_ms: 81,
      error_code: "webhook.delivery_unavailable",
      next_retry_at: "2026-07-28T01:03:00Z",
      delivery_kind: "original",
      replayed_from_delivery_id: null,
      endpoint_url: "https://secret.example/hook",
      payload: "WEBHOOK_PAYLOAD_MUST_NOT_RENDER",
    },
  }]);
  explanation.logs = section("complete", [{
    kind: "unknown_future_kind",
    values: {
      response: "RESPONSE_MUST_NOT_RENDER",
      provider_error: "PROVIDER_ERROR_MUST_NOT_RENDER",
    },
  }]);
  return {
    state: "ok",
    request_explanation: explanation,
    request_id: "req_prd054_operation",
    trace_id: "trc_prd054_operation",
  };
}

test("client encodes path and sends no tenant, raw query, body or content type", async () => {
  let captured = null;
  globalThis.fetch = async (url, options) => {
    captured = { url, options };
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      async json() {
        return responsePayload();
      },
    };
  };

  const result = await getRequestExplanation({
    controlApiUrl: "http://control.local",
    token: "jwt-secret",
    requestId: "req/with space",
    signal: new AbortController().signal,
  });

  assert.equal(captured.url, "http://control.local/request-explain/req%2Fwith%20space");
  assert.equal(captured.options.method, undefined);
  assert.equal(captured.options.body, undefined);
  assert.equal(captured.options.headers.Authorization, "Bearer jwt-secret");
  assert.equal("Content-Type" in captured.options.headers, false);
  assert.doesNotMatch(captured.url, /tenant|include_raw|prompt|response/i);
  assert.equal(result.request_id, "req_prd054_operation");
});

test("client converts non-2xx into AuthApiError with operation request_id", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 404,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "request_explanation.not_found",
        message: "请求不存在",
        request_id: "req_operation_error",
        trace_id: "trc_operation_error",
      };
    },
  });

  await assert.rejects(
    () => getRequestExplanation({
      controlApiUrl: "http://control.local",
      token: "jwt-secret",
      requestId: "req_missing",
    }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 404);
      assert.equal(error.code, "request_explanation.not_found");
      assert.equal(error.requestId, "req_operation_error");
      return true;
    },
  );
});

test("identity and source request validators preserve full security boundaries", () => {
  const admin = requestExplanationIdentitySnapshot(authContext());
  const developer = requestExplanationIdentitySnapshot(authContext({ role: "developer" }));
  const viewer = requestExplanationIdentitySnapshot(authContext({ role: "viewer", permissionCodes: [] }));
  assert.ok(admin);
  assert.ok(developer);
  assert.equal(viewer, "");
  assert.equal(normalizeRequestExplanationId("req_exact"), "req_exact");
  assert.equal(normalizeRequestExplanationId(" req_exact "), null);
  assert.equal(normalizeRequestExplanationId("req/unsafe"), null);
  assert.equal(normalizeRequestExplanationId(""), null);
});

test("handoff is identity-bound, one-shot, and its diagnostic snapshot hides request_id", () => {
  const handoff = createRequestExplanationHandoff();
  const current = owner();
  assert.equal(handoff.stage({
    identity: current.identity,
    tenantId: current.tenantId,
    requestId: "req_handoff",
    source: "request_logs",
  }), true);
  assert.deepEqual(handoff.snapshot(), { generation: 1, pending: true });
  assert.equal(JSON.stringify(handoff.snapshot()).includes("req_handoff"), false);
  assert.equal(handoff.consume({
    identity: current.identity,
    tenantId: "tenant_b",
    navigation: "request-explanations",
  }), null);
  assert.deepEqual(handoff.snapshot(), { generation: 1, pending: false });

  handoff.stage({
    identity: current.identity,
    tenantId: current.tenantId,
    requestId: "req_handoff",
  });
  assert.deepEqual(handoff.consume(current), {
    requestId: "req_handoff",
    source: "request_logs",
  });
  assert.equal(handoff.consume(current), null);
});

test("page memory accepts only current owner and does not repeat GET on periodic refresh", () => {
  const memory = createRequestExplanationPageMemory();
  const current = owner();
  assert.equal(memory.beginRead(current), null);
  assert.equal(memory.select({ ...current, requestId: "req_one", source: "manual" }), true);
  const ticket = memory.beginRead(current);
  assert.ok(ticket);
  assert.equal(memory.acceptSuccess(ticket, responsePayload({ sourceRequestId: "req_one" }), current), true);
  assert.equal(memory.snapshot().request_explanation.source_request_id, "req_one");
  // 已接纳 revision 的后续 15 秒刷新只复用快照，不再制造有审计副作用的 GET。
  assert.equal(memory.beginRead(current), null);
  assert.equal(memory.query().loadedRevision, memory.query().desiredRevision);

  memory.select({ ...current, requestId: "req_two", source: "manual" });
  assert.equal(ticket.signal.aborted, true);
  assert.equal(memory.acceptSuccess(ticket, responsePayload({ sourceRequestId: "req_one" }), current), false);
  const nextTicket = memory.beginRead(current);
  assert.equal(nextTicket.requestId, "req_two");
  assert.equal(memory.acceptSuccess(
    nextTicket,
    responsePayload({ sourceRequestId: "req_wrong" }),
    current,
  ), false);
});

test("page memory preserves draft disclosure and focus, then clears on identity change", () => {
  const memory = createRequestExplanationPageMemory();
  const current = owner();
  memory.select({ ...current, requestId: "req_ui" });
  assert.equal(memory.setDraft({ ...current, requestId: "req_ui_draft" }), true);
  assert.equal(memory.setSectionDisclosure({
    ...current,
    sectionKey: "provider_response",
    open: true,
  }), true);
  assert.equal(memory.setFocusTarget({
    ...current,
    target: "section:provider_response",
  }), true);
  assert.equal(memory.setSectionDisclosure({
    ...current,
    sectionKey: "prompt",
    open: true,
  }), false);
  const ui = memory.snapshot();
  assert.equal(ui.draftRequestId, "req_ui_draft");
  assert.equal(ui.sectionDisclosure.provider_response, true);
  assert.equal(ui.focusTarget, "section:provider_response");

  const ticket = memory.beginRead(current);
  const other = owner(authContext({ tenantId: "tenant_b", userId: "user_b" }), 1);
  memory.setDraft({ ...other, requestId: "req_other" });
  assert.equal(ticket.signal.aborted, true);
  assert.equal(memory.acceptFailure(ticket, new Error("late"), current), false);
  assert.equal(memory.snapshot().requestId, "");
  assert.equal(memory.snapshot().draftRequestId, "req_other");

  memory.clearSelection(other);
  assert.equal(memory.snapshot().state, "idle");
  assert.equal(memory.snapshot().draftRequestId, "");
  assert.equal(memory.snapshot().focusTarget, "request-id");
});

test("renderer shows exact safe facts, large integer strings, and no unknown sensitive fields", () => {
  const state = {
    ...responsePayload(),
    draftRequestId: "req_prd054_frontend",
    requestId: "req_prd054_frontend",
    sectionDisclosure: { authentication: false, provider_response: true },
  };
  const html = renderRequestExplanationPanel(state, authContext());
  assert.match(html, /请求解释中心/);
  assert.match(html, /rxe_0123456789abcdef0123456789abcdef/);
  assert.match(html, /已持久化，待投递/);
  assert.match(html, /账务核心/);
  assert.match(html, /deepseek-chat&lt;script&gt;/);
  assert.match(html, /900719925474099312345/);
  assert.match(html, /告警回调&lt;script&gt;/);
  assert.match(html, /data-request-explanation-section="authentication"/);
  assert.doesNotMatch(
    html,
    /PROMPT_MUST_NOT_RENDER|RAW_USAGE_MUST_NOT_RENDER|WEBHOOK_PAYLOAD_MUST_NOT_RENDER|RESPONSE_MUST_NOT_RENDER|PROVIDER_ERROR_MUST_NOT_RENDER|secret\.example/,
  );
  assert.doesNotMatch(html, /data-request-explanation-section="authentication"[^>]* open/);
  assert.match(html, /data-request-explanation-section="provider_response"[^>]* open/);
});

test("renderer covers partial, error and viewer zero-DOM states", () => {
  const partial = responsePayload({
    explanationStatus: "partial",
    missingSections: ["logs"],
  });
  partial.request_explanation.logs = section("not_recorded", []);
  const partialHtml = renderRequestExplanationPanel(partial, authContext({ role: "developer" }));
  assert.match(partialHtml, /证据部分缺失/);
  assert.match(partialHtml, /缺失区块/);
  assert.match(partialHtml, /日志/);

  const errorHtml = renderRequestExplanationPanel({
    state: "error",
    requestId: "req_failed",
    draftRequestId: "req_failed",
    error: { message: "解释不可用<script>", requestId: "req_operation_error" },
  }, authContext());
  assert.match(errorHtml, /解释不可用&lt;script&gt;/);
  assert.match(errorHtml, /req_operation_error/);
  assert.match(errorHtml, /request-explanation-body request-explanation-error-body/);
  assert.match(errorHtml, /data-explanation-status="error"/);
  assert.match(errorHtml, /request-explanation-summary/);
  assert.match(errorHtml, /request-explanation-evidence/);
  assert.match(errorHtml, /无可验证证据/);
  assert.doesNotMatch(errorHtml, /req_prd054_complete|req_prd054_partial|request-explanation-stage|data-request-explanation-section/);
  assert.equal(renderRequestExplanationPanel(
    partial,
    authContext({ role: "viewer", permissionCodes: [] }),
  ), "");
});

test("main wires no-prefetch RBAC, cleanup, handoff and generic page-view skip", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  assert.match(source, /pagehide[\s\S]*clearRequestExplanationSessionState\(\)/);
  assert.match(source, /function renderLogin[\s\S]*clearRequestExplanationSessionState\(\)/);
  assert.match(source, /logoutButton[\s\S]*clearRequestExplanationSessionState\(\)/);
  assert.match(source, /!sameWorkspaceConsoleSession\(authSession, session\)[\s\S]*clearConsoleSessionState\(\{ clearNavigation: false \}\)/);
  assert.match(source, /function clearConsoleSessionState[\s\S]*clearRequestExplanationSessionState\(\)/);
  assert.match(source, /activeNavigationItemId !== "request-explanations"[\s\S]*clearRequestExplanationSessionState\(\)/);
  assert.match(source, /hasPermission\(authSession\.context, PERMISSIONS\.REQUEST_EXPLANATION_READ\)[\s\S]*activeNavigationItemId !== "request-explanations"/);
  assert.match(source, /requestExplanationHandoff\.stage\([\s\S]*switchConsoleNavigation/);
  const sideEffects = source.slice(source.indexOf("function scheduleConsoleNavigationSideEffects("),
    source.indexOf("async function toggleConsoleSidebarGroup("));
  assert.doesNotMatch(sideEffects, /recordConsolePageView/);
  assert.match(source, /getRequestExplanation\([\s\S]*signal: ticket\.signal/);
  assert.doesNotMatch(source, /localStorage[\s\S]{0,200}requestExplanation|sessionStorage[\s\S]{0,200}requestExplanation/);
});
