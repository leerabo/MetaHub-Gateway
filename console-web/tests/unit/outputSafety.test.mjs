/**
 * PRD-071 Console 输出安全专项测试。
 *
 * 测试职责：覆盖 PAGE-099/100、RequestLog owner 与 FinOps 最小争议投影。
 * 安全边界：权限拒绝、租户切换和迟到响应都必须清空旧详情，正文不得进入 DOM。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderFinOpsLedger } from "../../src/features/finops-ledger/renderFinOpsLedger.js";
import { renderOutputSafety } from "../../src/features/output-safety/renderOutputSafety.js";
import { createRequestLogPageMemory } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { compactAdvancedFilters } from "../../src/lib/requestLogs.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造带完整 tenant/user/session 维度的可读身份，角色名称不参与授权结论。 */
function requestLogReader(permissions = [PERMISSIONS.REQUEST_LOG_READ]) {
  return withPermissionSnapshot({
    user: { tenant_id: "tenant_prd071", user_id: "user_prd071", role: "custom" },
    tenant: { tenant_id: "tenant_prd071" },
    session: { session_id: "session_prd071", session_revision: 7 },
  }, permissions, { permissionRevision: 7 });
}

/** 构造列表和详情共用的无正文安全证据。 */
function outputSafetyState() {
  const summary = {
    output_safety_scan_id: "oss_" + "a".repeat(32),
    scan_mode: "stream_window",
    scan_status: "completed",
    decision: "blocked",
    exposure_state: "partial",
    emitted_token_count: 8,
    emitted_sse_event_count: 1,
    emitted_bytes: 96,
    billing_disposition: "dispute_pending",
  };
  return {
    state: "ok",
    request_logs: [{
      tenant_id: "tenant_prd071",
      request_id: "req_prd071_blocked",
      created_at: "2026-08-04T08:00:00Z",
      output_safety: summary,
    }],
    total: 1,
    limit: 20,
    offset: 0,
    has_more: false,
    filters: { terminalStage: "output_safety" },
    detail: {
      request_log: {
        log: {
          tenant_id: "tenant_prd071",
          request_id: "req_prd071_blocked",
          trace_id: "trc_prd071",
          status: "failed",
          error_code: "content_policy_violation",
          created_at: "2026-08-04T08:00:00Z",
        },
        output_safety: {
          ...summary,
          action: "block",
          dlp_rule_id: "dlp_prd071_rule",
          rule_type: "keyword",
          detector_version: "output-safety-dlp-v1",
          checkpoint_count: 2,
          observed_output_token_count: 20,
          scanned_token_count: 16,
          failure_reason_code: null,
          dispute: {
            review_status: "dispute_pending",
            reversal_eligible: true,
          },
          response: "MUST_NOT_RENDER_OUTPUT_BODY",
          pattern: "MUST_NOT_RENDER_RULE_PATTERN",
        },
        routing_decision: null,
        routing_candidates: [],
        usage: [],
        routing_attempts: [],
        cache_affinity_selections: [],
        budget_evaluations: [],
        dlp_matches: [],
        intent_risk: null,
        stream_session: null,
        error_diagnosis: null,
      },
    },
  };
}

test("PAGE-099 renders blocked event and PAGE-100 evidence without sensitive output", () => {
  const html = renderOutputSafety(outputSafetyState(), requestLogReader());

  assert.match(html, /输出安全事件/);
  assert.match(html, /流式滑窗/);
  assert.match(html, /部分已发送/);
  assert.match(html, /observed|Provider 已观察 Token/);
  assert.match(html, /20/);
  assert.match(html, /已扫描 Token/);
  assert.match(html, /16/);
  assert.match(html, /待争议 \/ 可冲正/);
  assert.doesNotMatch(html, /MUST_NOT_RENDER_OUTPUT_BODY|MUST_NOT_RENDER_RULE_PATTERN/);
});

test("PAGE-099 returns zero DOM without runtime request_log.read", () => {
  assert.equal(renderOutputSafety(outputSafetyState(), requestLogReader([])), "");
});

test("output safety advanced filters map to strict server keys without tenant", () => {
  const filters = compactAdvancedFilters({
    terminalStage: "output_safety",
    outputSafetyStatus: "unavailable",
    outputSafetyMode: "stream_window",
    outputSafetyExposure: "partial",
    outputSafetyBilling: "dispute_pending",
    tenantId: "other_tenant",
  });

  assert.deepEqual(filters, {
    terminal_stage: "output_safety",
    output_safety_status: "unavailable",
    output_safety_mode: "stream_window",
    output_safety_exposure: "partial",
    output_safety_billing: "dispute_pending",
  });
});

