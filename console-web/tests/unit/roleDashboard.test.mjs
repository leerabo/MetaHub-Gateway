// PRD-065 前端专项覆盖 client、完整 owner、视图切换、partial、DOM 白名单、事件和响应式布局。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  createRoleDashboardPageState,
  roleDashboardIdentitySnapshot,
  roleDashboardNavigationOwned,
  sanitizeRoleDashboardPayload,
} from "../../src/features/dashboard/roleDashboardPageState.js";
import { renderRoleDashboardPanel } from "../../src/features/dashboard/renderRoleDashboardPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { recordConsolePageView } from "../../src/lib/consoleShell.js";
import { getDashboardRoleView } from "../../src/lib/dashboard.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 敏感测试标记只用于证明未知 Provider Key、Prompt 和 Response 字段不会进入状态、DOM 或事件。
const SENSITIVE_CANARY = "sk-prd065-sensitive-canary";

/** 构造带 tenant、Session、revision 和授权指纹的真实 PAGE-092 测试 owner，角色名称不能隐式补权。 */
function dashboardAuth(options = {}) {
  // 测试身份也必须显式签发权限快照，避免 fixture 因 admin 角色文本绕过真实 owner 约束。
  return withPermissionSnapshot({
    user: {
      tenant_id: options.tenantId ?? "tenant_dashboard_1",
      user_id: options.userId ?? "usr_dashboard_1",
      email: "admin@example.com",
      role: options.role ?? "admin",
    },
    session: {
      session_id: options.sessionId ?? "ses_dashboard_1",
      session_revision: options.sessionRevision ?? 1,
    },
  }, options.permissions ?? [
    PERMISSIONS.DASHBOARD_READ,
    PERMISSIONS.WORKSPACE_CONSOLE_OVERVIEW_READ,
  ], {
    permissionRevision: options.permissionRevision ?? 11,
    authorizationFingerprint: options.fingerprint ?? "dashboard_fingerprint_11",
  });
}

/** 构造同轮后端授权的 overview 导航；额外导航用于验证卡片命令双门禁。 */
function dashboardShell(extraItems = []) {
  return {
    state: "ok",
    activeNavigationItemId: "overview",
    navigation: {
      active_navigation_item_id: "overview",
      navigation_items: [
        {
          id: "overview",
          label: "概览",
          group: "总览",
          target_section: "overview",
          required_permissions: [PERMISSIONS.DASHBOARD_READ],
          visible: true,
          disabled: false,
        },
        ...extraItems,
      ],
    },
  };
}

/** 构造六卡片角色首页响应；顺序由参数决定，前端不得重排。 */
function rolePayload({
  selectedView = "executive",
  availableViews = ["executive", "finance"],
  cardOrder = ["cost", "stability", "adoption", "provider_health", "budget_risk", "security"],
} = {}) {
  const labels = {
    executive: "经营总览",
    finance: "财务视图",
    security: "安全视图",
    developer: "开发视图",
    operations: "运维视图",
  };
  const cardLabels = {
    cost: "成本 <今日>",
    security: "安全",
    stability: "稳定性",
    adoption: "接入进度",
    provider_health: "Provider 健康",
    budget_risk: "预算风险",
  };
  const targets = {
    cost: "usage",
    security: "dlp-rules",
    stability: "request-logs",
    adoption: "models",
    provider_health: "provider-health",
    budget_risk: "budgets",
  };
  return {
    state: "ok",
    config: {
      role_dashboard_config_id: "rdc_0123456789abcdef0123456789abcdef",
      config_revision: 3,
      default_view_code: availableViews[0],
      selected_view_code: selectedView,
      available_views: availableViews.map((code, index) => ({
        code,
        label: labels[code],
        is_default: index === 0,
        provider_key: SENSITIVE_CANARY,
      })),
      permission_codes: ["secret.permission"],
    },
    window: {
      timezone: "Asia/Shanghai",
      start_at: "2026-08-01T00:00:00+08:00",
      end_at: "2026-08-02T00:00:00+08:00",
      generated_at: "2026-08-01T12:00:00Z",
      tenant_id: "tenant_should_not_render",
    },
    cards: cardOrder.map((code, index) => ({
      code,
      label: cardLabels[code],
      status: index === 1 ? "warning" : "ok",
      primary_value: index + 1.25,
      primary_unit: code === "cost" ? "cny" : "count",
      summary: `${cardLabels[code]}摘要`,
      facts: [{
        code: `${code}_count`,
        label: "安全事实 <script>",
        value: index + 2,
        unit: "count",
        prompt: SENSITIVE_CANARY,
      }],
      target_navigation_item_id: targets[code],
      response: SENSITIVE_CANARY,
    })),
    empty_state: { is_empty: false, title: "", description: "", prompt: SENSITIVE_CANARY },
    request_id: "req_prd065_dashboard",
    trace_id: "trc_prd065_dashboard",
    provider_key: SENSITIVE_CANARY,
  };
}

