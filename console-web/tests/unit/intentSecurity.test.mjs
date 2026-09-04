/**
 * 测试职责：锁定 PRD-069 分类 client、完整 owner 生命周期、零正文状态和 PAGE-096/097 安全渲染。
 * 安全边界：样例正文仅用于证明不会进入 URL、Header、状态、DOM、Storage 或统一错误对象。
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createIntentSecurityPageState,
  intentSecurityPageOwner,
} from "../../src/features/intent-security/intentSecurityPageState.js";
import { renderIntentSecurity } from "../../src/features/intent-security/renderIntentSecurity.js";
import { renderRequestLogPanel } from "../../src/features/request-logs/renderRequestLogPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  classifyIntent,
  normalizeClassificationResponse,
} from "../../src/lib/intentSecurity.js";
import { PERMISSIONS } from "../../src/lib/permissions.js";
import { withPermissionSnapshot } from "./permissionTestContext.mjs";

// 假正文和固定 DTO 仅驻留本测试进程，用于发现浏览器状态、错误和模板中的意外回显。
const TEXT_SENTINEL = "PRD069_TEXT_SENTINEL_MUST_NOT_ESCAPE";
const RESULT_ID = `irr_${"a".repeat(32)}`;
const CLASSIFICATION_RESPONSE = Object.freeze({
  intent_risk_result: Object.freeze({
    intent_risk_result_id: RESULT_ID,
    status: "detected",
    classification_mode: "manual_sync",
    risk_type: "system_prompt_extraction",
    risk_score: 9800,
    risk_score_scale: 10_000,
    action: "review",
    reason_code: "explicit_system_prompt_request",
    detector_version: "intent-rule-engine-v1",
    ruleset_version: "intent-rules-2026-08-03-v1",
    input_code_points: TEXT_SENTINEL.length,
    input_truncated: false,
    duration_ms: 2,
    completed_at: "2026-08-03T12:00:00Z",
  }),
  request_id: "req_internal_069",
  trace_id: "trc_internal_069",
});

/** 构造带完整 tenant/user/Session/RBAC 快照的 PAGE-096 测试身份。 */
function intentAuth(tenantId = "tenant_a", userId = "user_a", sessionId = "session_a") {
  return withPermissionSnapshot({
    user: { tenant_id: tenantId, user_id: userId, role: "security_admin" },
    tenant: { tenant_id: tenantId, name: "安全租户" },
    session: { session_id: sessionId, session_revision: 1 },
  }, [PERMISSIONS.INTENT_RISK_CLASSIFY]);
}

/** 构造与认证权限 revision/fingerprint 完全一致的后端导航快照。 */
function intentNavigation(authContext, activeItemId = "intent-security") {
  return {
    activeNavigationItemId: activeItemId,
    navigation: {
      active_navigation_item_id: activeItemId,
      permission_codes: authContext.permission_codes,
      permission_revision: authContext.permission_revision,
      authorization_fingerprint: authContext.authorization_fingerprint,
      navigation_items: [{
        id: "intent-security",
        target_section: "intent-security",
        group: "安全与治理",
        group_label: "安全与治理",
        label: "敏感意图分类",
        description: "本地轻量分类",
        required_permissions: [PERMISSIONS.INTENT_RISK_CLASSIFY],
        visible: true,
        disabled: false,
        order: 91,
      }],
    },
  };
}

/** 构造 Response Header 适配器，保持 Node 测试与浏览器 Headers.get 合同一致。 */
function responseHeaders(values = {}) {
  return { get(name) { return values[name] ?? null; } };
}

test("intent client sends only bearer/json headers and never supplies external request or trace ids", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: 200,
      headers: responseHeaders(),
      async json() { return CLASSIFICATION_RESPONSE; },
    };
  };
  const controller = new AbortController();
  const result = await classifyIntent({
    controlApiUrl: "http://control.local",
    token: "session-token",
    text: TEXT_SENTINEL,
    signal: controller.signal,
  });
  assert.equal(result.intent_risk_result.status, "detected");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.local/security/classify-intent");
  assert.deepEqual(Object.keys(calls[0].options.headers).sort(), ["Authorization", "Content-Type"]);
  assert.equal(calls[0].options.headers.Authorization, "Bearer session-token");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[0].options.cache, "no-store");
  assert.deepEqual(JSON.parse(calls[0].options.body), { text: TEXT_SENTINEL });
  assert.doesNotMatch(calls[0].url, /tenant|request|trace|PRD069_TEXT/);
});

