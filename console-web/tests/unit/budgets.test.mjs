// 本单测覆盖预算告警前端能力，确保 API 参数、错误映射、预算状态展示和导航接入保持租户安全边界。
import assert from "node:assert/strict";
import test from "node:test";

import { renderDeveloperStart } from "../../src/features/developer-start/renderDeveloperStart.js";
import { renderBudgetPanel } from "../../src/features/budgets/renderBudgetPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import { getBudgetStatus, listBudgets, updateBudgetMode, updateBudgetRule } from "../../src/lib/budgets.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造预算读写授权快照，测试不再根据 admin 角色名推导能力。 */
function budgetWriterContext(baseContext = { user: { role: "admin" } }) {
  return withPermissionSnapshot(
    baseContext,
    [PERMISSIONS.BILLING_BUDGET_READ, PERMISSIONS.BILLING_BUDGET_WRITE],
  );
}

/** 构造被撤销写权限但仍可读取预算的 FinanceAdmin 快照。 */
function budgetReaderContext(baseContext = { user: { role: "finance_admin" } }) {
  return withPermissionSnapshot(baseContext, [PERMISSIONS.BILLING_BUDGET_READ]);
}

const budgetRulesPayload = {
  budget_rules: [
    {
      budget_rule_id: "br_1",
      tenant_id: "tenant_1",
      scope_type: "virtual_key",
      scope_id: "vk_1",
      scope_label: "生产 Key (mhv_prod_****)",
      name: "生产 Key 日预算",
      daily_budget_cny: "100",
      warning_threshold_percent: 80,
      exhausted_threshold_percent: 100,
      exhausted_action: "block",
      status: "active",
      today_status: {
        budget_rule_id: "br_1",
        scope_type: "virtual_key",
        scope_id: "vk_1",
        scope_label: "生产 Key (mhv_prod_****)",
        budget_date: "2026-07-09",
        spent_cny: "82",
        estimated_remaining_cny: "18",
        budget_cny: "100",
        usage_percent: 82,
        status: "threshold_reached",
        exhausted_action: "block",
        next_action: "warn",
        last_event_type: "budget.threshold_reached",
        block_status: "active",
        budget_block_id: "bb_1",
        blocked_at: "2026-07-09T08:30:00Z",
      },
      active_block: {
        budget_block_id: "bb_1",
        budget_rule_id: "br_1",
        blocked_cost_cny: "101",
        budget_cny: "100",
        blocked_request_id: "req_blocked_1",
        blocked_at: "2026-07-09T08:30:00Z",
        status: "active",
      },
      created_at: "2026-07-09T08:00:00Z",
      updated_at: "2026-07-09T08:00:00Z",
    },
  ],
  request_id: "req_budget_rules",
  trace_id: "trc_budget_rules",
};

const budgetStatusPayload = {
  budget_date: "2026-07-09",
  statuses: [budgetRulesPayload.budget_rules[0].today_status],
  request_id: "req_budget_status",
  trace_id: "trc_budget_status",
};

const projectState = {
  projects: [
    { project_id: "prj_1", project_code: "billing", name: "账务核心", status: "active" },
  ],
  applications: [
    { application_id: "app_1", name: "账务 API", project_name: "账务核心", status: "active" },
  ],
};

const virtualKeysState = {
  virtual_keys: [
    {
      virtual_key_id: "vk_1",
      name: "生产 Key",
      key_prefix: "mhv_prod_****",
    },
  ],
};

test("budgets client sends safe query and patch payloads", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      headers: new Map([["X-Request-ID", "req_budget"]]),
      async json() {
        if (String(url).includes("/budget-status")) {
          return budgetStatusPayload;
        }
        return budgetRulesPayload;
      },
    };
  };

  const listed = await listBudgets({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    scopeType: "virtual_key",
    status: "active",
  });
  const status = await getBudgetStatus({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_1",
    projectId: "prj_1",
    applicationId: "app_1",
  });
  await updateBudgetRule({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    scopeType: "virtual_key",
    scopeId: "vk_1",
    name: "",
    dailyBudgetCny: "100",
    warningThresholdPercent: 80,
    exhaustedAction: "block",
    status: "active",
  });
  await updateBudgetMode({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    budgetRuleId: "br_1/unsafe",
    mode: "alert_only",
  });

  assert.equal(listed.state, "ok");
  assert.equal(status.statuses[0].status, "threshold_reached");
  assert.match(calls[0].url, /scope_type=virtual_key/);
  assert.match(calls[0].url, /status=active/);
  assert.match(calls[1].url, /virtual_key_id=vk_1/);
  assert.match(calls[1].url, /project_id=prj_1/);
  assert.match(calls[1].url, /application_id=app_1/);
  assert.doesNotMatch(calls[0].url, /tenant_id=/);
  assert.equal(calls[2].options.method, "PATCH");
  assert.equal(calls[2].options.headers.Authorization, "Bearer jwt-token");
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    scope_type: "virtual_key",
    scope_id: "vk_1",
    daily_budget_cny: "100",
    warning_threshold_percent: 80,
    exhausted_action: "block",
    status: "active",
  });
  assert.equal(calls[3].options.method, "PATCH");
  assert.match(calls[3].url, /\/budgets\/br_1%2Funsafe\/mode$/);
  assert.deepEqual(JSON.parse(calls[3].options.body), { mode: "alert_only" });
  assert.doesNotMatch(calls[3].url, /tenant_id=/);
});

