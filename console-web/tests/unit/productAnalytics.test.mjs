/** PRD-085 PAGE-117 客户端、授权 owner、生命周期、渲染与接线的精确边界测试。 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  createProductAnalyticsPageState,
  productAnalyticsPageOwner,
  productAnalyticsReadIdentitySnapshot,
} from "../../src/features/product-analytics/productAnalyticsPageState.js";
import { renderProductAnalytics } from "../../src/features/product-analytics/renderProductAnalytics.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { createI18n } from "../../src/i18n/messages.js";
import {
  getProductAnalyticsFunnel,
  normalizeProductAnalyticsFunnel,
  PRODUCT_ANALYTICS_STAGE_TYPES,
} from "../../src/lib/productAnalytics.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 测试租户只用于验证前端 owner 与响应归属，任何请求都不得把它拼入 query 或正文。
const TENANT_ID = "tenant_growth_a";

/** 构造权限 revision、fingerprint 与双份租户镜像一致的认证上下文。 */
function context(tenantId = TENANT_ID, permissions = [PERMISSIONS.PRODUCT_ANALYTICS_READ], options = {}) {
  return withPermissionSnapshot({
    tenant: { tenant_id: tenantId, name: "Growth tenant" },
    user: {
      tenant_id: tenantId,
      user_id: options.userId ?? "usr_growth_a",
      role: options.role ?? "ops_admin",
      email: "growth@example.test",
    },
    session: {
      session_id: options.sessionId ?? `ses_${tenantId}`,
      session_revision: options.sessionRevision ?? 1,
    },
  }, permissions, {
    permissionRevision: options.permissionRevision ?? 20,
    authorizationFingerprint: options.fingerprint ?? `growth_authz_${tenantId}`,
  });
}

/** 构造后端确认的 PAGE-117 导航快照，用于验证前端不能凭角色名自行授权。 */
function shell(auth = context(), overrides = {}) {
  const item = {
    id: "growth-analytics",
    target_section: "growth-analytics",
    label: "增长分析",
    group: "总览",
    visible: true,
    disabled: false,
    required_permissions: [PERMISSIONS.PRODUCT_ANALYTICS_READ],
    ...(overrides.item ?? {}),
  };
  return {
    state: "ok",
    activeNavigationItemId: "growth-analytics",
    navigation: {
      active_navigation_item_id: "growth-analytics",
      permission_codes: auth.permission_codes,
      permission_revision: auth.permission_revision,
      authorization_fingerprint: auth.authorization_fingerprint,
      navigation_items: [item],
      ...(overrides.navigation ?? {}),
    },
    ...overrides.shell,
  };
}

/** 构造满足七阶段计数、累计转化和 UTC 时间不变量的权威响应信封。 */
function payload({ reached = Array(7).fill(true), counts = Array(7).fill(1), overrides = {} } = {}) {
  let prefix = true;
  const stages = PRODUCT_ANALYTICS_STAGE_TYPES.map((eventType, index) => {
    const stageReached = reached[index];
    const cumulative = prefix && stageReached;
    const previousCumulative = index === 0 ? 1 : Number(prefix);
    const conversion = previousCumulative === 0 ? null : (cumulative ? 10000 : 0);
    prefix = cumulative;
    return {
      order: index + 1,
      event_type: eventType,
      event_count: stageReached ? counts[index] : 0,
      reached: stageReached,
      cumulative_reached: cumulative,
      reached_tenant_count: stageReached ? 1 : 0,
      cumulative_tenant_count: cumulative ? 1 : 0,
      conversion_from_previous_bps: conversion,
      first_occurred_at: stageReached ? `2026-08-${String(index + 10).padStart(2, "0")}T00:00:00Z` : null,
      last_occurred_at: stageReached ? `2026-08-${String(index + 10).padStart(2, "0")}T01:00:00Z` : null,
    };
  });
  const completed = reached.filter(Boolean).length;
  let cumulative = 0;
  for (const value of reached) {
    if (!value) break;
    cumulative += 1;
  }
  return {
    tenant_id: TENANT_ID,
    window: { kind: "all_time", timezone: "UTC" },
    cohort_tenant_count: 1,
    stages,
    summary: {
      completed_stage_count: completed,
      cumulative_stage_count: cumulative,
      total_stage_count: 7,
      completion_bps: Math.floor((completed * 10000) / 7 + 0.5),
      activated: completed === 7,
    },
    generated_at: "2026-08-17T02:00:00Z",
    request_id: "req_growth_a",
    trace_id: "trc_growth_a",
    ...overrides,
  };
}

/** 构造头体链路一致且明确 no-store 的 Fetch 响应替身。 */
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

