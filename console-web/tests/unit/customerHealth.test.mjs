/** PRD-086 专项冻结严格客户端、租户 owner、请求生命周期、渲染器和 Shell 接线。 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  createCustomerHealthPageState,
  customerHealthPageOwner,
  customerHealthReadIdentitySnapshot,
} from "../../src/features/customer-health/customerHealthPageState.js";
import { renderCustomerHealth } from "../../src/features/customer-health/renderCustomerHealth.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { createI18n } from "../../src/i18n/messages.js";
import {
  CUSTOMER_HEALTH_SIGNAL_CODES,
  getCustomerHealth,
  normalizeCustomerHealth,
} from "../../src/lib/customerHealth.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 测试租户和六信号权重固定合同口径，所有跨租户断言均从该基线派生。
const TENANT_ID = "tenant_health_a";
const SIGNAL_WEIGHTS = [2500, 2500, 1500, 1000, 1500, 1000];

/** 构造带权限修订和授权指纹的完整认证上下文，避免只按角色模拟授权。 */
function context(tenantId = TENANT_ID, permissions = [PERMISSIONS.CUSTOMER_HEALTH_READ], options = {}) {
  // tenant、user 与 Session 必须共同进入授权快照，确保身份漂移测试覆盖完整 owner。
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, name: "Health tenant" },
    user: {
      tenant_id: tenantId,
      user_id: options.userId ?? `usr_${tenantId}`,
      role: options.role ?? "ops_admin",
      email: "health@example.test",
    },
    session: {
      session_id: options.sessionId ?? `ses_${tenantId}`,
      session_revision: options.sessionRevision ?? 1,
    },
  }, permissions, {
    permissionRevision: options.permissionRevision ?? 21,
    authorizationFingerprint: options.fingerprint ?? `health_authz_${tenantId}`,
  });
}

/** 构造后端导航镜像条目，用于验证读取权限与目标区段必须精确一致。 */
function navigationItem(id, requiredPermissions) {
  return {
    id,
    target_section: id,
    label: id,
    group: "总览",
    visible: true,
    disabled: false,
    required_permissions: requiredPermissions,
  };
}

/** 构造 Shell 导航状态，并允许测试精确覆盖修订、指纹和可见性字段。 */
function shell(auth = context(), overrides = {}) {
  const items = overrides.items ?? [navigationItem("customer-health", [PERMISSIONS.CUSTOMER_HEALTH_READ])];
  return {
    state: "ok",
    activeNavigationItemId: "customer-health",
    navigation: {
      active_navigation_item_id: "customer-health",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: items,
      ...(overrides.navigation ?? {}),
    },
    ...(overrides.shell ?? {}),
  };
}

/** 生成健康租户的六信号基线，其中工单信号始终保持明确未接入。 */
function baseSignals() {
  return [
    signal("usage_trend", 2500, "available", "healthy", 10000, 100, 100, 10000, "request_log_aggregate"),
    signal("reliability", 2500, "available", "healthy", 10000, 100, 100, 10000, "request_log_aggregate"),
    signal("cost_anomaly_health", 1500, "available", "healthy", 10000, 0, 0, null, "cost_anomaly_aggregate"),
    signal("budget_readiness", 1000, "available", "healthy", 10000, 1, null, null, "budget_rule_aggregate"),
    signal("team_activity", 1500, "available", "healthy", 10000, 2, 2, 10000, "user_activity_aggregate"),
    signal("ticket_health", 1000, "unavailable", "unknown", null, null, null, null, "support_ticket_unconfigured"),
  ];
}

/** 构造单个冻结信号 DTO，贡献值随后由整数公式统一复算。 */
function signal(code, weight, availability, healthStatus, score, current, comparison, rate, sourceKind) {
  return {
    code,
    weight_bps: weight,
    availability,
    health_status: healthStatus,
    score_bps: score,
    contribution_bps: 0,
    current_value: current,
    comparison_value: comparison,
    rate_bps: rate,
    source_kind: sourceKind,
  };
}

