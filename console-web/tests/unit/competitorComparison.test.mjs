// 本单测覆盖 PRD-041 中立定位、权限过滤、Provider 分类边界、ARIA tabs 和唯一访问事件契约。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import {
  buildCompetitorComparison,
  competitorTabForKey,
  renderCompetitorComparison,
} from "../../src/features/competitor-comparison/renderCompetitorComparison.js";
import { renderProviderPanel } from "../../src/features/providers/renderProviderPanel.js";
import { recordConsolePageView } from "../../src/lib/consoleShell.js";
import { createUniquePageVisitTracker } from "../../src/lib/consoleVisitTracker.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造后端已确认的竞品帮助导航和当前角色可见映射，测试不自行伪造角色结论。 */
function shellState(navigationItems = null) {
  return {
    activeNavigationItemId: "competitor-comparison",
    navigation: {
      active_navigation_item_id: "competitor-comparison",
      navigation_items: navigationItems ?? [
        { id: "competitor-comparison", label: "竞品对比", group: "帮助中心", target_section: "competitor-comparison", required_permissions: [PERMISSIONS.CONSOLE_BASIC_READ], visible: true, disabled: false, order: 115 },
        { id: "api-docs", label: "API <文档>", target_section: "api-docs", required_permissions: [PERMISSIONS.CONSOLE_BASIC_READ], visible: true, disabled: false },
        { id: "providers", label: "Provider 管理", target_section: "providers", required_permissions: [PERMISSIONS.PROVIDER_READ], visible: false, disabled: false },
      ],
    },
  };
}