test("product analytics capability and backend navigation form the only PAGE-117 owner", () => {
  assert.equal(PERMISSIONS.PRODUCT_ANALYTICS_READ, "product_analytics.read");
  const auth = context();
  const owner = productAnalyticsPageOwner(auth, shell(auth));
  assert.ok(owner);
  assert.equal(owner.tenantId, TENANT_ID);
  assert.match(productAnalyticsReadIdentitySnapshot(auth), /product_analytics\.read/);

  const roleOnly = context(TENANT_ID, [], { role: "admin", fingerprint: "role_only" });
  assert.equal(productAnalyticsPageOwner(roleOnly, shell(roleOnly)), null);
  assert.equal(productAnalyticsPageOwner(auth, shell(auth, {
    item: { required_permissions: [PERMISSIONS.DASHBOARD_READ] },
  })), null);
  assert.equal(productAnalyticsPageOwner(auth, shell(auth, {
    navigation: { permission_revision: 19 },
  })), null);
  assert.equal(productAnalyticsPageOwner(auth, shell(auth, {
    shell: { activeNavigationItemId: "overview" },
  })), null);
});

test("GET uses an exact query-free wire and validates the header/body boundary", async (t) => {
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse(payload());
  };

  const controller = new AbortController();
  const result = await getProductAnalyticsFunnel({
    controlApiUrl: "http://control.local",
    token: "fixture-session-token",
    tenantId: TENANT_ID,
    signal: controller.signal,
  });
  assert.equal(result.stages.length, 7);
  assert.equal(calls[0].url, "http://control.local/product-analytics/funnel");
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
    getProductAnalyticsFunnel({ controlApiUrl: "http://control.local", token: "token", tenantId: TENANT_ID }),
    (error) => error.code === "product_analytics.response_invalid" && error.status === 502,
  );
});

test("normalizer rejects unknown or sensitive fields, tenant drift, order drift, and aggregate drift", () => {
  assert.ok(normalizeProductAnalyticsFunnel(payload(), TENANT_ID));
  assert.equal(normalizeProductAnalyticsFunnel(payload({
    overrides: { prompt: "must-not-survive" },
  }), TENANT_ID), null);
  assert.equal(normalizeProductAnalyticsFunnel(payload({
    overrides: { tenant_id: "tenant_other" },
  }), TENANT_ID), null);

  const wrongOrder = payload();
  wrongOrder.stages[2] = { ...wrongOrder.stages[2], event_type: "response.body" };
  assert.equal(normalizeProductAnalyticsFunnel(wrongOrder, TENANT_ID), null);

  const unknownStage = payload();
  unknownStage.stages[0] = { ...unknownStage.stages[0], token: "must-not-survive" };
  assert.equal(normalizeProductAnalyticsFunnel(unknownStage, TENANT_ID), null);

  const badSummary = payload();
  badSummary.summary = { ...badSummary.summary, completion_bps: 9999 };
  assert.equal(normalizeProductAnalyticsFunnel(badSummary, TENANT_ID), null);

  const badTime = payload();
  badTime.stages[0] = { ...badTime.stages[0], first_occurred_at: null };
  assert.equal(normalizeProductAnalyticsFunnel(badTime, TENANT_ID), null);

  const impossibleDate = payload();
  impossibleDate.generated_at = "2026-02-30T00:00:00Z";
  assert.equal(normalizeProductAnalyticsFunnel(impossibleDate, TENANT_ID), null);

  const reversedMicroseconds = payload();
  reversedMicroseconds.stages[0] = {
    ...reversedMicroseconds.stages[0],
    first_occurred_at: "2026-08-10T00:00:00.000002Z",
    last_occurred_at: "2026-08-10T00:00:00.000001Z",
  };
  assert.equal(normalizeProductAnalyticsFunnel(reversedMicroseconds, TENANT_ID), null);
});

test("normalizer accepts an out-of-order stage only with the frozen cumulative and null conversion semantics", () => {
  const reached = [true, true, false, true, true, false, true];
  const normalized = normalizeProductAnalyticsFunnel(payload({ reached }), TENANT_ID);
  assert.ok(normalized);
  assert.equal(normalized.stages[2].conversion_from_previous_bps, 0);
  assert.equal(normalized.stages[3].reached, true);
  assert.equal(normalized.stages[3].cumulative_reached, false);
  assert.equal(normalized.stages[3].conversion_from_previous_bps, null);
  assert.equal(normalized.summary.completed_stage_count, 5);
  assert.equal(normalized.summary.cumulative_stage_count, 2);
});

test("page state aborts old generations and rejects stale tenant or refresh callbacks", () => {
  let observedSignal;
  let clearSawAborted;
  const state = createProductAnalyticsPageState({
    clearDom() { clearSawAborted = observedSignal?.aborted; },
  });
  const authA = context();
  const ownerA = productAnalyticsPageOwner(authA, shell(authA));
  assert.equal(state.enter(ownerA), true);
  const ticketA = state.beginLoad(ownerA, 4);
  observedSignal = ticketA.controller.signal;
  assert.equal(state.isCurrent(ticketA, ownerA, 5), false);

  const authB = context("tenant_growth_b", [PERMISSIONS.PRODUCT_ANALYTICS_READ], {
    userId: "usr_growth_b",
    fingerprint: "growth_authz_b",
  });
  const ownerB = productAnalyticsPageOwner(authB, shell(authB));
  assert.equal(state.enter(ownerB), true);
  assert.equal(clearSawAborted, false);
  assert.equal(ticketA.controller.signal.aborted, true);
  assert.equal(state.accept(ticketA, payload(), ownerA, 4), false);

  const ticketB = state.beginLoad(ownerB, 5);
  assert.equal(state.accept(ticketB, payload(), ownerB, 5), false);
  assert.equal(state.snapshot().data, null);
  state.clear();
  assert.equal(ticketB.controller.signal.aborted, true);
});