test("intent client ignores free error text and rejects extra or inconsistent response fields", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: responseHeaders(),
    async json() {
      return {
        code: "intent_risk.text_invalid",
        message: TEXT_SENTINEL,
        request_id: "req_invalid_069",
        trace_id: "trc_invalid_069",
        text: TEXT_SENTINEL,
      };
    },
  });
  await assert.rejects(
    () => classifyIntent({
      controlApiUrl: "http://control.local",
      token: "session-token",
      text: TEXT_SENTINEL,
    }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "intent_risk.text_invalid");
      assert.equal(error.message, "分类文本不合法");
      assert.equal(error.requestId, "req_invalid_069");
      assert.doesNotMatch(JSON.stringify(error), new RegExp(TEXT_SENTINEL));
      return true;
    },
  );
  assert.equal(normalizeClassificationResponse({ ...CLASSIFICATION_RESPONSE, text: TEXT_SENTINEL }), null);
  assert.equal(normalizeClassificationResponse({
    ...CLASSIFICATION_RESPONSE,
    intent_risk_result: { ...CLASSIFICATION_RESPONSE.intent_risk_result, action: "block" },
  }), null);
});

test("intent client counts Unicode code points instead of UTF-16 units", async () => {
  const acceptedText = "😀".repeat(20_000);
  let callCount = 0;
  globalThis.fetch = async () => {
    callCount += 1;
    return {
      ok: true,
      status: 200,
      headers: responseHeaders(),
      async json() {
        return {
          ...CLASSIFICATION_RESPONSE,
          intent_risk_result: {
            ...CLASSIFICATION_RESPONSE.intent_risk_result,
            input_code_points: 20_000,
          },
        };
      },
    };
  };
  await classifyIntent({
    controlApiUrl: "http://control.local",
    token: "session-token",
    text: acceptedText,
  });
  await assert.rejects(
    () => classifyIntent({
      controlApiUrl: "http://control.local",
      token: "session-token",
      text: `${acceptedText}😀`,
    }),
    (error) => error instanceof AuthApiError && error.code === "intent_risk.text_invalid",
  );
  assert.equal(callCount, 1);
});

test("PAGE-096 state aborts cancel/leave and rejects stale tenant navigation and refresh owners", () => {
  const authA = intentAuth();
  const shellA = intentNavigation(authA);
  const ownerA = intentSecurityPageOwner(authA, shellA, { refreshId: 7, currentRefreshId: 7 });
  const state = createIntentSecurityPageState();
  assert.equal(state.enter(ownerA), true);
  const operationA = state.begin(ownerA);
  assert.ok(operationA);
  assert.equal(state.snapshot().status, "submitting");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), new RegExp(TEXT_SENTINEL));

  const staleRefreshOwner = intentSecurityPageOwner(authA, shellA, { refreshId: 8, currentRefreshId: 8 });
  assert.equal(state.accept(operationA, CLASSIFICATION_RESPONSE, staleRefreshOwner), false);
  assert.equal(state.enter(staleRefreshOwner), true);
  assert.equal(operationA.signal.aborted, true);
  assert.deepEqual(state.snapshot(), { status: "idle", result: null, error: null });

  const cancelledOperation = state.begin(staleRefreshOwner);
  assert.equal(state.cancel(), true);
  assert.equal(cancelledOperation.signal.aborted, true);
  assert.deepEqual(state.snapshot(), { status: "idle", result: null, error: null });

  const operationB = state.begin(staleRefreshOwner);
  assert.ok(operationB);
  const authB = intentAuth("tenant_b", "user_b", "session_b");
  const ownerB = intentSecurityPageOwner(authB, intentNavigation(authB), {
    refreshId: 8,
    currentRefreshId: 8,
  });
  assert.equal(state.accept(operationB, CLASSIFICATION_RESPONSE, ownerB), false);
  assert.equal(state.enter(ownerB), true);
  assert.equal(operationB.signal.aborted, true);
  const operationB2 = state.begin(ownerB);
  assert.equal(state.accept(operationB2, CLASSIFICATION_RESPONSE, ownerB), true);
  assert.equal(state.snapshot().status, "success");
  state.clear();
  const failedOperation = state.begin(ownerB);
  assert.equal(state.reject(failedOperation, {
    code: "intent_risk.classifier_unavailable",
    message: TEXT_SENTINEL,
    requestId: "req_failed_069",
  }, ownerB), true);
  assert.equal(state.snapshot().error.message, "本地分类器暂不可用");
  assert.doesNotMatch(JSON.stringify(state.snapshot()), new RegExp(TEXT_SENTINEL));
  const leaveOperation = state.begin(ownerB);
  state.leave();
  assert.equal(leaveOperation.signal.aborted, true);
  assert.deepEqual(state.snapshot(), { status: "idle", result: null, error: null });
});