test("budgets client returns structured errors", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 403,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "auth.permission_denied",
        message: "Permission denied.",
        request_id: "req_denied",
        trace_id: "trc_denied",
      };
    },
  });

  await assert.rejects(
    () => listBudgets({ controlApiUrl: "http://control.local", token: "jwt-token" }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 403);
      assert.equal(error.requestId, "req_denied");
      return true;
    },
  );
});

test("renderBudgetPanel puts one rule list before employee lookup and opens an editor dialog", () => {
  const html = renderBudgetPanel(
    {
      state: "ok",
      rules: budgetRulesPayload,
      status: budgetStatusPayload,
      feedback: "预算已保存",
    },
    budgetWriterContext(),
    virtualKeysState,
    projectState,
  );

  assert.match(html, /budget-rule-form/);
  assert.match(html, /<dialog class="budget-rule-dialog"/);
  assert.match(html, /data-budget-open-form/);
  assert.match(html, /预算周期/);
  assert.match(html, /启用时间/);
  assert.match(html, /生产 Key 日预算/);
  assert.match(html, /达到告警/);
  assert.match(html, /data-budget-edit-rule-id="br_1"/);
  assert.match(html, /预算已保存/);
  assert.match(html, /项目 · 账务核心/);
  assert.match(html, /应用 · 账务 API/);
  assert.match(html, /员工姓名或邮箱/);
  assert.doesNotMatch(html, /id="budget-scope-chain-key"/);
  assert.doesNotMatch(html, /mhv_prod_\*\*\*\*/);
  assert.doesNotMatch(html, /tenant_id/);
  assert.ok(html.indexOf("已设置的预算") < html.indexOf("查询员工用量"));
  assert.doesNotMatch(html, /运行环境摘要|组织预算概览/);
});

test("renderBudgetPanel shows separate daily and monthly rules and unknown employee spend", () => {
  const daily = { ...budgetRulesPayload.budget_rules[0].today_status, budget_period: "day" };
  const monthly = {
    ...daily,
    budget_rule_id: "br_month",
    budget_period: "month",
    effective_from_utc: "2026-07-09T09:00:00Z",
    budget_cny: "500",
  };
  const scope = {
    scope_type: "virtual_key", scope_id: "vk_1", scope_label: "生产 Key",
  };
  const html = renderBudgetPanel({
    state: "ok",
    rules: { budget_rules: [budgetRulesPayload.budget_rules[0], {
      ...budgetRulesPayload.budget_rules[0], budget_rule_id: "br_month",
      name: "生产 Key 月预算", budget_period: "month", daily_budget_cny: "500",
      effective_from_utc: "2026-07-09T09:00:00Z", today_status: monthly,
    }] },
    selectedPerson: { user_id: "u", display_name: "测试员工", email: "test@example.com",
      keys: [{ virtual_key_id: "vk_1", name: "生产 Key" }],
      spent_day_cny: "0", spent_month_cny: null, spent_year_cny: null },
    selectedVirtualKeyId: "vk_1",
    status: {
      budget_date: "2026-07-09",
      statuses: [daily, monthly],
      aggregate: {
        aggregate_status: "threshold_reached", effective_action: "warn",
        effective_remaining_cny: "18", limiting_scope: scope,
        scope_chain: [scope],
      },
    },
  }, budgetWriterContext(), virtualKeysState, projectState);
  assert.match(html, /每日 100 CNY/);
  assert.match(html, /每月 500 CNY/);
  assert.match(html, /本周期计费从/);
  assert.match(html, /无法核实/);
  assert.match(html, /所选凭证适用的预算/);
  assert.equal((html.match(/data-budget-rule-id=/g) ?? []).length, 2);
});

test("renderBudgetPanel is read only after write revocation and hidden without read permission", () => {
  const readerHtml = renderBudgetPanel(
    {
      state: "ok",
      rules: budgetRulesPayload,
      status: budgetStatusPayload,
    },
    budgetReaderContext(),
    virtualKeysState,
    projectState,
  );
  const viewerHtml = renderBudgetPanel(
    { state: "ok" },
    withPermissionSnapshot({ user: { role: "viewer" } }, [PERMISSIONS.CONSOLE_BASIC_READ]),
    virtualKeysState,
  );

  assert.match(readerHtml, /当前角色可查看预算状态/);
  assert.doesNotMatch(readerHtml, /budget-rule-form/);
  assert.equal(viewerHtml, "");
});

test("renderDeveloperStart renders budgets active navigation content", () => {
  const container = { innerHTML: "" };
  renderDeveloperStart(container, {
    gatewayUrl: "http://gateway.local",
    controlApiUrl: "http://control.local",
    consoleUrl: "http://console.local",
    authContext: budgetWriterContext({
      user: { display_name: "Admin", email: "admin@example.com", role: "admin", tenant_id: "tenant_1" },
      tenant: { name: "Tenant" },
      session: { expires_at: "2026-07-09T12:00:00Z" },
    }),
    consoleShellState: {
      navigation: {
        active_navigation_item_id: "budgets",
        navigation_items: [
          {
            id: "budgets",
            label: "预算告警",
            group: "调用与成本",
            target_section: "budgets",
            description: "Budgets",
            empty_state_hint: "Add budget",
            order: 85,
          },
        ],
      },
    },
    budgetState: {
      state: "ok",
      rules: budgetRulesPayload,
      status: budgetStatusPayload,
    },
    virtualKeysState,
    projectState,
  });

  assert.match(container.innerHTML, /budgets-panel/);
  assert.match(container.innerHTML, /生产 Key 日预算/);
});