/** 构造严格客户健康响应，并从信号重算总分、风险与建议顺序。 */
function payload({ signals = baseSignals(), tags = [], actionTargets = {}, overrides = {} } = {}) {
  const finalized = finalizeContributions(signals.map((item) => ({ ...item })));
  const actionByTag = {
    usage_declining: "restore_gateway_activity",
    high_error_rate: "review_failed_requests",
    cost_anomaly_open: "review_cost_anomalies",
    budget_missing: "configure_tenant_budget",
    team_inactive: "reengage_team",
  };
  const defaultTargets = {
    restore_gateway_activity: "request-logs",
    review_failed_requests: "request-logs",
    review_cost_anomalies: "cost-anomalies",
    configure_tenant_budget: "budgets",
    reengage_team: "users",
    connect_support_ticket_source: null,
  };
  const codes = [...tags.map((tag) => actionByTag[tag]), "connect_support_ticket_source"];
  return {
    tenant_id: TENANT_ID,
    customer_health_score_id: "chs_0123456789abcdef0123456789abcdef",
    score_version: "customer-health-v1",
    score_bps: finalized.score,
    risk_level: expectedRisk(finalized.score, finalized.signals),
    risk_tags: tags,
    missing_signals: ["ticket_health"],
    effective_weight_bps: 9000,
    freshness_status: "fresh",
    recommended_actions: codes.map((code) => ({
      code,
      target_navigation_item_id: Object.hasOwn(actionTargets, code)
        ? actionTargets[code]
        : defaultTargets[code],
    })),
    window: {
      start: "2026-08-10T08:00:00Z",
      end: "2026-08-17T08:00:00Z",
      comparison_start: "2026-08-03T08:00:00Z",
      comparison_end: "2026-08-10T08:00:00Z",
      timezone: "UTC",
    },
    signals: finalized.signals,
    activation: { completed_stage_count: 4, total_stage_count: 7, activated: false },
    evaluated_at: "2026-08-17T08:01:00Z",
    generated_at: "2026-08-17T08:03:00Z",
    request_id: "req_health_a",
    trace_id: "trc_health_a",
    ...overrides,
  };
}

/** 用整数最大余数法填充贡献值，测试实现独立复现公开评分合同。 */
function finalizeContributions(signals) {
  const denominator = 9000n;
  const rows = signals
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => item.score_bps !== null)
    .map(({ item, index }) => {
      const raw = BigInt(item.score_bps) * BigInt(item.weight_bps);
      return { index, floor: Number(raw / denominator), remainder: raw % denominator, raw };
    });
  const score = Number((rows.reduce((sum, row) => sum + row.raw, 0n) + 4500n) / denominator);
  for (const row of rows) signals[row.index].contribution_bps = row.floor;
  let remainder = score - rows.reduce((sum, row) => sum + row.floor, 0);
  rows.sort((left, right) => (
    left.remainder === right.remainder ? left.index - right.index : left.remainder > right.remainder ? -1 : 1
  ));
  for (let index = 0; index < remainder; index += 1) signals[rows[index].index].contribution_bps += 1;
  return { score, signals };
}

/** 从总分与三项风险下限计算预期等级，验证服务端等级不能掩盖失败事实。 */
function expectedRisk(score, signals) {
  const levels = ["healthy", "watch", "at_risk", "critical"];
  const scoreLevel = score >= 8000 ? "healthy" : score >= 6000 ? "watch" : score >= 3000 ? "at_risk" : "critical";
  let rank = levels.indexOf(scoreLevel);
  // 当前用量归零时风险至少为 at_risk，即使其他信号仍为健康。
  if (signals[0].current_value === 0) rank = Math.max(rank, 2);
  if (signals[1].comparison_value >= 5 && signals[1].rate_bps !== null) {
    const errorRate = 10000 - signals[1].rate_bps;
    // 有足够样本时分别固定百分之十和百分之五失败率的风险下限。
    if (errorRate >= 1000) rank = Math.max(rank, 2);
    else if (errorRate >= 500) rank = Math.max(rank, 1);
  }
  if (signals[2].comparison_value > 0) rank = 3;
  else if (signals[2].current_value > 0) rank = Math.max(rank, 1);
  return levels[rank];
}

/** 构造高失败率租户响应，用于验证风险下限与建议导航白名单。 */
function highErrorPayload(options = {}) {
  const signals = baseSignals();
  signals[1] = signal("reliability", 2500, "available", "critical", 0, 0, 10, 0, "request_log_aggregate");
  return payload({
    signals,
    tags: ["high_error_rate"],
    actionTargets: options.actionTargets ?? {},
  });
}

/** 构造缺少用量和团队样本的响应，固定无样本与缺预算组合语义。 */
function noSamplesPayload() {
  const signals = baseSignals();
  signals[0] = signal("usage_trend", 2500, "no_samples", "critical", 0, 0, 0, null, "request_log_aggregate");
  signals[1] = signal("reliability", 2500, "no_samples", "unknown", 5000, 0, 0, null, "request_log_aggregate");
  signals[3] = signal("budget_readiness", 1000, "available", "at_risk", 3000, 0, null, null, "budget_rule_aggregate");
  signals[4] = signal("team_activity", 1500, "no_samples", "critical", 0, 0, 0, null, "user_activity_aggregate");
  return payload({ signals, tags: ["usage_declining", "budget_missing", "team_inactive"] });
}