test("RequestLog owner isolates request-logs and output-safety navigation generations", () => {
  const memory = createRequestLogPageMemory();
  const base = {
    identity: "identity_prd071",
    tenantId: "tenant_prd071",
    refreshId: 11,
    currentRefreshId: 11,
  };
  memory.configure({ ...base, navigation: "request-logs", nextFilters: { limit: 20 } });
  const oldTicket = memory.beginList({ ...base, navigation: "request-logs" });

  memory.configure({
    ...base,
    navigation: "output-safety",
    nextFilters: { terminalStage: "output_safety", limit: 20 },
  });
  const newTicket = memory.beginList({ ...base, navigation: "output-safety" });

  assert.equal(oldTicket.signal.aborted, true);
  assert.equal(memory.acceptList(oldTicket, { request_logs: [] }, { ...base, navigation: "request-logs" }), false);
  assert.equal(memory.acceptList(newTicket, {
    request_logs: [{ tenant_id: "tenant_prd071", request_id: "req_new" }],
  }, { ...base, navigation: "output-safety" }), true);
  assert.equal(memory.snapshot().filters.terminalStage, "output_safety");
});

test("main forces output_safety stage and applies the same owner gate to both pages", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

  assert.match(source, /REQUEST_LOG_NAVIGATION_IDS = new Set\(\["request-logs", "output-safety"\]\)/);
  assert.match(source, /activeNavigation === "output-safety"[\s\S]*terminalStage: "output_safety"/);
  assert.match(source, /function outputSafetyFiltersFromForm[\s\S]*terminalStage: "output_safety"/);
  assert.match(source, /REQUEST_LOG_NAVIGATION_IDS\.has\(consoleShellState\?\.activeNavigationItemId\)/);
});

test("PAGE-099 detail uses delegated owner read and local PAGE-100 rendering", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const bindingStart = source.indexOf("function bindOutputSafetyDetailInteractions(consoleShellState)");
  const refreshStart = source.indexOf("async function refreshOutputSafetyDetail(consoleShellState)");
  const gateStart = source.indexOf("function requestLogInteractionAllowed(consoleShellState)");
  const localBindingCall = source.indexOf("bindOutputSafetyDetailInteractions(consoleShellState);");
  const genericBindingCall = source.indexOf("bindAuthenticatedEvents(consoleShellState);");
  const binding = source.slice(bindingStart, refreshStart);
  const refresh = source.slice(refreshStart, gateStart);

  assert.ok(bindingStart >= 0 && refreshStart > bindingStart && gateStart > refreshStart);
  // PAGE-099 必须在通用长绑定链之前独立接管稳定父面板，不能被无关模块的绑定异常跳过。
  assert.ok(localBindingCall >= 0 && localBindingCall < genericBindingCall);

  // 父面板委托在 PAGE-100 局部替换后仍有效，且刷新按钮不能借伪造 ID 切换 owner。
  assert.match(binding, /\.output-safety-panel/);
  assert.match(binding, /outputSafetyDetailBound/);
  assert.match(binding, /data-request-log-detail-refresh/);
  assert.match(binding, /requestId !== selectedRequestId/);
  assert.match(binding, /configureCurrentRequestLogState/);
  assert.match(binding, /consoleShellState\.activeNavigationItemId/);
  assert.match(binding, /if \(!configured\) return/);
  assert.match(binding, /refreshOutputSafetyDetail\(consoleShellState\)/);
  // 详情点击只复用 tenant-qualified RequestLog 读取并替换右侧，不得退回整套 refresh()。
  assert.match(refresh, /loadRequestLogsState\(consoleShellState, refreshId\)/);
  assert.match(refresh, /state\.code === "auth\.permission_denied"/);
  assert.match(refresh, /clearRequestLogSessionState\(\)/);
  assert.match(refresh, /refreshId !== refreshSequence/);
  assert.match(refresh, /renderRequestLogDetail/);
  assert.equal((refresh.match(/await refresh\(\)/g) ?? []).length, 1);
  assert.match(refresh, /auth\.permission_denied[\s\S]*clearRequestLogSessionState\(\)[\s\S]*await refresh\(\)/);
});

