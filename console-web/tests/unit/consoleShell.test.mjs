// 本单测覆盖控制台骨架与导航：客户端不得传 tenant_id，渲染必须按后端导航项展示并转义用户输入。
import assert from "node:assert/strict";
import test from "node:test";

import {
  bindNavigationHashRefresh,
  getConsoleNavigation,
  parseNavigationHash,
  recordConsolePageView,
  resolveVisibleNavigation,
  updateConsolePreferences,
} from "../../src/lib/consoleShell.js";
import {
  activeNavigationItem,
  localizeConsoleGroup,
  navigationItemsFromState,
  renderConsoleShell,
} from "../../src/features/console-shell/renderConsoleShell.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { createI18n } from "../../src/i18n/messages.js";
import { renderWorkspaceOverview } from "../../src/features/workspace-console-experience/renderWorkspaceOverview.js";

test("console shell client sends navigation and preference requests without tenant id", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_console"]]),
      async json() {
        if (String(url).endsWith("/console/events")) {
          // 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
          return { accepted: true, event_id: "evt_1", request_id: "req_event", trace_id: "trc_event" };
        }
        if (String(url).endsWith("/console/preferences")) {
          return {
            preference: { active_navigation_item_id: "usage", sidebar_collapsed: false },
            request_id: "req_pref",
            trace_id: "trc_pref",
          };
        }
        return {
          navigation_items: [],
          active_navigation_item_id: "usage",
          request_id: "req_nav",
          trace_id: "trc_nav",
        };
      },
    };
  };

  await getConsoleNavigation({ controlApiUrl: "http://control.local", token: "jwt-token", activeItemId: "usage" });
  await updateConsolePreferences({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    activeNavigationItemId: "usage",
    sidebarCollapsed: true,
  });
  await recordConsolePageView({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    navigationItemId: "usage",
    targetSection: "usage",
    path: "/#usage",
    payload: { source: "sidebar" },
  });

  assert.equal(calls[0].url, "http://control.local/console/navigation?active_item_id=usage");
  assert.equal(calls[1].options.headers.Authorization, "Bearer jwt-token");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    active_navigation_item_id: "usage",
    sidebar_collapsed: true,
  });
  assert.equal(JSON.stringify(calls).includes("tenant_id"), false);
  assert.equal(JSON.stringify(calls).includes("jwt-token"), true);
});

test("console shell client maps unified errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 422,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "console.event_invalid",
        message: "Console event payload is invalid.",
        request_id: "req_event",
        trace_id: "trc_event",
      };
    },
  });

  await assert.rejects(
    () =>
      recordConsolePageView({
        controlApiUrl: "http://control.local",
        token: "jwt-token",
        navigationItemId: "usage",
        targetSection: "usage",
        path: "/#usage",
      }),
    (error) => {
      assert.equal(error instanceof AuthApiError, true);
      assert.equal(error.code, "console.event_invalid");
      assert.equal(error.requestId, "req_event");
      return true;
    },
  );
});

test("console preference update forwards cancellation ownership to fetch", async () => {
  const controller = new AbortController();
  let observedSignal = null;
  globalThis.fetch = async (_url, options = {}) => {
    observedSignal = options.signal;
    return {
      ok: true,
      headers: new Map(),
      async json() {
        return { preference: { sidebar_collapsed: true } };
      },
    };
  };

  await updateConsolePreferences({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    sidebarCollapsed: true,
    signal: controller.signal,
  });
  assert.equal(observedSignal, controller.signal);
});

test("navigation hash is resolved only from backend visible items", () => {
  const navigation = {
    active_navigation_item_id: "competitor-comparison",
    navigation_items: [
      { id: "overview", visible: true, disabled: false },
      { id: "competitor-comparison", visible: true, disabled: false },
      { id: "usage", visible: false, disabled: false },
    ],
  };

  assert.deepEqual(resolveVisibleNavigation(navigation, "competitor-comparison"), {
    activeItemId: "competitor-comparison",
    rejectedRequestedItem: false,
  });
  assert.deepEqual(resolveVisibleNavigation(navigation, "usage"), {
    activeItemId: "overview",
    rejectedRequestedItem: true,
  });
  assert.deepEqual(resolveVisibleNavigation(navigation), {
    activeItemId: "competitor-comparison",
    rejectedRequestedItem: false,
  });
  assert.deepEqual(resolveVisibleNavigation(navigation, undefined, { requestedRejected: true }), {
    activeItemId: "overview",
    rejectedRequestedItem: true,
  });
});