test("PAGE-096 renderer requires matching backend navigation and never restores textarea body", () => {
  const auth = intentAuth();
  const shell = intentNavigation(auth);
  const state = {
    status: "success",
    result: CLASSIFICATION_RESPONSE,
    error: null,
  };
  const html = renderIntentSecurity(state, auth, shell);
  assert.match(html, /敏感意图分类/);
  assert.match(html, /凭据索取/);
  assert.match(html, /系统提示词提取/);
  assert.match(html, /9800\/10000/);
  assert.match(html, /req_internal_069/);
  assert.match(html, /<textarea[^>]*name="text"[^>]*><\/textarea>/);
  assert.match(html, /maxlength="40000"[^>]*data-max-code-points="20000"/);
  assert.doesNotMatch(html, new RegExp(TEXT_SENTINEL));
  assert.doesNotMatch(html, /value="PRD069/);

  const deniedAuth = withPermissionSnapshot({
    user: { tenant_id: "tenant_a", user_id: "viewer_a", role: "viewer" },
    tenant: { tenant_id: "tenant_a" },
    session: { session_id: "session_viewer" },
  }, []);
  assert.equal(renderIntentSecurity(state, deniedAuth, intentNavigation(auth)), "");
  assert.equal(renderIntentSecurity(state, auth, {
    ...shell,
    activeNavigationItemId: "overview",
    navigation: { ...shell.navigation, active_navigation_item_id: "overview" },
  }), "");
  assert.equal(renderIntentSecurity(state, auth, {
    ...shell,
    navigation: { ...shell.navigation, authorization_fingerprint: "forged" },
  }), "");
});

test("developer shell renders PAGE-096 only for the authorized active navigation", () => {
  const auth = intentAuth();
  const authorized = { innerHTML: "" };
  renderDeveloperStart(authorized, {
    authContext: auth,
    consoleShellState: intentNavigation(auth),
    intentSecurityState: { status: "idle", result: null, error: null },
  });
  assert.match(authorized.innerHTML, /class="panel intent-security-panel"/);
  assert.match(authorized.innerHTML, /id="intent-security-form"/);

  const missingNavigation = { innerHTML: "" };
  renderDeveloperStart(missingNavigation, {
    authContext: auth,
    consoleShellState: {
      activeNavigationItemId: "intent-security",
      navigation: {
        ...intentNavigation(auth).navigation,
        navigation_items: [],
      },
    },
    intentSecurityState: { status: "idle", result: null, error: null },
  });
  assert.doesNotMatch(missingNavigation.innerHTML, /intent-security-form/);
});

/** 构造 PAGE-097 详情，除 intent_risk 外只提供现有 renderer 所需的脱敏空集合。 */
function requestLogDetail(intentRisk) {
  return {
    request_log: {
      log: {
        request_id: "req_gateway_069",
        trace_id: "trc_gateway_069",
        status: "success",
        endpoint: "/v1/chat/completions",
        estimated_cost_amount: 0,
        currency: "CNY",
      },
      routing_decision: null,
      routing_candidates: [],
      routing_attempts: [],
      cache_affinity_selections: [],
      usage: [],
      budget_evaluations: [],
      dlp_matches: [],
      stream_session: null,
      error_diagnosis: null,
      feedback: null,
      feedback_eligible: false,
      feedback_ineligible_reason: "request_not_completed",
      intent_risk: intentRisk,
    },
    request_id: "req_detail_069",
    trace_id: "trc_detail_069",
  };
}

/** 通过公开面板 renderer 渲染一项 PAGE-097 状态，验证现有 request_log.read 权限保持不变。 */
function renderIntentRiskStatus(status, overrides = {}) {
  const auth = withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.REQUEST_LOG_READ]);
  return renderRequestLogPanel({
    state: "ok",
    request_logs: [],
    total: 0,
    limit: 20,
    offset: 0,
    filters: {},
    detail: requestLogDetail({
      intent_risk_result_id: RESULT_ID,
      status,
      classification_mode: status === "blocked" ? "sync_strong" : "async_light",
      risk_type: ["clean", "disabled", "pending", "failed", "skipped"].includes(status) ? "none" : "jailbreak",
      risk_score: ["detected", "blocked"].includes(status) ? 9600 : 0,
      risk_score_scale: 10_000,
      action: status === "blocked" ? "block" : status === "detected" ? "observe" : status === "clean" ? "allow" : "none",
      reason_code: status === "pending" ? "queued" : status === "skipped" ? "queue_full" : "no_risk_detected",
      detector_version: "intent-rule-engine-v1",
      ruleset_version: status === "pending" ? null : "intent-rules-2026-08-03-v1",
      input_code_points: status === "pending" ? null : 32,
      input_truncated: false,
      duration_ms: status === "pending" ? null : 2,
      completed_at: status === "pending" ? null : "2026-08-03T12:00:00Z",
      ...overrides,
    }),
  }, auth);
}

