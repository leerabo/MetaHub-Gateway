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
  navigationItemsFromState,
  renderConsoleShell,
} from "../../src/features/console-shell/renderConsoleShell.js";
import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { AuthApiError } from "../../src/lib/auth.js";

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
  assert.match(html, /class="sidebar-link active"/);
  assert.doesNotMatch(html, /<Admin>/);
  assert.match(html, /&lt;Admin&gt;/);
  assert.match(html, /&lt;Tenant&gt;/);
  assert.match(html, /Trusted content/);
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