test("viewer manual request-log hash triggers backend navigation revalidation and sanitization", () => {
  let hashChangeListener = null;
  let refreshCount = 0;
  const target = {
    /** 保存应用注册的 hashchange 回调，模拟浏览器同页锚点导航。 */
    addEventListener(type, listener) {
      if (type === "hashchange") hashChangeListener = listener;
    },
  };
  const navigation = {
    active_navigation_item_id: "overview",
    navigation_items: [{ id: "overview", visible: true, disabled: false }],
  };
  assert.equal(bindNavigationHashRefresh(target, () => { refreshCount += 1; }), true);
  assert.equal(typeof hashChangeListener, "function");
  hashChangeListener();
  assert.equal(refreshCount, 1);
  const hashState = parseNavigationHash("#request-logs");
  assert.deepEqual(
    resolveVisibleNavigation(navigation, hashState.value, { requestedRejected: hashState.rejected }),
    { activeItemId: "overview", rejectedRequestedItem: true },
  );
  assert.equal(bindNavigationHashRefresh({}, () => {}), false);
});

test("feedback report direct hash is accepted only when backend navigation includes PAGE-071", () => {
  const developerNavigation = {
    active_navigation_item_id: "request-logs",
    navigation_items: [
      { id: "request-logs", visible: true, disabled: false },
      { id: "feedback-summary", visible: true, disabled: false },
    ],
  };
  const viewerNavigation = {
    active_navigation_item_id: "overview",
    navigation_items: [{ id: "overview", visible: true, disabled: false }],
  };

  assert.deepEqual(resolveVisibleNavigation(developerNavigation, "feedback-summary"), {
    activeItemId: "feedback-summary",
    rejectedRequestedItem: false,
  });
  assert.deepEqual(resolveVisibleNavigation(viewerNavigation, "feedback-summary"), {
    activeItemId: "overview",
    rejectedRequestedItem: true,
  });
});

test("growth analytics direct hash is accepted only when backend navigation includes PAGE-117", () => {
  const authorizedNavigation = {
    active_navigation_item_id: "overview",
    navigation_items: [
      { id: "overview", visible: true, disabled: false },
      { id: "growth-analytics", visible: true, disabled: false },
    ],
  };
  const restrictedNavigation = {
    active_navigation_item_id: "overview",
    navigation_items: [{ id: "overview", visible: true, disabled: false }],
  };

  assert.deepEqual(resolveVisibleNavigation(authorizedNavigation, "growth-analytics"), {
    activeItemId: "growth-analytics",
    rejectedRequestedItem: false,
  });
  assert.deepEqual(resolveVisibleNavigation(restrictedNavigation, "growth-analytics"), {
    activeItemId: "overview",
    rejectedRequestedItem: true,
  });
});

test("navigation hash parser distinguishes missing malformed and oversized values", () => {
  assert.deepEqual(parseNavigationHash(""), { value: undefined, rejected: false });
  assert.deepEqual(parseNavigationHash("#usage"), { value: "usage", rejected: false });
  assert.deepEqual(parseNavigationHash("#%75sage"), { value: "usage", rejected: false });
  assert.deepEqual(parseNavigationHash("#%E0%A4%A"), { value: undefined, rejected: true });
  assert.deepEqual(parseNavigationHash(`#${"x".repeat(81)}`), { value: undefined, rejected: true });
  assert.deepEqual(parseNavigationHash("#%20"), { value: undefined, rejected: true });
});

test("renderConsoleShell groups navigation and escapes user controlled fields", () => {
  const consoleShellState = {
    navigation: {
      active_navigation_item_id: "providers",
      navigation_items: [
        {
          id: "overview",
          label: "概览",
          group: "总览",
          target_section: "overview",
          description: "Overview",
          empty_state_hint: "Overview hint",
          order: 10,
        },
        {
          id: "providers",
          label: "Provider",
          group: "配置",
          target_section: "providers",
          description: "Provider config",
          empty_state_hint: "Add provider",
          order: 20,
        },
      ],
    },
  };
  const html = renderConsoleShell({
    authContext: {
      user: { display_name: "<Admin>", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
      tenant: { name: "<Tenant>" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    },
    consoleShellState,
    contentHtml: "<section>Trusted content</section>",
  });

  assert.match(html, /data-console-nav="providers"/);
  assert.match(html, /data-sidebar-group-toggle="providers"/);
  assert.match(html, /class="sidebar-group-items"/);
  assert.match(html, /class="sidebar-link active"/);
  assert.doesNotMatch(html, /<Admin>/);
  assert.match(html, /&lt;Admin&gt;/);
  assert.match(html, /&lt;Tenant&gt;/);
  assert.match(html, /Trusted content/);
});

test("renderConsoleShell localizes the advanced navigation group", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "error-maps",
        navigation_items: [{
          id: "error-maps",
          label: "错误映射",
          group: "advanced",
          group_code: "advanced",
          placement: "advanced",
          order: 1,
        }],
      },
    },
    contentHtml: "",
    i18n: createI18n("zh-CN"),
  });
  assert.match(html, /<span class="sidebar-group-label">高级工具<\/span>/u);
  assert.doesNotMatch(html, />advanced<\/span>/u);
});