/** 构造头体 request_id、trace_id 一致且禁止缓存的真实 Response。 */
function jsonResponse(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store, private",
      "X-Request-ID": value.request_id,
      "X-Trace-ID": value.trace_id,
      ...headers,
    },
  });
}

test("customer_health.read and exact backend navigation are the only PAGE-118 owner", () => {
  assert.equal(PERMISSIONS.CUSTOMER_HEALTH_READ, "customer_health.read");
  const auth = context();
  const owner = customerHealthPageOwner(auth, shell(auth));
  assert.equal(owner.tenantId, TENANT_ID);
  assert.match(customerHealthReadIdentitySnapshot(auth), /customer_health\.read/);
  const roleOnly = context(TENANT_ID, [], { role: "admin", fingerprint: "role_only" });
  assert.equal(customerHealthPageOwner(roleOnly, shell(roleOnly)), null);
  assert.equal(customerHealthPageOwner(auth, shell(auth, {
    items: [navigationItem("customer-health", [PERMISSIONS.DASHBOARD_READ])],
  })), null);
  assert.equal(customerHealthPageOwner(auth, shell(auth, {
    navigation: { permission_revision: 20 },
  })), null);
  assert.equal(customerHealthPageOwner(auth, shell(auth, {
    shell: { activeNavigationItemId: "overview" },
  })), null);
});

test("GET is query-free, body-free, no-store, and validates header/body linkage", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(payload());
  };
  const controller = new AbortController();
  const result = await getCustomerHealth({
    controlApiUrl: "http://control.local",
    token: "fixture-session-token",
    tenantId: TENANT_ID,
    signal: controller.signal,
  });
  assert.equal(result.signals.length, 6);
  assert.equal(calls[0].url, "http://control.local/customer-health");
  assert.equal(new URL(calls[0].url).search, "");
  assert.equal(calls[0].options.method, undefined);
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.cache, "no-store");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.signal, controller.signal);
  assert.deepEqual(calls[0].options.headers, {
    Authorization: "Bearer fixture-session-token",
    Accept: "application/json",
  });
  globalThis.fetch = async () => jsonResponse(payload(), 200, { "X-Request-ID": "req_other" });
  await assert.rejects(
    getCustomerHealth({ controlApiUrl: "http://control.local", token: "token", tenantId: TENANT_ID }),
    (error) => error.code === "customer_health.response_invalid" && error.status === 502,
  );
});

test("normalizer recomputes six signals, largest remainders, risk floors, tags, and action order", () => {
  const healthy = normalizeCustomerHealth(payload(), TENANT_ID);
  assert.ok(healthy);
  assert.deepEqual(healthy.signals.map((item) => item.code), CUSTOMER_HEALTH_SIGNAL_CODES);
  assert.deepEqual(healthy.signals.map((item) => item.weight_bps), SIGNAL_WEIGHTS);
  assert.deepEqual(healthy.signals.map((item) => item.contribution_bps), [2778, 2778, 1667, 1111, 1666, 0]);
  assert.equal(healthy.score_bps, 10000);
  assert.equal(healthy.risk_level, "healthy");
  assert.deepEqual(healthy.missing_signals, ["ticket_health"]);

  const highError = normalizeCustomerHealth(highErrorPayload(), TENANT_ID);
  assert.ok(highError);
  assert.equal(highError.risk_level, "at_risk");
  assert.deepEqual(highError.risk_tags, ["high_error_rate"]);
  assert.deepEqual(highError.recommended_actions.map((item) => item.code), [
    "review_failed_requests",
    "connect_support_ticket_source",
  ]);

  const empty = normalizeCustomerHealth(noSamplesPayload(), TENANT_ID);
  assert.ok(empty);
  assert.equal(empty.signals[0].availability, "no_samples");
  assert.equal(empty.signals[1].health_status, "unknown");
  assert.equal(empty.signals[5].score_bps, null);

  const nonIntegralUsageSignals = baseSignals();
  nonIntegralUsageSignals[0] = signal(
    "usage_trend", 2500, "available", "at_risk", 4761,
    10, 21, 4761, "request_log_aggregate",
  );
  const nonIntegralUsage = payload({
    signals: nonIntegralUsageSignals,
    tags: ["usage_declining"],
  });
  assert.ok(normalizeCustomerHealth(nonIntegralUsage, TENANT_ID));
  nonIntegralUsage.signals[0].rate_bps = 4762;
  nonIntegralUsage.signals[0].score_bps = 4762;
  assert.equal(normalizeCustomerHealth(nonIntegralUsage, TENANT_ID), null);
});

