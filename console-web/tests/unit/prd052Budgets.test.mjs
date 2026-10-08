// PRD-052 预算中心专项：冻结 create-only、三级聚合、可信作用链、权限呈现和转义合同。
import assert from "node:assert/strict";
import test from "node:test";

import { renderBudgetPanel } from "../../src/features/budgets/renderBudgetPanel.js";
import { AuthApiError } from "../../src/lib/auth.js";
import {
  createBudgetRule,
  getBudgetStatus,
  listBudgets,
  updateBudgetRule,
} from "../../src/lib/budgets.js";
import { PERMISSIONS, withPermissionSnapshot } from "./permissionTestContext.mjs";

/** 构造预算中心的显式后端权限快照。 */
function budgetContext(permissionCodes, role = "finance_admin") {
  return withPermissionSnapshot({ user: { role } }, permissionCodes);
}

/**
 * 构造测试专用 BudgetScope DTO。
 * 参数只使用不透明 ID 和展示标签，不包含真实 Key 或跨租户业务数据。
 */
function scope(scopeType, scopeId, label, priority, parentScopeType = null, parentScopeId = null) {
  return {
    budget_scope_id: `bsc_${scopeType}_${scopeId}`,
    tenant_id: "tenant_1",
    environment_id: "env_prod",
    scope_type: scopeType,
    scope_id: scopeId,
    scope_label: label,
    priority,
    parent_scope_type: parentScopeType,
    parent_scope_id: parentScopeId,
  };
}

const tenantScope = scope("tenant", "tenant_1", "示例租户", 0);
const projectScope = scope("project", "prj_a", "账务项目", 1, "tenant", "tenant_1");
const keyScope = scope("virtual_key", "vk_a", "生产 Key (mhv_prod_****)", 3, "project", "prj_a");

/**
 * 构造单条预算状态，金额均使用后端十进制字符串，避免测试把财务值转成浮点数。
 */
function statusFor(budgetScope, overrides = {}) {
  return {
    budget_rule_id: `br_${budgetScope.scope_type}`,
    environment_id: "env_prod",
    environment: "prod",
    environment_name: "生产环境",
    scope_type: budgetScope.scope_type,
    scope_id: budgetScope.scope_id,
    scope_label: budgetScope.scope_label,
    budget_scope: budgetScope,
    budget_date: "2026-07-27",
    ledger_spent_cny: "72.000000",
    reserved_cny: "8.000000",
    effective_spend_cny: "80.000000",
    effective_remaining_cny: "20.000000",
    spent_cny: "72.000000",
    estimated_remaining_cny: "20.000000",
    budget_cny: "100.000000",
    usage_percent: 80,
    status: "threshold_reached",
    exhausted_action: "block",
    next_action: "warn",
    last_event_type: "budget.threshold_reached",
    block_status: null,
    budget_block_id: null,
    blocked_at: null,
    ...overrides,
  };
}

const statuses = [
  statusFor(tenantScope, {
    budget_rule_id: "br_tenant",
    budget_cny: "500.000000",
    effective_remaining_cny: "420.000000",
    usage_percent: 16,
    status: "ok",
    next_action: "allow",
  }),
  statusFor(projectScope, {
    budget_rule_id: "br_project",
    status: "exhausted",
    next_action: "block",
    effective_remaining_cny: "0",
    usage_percent: 100,
    block_status: "active",
    budget_block_id: "bb_project_a",
    blocked_at: "2026-07-27T04:30:00Z",
  }),
  statusFor(keyScope, {
    budget_rule_id: "br_key",
    effective_remaining_cny: "5.000000",
    usage_percent: 95,
  }),
];