test("renderer uses backend totals and UTC values, shows gap state, and emits zero DOM without capability", () => {
  const auth = context();
  const navigation = shell(auth);
  const reached = [true, true, false, true, true, false, true];
  const data = normalizeProductAnalyticsFunnel(payload({
    reached,
    counts: [1, 4, 0, 1, 3, 0, 9],
  }), TENANT_ID);
  const html = renderProductAnalytics(
    { state: "ready", data, error: null },
    auth,
    navigation,
    createI18n("en-US"),
  );
  assert.match(html, /Growth analytics/);
  assert.match(html, /5 \/ 7/);
  assert.match(html, /71\.43%/);
  assert.match(html, /Occurred with an earlier gap/);
  assert.match(html, /Cumulative path interrupted/);
  assert.match(html, /2026-08-10 00:00:00 UTC/);
  assert.match(html, /data-product-analytics-refresh/);
  assert.doesNotMatch(html, /tenant_growth_a|actor|provider_key|fixture-session-token|prompt|response/i);

  const roleOnly = context(TENANT_ID, [], { role: "admin", fingerprint: "role_only_render" });
  assert.equal(renderProductAnalytics(
    { state: "ready", data, error: null },
    roleOnly,
    shell(roleOnly),
    createI18n("zh-CN"),
  ), "");

  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: auth,
    consoleShellState: navigation,
    productAnalyticsState: { state: "ready", data, error: null },
    i18n: createI18n("en-US"),
  });
  assert.match(container.innerHTML, /class="panel growth-analytics-panel"/);
  assert.match(container.innerHTML, /data-active-navigation="growth-analytics"/);
});

test("loading, registration-only, and escaped fixed error states are complete", () => {
  const auth = context();
  const navigation = shell(auth);
  assert.match(renderProductAnalytics(
    { state: "loading", data: null, error: null }, auth, navigation, createI18n("zh-CN"),
  ), /正在读取激活漏斗/);

  const initial = normalizeProductAnalyticsFunnel(payload({
    reached: [true, false, false, false, false, false, false],
  }), TENANT_ID);
  assert.match(renderProductAnalytics(
    { state: "ready", data: initial, error: null }, auth, navigation, createI18n("zh-CN"),
  ), /租户已注册/);

  const errorHtml = renderProductAnalytics({
    state: "error",
    data: null,
    error: { code: "product_analytics.<unsafe>", requestId: "req_<unsafe>" },
  }, auth, navigation, createI18n("en-US"));
  assert.match(errorHtml, /product_analytics\.&lt;unsafe&gt;/);
  assert.match(errorHtml, /req_&lt;unsafe&gt;/);
  assert.doesNotMatch(errorHtml, /<unsafe>/);
});

test("main and responsive CSS wire all PAGE-117 lifecycle boundaries without tenant query state", async () => {
  const [main, renderer, css, messages] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
    readFile(new URL("../../src/i18n/messages.js", import.meta.url), "utf8"),
  ]);
  assert.match(main, /pagehide[\s\S]*productAnalyticsPageState\.clear\(\)/);
  assert.match(main, /function renderLogin[\s\S]*productAnalyticsPageState\.clear\(\)/);
  assert.match(main, /logoutButton[\s\S]*productAnalyticsPageState\.clear\(\)/);
  assert.match(main, /clearProductAnalyticsStateOnIdentityChange\(authSession\.context, context\)/);
  assert.match(main, /activeNavigationItemId !== "growth-analytics"[\s\S]*productAnalyticsPageState\.clear\(\)/);
  assert.match(main, /getProductAnalyticsFunnel\(\{[\s\S]{0,400}tenantId: owner\.tenantId[\s\S]{0,400}signal: operation\.controller\.signal/);
  assert.doesNotMatch(main, /getProductAnalyticsFunnel\(\{[\s\S]{0,500}(actor|fromTime|toTime|query)/);
  assert.match(renderer, /case "growth-analytics"/);
  assert.match(messages, /"nav\.growth-analytics"/);
  assert.match(messages, /"product_analytics\.stage\.request_logs_viewed"/);
  assert.match(css, /\.product-analytics-steps\s*\{[\s\S]*grid-template-columns: repeat\(7, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 620px\)[\s\S]*\.product-analytics-steps[\s\S]*grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /\.product-analytics-row > \*[\s\S]*overflow-wrap: anywhere/);
});