test("desktop sidebar scroll cannot leave PAGE-099 above the viewport", async () => {
  const styles = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");

  // 桌面端把超长导航限制在视口内独立滚动，避免点击靠下入口时滚动整个页面并露出空白主区。
  assert.match(
    styles,
    /\.console-sidebar\s*\{[^}]*max-height:\s*calc\(100vh - 24px\);[^}]*overflow-y:\s*auto;/s,
  );
  // 移动端是单列自然流，必须撤销桌面嵌套滚动，否则窄屏会出现两个纵向滚动区域。
  assert.match(
    styles,
    /@media \(max-width: 820px\)[\s\S]*?\.console-sidebar\s*\{[^}]*max-height:\s*none;[^}]*overflow-y:\s*visible;/,
  );
});

test("PAGE-099 stacks detail before seven-column actions can overflow", async () => {
  const styles = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");

  // 1280/1366 桌面仍有固定侧栏，PAGE-099 必须先改为上下布局，避免操作列落到详情覆盖层下方。
  assert.match(
    styles,
    /@media \(max-width: 1680px\) \{[\s\S]*?\.output-safety-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);/,
  );
  assert.match(
    styles,
    /@media \(max-width: 1680px\) \{[\s\S]*?\.output-safety-detail-pane\s*\{[^}]*border-top:\s*1px solid var\(--line\);/,
  );
  // PAGE-100 的长 StreamSession ID 不能让隐式 auto 轨道超过移动端详情容器。
  assert.match(
    styles,
    /\.stream-session-list,\s*\.routing-attempt-list\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*min-width:\s*0;/s,
  );
  assert.match(
    styles,
    /\.stream-session-item > header strong\s*\{[^}]*overflow-wrap:\s*anywhere;[^}]*word-break:\s*break-word;/s,
  );
  // 390px 下 PAGE-100 的嵌套详情必须显式受父列约束，不能依赖通用 Grid 断点推导宽度。
  assert.match(
    styles,
    /@media \(max-width: 560px\)[\s\S]*?\.output-safety-detail-pane \.stream-session-item,[\s\S]*?\.output-safety-detail-pane \.request-log-detail-grid\s*\{[^}]*width:\s*100%;[^}]*min-width:\s*0;[^}]*max-width:\s*100%;/s,
  );
  assert.match(
    styles,
    /@media \(max-width: 560px\)[\s\S]*?\.output-safety-detail-pane \.stream-session-item > header\s*\{[^}]*flex-direction:\s*column;/s,
  );
});

test("FinOps exposes only fixed dispute badge and trusts reversal eligibility flag", () => {
  const auth = requestLogReader([PERMISSIONS.BILLING_FINOPS_READ]);
  const entry = {
    finops_ledger_entry_id: "fle_prd071",
    entry_type: "usage_charge",
    direction: "debit",
    amount: "0.100000",
    currency: "CNY",
    quota_type: "paid",
    source_request_id: "req_prd071_blocked",
    price_evidence_status: "priced",
    created_at: "2026-08-04T08:00:00Z",
    output_safety_dispute: {
      output_safety_scan_id: "oss_" + "b".repeat(32),
      review_status: "dispute_pending",
      reversal_eligible: true,
      dlp_rule_id: "MUST_NOT_RENDER_FINOPS_RULE",
    },
  };
  const html = renderFinOpsLedger({
    state: "ok",
    ledger_entries: [entry],
    selectedEntry: entry,
    total: 1,
    filters: {},
    totals: { by_currency: {}, net_cost_cny: "0.100000", unnormalized_entry_count: 0 },
  }, auth);

  assert.match(html, /输出安全争议待复核 \/ 可冲正|输出安全争议待复核，可冲正/);
  assert.match(html, new RegExp(entry.output_safety_dispute.output_safety_scan_id));
  assert.doesNotMatch(html, /MUST_NOT_RENDER_FINOPS_RULE/);

  const ineligibleHtml = renderFinOpsLedger({
    state: "ok",
    ledger_entries: [{ ...entry, amount: "0.000000", output_safety_dispute: { ...entry.output_safety_dispute, reversal_eligible: false } }],
    total: 1,
    filters: {},
    totals: { by_currency: {}, net_cost_cny: "0.000000", unnormalized_entry_count: 0 },
  }, auth);
  assert.match(ineligibleHtml, /输出安全争议待复核/);
  assert.doesNotMatch(ineligibleHtml, /可冲正/);
});