const rulesPayload = {
  budget_rules: statuses.map((item, index) => ({
    budget_rule_id: item.budget_rule_id,
    tenant_id: "tenant_1",
    environment_id: "env_prod",
    environment: "prod",
    environment_name: "生产环境",
    scope_type: item.scope_type,
    scope_id: item.scope_id,
    scope_label: item.scope_label,
    budget_scope: item.budget_scope,
    name: ["租户日预算", "Project A 日预算", "Key A 日预算"][index],
    daily_budget_cny: item.budget_cny,
    warning_threshold_percent: 80,
    exhausted_threshold_percent: 100,
    exhausted_action: item.exhausted_action,
    status: "active",
    today_status: item,
    active_block: item.budget_block_id ? {
      budget_block_id: item.budget_block_id,
      budget_rule_id: item.budget_rule_id,
      environment_id: "env_prod",
      scope_type: item.scope_type,
      scope_id: item.scope_id,
      budget_date: "2026-07-27",
      status: "active",
      blocked_cost_cny: "100.000000",
      budget_cny: item.budget_cny,
      blocked_request_id: "req_blocked",
      blocked_trace_id: "trc_blocked",
      blocked_at: item.blocked_at,
    } : null,
    created_at: "2026-07-27T01:00:00Z",
    updated_at: "2026-07-27T02:00:00Z",
  })),
  environment_summaries: [
    {
      environment_id: "env_prod",
      environment: "prod",
      environment_name: "生产环境",
      budget_date: "2026-07-27",
      scope_summaries: [
        { scope_type: "tenant", rule_count: 1, ok_count: 1, warning_count: 0, exhausted_count: 0, block_count: 0 },
        { scope_type: "project", rule_count: 1, ok_count: 0, warning_count: 0, exhausted_count: 1, block_count: 1 },
        { scope_type: "application", rule_count: 0, ok_count: 0, warning_count: 0, exhausted_count: 0, block_count: 0 },
        { scope_type: "virtual_key", rule_count: 1, ok_count: 0, warning_count: 1, exhausted_count: 0, block_count: 0 },
      ],
    },
  ],
  request_id: "req_rules",
  trace_id: "trc_rules",
};

const statusPayload = {
  budget_date: "2026-07-27",
  statuses,
  aggregate: {
    aggregate_status: "exhausted",
    effective_action: "block",
    effective_remaining_cny: "0",
    limiting_rule_id: "br_project",
    limiting_scope: projectScope,
    conflict_policy: "all_applicable_caps_deny_wins_v1",
    scope_chain: [tenantScope, projectScope, keyScope],
  },
  request_id: "req_status",
  trace_id: "trc_status",
};

test("PRD-052 budget client separates create-only POST from compatibility PATCH", async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    return {
      ok: true,
      status: options.method === "POST" ? 201 : 200,
      headers: new Map([["X-Request-ID", "req_budget_write"]]),
      async json() {
        return { budget_rule: rulesPayload.budget_rules[0], request_id: "req_budget_write", trace_id: "trc_budget_write" };
      },
    };
  };

  const input = {
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    scopeType: "project",
    scopeId: "prj_a",
    environmentId: "env_prod",
    name: "Project A 日预算",
    dailyBudgetCny: "100.000000",
    warningThresholdPercent: 80,
    exhaustedAction: "block",
    status: "active",
  };
  await createBudgetRule(input);
  await updateBudgetRule(input);

  assert.equal(calls[0].options.method, "POST");
  assert.equal(calls[1].options.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    scope_type: "project",
    scope_id: "prj_a",
    environment_id: "env_prod",
    name: "Project A 日预算",
    daily_budget_cny: "100.000000",
    warning_threshold_percent: 80,
    exhausted_action: "block",
    status: "active",
  });
  assert.doesNotMatch(calls[0].options.body, /tenant_id|actor|token/i);
});

test("PRD-052 budget client keeps 409 code and request_id without fallback overwrite", async () => {
  globalThis.fetch = async () => ({
    ok: false,
    status: 409,
    headers: new Map([["X-Request-ID", "req_header"]]),
    async json() {
      return {
        code: "budget.rule_conflict",
        message: "该作用域预算规则已存在。",
        request_id: "req_conflict",
        trace_id: "trc_conflict",
      };
    },
  });

  await assert.rejects(
    () => createBudgetRule({
      controlApiUrl: "http://control.local",
      token: "jwt-token",
      scopeType: "tenant",
      environmentId: "env_prod",
      dailyBudgetCny: "100",
    }),
    (error) => {
      assert.ok(error instanceof AuthApiError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "budget.rule_conflict");
      assert.equal(error.requestId, "req_conflict");
      return true;
    },
  );
});

