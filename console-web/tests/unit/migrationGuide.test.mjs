// 本单测覆盖 PRD-040 核心内容、动态配置边界、角色门禁、专属 ARIA tabs 和四块独立复制契约。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { createMigrationGuideVisitTracker } from "../../src/app/main.js";
import {
  buildMigrationGuide,
  migrationLanguageForKey,
  renderMigrationGuide,
} from "../../src/features/migration-guide/renderMigrationGuide.js";
import { recordConsolePageView } from "../../src/lib/consoleShell.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

// 合法状态固定包含一个 active Environment 和一个四重 active alias，用于验证显式选择前后的能力变化。
const ACTIVE_STATES = {
  environmentState: {
    environments: [{ environment_id: "env_prod", code: "prod", name: "生产<环境>", status: "active" }],
  },
  modelAliasesState: {
    model_aliases: [{
      alias: "chat<prod>",
      environment_id: "env_prod",
      status: "active",
      environment_status: "active",
      default_model_status: "active",
      default_provider_status: "active",
    }],
  },
};

/**
 * 生成后端确认迁移入口和可信 Gateway URL 的最小 shell 状态，避免测试使用前端 localhost 兜底。
 */
function shellState(status = "ready") {
  return {
    navigation: {
      active_navigation_item_id: "migration-guide",
      gateway_base_url_status: status,
      gateway_base_url: status === "ready" ? "https://gateway.example.com/v1" : null,
      navigation_items: [{
        id: "migration-guide",
        label: "迁移指南",
        group: "开发者",
        target_section: "migration-guide",
        description: "从直连迁移",
        empty_state_hint: "选择配置",
        required_permissions: [PERMISSIONS.DEVELOPER_PORTAL_READ],
        visible: true,
        disabled: false,
        order: 30,
      }],
    },
  };
}

test("guide requires trusted gateway and explicit same-environment selections", () => {
  const pending = buildMigrationGuide({ consoleShellState: shellState(), ...ACTIVE_STATES });
  assert.equal(pending.copy_ready, false);
  assert.equal(pending.selected_environment_id, null);
  assert.equal(pending.selected_model_alias, null);

  const ready = buildMigrationGuide({
    consoleShellState: shellState(),
    ...ACTIVE_STATES,
    selectedEnvironmentId: "env_prod",
    selectedModelAlias: "chat<prod>",
  });
  assert.equal(ready.copy_ready, true);
  assert.equal(ready.gateway_base_url, "https://gateway.example.com/v1");

  const blocked = buildMigrationGuide({
    consoleShellState: shellState("configuration_required"),
    ...ACTIVE_STATES,
    selectedEnvironmentId: "env_prod",
    selectedModelAlias: "chat<prod>",
  });
  assert.equal(blocked.copy_ready, false);
  assert.equal(blocked.gateway_base_url, null);
});

test("guide renders complete migration contract and four isolated copy targets", () => {
  const guide = buildMigrationGuide({
    consoleShellState: shellState(),
    ...ACTIVE_STATES,
    selectedEnvironmentId: "env_prod",
    selectedModelAlias: "chat<prod>",
  });
  const html = renderMigrationGuide({
    guide,
    authContext: withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.DEVELOPER_PORTAL_READ],
    ),
  });

  assert.match(html, /\/v1\/chat\/completions/);
  assert.match(html, /Responses、Assistants/);
  assert.match(html, /event: error/);
  assert.match(html, /gateway\.budget_exhausted/);
  assert.match(html, /LLM_ROUTE=direct\|metahub/);
  assert.match(html, /openai@6\.47\.0/);
  assert.match(html, /openai&gt;=1\.40,&lt;2/);
  assert.equal((html.match(/data-migration-copy-target=/g) ?? []).length, 4);
  assert.equal((html.match(/class="migration-copy-feedback"/g) ?? []).length, 4);
  assert.equal(new Set([...html.matchAll(/data-migration-copy-target="([^"]+)"/g)].map((match) => match[1])).size, 4);
  assert.doesNotMatch(html, /生产<环境>|chat<prod>/);
  assert.match(html, /生产&lt;环境&gt;/);
  assert.match(html, /chat&lt;prod&gt;/);
});

test("Workspace migration takes authorized connection metadata and rejects stale legacy navigation fields", () => {
  const consoleShellState = shellState();
  consoleShellState.navigation.contract_version = "workspace-navigation-v1";
  const inputs = { consoleShellState, ...ACTIVE_STATES, selectedEnvironmentId: "env_prod", selectedModelAlias: "chat<prod>" };
  assert.equal(buildMigrationGuide(inputs).copy_ready, false);
  const ready = buildMigrationGuide({ ...inputs, connectionState: { state: "ok", gateway_base_url: "https://actual.gateway/v1" } });
  assert.equal(ready.copy_ready, true);
  assert.equal(ready.gateway_base_url, "https://actual.gateway/v1");
  for (const gateway_base_url of ["", "javascript:alert(1)", "https://user:secret@gateway.test/v1",
    "https://gateway.test/v1?key=secret", "https://gateway.test/v1#secret", "https://gateway.test/v1\n"]) {
    const invalid = buildMigrationGuide({ ...inputs, connectionState: { state: "ok", gateway_base_url } });
    assert.equal(invalid.copy_ready, false);
    assert.equal(invalid.gateway_base_url, null);
  }
  assert.equal(buildMigrationGuide({ ...inputs, connectionState: { state: "error" } }).copy_ready, false);
});

