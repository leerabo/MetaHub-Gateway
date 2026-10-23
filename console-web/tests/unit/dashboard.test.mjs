// 本单测覆盖 MVP Dashboard 前端能力，确保聚合摘要、空状态、错误映射和敏感字段展示边界稳定。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDashboardPanel } from "../../src/features/dashboard/renderDashboardPanel.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { getDashboardOverview } from "../../src/lib/dashboard.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造 Dashboard 显式授权快照；日志跳转权限可独立授予或撤销。 */
function dashboardContext({ role = "admin", canOpenLogs = true, baseContext = {} } = {}) {
  return withPermissionSnapshot(
    { ...baseContext, user: { ...(baseContext.user ?? {}), role } },
    [
      PERMISSIONS.DASHBOARD_READ,
      PERMISSIONS.WORKSPACE_CONSOLE_OVERVIEW_READ,
      ...(canOpenLogs ? [PERMISSIONS.REQUEST_LOG_READ] : []),
    ],
  );
}

const overviewPayload = {
  window: {
    timezone: "Asia/Shanghai",
    start_at: "2026-07-10T00:00:00+08:00",
    end_at: "2026-07-11T00:00:00+08:00",
    generated_at: "2026-07-10T12:00:00Z",
  },
  metrics: [
    {
      id: "today_requests",
      label: "今日请求",
      value: 3,
      unit: "count",
      status: "ok",
      description: "来自 request_logs",
    },
    {
      id: "success_rate",
      label: "成功率",
      value: 66.67,
      unit: "percent",
      status: "critical",
      description: "成功请求占比",
    },
    {
      id: "p95_latency_ms",
      label: "P95 延迟",
      value: 400,
      unit: "ms",
      status: "ok",
      description: "今日 P95",
    },
    {
      id: "today_cost_cny",
      label: "今日成本",
      value: 1.25,
      unit: "cny",
      status: "ok",
      description: "来自 usage_ledger",
    },
  ],
  top_models: [
    {
      model_id: "model_1",
      resolved_model: "deepseek-<script>",
      provider_code: "local_openai",
      request_count: 3,
      success_count: 2,
      total_tokens: 450,
      cost_cny: 1.25,
    },
  ],
  top_virtual_keys: [
    {
      virtual_key_id: "vk_1",
      virtual_key_name: "开发 Key",
      virtual_key_prefix: "mhv_dev_****",
      request_count: 3,
      success_count: 2,
      total_tokens: 450,
      cost_cny: 1.25,
    },
  ],
  recent_errors: [
    {
      request_id: "req_prd026_failed",
      trace_id: "trc_prd026_failed",
      error_code: "gateway.<bad>",
      diagnosis_title: "上游 <script>限流</script>",
      model: "deepseek-chat",
      provider_code: "local_openai",
      latency_ms: 120,
      created_at: "2026-07-10T11:59:00Z",
    },
  ],
  empty_state: {
    is_empty: false,
    title: "",
    description: "",
    recommended_actions: [],
  },
  request_id: "req_dashboard",
  trace_id: "trc_dashboard",
};

const emptyPayload = {
  ...overviewPayload,
  metrics: [
    {
      id: "today_requests",
      label: "今日请求",
      value: 0,
      unit: "count",
      status: "empty",
      description: "今天还没有日志",
    },
  ],
  top_models: [],
  top_virtual_keys: [],
  recent_errors: [],
  empty_state: {
    is_empty: true,
    title: "今天还没有调用数据",
    description: "完成一次调用后即可看到真实指标。",
    recommended_actions: [
      { label: "注册 Provider", target: "providers", description: "接入模型厂商。" },
      { label: "进入调试台", target: "debug-console", description: "发起一次测试调用。" },
    ],
  },
};

// PRD-065 后生产 overview 使用角色首页；旧 overviewPayload 仍用于兼容 client/renderer 的直接合同测试。
const roleOverviewPayload = {
  state: "ok",
  config: {
    role_dashboard_config_id: "rdc_0123456789abcdef0123456789abcdef",
    config_revision: 1,
    default_view_code: "executive",
    selected_view_code: "executive",
    available_views: [{ code: "executive", label: "经营总览", is_default: true }],
  },
  window: overviewPayload.window,
  cards: ["cost", "stability", "adoption", "provider_health", "budget_risk", "security"].map((code) => ({
    code,
    label: code === "cost" ? "成本" : code,
    status: "ok",
    primary_value: 1,
    primary_unit: "count",
    summary: "安全聚合摘要",
    facts: [],
    target_navigation_item_id: null,
  })),
  empty_state: { is_empty: false, title: "", description: "" },
  request_id: "req_role_dashboard",
  trace_id: "trc_role_dashboard",
};