/** 为状态机生成不含 Token 的当前 owner 投影；Token 仅作为内存等值比较值传入。 */
function owner(identity, requestedView = "", token = "session-token") {
  return { identity, navigation: "overview", requestedView, token };
}

test("role dashboard client sends only backend-issued view and supports abort", async () => {
  let captured;
  const controller = new AbortController();
  globalThis.fetch = async (url, options = {}) => {
    captured = { url: String(url), options };
    return {
      ok: true,
      headers: new Map(),
      async json() { return rolePayload({ selectedView: "finance" }); },
    };
  };

  await getDashboardRoleView({
    controlApiUrl: "http://control.local",
    token: "session-token",
    view: "finance",
    availableViewCodes: ["executive", "finance"],
    signal: controller.signal,
    tenant_id: "tenant_other",
    role: "security_admin",
  });
  const url = new URL(captured.url);
  assert.equal(url.pathname, "/dashboard/role-view");
  assert.deepEqual([...url.searchParams.entries()], [["view", "finance"]]);
  assert.equal(captured.options.headers.Authorization, "Bearer session-token");
  assert.equal(captured.options.signal, controller.signal);
  for (const forbiddenKey of ["tenant_id", "role", "user_id", "start_date", "end_date", "token"]) {
    assert.equal(url.searchParams.has(forbiddenKey), false);
  }

  let forbiddenFetches = 0;
  globalThis.fetch = async () => { forbiddenFetches += 1; };
  await assert.rejects(
    () => getDashboardRoleView({
      controlApiUrl: "http://control.local",
      token: "session-token",
      view: "security",
      availableViewCodes: ["executive", "finance"],
    }),
    (error) => error instanceof AuthApiError
      && error.status === 422
      && error.code === "dashboard.query_invalid",
  );
  await assert.rejects(
    () => getDashboardRoleView({
      controlApiUrl: "http://control.local",
      token: "session-token",
      view: { toString: () => "finance" },
      availableViewCodes: ["finance"],
    }),
    (error) => error instanceof AuthApiError && error.status === 422,
  );
  assert.equal(forbiddenFetches, 0);
});

test("default role dashboard request has no query and maps unified errors", async () => {
  let capturedUrl = "";
  globalThis.fetch = async (url) => {
    capturedUrl = String(url);
    return {
      ok: false,
      status: 503,
      headers: new Map([["X-Request-ID", "req_role_header"]]),
      async json() {
        return {
          code: "dashboard.unavailable",
          message: "角色首页暂不可用",
          request_id: "req_role_error",
          trace_id: "trc_role_error",
        };
      },
    };
  };
  await assert.rejects(
    () => getDashboardRoleView({ controlApiUrl: "http://control.local", token: "token" }),
    (error) => error instanceof AuthApiError
      && error.status === 503
      && error.requestId === "req_role_error",
  );
  assert.equal(capturedUrl, "http://control.local/dashboard/role-view");
});

test("authorization identity and navigation owner fail closed on every boundary", () => {
  const base = dashboardAuth();
  const identity = roleDashboardIdentitySnapshot(base);
  assert.ok(identity);
  for (const changed of [
    dashboardAuth({ tenantId: "tenant_dashboard_2" }),
    dashboardAuth({ sessionId: "ses_dashboard_2" }),
    dashboardAuth({ permissionRevision: 12 }),
    dashboardAuth({ fingerprint: "dashboard_fingerprint_12" }),
  ]) {
    assert.notEqual(roleDashboardIdentitySnapshot(changed), identity);
  }
  assert.equal(roleDashboardIdentitySnapshot(dashboardAuth({ permissions: [] })), "");
  assert.equal(roleDashboardNavigationOwned(dashboardShell(), {}), true);
  assert.equal(roleDashboardNavigationOwned(dashboardShell(), { rejected: true }), false);
  assert.equal(roleDashboardNavigationOwned(dashboardShell(), { value: "usage" }), false);
  assert.equal(roleDashboardNavigationOwned({ ...dashboardShell(), activeNavigationItemId: "usage" }), false);
});