test("normalizer rejects extra/sensitive fields and every drifted formula or nullable boundary", () => {
  assert.equal(normalizeCustomerHealth(payload({ overrides: { prompt: "must-not-survive" } }), TENANT_ID), null);
  assert.equal(normalizeCustomerHealth(payload({ overrides: { tenant_id: "tenant_other" } }), TENANT_ID), null);

  const wrongOrder = payload();
  [wrongOrder.signals[0], wrongOrder.signals[1]] = [wrongOrder.signals[1], wrongOrder.signals[0]];
  assert.equal(normalizeCustomerHealth(wrongOrder, TENANT_ID), null);

  const wrongContribution = payload();
  wrongContribution.signals[0].contribution_bps -= 1;
  assert.equal(normalizeCustomerHealth(wrongContribution, TENANT_ID), null);

  const fakeTicket = payload();
  fakeTicket.signals[5] = { ...fakeTicket.signals[5], availability: "available", score_bps: 10000 };
  assert.equal(normalizeCustomerHealth(fakeTicket, TENANT_ID), null);

  const badAction = highErrorPayload();
  badAction.recommended_actions[0].target_navigation_item_id = "budgets";
  assert.equal(normalizeCustomerHealth(badAction, TENANT_ID), null);

  const futureWindow = payload({ overrides: { generated_at: "2026-08-17T07:59:59Z" } });
  assert.equal(normalizeCustomerHealth(futureWindow, TENANT_ID), null);

  const staleMismatch = payload({ overrides: { generated_at: "2026-08-17T11:00:01Z" } });
  assert.equal(normalizeCustomerHealth(staleMismatch, TENANT_ID), null);
  assert.ok(normalizeCustomerHealth(payload({ overrides: {
    generated_at: "2026-08-17T11:00:01Z",
    freshness_status: "stale",
  } }), TENANT_ID));
});

test("large safe counts use exact BigInt ratio arithmetic instead of overflowing Number products", () => {
  const signals = baseSignals();
  signals[0] = signal(
    "usage_trend", 2500, "available", "healthy", 10000,
    922337203685477, 922337203685477, 10000, "request_log_aggregate",
  );
  assert.ok(normalizeCustomerHealth(payload({ signals }), TENANT_ID));
  signals[0].current_value += 1;
  assert.equal(normalizeCustomerHealth(payload({ signals }), TENANT_ID), null);
});

test("unified not-ready 503 is redacted while owner abort remains an AbortError", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const errorPayload = {
    code: "customer_health.not_ready",
    message: "private backend detail",
    request_id: "req_not_ready",
    trace_id: "trc_not_ready",
    suggestion: "private suggestion",
    diagnosis: {
      rule_id: "rule", category: "availability", reason: "fixed", suggestion: "fixed",
      action: "retry", severity: "warning", retryable: true, matched_by: "code", safe_summary: "fixed",
    },
  };
  globalThis.fetch = async () => jsonResponse(errorPayload, 503);
  await assert.rejects(
    getCustomerHealth({ controlApiUrl: "http://control.local", token: "token", tenantId: TENANT_ID }),
    (error) => error.code === "customer_health.not_ready"
      && error.status === 503
      && error.requestId === "req_not_ready"
      && !String(error.message).includes("private"),
  );
  globalThis.fetch = async (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  });
  const controller = new AbortController();
  const pending = getCustomerHealth({
    controlApiUrl: "http://control.local", token: "token", tenantId: TENANT_ID, signal: controller.signal,
  });
  controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
});