test("PRD-052 reads a key-qualified scope chain without sending tenant_id", async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return {
      ok: true,
      status: 200,
      headers: new Map(),
      async json() {
        return String(url).includes("/budget-status") ? statusPayload : rulesPayload;
      },
    };
  };

  await listBudgets({ controlApiUrl: "http://control.local", token: "jwt-token", environmentId: "env_prod" });
  const result = await getBudgetStatus({
    controlApiUrl: "http://control.local",
    token: "jwt-token",
    virtualKeyId: "vk_a",
  });

  assert.equal(result.aggregate.limiting_rule_id, "br_project");
  assert.match(calls[1], /virtual_key_id=vk_a/);
  assert.doesNotMatch(calls.join(" "), /tenant_id=/);
});

test("PRD-052 budget center keeps one rule list and contextual employee decision", () => {
  const html = renderBudgetPanel(
    {
      state: "ok",
      rules: rulesPayload,
      status: statusPayload,
      selectedVirtualKeyId: "vk_a",
      selectedPerson: {
        user_id: "user_a", display_name: "测试员工", email: "user@example.com", key_count: 1,
        keys: [{ virtual_key_id: "vk_a", name: "生产 Key", environment_id: "env_prod", environment_name: "生产环境" }],
      },
      feedback: "预算已创建",
    },
    budgetContext(
      [PERMISSIONS.BILLING_BUDGET_READ, PERMISSIONS.BILLING_BUDGET_WRITE],
      "admin",
    ),
    {
      virtual_keys: [
        { virtual_key_id: "vk_a", name: "生产 Key", key_prefix: "mhv_prod_****", environment_id: "env_prod" },
      ],
    },
    {
      projects: [{ project_id: "prj_a", project_code: "billing", name: "账务项目" }],
      applications: [],
    },
    {
      state: "ok",
      environments: [{ environment_id: "env_prod", code: "prod", name: "生产环境", status: "active" }],
    },
  );

  assert.match(html, /模型调用成本上限/);
  assert.doesNotMatch(html, /运行环境摘要|组织预算概览/);
  assert.match(html, /所选凭证适用的预算/);
  assert.match(html, /当前调用阻断/);
  assert.match(html, /Project A 日预算/);
  assert.match(html, /已用 80.000000/);
  assert.match(html, /当前已阻断调用/);
  assert.doesNotMatch(html, /bb_project_a/);
  assert.doesNotMatch(html, /<em>bsc_/);
  assert.match(html, /data-budget-scope-option="project"/);
  assert.match(html, /data-budget-scope-option="virtual_key"/);
  assert.match(html, /id="budget-person-search-form"/);
  assert.match(html, /data-budget-person-key="vk_a"/);
  assert.doesNotMatch(html, /tenant_id/);
});

test("PRD-052 budget center honors revoked write permission, hides unauthorized users and escapes names", () => {
  const maliciousRules = {
    ...rulesPayload,
    budget_rules: [{
      ...rulesPayload.budget_rules[0],
      name: '<img src=x onerror="alert(1)">',
      scope_label: "<script>bad()</script>",
    }],
  };
  const readerHtml = renderBudgetPanel(
    { state: "ok", rules: maliciousRules, status: statusPayload },
    budgetContext([PERMISSIONS.BILLING_BUDGET_READ]),
    { virtual_keys: [] },
    { projects: [], applications: [] },
    { state: "ok", environments: [] },
  );
  const viewerHtml = renderBudgetPanel(
    { state: "ok", rules: rulesPayload, status: statusPayload },
    budgetContext([PERMISSIONS.CONSOLE_BASIC_READ], "viewer"),
  );

  assert.match(readerHtml, /当前角色可查看预算状态/);
  assert.doesNotMatch(readerHtml, /budget-rule-form/);
  assert.ok(readerHtml.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"));
  assert.doesNotMatch(readerHtml, /<img|<script/);
  assert.equal(viewerHtml, "");
});