test("response projection drops unknown sensitive fields and preserves server card order", () => {
  const payload = rolePayload({
    cardOrder: ["security", "provider_health", "stability", "budget_risk", "adoption", "cost"],
  });
  const sanitized = sanitizeRoleDashboardPayload(payload);
  assert.ok(sanitized);
  assert.deepEqual(sanitized.cards.map((card) => card.code), [
    "security", "provider_health", "stability", "budget_risk", "adoption", "cost",
  ]);
  assert.doesNotMatch(JSON.stringify(sanitized), new RegExp(SENSITIVE_CANARY));
  assert.equal("tenant_id" in sanitized.window, false);
  assert.equal("permission_codes" in sanitized.config, false);
});

test("response projection accepts realtime revocation fallback when configured default is hidden", () => {
  const payload = rolePayload({ selectedView: "executive", availableViews: ["executive"] });
  payload.config.default_view_code = "finance";
  payload.config.available_views[0].is_default = false;
  const sanitized = sanitizeRoleDashboardPayload(payload);
  assert.ok(sanitized);
  assert.equal(sanitized.config.default_view_code, "finance");
  assert.equal(sanitized.config.selected_view_code, "executive");
  assert.deepEqual(sanitized.config.available_views, [
    { code: "executive", label: "经营总览", is_default: false },
  ]);
});

test("response projection rejects coerced values and contradictory empty states", () => {
  const malformedPayloads = [];
  const revisionString = structuredClone(rolePayload());
  revisionString.config.config_revision = "3";
  malformedPayloads.push(revisionString);
  const primaryBoolean = structuredClone(rolePayload());
  primaryBoolean.cards[0].primary_value = true;
  malformedPayloads.push(primaryBoolean);
  const factString = structuredClone(rolePayload());
  factString.cards[0].facts[0].value = "2";
  malformedPayloads.push(factString);
  const labelObject = structuredClone(rolePayload());
  labelObject.cards[0].label = { text: "成本" };
  malformedPayloads.push(labelObject);
  const windowObject = structuredClone(rolePayload());
  windowObject.window.start_at = { iso: "2026-08-01T00:00:00+08:00" };
  malformedPayloads.push(windowObject);
  const emptyStringBoolean = structuredClone(rolePayload());
  emptyStringBoolean.empty_state.is_empty = "false";
  malformedPayloads.push(emptyStringBoolean);
  const missingEmptyTitle = structuredClone(rolePayload());
  delete missingEmptyTitle.empty_state.title;
  malformedPayloads.push(missingEmptyTitle);
  const nullEmptyDescription = structuredClone(rolePayload());
  nullEmptyDescription.empty_state.description = null;
  malformedPayloads.push(nullEmptyDescription);
  const contradictoryNonEmptyState = structuredClone(rolePayload());
  contradictoryNonEmptyState.empty_state.title = "不应出现的空态标题";
  malformedPayloads.push(contradictoryNonEmptyState);
  const contradictoryEmptyState = structuredClone(rolePayload());
  contradictoryEmptyState.empty_state = { is_empty: true, title: "", description: "" };
  malformedPayloads.push(contradictoryEmptyState);
  for (const payload of malformedPayloads) {
    assert.equal(sanitizeRoleDashboardPayload(payload), null);
  }
});

test("state rejects late owners, clears immediately on switch, and limits 503 partial to same view", () => {
  const identity = roleDashboardIdentitySnapshot(dashboardAuth());
  const state = createRoleDashboardPageState();
  const first = state.begin({ ...owner(identity), availableViewCodes: [], source: "sidebar" });
  assert.ok(first);
  const accepted = state.accept(first, rolePayload(), owner(identity));
  assert.equal(accepted.state, "ok");
  assert.equal(state.selectedView(), "executive");

  const refreshTicket = state.begin({
    ...owner(identity, "executive"),
    availableViewCodes: state.availableViewCodes(),
    source: "refresh",
  });
  const partial = state.reject(refreshTicket, {
    status: 503,
    code: "dashboard.unavailable",
    message: "暂不可用",
    requestId: "req_partial",
  }, owner(identity, "executive"));
  assert.equal(partial.state, "partial");
  assert.equal(partial.cards.length, 6);

  const switchTicket = state.begin({
    ...owner(identity, "finance"),
    availableViewCodes: state.availableViewCodes(),
    source: "view_switch",
    transition: true,
  });
  assert.equal(state.snapshot().state, "loading");
  assert.equal("cards" in state.snapshot(), false);
  const deniedPartial = state.reject(switchTicket, {
    status: 503,
    code: "dashboard.unavailable",
    message: "暂不可用",
    requestId: "req_switch_down",
  }, owner(identity, "finance"));
  assert.equal(deniedPartial.state, "error");
  assert.equal("cards" in deniedPartial, false);

  const old = state.begin({ ...owner(identity), availableViewCodes: [] });
  const newer = state.begin({ ...owner(identity), availableViewCodes: [] });
  assert.equal(old.controller.signal.aborted, true);
  assert.equal(state.owns(old, owner(identity)), false);
  assert.equal(state.owns(newer, owner(identity)), true);
});