test("page state clears DOM before abort and drops late 200/401/403/503 across owners and generations", () => {
  let observedSignal;
  const clearStates = [];
  const state = createCustomerHealthPageState({
    clearDom() { clearStates.push(observedSignal?.aborted ?? null); },
  });
  const authA = context();
  const ownerA = customerHealthPageOwner(authA, shell(authA));
  assert.equal(state.enter(ownerA), true);
  const ticket200 = state.beginLoad(ownerA, 1);
  observedSignal = ticket200.controller.signal;

  const ticket401 = state.beginLoad(ownerA, 2);
  assert.equal(ticket200.controller.signal.aborted, true);
  assert.equal(state.accept(ticket200, payload(), ownerA, 1), false);
  assert.equal(state.reject(ticket200, { code: "auth.token_expired", requestId: "req_old_401" }, ownerA, 1), false);

  const authB = context("tenant_health_b", [PERMISSIONS.CUSTOMER_HEALTH_READ], { fingerprint: "health_b" });
  const ownerB = customerHealthPageOwner(authB, shell(authB));
  observedSignal = ticket401.controller.signal;
  assert.equal(state.enter(ownerB), true);
  assert.equal(clearStates.at(-1), false);
  assert.equal(ticket401.controller.signal.aborted, true);
  assert.equal(state.reject(ticket401, { code: "auth.permission_denied", requestId: "req_old_403" }, ownerA, 2), false);
  assert.equal(state.reject(ticket401, { code: "customer_health.unavailable", requestId: "req_old_503" }, ownerA, 2), false);

  const current = state.beginLoad(ownerB, 3);
  const tenantBPayload = payload({ overrides: { tenant_id: "tenant_health_b" } });
  assert.equal(state.accept(current, tenantBPayload, ownerB, 3), true);
  assert.equal(state.snapshot().state, "ready");
  const current503 = state.beginLoad(ownerB, 4);
  assert.equal(state.reject(current503, { code: "customer_health.not_ready", requestId: "req_current" }, ownerB, 4), true);
  assert.deepEqual(state.snapshot().error, { code: "customer_health.not_ready", requestId: "req_current" });
});

test("renderer shows safe aggregates and action links only after the second navigation RBAC gate", () => {
  const permissions = [PERMISSIONS.CUSTOMER_HEALTH_READ, PERMISSIONS.REQUEST_LOG_READ];
  const auth = context(TENANT_ID, permissions);
  const items = [
    navigationItem("customer-health", [PERMISSIONS.CUSTOMER_HEALTH_READ]),
    navigationItem("request-logs", [PERMISSIONS.REQUEST_LOG_READ]),
  ];
  const nav = shell(auth, { items });
  const data = normalizeCustomerHealth(highErrorPayload(), TENANT_ID);
  const html = renderCustomerHealth({ state: "ready", data, error: null }, auth, nav, createI18n("en-US"));
  assert.match(html, /Customer health/);
  assert.match(html, /At risk/);
  assert.match(html, /10/);
  assert.match(html, /href="#request-logs"/);
  assert.match(html, /Connect a support ticket source/);
  assert.doesNotMatch(html, /tenant_health_a|fixture-session-token|provider.?key|prompt|response|health@example/i);

  const withoutTarget = shell(auth, { items: [items[0]] });
  const plain = renderCustomerHealth({ state: "ready", data, error: null }, auth, withoutTarget, createI18n("en-US"));
  assert.doesNotMatch(plain, /href="#request-logs"/);
  assert.match(plain, /Review failed requests/);

  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: auth,
    consoleShellState: nav,
    customerHealthState: { state: "ready", data, error: null },
    i18n: createI18n("en-US"),
  });
  assert.match(container.innerHTML, /class="panel customer-health-panel"/);
  assert.match(container.innerHTML, /data-active-navigation="customer-health"/);
});

test("loading, no-samples, stale, escaped error, and unauthorized zero-DOM states are complete", () => {
  const auth = context();
  const nav = shell(auth);
  assert.match(renderCustomerHealth(
    { state: "loading", data: null, error: null }, auth, nav, createI18n("zh-CN"),
  ), /正在读取客户健康评分/);
  const initial = normalizeCustomerHealth(noSamplesPayload(), TENANT_ID);
  assert.match(renderCustomerHealth(
    { state: "ready", data: initial, error: null }, auth, nav, createI18n("zh-CN"),
  ), /暂无样本/);
  const stale = normalizeCustomerHealth(payload({ overrides: {
    generated_at: "2026-08-17T11:00:01Z", freshness_status: "stale",
  } }), TENANT_ID);
  assert.match(renderCustomerHealth(
    { state: "ready", data: stale, error: null }, auth, nav, createI18n("zh-CN"),
  ), /最后一份可信评分/);
  const errorHtml = renderCustomerHealth({
    state: "error", data: null, error: { code: "customer_health.<unsafe>", requestId: "req_<unsafe>" },
  }, auth, nav, createI18n("en-US"));
  assert.match(errorHtml, /customer_health\.&lt;unsafe&gt;/);
  assert.match(errorHtml, /req_&lt;unsafe&gt;/);
  assert.doesNotMatch(errorHtml, /<unsafe>/);
  const roleOnly = context(TENANT_ID, [], { role: "admin", fingerprint: "role_only_render" });
  assert.equal(renderCustomerHealth(
    { state: "ready", data: initial, error: null }, roleOnly, shell(roleOnly), createI18n("zh-CN"),
  ), "");
});