test("comparison renders neutral positioning, verified sources and escaped authorized mappings", () => {
  const comparison = buildCompetitorComparison({ consoleShellState: shellState() });
  const html = renderCompetitorComparison({
    comparison,
    authContext: withPermissionSnapshot(
      { user: { role: "viewer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
  });
  assert.match(html, /对标平台 \/ 竞品平台 \/ 迁移兼容对象，不是模型厂商，也不是 MetaHub 默认上游/);
  assert.match(html, /不构成采购、法律或安全结论/);
  assert.match(html, /未知或未核验不等于缺失/);
  assert.match(html, /2026-07-15/);
  assert.match(html, /target="_blank" rel="noopener noreferrer"/);
  assert.match(html, /data-console-nav="api-docs"/);
  assert.match(html, /API &lt;文档&gt;/);
  assert.doesNotMatch(html, /data-console-nav="providers"/);
  assert.doesNotMatch(html, /API <文档>/);
});

test("capability and sales tabs keep neutral current-state wording", () => {
  const comparison = buildCompetitorComparison({ consoleShellState: shellState() });
  const capabilityHtml = renderCompetitorComparison({
    comparison,
    authContext: withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    activeTab: "capabilities",
  });
  const salesHtml = renderCompetitorComparison({
    comparison,
    authContext: withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    activeTab: "sales",
  });
  assert.match(capabilityHtml, /公开数据策略、路由与用量能力/);
  assert.match(capabilityHtml, /公开流量、配额、观测与私有部署能力/);
  assert.match(capabilityHtml, /data-competitor-panel="capabilities" >/);
  assert.match(salesHtml, /禁止声称竞品没有治理、审计、私有部署或成本能力/);
  assert.match(salesHtml, /禁止使用“全面领先”“完全替代”“绝对合规”/);
  assert.match(salesHtml, /data-competitor-panel="sales" >/);
});

test("competitor tabs own complete ARIA relationships and keyboard mapping", () => {
  const comparison = buildCompetitorComparison({ consoleShellState: shellState() });
  const html = renderCompetitorComparison({
    comparison,
    authContext: withPermissionSnapshot(
      { user: { role: "viewer" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    activeTab: "sales",
  });
  assert.match(html, /role="tablist" aria-label="竞品对比视图"/);
  assert.match(html, /id="competitor-sales-tab" role="tab" aria-controls="competitor-sales-panel" aria-selected="true"/);
  assert.match(html, /data-competitor-panel="positioning" hidden/);
  assert.equal(competitorTabForKey("positioning", "ArrowRight"), "capabilities");
  assert.equal(competitorTabForKey("positioning", "ArrowLeft"), "sales");
  assert.equal(competitorTabForKey("sales", "Home"), "positioning");
  assert.equal(competitorTabForKey("positioning", "End"), "sales");
  assert.equal(competitorTabForKey("sales", " "), "sales");
  assert.equal(competitorTabForKey("sales", "Tab"), null);
});

test("legacy comparison renderer remains testable while the production dispatcher stays retired", () => {
  for (const role of ["admin", "developer", "viewer"]) {
    const authContext = withPermissionSnapshot(
      { user: { role } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    );
    assert.match(renderCompetitorComparison({
      comparison: buildCompetitorComparison({ consoleShellState: shellState() }),
      authContext,
    }), /data-competitor-comparison-root/);
    const container = { innerHTML: "" };
    renderDeveloperStart(container, {
      consoleShellState: shellState(),
      authContext,
    });
    assert.doesNotMatch(container.innerHTML, /data-competitor-comparison-root/);
  }
  const denied = renderCompetitorComparison({
    comparison: buildCompetitorComparison({ consoleShellState: shellState() }),
    authContext: withPermissionSnapshot({ user: { role: "admin" } }, []),
  });
  assert.equal(denied, "");
  const anonymous = renderCompetitorComparison({ comparison: buildCompetitorComparison({ consoleShellState: shellState() }), authContext: null });
  assert.equal(anonymous, "");
});

test("generic visit tracker records once per visit and on re-entry or session change", async () => {
  let calls = 0;
  let releaseFirst;
  const firstRequest = new Promise((resolve) => { releaseFirst = resolve; });
  const tracker = createUniquePageVisitTracker({
    targetItemId: "competitor-comparison",
    recordVisit: async () => {
      calls += 1;
      if (calls === 1) await firstRequest;
      return { accepted: true };
    },
  });
  const first = tracker.afterRender({ activeItemId: "competitor-comparison", sessionKey: "a", source: "direct_hash" });
  const concurrent = tracker.afterRender({ activeItemId: "competitor-comparison", sessionKey: "a", source: "direct_hash" });
  await Promise.resolve();
  assert.equal(first, concurrent);
  assert.equal(calls, 1);
  releaseFirst();
  await first;
  await tracker.afterRender({ activeItemId: "competitor-comparison", sessionKey: "a", source: "sidebar" });
  assert.equal(calls, 1);
  await tracker.afterRender({ activeItemId: "overview", sessionKey: "a", source: "sidebar" });
  await tracker.afterRender({ activeItemId: "competitor-comparison", sessionKey: "a", source: "sidebar" });
  assert.equal(calls, 2);
  await tracker.afterRender({ activeItemId: "competitor-comparison", sessionKey: "b", source: "direct_hash" });
  assert.equal(calls, 3);
});

test("competitor event client sends exact safe contract without tenant or user fields", async () => {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url: String(url), body: JSON.parse(options.body) };
    return { ok: true, headers: new Map(), async json() { return { accepted: true, event_id: "cpe_prd041", request_id: "req_prd041", trace_id: "trc_prd041" }; } };
  };
  await recordConsolePageView({
    controlApiUrl: "http://control.local",
    token: "session-token",
    navigationItemId: "competitor-comparison",
    targetSection: "competitor-comparison",
    path: "/#competitor-comparison",
    eventType: "help.competitor_viewed",
    payload: { source: "direct_hash", hash: "#competitor-comparison", viewport_class: "mobile" },
  });
  assert.equal(captured.url, "http://control.local/console/events");
  assert.deepEqual(captured.body, {
    event_type: "help.competitor_viewed",
    navigation_item_id: "competitor-comparison",
    target_section: "competitor-comparison",
    path: "/#competitor-comparison",
    payload: { source: "direct_hash", hash: "#competitor-comparison", viewport_class: "mobile" },
  });
  assert.equal(JSON.stringify(captured.body).includes("tenant_id"), false);
  assert.equal(JSON.stringify(captured.body).includes("user_id"), false);
});

test("provider UI exposes only generic migration adapter and never competitor provider values", () => {
  const html = renderProviderPanel(
    { state: "ok", providers: [], providerKeysByProviderId: {} },
    withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.PROVIDER_READ, PERMISSIONS.PROVIDER_WRITE],
    ),
  );
  assert.match(html, /value="migration_compatible"/);
  assert.match(html, /迁移兼容（聚合入口）/);
  assert.match(html, /不是模型厂商或默认上游/);
  assert.doesNotMatch(html, /value="openrouter"/i);
  assert.doesNotMatch(html, /value="silicon_?flow"/i);
});

test("production CSS omits the retired competitor runtime selectors", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.doesNotMatch(css, /\.competitor-/u);
});
