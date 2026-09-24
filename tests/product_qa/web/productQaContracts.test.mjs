/** Product-level WEB/UI/UCD/COMP contract suite. No browser credentials or request bodies are used here. */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderConsoleShell } from "../../../console-web/src/features/console-shell/renderConsoleShell.js";
import { renderLoginPage } from "../../../console-web/src/features/auth/renderLoginPage.js";

const UI_SOURCE = new URL("../../../console-web/src/features/console-shell/renderConsoleShell.js", import.meta.url);
const CSS_SOURCE = new URL("../../../console-web/src/styles/main.css", import.meta.url);
const BROWSER_RUNTIME_SOURCE = new URL("../../../scripts/quality/product_qa_browser_runtime.py", import.meta.url);

function shell(preferences = {}) {
  return renderConsoleShell({
    authContext: {
      user: { display_name: "QA Administrator", email: "qa-admin@example.test", role: "admin", tenant_id: "tenant_opaque" },
      tenant: { name: "QA Enterprise" },
      session: { expires_at: "2099-01-01T00:00:00Z" },
    },
    consoleShellState: {
      preferences,
      navigation: {
        active_navigation_item_id: "overview",
        navigation_items: [
          { id: "overview", label: "Overview", group: "System", target_section: "overview", description: "Review service status", order: 1 },
          { id: "providers", label: "Providers", group: "Configuration", target_section: "providers", description: "Manage model providers", order: 2 },
        ],
      },
    },
    contentHtml: '<section data-product-qa-content="true">Safe content</section>',
  });
}

test("WEB-01/UI-01 shell exposes a keyboard-operable sidebar with collapsed groups", () => {
  const expanded = shell();
  const collapsed = shell({ preference: { sidebar_collapsed: true } });
  assert.match(expanded, /data-sidebar-toggle/);
  assert.match(expanded, /aria-controls="console-sidebar"/);
  assert.match(expanded, /aria-expanded="true"/);
  assert.equal((expanded.match(/data-sidebar-group-toggle=/g) ?? []).length, 2);
  assert.equal((expanded.match(/class="sidebar-group-items" hidden/g) ?? []).length, 2);
  assert.equal((expanded.match(/data-sidebar-group-toggle=[\s\S]*?aria-expanded="false"/g) ?? []).length, 2);
  assert.match(expanded, /class="sidebar-link active"/);
  assert.match(expanded, /aria-current="page"/);
  assert.match(collapsed, /class="console-shell sidebar-collapsed"/);
  assert.match(collapsed, /data-sidebar-collapsed="true"/);
  assert.match(collapsed, /aria-expanded="false"/);
  assert.match(collapsed, /class="sidebar-link-short"/);
});

test("UI-02/UCD-05 visible shell content uses product labels and keeps internal canaries out", () => {
  const html = shell({ preference: { sidebar_collapsed: true } });
  for (const forbidden of ["PRD-", "0066", "migration_", "worker_state", "scheduler_", "tenant_opaque"]) {
    assert.doesNotMatch(html, new RegExp(forbidden, "i"));
  }
  // Navigation labels are localized for the actual product surface; keep the
  // contract compatible with the English fixture and the shipped Chinese UI.
  assert.match(html, /Overview|概览/);
  assert.match(html, /Providers|模型服务/);
  assert.match(html, /QA Enterprise/);
  assert.match(html, /Safe content/);
});

test("UI-03 login surface has labelled credential fields and escapes untrusted branding", () => {
  const container = { innerHTML: "" };
  renderLoginPage(container, {
    tenantSlug: "default",
    branding: { product_name: "<Brand>", login_title: "<Sign in>", login_message: "<Welcome>" },
    error: "<Error>",
    requestId: "rq_qa_summary",
  });
  const html = container.innerHTML;
  assert.match(html, /<form class="login-form" id="login-form">/);
  assert.match(html, /name="tenantSlug" autocomplete="organization"/);
  assert.match(html, /name="email" type="email" autocomplete="username"/);
  assert.match(html, /name="password" type="password" autocomplete="current-password"/);
  assert.match(html, /role="alert"/);
  assert.doesNotMatch(html, /<Brand>|<Sign in>|<Welcome>|<Error>/);
  assert.match(html, /&lt;Brand&gt;/);
});

test("COMP-01/UI-06 responsive sidebar contract prevents desktop overflow and mobile nested scrolling", async () => {
  const css = await readFile(CSS_SOURCE, "utf8");
  assert.match(css, /\.console-shell\s*\{[\s\S]*grid-template-columns:\s*248px minmax\(0, 1fr\)/);
  assert.match(css, /\.console-shell\.sidebar-collapsed\s*\{[\s\S]*78px minmax\(0, 1fr\)/);
  assert.match(css, /\.console-sidebar\s*\{[\s\S]*max-height:\s*calc\(100vh - 24px\)[\s\S]*overflow-y:\s*auto/);
  assert.match(css, /@media \(max-width: 820px\)[\s\S]*\.console-sidebar\s*\{[\s\S]*position:\s*static[\s\S]*max-height:\s*none[\s\S]*overflow-y:\s*visible/);
  assert.match(css, /@media \(max-width: 820px\)[\s\S]*\.console-shell\.sidebar-collapsed\s*\{[\s\S]*grid-template-columns:\s*1fr/);
  assert.match(css, /\.console-main\s*\{[\s\S]*min-width:\s*0/);
});

test("UI-04 source does not promote roadmap or implementation markers in shell rendering", async () => {
  const source = await readFile(UI_SOURCE, "utf8");
  assert.doesNotMatch(source, /PRD-\d{3}/);
  assert.doesNotMatch(source, /migration[_ -]?(?:id|version|number)/i);
  assert.doesNotMatch(source, /sqlite_master|\.db\b/i);
  assert.match(source, /request_id/);
  assert.match(source, /escapeHtml\(/);
});

test("FLOW-01/PERF-01 isolated browser runtime disables unrelated background schedulers", async () => {
  const source = await readFile(BROWSER_RUNTIME_SOURCE, "utf8");
  assert.match(source, /PRAGMA journal_mode=WAL/);
  assert.match(source, /"journal_mode": journal_mode/);
  assert.match(source, /"METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "false"/);
  assert.match(source, /"METAHUB_PROVIDER_HEALTH_ENABLED": "false"/);
  assert.match(source, /"METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false"/);
  assert.match(source, /"METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false"/);
});
