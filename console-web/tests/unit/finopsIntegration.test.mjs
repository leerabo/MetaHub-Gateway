// 本单测覆盖 PRD-051 Console 主应用接线，防止纯组件通过但导航、生命周期或响应式合同未实际启用。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

const entry = {
  finops_ledger_entry_id: "fle_integration_051",
  entry_type: "usage_charge",
  direction: "debit",
  amount: "0.123456",
  currency: "CNY",
  quota_type: "paid",
  source_request_id: "req_integration_051",
  source_trace_id: "trc_integration_051",
  price_snapshot_id: "prs_integration_051",
  price_schema_version: "v1",
  price_snapshot_source: "model_price",
  price_evidence_status: "priced",
  total_tokens: 12,
  cache_tokens: 0,
  created_at: "2026-07-27T00:00:00Z",
};

const navigation = {
  navigation: {
    active_navigation_item_id: "internal-finops",
    navigation_items: [{
      id: "internal-finops",
      label: "FinOps 账本",
      group: "调用与成本",
      target_section: "internal-finops",
      description: "正式成本账本",
      empty_state_hint: "暂无正式分录",
      order: 76,
    }],
  },
  activeNavigationItemId: "internal-finops",
};

test("canonical internal FinOps mounts ledger and cost rates only with the matching permissions", () => {
  const finOpsState = {
    state: "ok",
    ledger_entries: [entry],
    selectedEntry: entry,
    totals: { by_currency: { CNY: { debit: "0.123456", credit: "0.000000", net: "0.123456" } } },
    total: 1,
    filters: {},
    canNext: false,
    canPrevious: false,
    dialog: { open: false },
  };
  const financeContainer = { innerHTML: "" };
  const developerContainer = { innerHTML: "" };
  renderDeveloperStart(financeContainer, {
    authContext: withPermissionSnapshot(
      { user: { role: "finance_admin", display_name: "FinanceAdmin" }, tenant: { name: "Tenant" } },
      [PERMISSIONS.BILLING_FINOPS_READ, PERMISSIONS.BILLING_FINOPS_WRITE, PERMISSIONS.REQUEST_LOG_READ],
    ),
    consoleShellState: { ...navigation, activeNavigationItemId: "internal-finops" },
    finOpsState,
  });
  renderDeveloperStart(developerContainer, {
    authContext: withPermissionSnapshot(
      { user: { role: "developer", display_name: "Dev" }, tenant: { name: "Tenant" } },
      [PERMISSIONS.CONSOLE_BASIC_READ],
    ),
    consoleShellState: { ...navigation, activeNavigationItemId: "internal-finops" },
    finOpsState,
  });

  assert.match(financeContainer.innerHTML, /finops-ledger-panel/);
  assert.match(financeContainer.innerHTML, /data-workspace-commercial-root="internal-finops"/);
  assert.match(financeContainer.innerHTML, /id="finops-detail-title">分录详情/);
  assert.match(financeContainer.innerHTML, /data-finops-request-id="req_integration_051"/);
  assert.doesNotMatch(developerContainer.innerHTML, /finops-ledger-panel/);
  assert.doesNotMatch(developerContainer.innerHTML, /req_integration_051/);
  const financeWithoutLogs = { innerHTML: "" };
  renderDeveloperStart(financeWithoutLogs, {
    authContext: withPermissionSnapshot(
      { user: { role: "finance_admin" }, tenant: { name: "Tenant" } },
      [PERMISSIONS.BILLING_FINOPS_READ, PERMISSIONS.BILLING_FINOPS_WRITE],
    ),
    consoleShellState: { ...navigation, activeNavigationItemId: "internal-finops" },
    finOpsState,
  });
  assert.match(financeWithoutLogs.innerHTML, /finops-detail-title/);
  assert.doesNotMatch(financeWithoutLogs.innerHTML, /data-finops-request-id/);
});

test("main loader requires FinOps read permission, backend navigation, current hash and refresh owner", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

  assert.match(source, /async function loadFinOpsState\(consoleShellState, refreshId\)/);
  assert.match(source, /!hasPermission\(authSession\.context, PERMISSIONS\.BILLING_FINOPS_READ\)/);
  assert.match(source, /consoleShellState\?\.activeNavigationItemId !== "internal-finops"/);
  assert.match(source, /hashNavigation && hashNavigation !== "internal-finops"/);
  assert.match(source, /loadResourceState\("internal-finops", \[PERMISSIONS.BILLING_FINOPS_READ\]/);
  assert.doesNotMatch(source, /readActiveNavigationHash\(\) !== "finops-ledger"/);
  assert.match(source, /refreshId !== refreshSequence/);
  assert.match(source, /function clearConsoleSessionState\([\s\S]*?finOpsPageState\.clear\(\)/);
  assert.match(source, /finOpsPageState\.clear\(\)/);
  assert.match(source, /resourceMutationRequest\(operation, createFinOpsAdjustment, \{/);
  assert.match(source, /Idempotency|idempotencyKey/);
  assert.match(source, /bindFinOpsInteractions\(consoleShellState\)/);
});

test("main request jump uses current RequestLog owner and backend navigation button", async () => {
  const source = await readFile(new URL("../../src/app/main.js", import.meta.url), "utf8");

  assert.match(source, /async onOpenRequest\(requestId\)/);
  assert.match(source, /button\.dataset\.consoleNav === "request-logs"/);
  assert.match(source, /configureCurrentRequestLogState\(/);
  assert.match(source, /await switchConsoleNavigation\(navigationButton, consoleShellState\)/);
});

test("finops responsive CSS keeps compact table, modal and mobile label layout", async () => {
  const css = await readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8");

  assert.match(css, /\.finops-ledger-row \{[\s\S]*?grid-template-columns:/);
  assert.match(css, /\.finops-adjustment-dialog \{[\s\S]*?width: min\(520px, calc\(100vw - 32px\)\)/);
  assert.match(css, /@media \(max-width: 1100px\)[\s\S]*?\.finops-ledger-head \{\s*display: none;/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*?\.finops-filter-form,[\s\S]*?grid-template-columns: minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 390px\)[\s\S]*?\.finops-dialog-actions/);
  assert.doesNotMatch(css, /\.finops[^\{]*\{[^}]*letter-spacing:\s*-/s);
});


test("adjustment dialog focuses an editable field instead of the close action", async () => {
  const source = await readFile(
    new URL("../../src/features/finops-ledger/bindFinOpsLedger.js", import.meta.url),
    "utf8",
  );
  assert.match(source, /textarea:not\(\[disabled\]\)"\)\?\.focus\(\)/);
  assert.doesNotMatch(source, /textarea:not\(\[disabled\]\), button:not/);
});