test("organization admins do not see the platform-only data management entry", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      activeNavigationItemId: "overview",
      navigation: {
        role_code: "admin",
        active_navigation_item_id: "overview",
        navigation_items: [
          { id: "overview", label: "概览", group: "总览", target_section: "overview", order: 1 },
          { id: "tenant-data-lifecycle", label: "数据管理", group: "空间", target_section: "tenant-data-lifecycle", order: 2 },
        ],
      },
    },
    contentHtml: "",
    i18n: createI18n("zh-CN"),
  });
  assert.doesNotMatch(html, /data-console-nav="tenant-data-lifecycle"/u);
  assert.doesNotMatch(html, /数据管理/u);
});

test("localizeConsoleGroup keeps internal group codes stable while localizing display text", () => {
  const zh = createI18n("zh-CN");
  const en = createI18n("en-US");
  assert.equal(localizeConsoleGroup("advanced", zh), "高级工具");
  assert.equal(localizeConsoleGroup("高级工具", zh), "高级工具");
  assert.equal(localizeConsoleGroup("advanced", en), "Advanced tools");
  assert.equal(localizeConsoleGroup("unknown-group", zh), "unknown-group");
});

test("renderConsoleShell restores the persisted collapsed sidebar state", () => {
  const html = renderConsoleShell({
    authContext: {
      user: { display_name: "Admin", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
    },
    consoleShellState: {
      preferences: { preference: { sidebar_collapsed: true } },
      navigation: {
        active_navigation_item_id: "overview",
        navigation_items: [
          { id: "overview", label: "Overview", group: "System", target_section: "overview", order: 1 },
        ],
      },
    },
    contentHtml: "<section>Content</section>",
  });

  assert.match(html, /class="console-shell sidebar-collapsed"/);
  assert.match(html, /data-sidebar-collapsed="true"/);
  assert.match(html, /id="console-sidebar"/);
  assert.match(html, /data-sidebar-toggle/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /展开导航/);
  assert.match(html, /class="sidebar-link-short"/);
});

test("renderConsoleShell defaults every navigation group to collapsed", () => {
  const html = renderConsoleShell({
    authContext: {
      user: { display_name: "Admin", email: "admin@example.com", role: "admin" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "providers",
        navigation_items: [
          { id: "overview", label: "概览", group: "总览", target_section: "overview", order: 1 },
          { id: "providers", label: "Provider", group: "配置", target_section: "providers", order: 2 },
        ],
      },
    },
    contentHtml: "<section>Content</section>",
  });

  assert.match(html, /data-sidebar-group-toggle="overview"[\s\S]*aria-expanded="false"/);
  assert.match(html, /data-sidebar-group-toggle="providers"[\s\S]*aria-expanded="false"/);
  assert.equal((html.match(/class="sidebar-group-items" hidden/g) ?? []).length, 2);
  assert.match(html, /data-console-nav="providers"/);
});

test("renderConsoleShell restores only one expanded navigation group", () => {
  const html = renderConsoleShell({
    authContext: {
      user: { display_name: "Admin", email: "admin@example.com", role: "admin" },
    },
    consoleShellState: {
      sidebarExpandedGroupKey: "overview",
      navigation: {
        active_navigation_item_id: "providers",
        navigation_items: [
          { id: "overview", label: "概览", group: "总览", target_section: "overview", order: 1 },
          { id: "providers", label: "Provider", group: "配置", target_section: "providers", order: 2 },
        ],
      },
    },
    contentHtml: "<section>Content</section>",
  });

  assert.match(html, /data-sidebar-group-toggle="overview"[\s\S]*aria-expanded="true"/);
  assert.match(html, /data-sidebar-group-toggle="providers"[\s\S]*aria-expanded="false"/);
  assert.equal((html.match(/class="sidebar-group-items" hidden/g) ?? []).length, 1);
});

test("renderConsoleShell keeps all home entries together", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      sidebarExpandedGroupKey: "home",
      navigation: {
        active_navigation_item_id: "quickstart",
        navigation_items: [
          { id: "overview", label: "Overview", group: "Home", group_code: "home", order: 1 },
          { id: "assistant", label: "Assistant", group: "Home", group_code: "home", order: 2 },
          { id: "approvals", label: "Approvals", group: "Home", group_code: "home", order: 3 },
          { id: "quickstart", label: "Getting started", group: "Home", group_code: "home", order: 4 },
        ],
      },
    },
    contentHtml: "<section>Content</section>",
  });

  assert.doesNotMatch(html, /class="sidebar-group-more"/u);
  assert.match(html, /data-console-nav="quickstart"[^>]*aria-current="page"/u);
});

