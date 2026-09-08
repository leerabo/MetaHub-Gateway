// PRD-052 页面生命周期专项：冻结跨身份异步门禁、表单联动、409 证据保留和 1280 响应式合同。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  budgetIdentitySnapshot,
  createBudgetMutationLifecycle,
  isBudgetRefreshOwnerCurrent,
  selectBudgetScopeOption,
} from "../../src/features/budgets/budgetPageLifecycle.js";
import { renderBudgetPanel } from "../../src/features/budgets/renderBudgetPanel.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造不含 Token 的认证上下文，便于验证 tenant/user/session 任一变化都会淘汰旧 owner。 */
function authContext(tenantId = "tenant_a", userId = "usr_a", sessionId = "session_a") {
  return withPermissionSnapshot(
    {
      user: { tenant_id: tenantId, user_id: userId, role: "admin" },
      session: { session_id: sessionId, session_revision: 1 },
    },
    [PERMISSIONS.BILLING_BUDGET_READ, PERMISSIONS.BILLING_BUDGET_WRITE],
  );
}

test("PRD-052 budget refresh owner rejects old tenant user and generation", () => {
  const identity = budgetIdentitySnapshot(authContext());
  const owner = { identity, refreshGeneration: 12 };

  assert.equal(isBudgetRefreshOwnerCurrent(owner, { identity, refreshGeneration: 12 }), true);
  assert.equal(isBudgetRefreshOwnerCurrent(owner, {
    identity: budgetIdentitySnapshot(authContext("tenant_b")),
    refreshGeneration: 12,
  }), false);
  assert.equal(isBudgetRefreshOwnerCurrent(owner, {
    identity: budgetIdentitySnapshot(authContext("tenant_a", "usr_b")),
    refreshGeneration: 12,
  }), false);
  assert.equal(isBudgetRefreshOwnerCurrent(owner, { identity, refreshGeneration: 13 }), false);
});

test("PRD-052 budget mutation lifecycle ignores superseded and invalidated callbacks", () => {
  const lifecycle = createBudgetMutationLifecycle();
  const identity = budgetIdentitySnapshot(authContext());
  const first = lifecycle.begin({ identity, navigation: "budgets", refreshGeneration: 7 });
  assert.equal(lifecycle.isCurrent(first, { identity, navigation: "budgets", refreshGeneration: 7 }), true);
  assert.equal(lifecycle.isBusy(), true);

  const second = lifecycle.begin({ identity, navigation: "budgets", refreshGeneration: 7 });
  assert.equal(lifecycle.isCurrent(first, { identity, navigation: "budgets", refreshGeneration: 7 }), false);
  assert.equal(lifecycle.isCurrent(second, { identity, navigation: "usage", refreshGeneration: 7 }), false);
  lifecycle.invalidate();
  assert.equal(lifecycle.isCurrent(second, { identity, navigation: "budgets", refreshGeneration: 7 }), false);
  assert.equal(lifecycle.isBusy(), false);
});

test("PRD-052 automatic Key selection carries its Environment hint", () => {
  const options = [
    { scopeType: "tenant", value: "", environmentId: "" },
    { scopeType: "virtual_key", value: "vk_prod", environmentId: "env_prod" },
    { scopeType: "virtual_key", value: "vk_test", environmentId: "env_test" },
  ];
  assert.deepEqual(selectBudgetScopeOption({
    scopeType: "virtual_key",
    currentScopeId: "",
    options,
  }), { scopeId: "vk_prod", environmentId: "env_prod" });
  assert.deepEqual(selectBudgetScopeOption({
    scopeType: "virtual_key",
    currentScopeId: "vk_test",
    options,
  }), { scopeId: "vk_test", environmentId: "env_test" });
});

test("PRD-052 refresh failure preserves preceding 409 message and request_id", () => {
  const html = renderBudgetPanel({
    state: "error",
    message: "预算状态刷新失败",
    requestId: "req_refresh_failed",
    actionError: {
      message: "该作用域预算规则已存在。",
      requestId: "req_conflict",
    },
  }, authContext());

  assert.match(html, /该作用域预算规则已存在。/);
  assert.match(html, /req_conflict/);
  assert.match(html, /预算状态刷新失败/);
  assert.match(html, /req_refresh_failed/);
});

test("PRD-052 main and CSS freeze owner gates and responsive record conversion", async () => {
  const [mainSource, css] = await Promise.all([
    readFile(new URL("../../src/app/main.js", import.meta.url), "utf8"),
    readFile(new URL("../../src/styles/main.css", import.meta.url), "utf8"),
  ]);

  assert.match(mainSource, /loadBudgetState\(virtualKeysState, consoleShellState, refreshId\)/);
  assert.match(mainSource, /isBudgetRefreshOwnerCurrent\(refreshOwner/);
  assert.match(mainSource, /function clearConsoleSessionState\([\s\S]*?clearBudgetSessionState\(\)/);
  assert.match(mainSource, /selectBudgetScopeOption\(/);
  assert.match(css, /@media \(max-width: 1400px\)\s*\{[\s\S]*?\.budget-rule-row\s*\{\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.budget-rule-row:not\(\.budget-rule-head\) > \[data-label\]::before/);
  assert.equal((css.match(/\{/g) ?? []).length, (css.match(/\}/g) ?? []).length);
  assert.match(css, /\.budget-status-grid,\s*\.budget-rule-form,\s*\.budget-rule-row,[\s\S]*?grid-template-columns: 1fr;/);
});