test("migration tabs own ARIA relationships and complete keyboard mapping", () => {
  const guide = buildMigrationGuide({ consoleShellState: shellState(), ...ACTIVE_STATES });
  const html = renderMigrationGuide({
    guide,
    authContext: withPermissionSnapshot(
      { user: { role: "admin" } },
      [PERMISSIONS.DEVELOPER_PORTAL_READ],
    ),
    activeLanguage: "node",
  });
  assert.match(html, /role="tablist" aria-label="迁移示例语言"/);
  assert.match(html, /id="migration-node-tab" aria-controls="migration-node-panel" aria-selected="true"/);
  assert.match(html, /data-migration-language-panel="python" hidden/);
  assert.equal(migrationLanguageForKey("python", "ArrowRight"), "node");
  assert.equal(migrationLanguageForKey("node", "Home"), "python");
  assert.equal(migrationLanguageForKey("python", "End"), "node");
  assert.equal(migrationLanguageForKey("node", " "), "node");
  assert.equal(migrationLanguageForKey("python", "Tab"), null);
});

test("migration distinguishes a failed configuration read from an empty environment", () => {
  const guide = buildMigrationGuide({ consoleShellState: shellState(),
    environmentState: { state: "error" }, modelAliasesState: { model_aliases: [] } });
  const html = renderMigrationGuide({ guide,
    authContext: withPermissionSnapshot({ user: { role: "developer" } }, [PERMISSIONS.DEVELOPER_PORTAL_READ]) });
  assert.equal(guide.copy_ready, false);
  assert.match(html, /role="alert"[\s\S]*data-resource-retry/);
  assert.doesNotMatch(html, /暂无可用环境/);
});

test("developer start dispatches with developer_portal.read and fails closed without it", () => {
  const base = {
    consoleShellState: shellState(),
    ...ACTIVE_STATES,
    selectedMigrationEnvironmentId: "env_prod",
    selectedMigrationModelAlias: "chat<prod>",
  };
  const developer = { innerHTML: "" };
  renderDeveloperStart(developer, {
    ...base,
    authContext: withPermissionSnapshot(
      { user: { role: "developer" } },
      [PERMISSIONS.DEVELOPER_PORTAL_READ],
    ),
  });
  assert.match(developer.innerHTML, /data-migration-guide-root/);

  const viewer = { innerHTML: "" };
  renderDeveloperStart(viewer, {
    ...base,
    authContext: withPermissionSnapshot({ user: { role: "viewer" } }, []),
  });
  assert.doesNotMatch(viewer.innerHTML, /data-migration-guide-root/);
});

test("visit tracker records once across concurrent and refresh renders then records after re-entry", async () => {
  let calls = 0;
  let releaseFirst;
  const firstRequest = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const tracker = createMigrationGuideVisitTracker(async () => {
    calls += 1;
    if (calls === 1) await firstRequest;
    return { accepted: true };
  });

  const first = tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "direct_hash" });
  const concurrent = tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "direct_hash" });
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(first, concurrent);
  releaseFirst();
  await first;

  await tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "direct_hash" });
  assert.equal(calls, 1);
  await tracker.afterRender({ activeItemId: "overview", sessionKey: "session-a", source: "sidebar" });
  await tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "sidebar" });
  assert.equal(calls, 2);
});

test("visit tracker marks failed visit attempted and session change creates a new generation", async () => {
  let calls = 0;
  const tracker = createMigrationGuideVisitTracker(async () => {
    calls += 1;
    // 首次请求模拟非 401 遥测故障，验证 attempted 会阻止本 visit 的自动重试。
    if (calls === 1) throw new Error("telemetry unavailable");
    return { accepted: true };
  });

  await assert.rejects(
    tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "direct_hash" }),
    /telemetry unavailable/,
  );
  await tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-a", source: "direct_hash" });
  assert.equal(calls, 1);
  await tracker.afterRender({ activeItemId: "migration-guide", sessionKey: "session-b", source: "direct_hash" });
  assert.equal(calls, 2);
  assert.equal(tracker.snapshot().attempted, true);
});

test("migration event client sends the exact safe contract without tenant or user fields", async () => {
  let captured;
  globalThis.fetch = async (url, options) => {
    captured = { url: String(url), body: JSON.parse(options.body) };
    return {
      ok: true,
      headers: new Map(),
      async json() {
        return { accepted: true, event_id: "cpe_1", request_id: "req_1", trace_id: "trc_1" };
      },
    };
  };

  await recordConsolePageView({
    controlApiUrl: "http://control.local",
    token: "session-token",
    navigationItemId: "migration-guide",
    targetSection: "migration-guide",
    path: "/#migration-guide",
    eventType: "migration.guide_viewed",
    payload: { source: "direct_hash", hash: "#migration-guide", viewport_class: "mobile" },
  });

  assert.equal(captured.url, "http://control.local/console/events");
  assert.deepEqual(captured.body, {
    event_type: "migration.guide_viewed",
    navigation_item_id: "migration-guide",
    target_section: "migration-guide",
    path: "/#migration-guide",
    payload: { source: "direct_hash", hash: "#migration-guide", viewport_class: "mobile" },
  });
  assert.equal(JSON.stringify(captured.body).includes("tenant_id"), false);
  assert.equal(JSON.stringify(captured.body).includes("user_id"), false);
});

test("responsive CSS keeps the document root bounded and localizes horizontal scrolling", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");
  assert.match(css, /\.migration-guide\s*\{[^}]*overflow-x:\s*hidden/s);
  assert.match(css, /\.migration-table-wrap\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(css, /\.migration-code-block pre\s*\{[^}]*overflow:\s*auto/s);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.migration-config-grid,[\s\S]*?\.migration-code-grid\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 390px\)/);
});