test("dashboard client reads overview without tenant or date filters", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_header"]]),
      async json() {
        // Dashboard 只读当前登录租户聚合摘要，前端不能通过查询串扩大租户或时间窗口。
        return overviewPayload;
      },
    };
  };

  const overview = await getDashboardOverview({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
  });

  assert.equal(overview.state, "ok");
  assert.equal(overview.metrics[0].id, "today_requests");
  assert.equal(calls[0].options.headers.Authorization, "Bearer jwt-token");
  assert.match(calls[0].url, /\/dashboard\/overview$/);
  assert.doesNotMatch(calls[0].url, /tenant_id=/);
  assert.doesNotMatch(calls[0].url, /start_date=/);
  assert.doesNotMatch(calls[0].url, /end_date=/);
});

test("dashboard client returns structured unified errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "dashboard.unavailable",
        message: "Dashboard is temporarily unavailable.",
        request_id: "req_dashboard_down",
        trace_id: "trc_dashboard_down",
        suggestion: "Retry later.",
      };
    },
  });

  await assert.rejects(
    () => getDashboardOverview({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 503);
      assert.equal(error.code, "dashboard.unavailable");
      assert.equal(error.requestId, "req_dashboard_down");
      return true;
    },
  );
});

test("dashboard panel renders metrics rankings and escaped recent errors", () => {
  const html = renderDashboardPanel(
    { state: "ok", ...overviewPayload },
    dashboardContext(),
  );

  assert.match(html, /总览 Dashboard/);
  assert.match(html, /今日请求/);
  assert.match(html, /66.67%/);
  assert.match(html, /400 ms/);
  assert.match(html, /¥1.25/);
  assert.match(html, /deepseek-&lt;script&gt;/);
  assert.match(html, /mhv_dev_\*\*\*\*/);
  assert.doesNotMatch(html, /gateway.&lt;bad&gt;/);
  assert.match(html, /上游 &lt;script&gt;限流&lt;\/script&gt;/);
  assert.match(html, /data-request-id="req_prd026_failed"/);
  assert.doesNotMatch(html, /<script>限流<\/script>/);
  assert.doesNotMatch(html, /sk-dashboard-secret/);
});

test("dashboard panel renders empty state and hides log link for viewer", () => {
  const emptyHtml = renderDashboardPanel(
    { state: "ok", ...emptyPayload },
    dashboardContext({ role: "viewer", canOpenLogs: false }),
  );
  const viewerErrorHtml = renderDashboardPanel(
    { state: "ok", ...overviewPayload },
    dashboardContext({ role: "viewer", canOpenLogs: false }),
  );

  assert.match(emptyHtml, /今天还没有调用数据/);
  assert.match(emptyHtml, /data-console-nav="providers"/);
  assert.match(emptyHtml, /data-console-nav="debug-console"/);
  assert.doesNotMatch(emptyHtml, /data-request-id=/);
  assert.doesNotMatch(viewerErrorHtml, /req_prd026_failed/);
  assert.doesNotMatch(viewerErrorHtml, /data-request-id="req_prd026_failed"/);
});

test("developer start overview renders PAGE-092 role dashboard state", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    authContext: dashboardContext({
      baseContext: {
        user: { display_name: "Admin", email: "admin@example.com" },
        tenant: { name: "Default" },
      },
    }),
    consoleShellState: {
      activeNavigationItemId: "overview",
      navigation: {
        active_navigation_item_id: "overview",
        navigation_items: [{
          id: "overview",
          label: "概览",
          group: "总览",
          target_section: "overview",
          description: "租户聚合指标",
          empty_state_hint: "暂无数据",
          order: 10,
        }],
      },
    },
    roleDashboardState: roleOverviewPayload,
  });

  assert.match(container.innerHTML, /workspace-console-experience-panel/);
  assert.match(container.innerHTML, /id="console-main-title"/);
  assert.doesNotMatch(container.innerHTML, /dashboard-overview-shell|角色首页|data-role-dashboard-card|Top 模型|req_prd026_failed/);
});

test("dashboard styles keep nested facts inside the 900px sidebar layout", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");

  // 中窄视口的 Console 仍保留侧栏；嵌套网格必须显式允许收缩，长模型名也不能扩大根节点。
  assert.match(
    css,
    /\.dashboard-overview-shell,[\s\S]*?\.dashboard-error-row dl \{\s*min-width: 0;/,
  );
  // 指标卡直接子项的默认最小内容宽度会撑破六列网格，因此必须在同一规则块内同时释放宽度并允许断行。
  const metricChildren = css.match(/\.dashboard-metric\s*>\s*\*\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.match(metricChildren, /(?:^|\s)min-width:\s*0\s*;/);
  assert.match(metricChildren, /(?:^|\s)overflow-wrap:\s*anywhere\s*;/);
  assert.match(
    css,
    /\.dashboard-list-row strong,[\s\S]*?overflow-wrap: anywhere;/,
  );
  // 720px 以下已有单列覆盖；这里锁定 1100px 以下的两列中间态，避免四列最小宽度再次撑破 900px。
  assert.match(
    css,
    /@media \(max-width: 1100px\) \{[\s\S]*?\.dashboard-list-row dl,[\s\S]*?grid-template-columns: repeat\(2, minmax\(72px, 1fr\)\);/,
  );
});