test("401 and 403 clear protected cards while a later 200 recovers", () => {
  const identity = roleDashboardIdentitySnapshot(dashboardAuth());
  for (const status of [401, 403]) {
    const state = createRoleDashboardPageState();
    const initial = state.begin({ ...owner(identity), availableViewCodes: [] });
    state.accept(initial, rolePayload(), owner(identity));
    const ticket = state.begin({
      ...owner(identity, "executive"),
      availableViewCodes: state.availableViewCodes(),
    });
    const rejected = state.reject(ticket, {
      status,
      code: status === 401 ? "auth.token_invalid" : "auth.permission_denied",
      message: "访问被拒绝",
      requestId: `req_${status}`,
    }, owner(identity, "executive"));
    assert.equal(rejected.state, "error");
    assert.equal(state.selectedView(), "");
    assert.deepEqual(state.availableViewCodes(), []);
    assert.equal("cards" in rejected, false);

    const recovery = state.begin({ ...owner(identity), availableViewCodes: [] });
    const recovered = state.accept(recovery, rolePayload(), owner(identity));
    assert.equal(recovered.state, "ok");
    assert.equal(recovered.cards.length, 6);
  }
});

test("renderer escapes API text, preserves card order, and gates navigation against backend shell", () => {
  const sanitized = sanitizeRoleDashboardPayload(rolePayload({
    cardOrder: ["security", "cost", "stability", "adoption", "provider_health", "budget_risk"],
  }));
  const shell = dashboardShell([{
    id: "usage",
    label: "用量",
    group: "成本",
    target_section: "usage",
    visible: true,
    disabled: false,
  }]);
  const html = renderRoleDashboardPanel({ state: "ok", ...sanitized }, dashboardAuth(), shell);
  assert.match(html, /角色首页/);
  assert.match(html, /data-role-dashboard-view-option="executive"/);
  assert.ok(html.indexOf('data-role-dashboard-card="security"') < html.indexOf('data-role-dashboard-card="cost"'));
  assert.match(html, /成本 &lt;今日&gt;/);
  assert.match(html, /安全事实 &lt;script&gt;/);
  assert.match(html, /data-console-nav="usage"/);
  assert.doesNotMatch(html, /data-console-nav="provider-health"/);
  assert.doesNotMatch(html, new RegExp(SENSITIVE_CANARY));
  assert.doesNotMatch(html, /<script>/);
});

test("renderer clears protected commands for errors and marks same-view partial", () => {
  const errorHtml = renderRoleDashboardPanel({
    state: "error",
    message: "权限 <拒绝>",
    requestId: "req_<403>",
  }, dashboardAuth(), dashboardShell());
  assert.match(errorHtml, /权限 &lt;拒绝&gt;/);
  assert.doesNotMatch(errorHtml, /data-role-dashboard-view-option|data-role-dashboard-card|href=/);

  const sanitized = sanitizeRoleDashboardPayload(rolePayload());
  const partialHtml = renderRoleDashboardPanel({
    state: "partial",
    ...sanitized,
    error: { requestId: "req_partial" },
  }, dashboardAuth(), dashboardShell());
  assert.match(partialHtml, /最近一次安全快照/);
  assert.doesNotMatch(partialHtml, /req_partial/);
  assert.match(partialHtml, /data-role-dashboard-card="cost"/);
});

test("developer shell uses PAGE-092 and does not render the legacy dashboard or health preview", () => {
  const container = { innerHTML: "" };
  const sanitized = sanitizeRoleDashboardPayload(rolePayload());
  renderDeveloperStart(container, {
    authContext: dashboardAuth(),
    consoleShellState: dashboardShell(),
    roleDashboardState: { state: "ok", ...sanitized },
  });
  assert.match(container.innerHTML, /workspace-console-experience-panel/);
  assert.match(container.innerHTML, /id="console-main-title"/);
  assert.doesNotMatch(container.innerHTML, /data-role-dashboard-root|角色首页|总览 Dashboard|Top 模型|provider-health-dashboard/);
});