test("PAGE-097 renders all semantic states and offers one explicit bounded detail refresh", () => {
  const expectations = {
    disabled: "未启用",
    pending: "待完成（best-effort）",
    clean: "未检出风险",
    detected: "已检测（旁路观察）",
    blocked: "Provider 前已阻断",
    failed: "分类失败",
    skipped: "未调度",
  };
  for (const [status, label] of Object.entries(expectations)) {
    const html = renderIntentRiskStatus(status);
    assert.match(html, new RegExp(`data-intent-risk-status="${status}"`));
    assert.match(html, new RegExp(label.replace(/[()]/g, "\\$&")));
    assert.match(html, /data-request-log-detail-refresh="req_gateway_069"/);
    assert.match(html, /刷新详情/);
    assert.match(html, /request_id[\s\S]*req_gateway_069/);
  }
  assert.match(renderIntentRiskStatus("pending"), /进程内 best-effort 队列/);
  assert.match(renderIntentRiskStatus("detected"), /模型请求未因此被阻断/);
  assert.doesNotMatch(renderIntentRiskStatus("detected"), /Provider 前已阻断/);
  assert.match(renderIntentRiskStatus("blocked"), /Provider 调用前阻断/);
});

test("PAGE-097 escapes fixed-enum drift and main uses command refresh without polling", async () => {
  const unsafe = '<img src=x onerror="alert(1)">';
  const html = renderIntentRiskStatus("failed", {
    reason_code: unsafe,
    detector_version: unsafe,
  });
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.doesNotMatch(html, /<img src=x/);

  const mainSource = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");
  const clientSource = await readFile(new URL("../../src/lib/intentSecurity.js", import.meta.url), "utf8");
  const stateSource = await readFile(new URL("../../src/features/intent-security/intentSecurityPageState.js", import.meta.url), "utf8");
  assert.match(mainSource, /textarea\.value = "";[\s\S]*intentSecurityPageState\.begin\(submitOwner\)/);
  assert.match(mainSource, /clearIntentSecuritySessionState\(\)/);
  assert.match(mainSource, /error\.status === 401/);
  assert.match(mainSource, /error\.status === 403/);
  assert.equal(mainSource.match(/classifyIntent\(/g)?.length, 1);
  assert.match(mainSource, /data-request-log-detail-refresh/);
  assert.match(mainSource, /configureCurrentRequestLogState\(requestLogPageMemory\.query\(\)\.filters, selectedRequestId\)/);
  const refreshHandlerStart = mainSource.indexOf("const requestLogDetailRefresh");
  const refreshHandlerEnd = mainSource.indexOf(
    'for (const button of document.querySelectorAll("[data-request-log-explain]"))',
    refreshHandlerStart,
  );
  const refreshHandler = mainSource.slice(refreshHandlerStart, refreshHandlerEnd);
  assert.ok(refreshHandlerStart > 0 && refreshHandlerEnd > refreshHandlerStart);
  assert.doesNotMatch(refreshHandler, /setInterval|setTimeout|while\s*\(/);
  assert.doesNotMatch(clientSource, /["']X-(?:Request|Trace)-ID["']/i);
  assert.doesNotMatch(`${clientSource}\n${stateSource}`, /(?:localStorage|sessionStorage|indexedDB)\s*\./i);
  assert.doesNotMatch(stateSource, /\b(?:text|prompt)\s*:/i);
});