test("renderConsoleShell exposes every item in an expanded group", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      sidebarExpandedGroupKey: "gateway",
      navigation: {
        active_navigation_item_id: "providers",
        navigation_items: [
          { id: "overview", label: "概览", group: "首页", group_code: "home", order: 1 },
          { id: "providers", label: "模型服务", group: "Gateway", group_code: "gateway", order: 1 },
          { id: "models", label: "模型目录", group: "Gateway", group_code: "gateway", order: 2 },
          { id: "routing", label: "路由", group: "Gateway", group_code: "gateway", order: 3 },
          { id: "keys", label: "KEY 管理", group: "Gateway", group_code: "gateway", order: 4 },
        ],
      },
    },
    contentHtml: "",
  });

  assert.match(html, /data-sidebar-group="gateway"[^>]*>[\s\S]*data-console-nav="providers"[\s\S]*data-console-nav="models"[\s\S]*data-console-nav="routing"[\s\S]*data-console-nav="keys"/u);
  assert.doesNotMatch(html, /sidebar-group-more|sidebar-deferred-controls|更多控制/u);
  assert.match(html, /data-sidebar-group-toggle="gateway"[^>]*aria-expanded="true"/u);
  assert.match(html, /data-sidebar-group-toggle="home"[^>]*aria-expanded="false"/u);
});

test("organization overview does not show a self-return home link", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      navigation: {
        role_code: "admin",
        active_navigation_item_id: "overview",
        navigation_items: [
          { id: "overview", label: "Overview", group: "Home", group_code: "home", order: 1 },
          { id: "quickstart", label: "Getting started", group: "Home", group_code: "home", order: 2 },
        ],
      },
      activeNavigationItemId: "overview",
      activeRouteCode: "workspace.home.overview",
    },
    contentHtml: "<section>Content</section>",
  });
  assert.doesNotMatch(html, /console-home-return/u);
});

test("overview owns one accessible page title and other pages keep an inline home return", () => {
  const state = {
    activeNavigationItemId: "overview",
    navigation: { role_code: "owner", navigation_items: [
      { id: "overview", label: "概览", group_code: "home", order: 1 },
      { id: "models", label: "模型目录", group_code: "gateway", order: 2 },
    ] },
  };
  const overviewHtml = renderConsoleShell({ consoleShellState: state,
    contentHtml: renderWorkspaceOverview({ status: "ready", overview: { sections: [] } }, { workspaceName: "测试组织" }) });
  assert.equal((overviewHtml.match(/id="console-main-title"/gu) ?? []).length, 1);
  assert.match(overviewHtml, /aria-labelledby="console-main-title"/u);
  assert.match(overviewHtml, /测试组织/u);
  assert.doesNotMatch(overviewHtml, /console-page-context|console-main-heading/u);
  const modelHtml = renderConsoleShell({ consoleShellState: { ...state, activeNavigationItemId: "models" }, contentHtml: "<section>模型列表</section>" });
  assert.match(modelHtml, /<header class="console-main-heading">\s*<a class="console-home-return"/u);
  assert.match(modelHtml, /id="console-main-title">模型目录</u);
  assert.doesNotMatch(modelHtml, /console-page-context/u);
});

test("hidden navigation items stay out of the sidebar", () => {
  const html = renderConsoleShell({
    authContext: { user: { display_name: "Admin", email: "admin@example.com", role: "admin" } },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "usage",
        navigation_items: [
          { id: "usage", label: "Usage", group: "Finance", group_code: "finance", placement: "primary", sidebar_visible: false, order: 1 },
        ],
      },
    },
    contentHtml: "<section>Content</section>",
  });
  assert.doesNotMatch(html, /data-console-nav="usage"|sidebar-deferred-controls|data-sidebar-deferred-group/u);
});

test("renderDeveloperStart renders active navigation content when shell state is present", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: {
      user: { display_name: "Dev", email: "dev@example.com", role: "developer", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    },
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "api-docs",
        navigation_items: [
          {
            id: "api-docs",
            label: "API 文档",
            group: "开发者",
            target_section: "api-docs",
            description: "Docs",
            empty_state_hint: "Docs",
            order: 10,
          },
        ],
      },
    },
  });

  assert.match(container.innerHTML, /API/);
  assert.match(container.innerHTML, /\/v1\/chat\/completions/);
  assert.doesNotMatch(container.innerHTML, /Provider 与 KeyPool/);
});

test("navigation helper falls back to overview when active item is unavailable", () => {
  const state = {
    navigation: {
      active_navigation_item_id: "missing",
      navigation_items: [{ id: "overview", label: "概览", group: "总览", order: 1 }],
    },
  };
  const items = navigationItemsFromState(state);
  assert.equal(activeNavigationItem(state, items).id, "overview");
});