test("view event is claimed once per generation and a user switch opens a new claim", () => {
  const identity = roleDashboardIdentitySnapshot(dashboardAuth());
  const state = createRoleDashboardPageState();
  const initial = state.begin({
    ...owner(identity),
    availableViewCodes: [],
    source: "sidebar",
  });
  state.accept(initial, rolePayload(), owner(identity));
  // DOM 投影前同视图自动刷新完成时，事件仍应保留真实 sidebar 进入来源。
  const automatic = state.begin({
    ...owner(identity, "executive"),
    availableViewCodes: state.availableViewCodes(),
    source: "refresh",
  });
  state.accept(automatic, rolePayload(), owner(identity, "executive"));
  assert.equal(state.claimRenderedEvent({
    ...owner(identity, "executive", "rotated-token"),
    selectedView: "executive",
    configRevision: 3,
  }), null);
  const firstEvent = state.claimRenderedEvent({
    ...owner(identity, "executive"),
    selectedView: "executive",
    configRevision: 3,
  });
  assert.equal(firstEvent.source, "sidebar");
  state.finishEvent(firstEvent);
  assert.equal(state.claimRenderedEvent({
    ...owner(identity, "executive"),
    selectedView: "executive",
    configRevision: 3,
  }), null);

  const switched = state.begin({
    ...owner(identity, "finance"),
    availableViewCodes: state.availableViewCodes(),
    source: "view_switch",
    transition: true,
  });
  state.accept(switched, rolePayload({ selectedView: "finance" }), owner(identity, "finance"));
  const secondEvent = state.claimRenderedEvent({
    ...owner(identity, "finance"),
    selectedView: "finance",
    configRevision: 3,
  });
  assert.equal(secondEvent.source, "view_switch");
  assert.equal(secondEvent.selectedView, "finance");
});

test("dashboard.role_viewed event sends the exact low-risk contract", async () => {
  let body;
  globalThis.fetch = async (_url, options) => {
    body = JSON.parse(options.body);
    return {
      ok: true,
      headers: new Map(),
      async json() { return { accepted: true, event_id: "evt_role", request_id: "req_event", trace_id: "trc_event" }; },
    };
  };
  await recordConsolePageView({
    controlApiUrl: "http://control.local",
    token: "session-token",
    navigationItemId: "overview",
    targetSection: "overview",
    path: "/#overview",
    eventType: "dashboard.role_viewed",
    payload: {
      source: "view_switch",
      hash: "#overview",
      viewport_class: "mobile",
      view_code: "finance",
      config_revision: 3,
    },
  });
  assert.deepEqual(body, {
    event_type: "dashboard.role_viewed",
    navigation_item_id: "overview",
    target_section: "overview",
    path: "/#overview",
    payload: {
      source: "view_switch",
      hash: "#overview",
      viewport_class: "mobile",
      view_code: "finance",
      config_revision: 3,
    },
  });
  assert.deepEqual(Object.keys(body.payload).sort(), [
    "config_revision", "hash", "source", "view_code", "viewport_class",
  ]);
  assert.doesNotMatch(JSON.stringify(body), new RegExp(SENSITIVE_CANARY));
});

test("PAGE-092 source and styles forbid persistence, legacy prefetch, overflow and unstable tracks", async () => {
  const [stateSource, mainSource, developerSource, css] = await Promise.all([
    readFile(new URL("../../src/features/dashboard/roleDashboardPageState.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/features/developer-start/renderDeveloperStart.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/console-experience.css", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(stateSource, /localStorage|sessionStorage|updateUserPreferences|updateConsolePreferences/);
  assert.doesNotMatch(mainSource, /getDashboardOverview|dashboard\.viewed|listProviderHealth/);
  assert.doesNotMatch(developerSource, /renderDashboardPanel|renderProviderHealthDashboardCard/);
  assert.doesNotMatch(mainSource, /getDashboardRoleView/);
  assert.match(mainSource, /case "overview":[\s\S]*loadWorkspaceConsoleExperienceState\([\s\S]*"overview"/);
  assert.match(developerSource, /renderWorkspaceConsoleExperience\(options\.workspaceConsoleExperienceState, "overview"(?:, [^)]*)?\)/);
  assert.match(css, /\.console-experience-metrics\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)/s);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.console-experience-metrics,[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s);
  assert.match(css, /\.console-experience-metrics span,[\s\S]*overflow-wrap:\s*anywhere/s);
});
