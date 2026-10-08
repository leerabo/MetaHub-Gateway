import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 状态标签只解释后端预算状态，不在浏览器内重算阈值或替代账本判定。
const STATUS_LABELS = {
  unconfigured: "未配置",
  ok: "正常",
  threshold_reached: "达到告警",
  exhausted: "已耗尽",
};

// 预算动作标签用于把阻断或告警策略翻译为控制台可读文案，不能替代后端预算判定。
const ACTION_LABELS = {
  alert_only: "仅告警",
  block: "阻断",
  allow: "放行",
  warn: "告警",
};

const PERIOD_LABELS = { day: "每日", month: "每月", year: "每年" };

// 固定层级标签只负责展示，预算冲突顺序和 deny-wins 结论必须使用后端 aggregate。
const SCOPE_LABELS = {
  tenant: "租户",
  project: "项目",
  application: "应用",
  virtual_key: "调用 KEY",
};

/**
 * 渲染预算设置与状态面板。
 * 面板只做体验层面的角色隐藏；真实读取和写入权限仍由 /budgets 与 /budget-status 后端依赖校验。
 */
export function renderBudgetPanel(budgetState, authContext, virtualKeysState, projectState, environmentState) {
  // Console 只消费后端权限快照；缺少读取权限时零 DOM，不能用角色名恢复历史入口。
  if (!hasPermission(authContext, PERMISSIONS.BILLING_BUDGET_READ)) {
    return "";
  }
  if (!budgetState) {
    return renderLoadingPanel();
  }
  if (budgetState.state === "error") {
    return `${renderActionError(budgetState.actionError)}${renderErrorPanel(budgetState)}`;
  }

  const rulesPayload = budgetState.rules ?? {};
  const statusPayload = budgetState.status ?? {};
  const rules = rulesPayload.budget_rules ?? budgetState.budget_rules ?? [];
  const statuses = statusPayload.statuses ?? [];
  const aggregate = statusPayload.aggregate ?? deriveLegacyAggregate(statuses);
  const summaries = rulesPayload.environment_summaries ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.BILLING_BUDGET_WRITE);
  return `
    <div class="panel-heading budget-heading">
      <div>
        <h2 id="budgets-title">调用成本预算</h2>
        <p>按日、月或年限制模型调用成本。<a href="console.html#workspace.finance.quota">额度管理</a>查看组织账户的付费、试用等余额。</p>
      </div>
      <span class="budget-count">${escapeHtml(rules.length)} 条规则</span>
    </div>
    <div class="console-page-toolbar budget-toolbar">
      ${renderActionError(budgetState.actionError)}
      ${renderFeedback(budgetState.feedback)}
    </div>
    ${renderPeoplePicker(budgetState)}
    ${renderAggregateBand(aggregate, statusPayload.budget_date, rules.length, budgetState.selectedPerson)}
    ${renderEnvironmentSummaries(summaries)}
    ${renderScopeChain(statuses, aggregate, budgetState.selectedPerson)}
    ${canWrite ? renderBudgetForm(budgetState.selectedPerson, projectState, environmentState, rules) : renderReadOnlyNotice()}
    ${renderRuleTable(rules, canWrite)}
  `;
}

/**
 * 渲染预算接口加载状态，避免导航切换时出现空白区域。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="budgets-title">预算中心</h2>
      <p>正在读取当前租户预算规则与今日状态。</p>
    </div>
    <div class="empty-state">等待 /budgets 和 /budget-status 响应。</div>
  `;
}

/**
 * 渲染预算接口错误状态。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="budgets-title">预算中心</h2>
      <p>预算状态暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "预算接口读取失败，请稍后重试")}
      ${state.requestId ? `<small>请求 ID：${escapeHtml(state.requestId)}</small>` : ""}
    </div>
  `;
}

/**
 * 为旧响应构造只读聚合后备值。
 *
 * 新合同始终以后端 aggregate 为准；该分支只保证升级期间旧测试夹具和滚动发布节点可读，
 * 不在前端重新解释跨层预算优先级。
 */
function deriveLegacyAggregate(statuses) {
  if (!statuses.length) {
    return {
      aggregate_status: "unconfigured",
      effective_action: "allow",
      effective_remaining_cny: null,
      limiting_rule_id: null,
      limiting_scope: null,
      conflict_policy: "all_applicable_caps_deny_wins_v1",
      scope_chain: [],
    };
  }
  const limiting = statuses.find((item) => item.next_action === "block") ?? statuses[0];
  return {
    aggregate_status: statuses.some((item) => item.status === "exhausted")
      ? "exhausted"
      : statuses.some((item) => item.status === "threshold_reached") ? "threshold_reached" : "ok",
    effective_action: statuses.some((item) => item.next_action === "block")
      ? "block"
      : statuses.some((item) => item.next_action === "warn") ? "warn" : "allow",
    effective_remaining_cny: limiting.effective_remaining_cny ?? limiting.estimated_remaining_cny ?? null,
    limiting_rule_id: limiting.budget_rule_id ?? null,
    limiting_scope: limiting.budget_scope ?? null,
    conflict_policy: "all_applicable_caps_deny_wins_v1",
    scope_chain: statuses.map((item) => item.budget_scope ?? {
      scope_type: item.scope_type,
      scope_id: item.scope_id,
      scope_label: item.scope_label,
    }),
  };
}

/**
 * 渲染当前可信 scope chain 的总门禁结论。
 * 前端只展示后端 deny-wins 结果，不根据卡片颜色重新决定是否放行。
 */
function renderAggregateBand(aggregate, budgetDate, ruleCount, selectedPerson) {
  const limitingScope = aggregate.limiting_scope;
  return `
    <section class="budget-aggregate status-${escapeAttribute(aggregate.aggregate_status ?? "unconfigured")}" aria-label="有效预算约束">
      <h3 class="budget-aggregate-title">${selectedPerson ? `${escapeHtml(selectedPerson.display_name)}所选调用凭证的预算判断` : "组织预算概览"}</h3>
      <div>
        <span>当前状态</span>
        <strong>${escapeHtml(STATUS_LABELS[aggregate.aggregate_status] ?? aggregate.aggregate_status ?? "未配置")}</strong>
      </div>
      <div>
        <span>有效动作</span>
        <strong>${escapeHtml(ACTION_LABELS[aggregate.effective_action] ?? aggregate.effective_action ?? "放行")}</strong>
      </div>
      <div>
        <span>限制层级</span>
        <strong>${escapeHtml(scopeDisplayName(limitingScope))}</strong>
      </div>
      <div>
        <span>有效剩余</span>
        <strong>${escapeHtml(aggregate.effective_remaining_cny ?? "-")} CNY</strong>
      </div>
      <div>
        <span>业务日</span>
        <strong>${escapeHtml(budgetDate ?? "-")}</strong>
      </div>
      <div>
        <span>组织规则总数</span>
        <strong>${escapeHtml(ruleCount)}</strong>
      </div>
    </section>
  `;
}

/**
 * 按 Environment 展示三级状态计数。
 * 同一 Usage 会被多层规则覆盖，因此这里只显示规则和状态数量，绝不在浏览器内相加金额。
 */
function renderEnvironmentSummaries(summaries) {
  if (!summaries.length) {
    return "";
  }
  return `
    <section class="budget-summary-section" aria-label="运行环境预算摘要">
      <h3>运行环境摘要</h3>
      ${summaries.map((environment) => `
        <div class="budget-environment-summary">
          <header>
            <strong>${escapeHtml(environment.environment_name ?? environment.environment ?? environment.environment_id)}</strong>
            <span>${escapeHtml(environment.environment ?? environment.environment_id)} · ${escapeHtml(environment.budget_date ?? "-")}</span>
          </header>
          <div class="budget-scope-summary-grid">
            ${(environment.scope_summaries ?? [])
              .filter((item) => item.scope_type !== "application" || item.rule_count > 0)
              .map(renderScopeSummary)
              .join("")}
          </div>
        </div>
      `).join("")}
    </section>
  `;
}

/**
 * 渲染单个层级的状态计数，字段全部来自后端同一事务快照。
 */
function renderScopeSummary(item) {
  return `
    <div class="budget-scope-summary">
      <strong>${escapeHtml(SCOPE_LABELS[item.scope_type] ?? item.scope_type)}</strong>
      <span>${escapeHtml(item.rule_count ?? 0)} 条</span>
      <dl>
        ${renderPair("正常", item.ok_count ?? 0)}
        ${renderPair("告警", item.warning_count ?? 0)}
        ${renderPair("耗尽", item.exhausted_count ?? 0)}
        ${renderPair("阻断", item.block_count ?? 0)}
      </dl>
    </div>
  `;
}

/**
 * 渲染调用 KEY 作用链选择器。
 * 选择值只发送不透明 Key ID；运行环境、项目和应用由后端重新恢复并校验。
 */
function renderPeoplePicker(state) {
  const people = state.people?.people ?? [];
  const total = state.people?.total ?? 0;
  const offset = state.peopleOffset ?? 0;
  const selected = state.selectedPerson;
  return `
    <section class="budget-people" aria-label="查找员工预算">
      <div class="budget-people-heading">
        <div><h3>查看员工预算</h3><p>按姓名或邮箱查找员工。选中后查看他的调用凭证及适用的组织、项目、应用预算。</p></div>
        <form id="budget-person-search-form" role="search">
          <label for="budget-person-search">员工姓名或邮箱</label>
          <div><input id="budget-person-search" name="search" value="${escapeAttribute(state.peopleSearch ?? "")}" placeholder="输入姓名或邮箱" maxlength="160" />
          <button type="submit">查找</button></div>
        </form>
      </div>
      <div class="budget-person-results" aria-label="员工查找结果">
        ${people.map((person) => `<button type="button" data-budget-person-id="${escapeAttribute(person.user_id)}" class="${selected?.user_id === person.user_id ? "is-selected" : ""}">
          <strong>${escapeHtml(person.display_name)}</strong><span>${escapeHtml(person.email)}</span><small>${escapeHtml(person.key_count)} 个调用凭证</small>
        </button>`).join("") || "<p>没有找到员工。请核对姓名或邮箱。</p>"}
      </div>
      ${total > 20 ? `<div class="budget-person-pagination">
        <button type="button" data-budget-person-page="-1" ${offset === 0 ? "disabled" : ""}>上一页</button>
        <span>${escapeHtml(offset + 1)}–${escapeHtml(Math.min(offset + 20, total))} / ${escapeHtml(total)}</span>
        <button type="button" data-budget-person-page="1" ${offset + 20 >= total ? "disabled" : ""}>下一页</button>
      </div>` : ""}
      ${selected ? `<div class="budget-selected-person"><strong>${escapeHtml(selected.display_name)}的调用成本</strong><span>${escapeHtml(selected.email)}</span>
        <div class="budget-person-spend">
          <div><small>今日已入账</small><strong>${escapeHtml(selected.spent_day_cny ?? "-")} CNY</strong></div>
          <div><small>本月已入账</small><strong>${escapeHtml(selected.spent_month_cny ?? "-")} CNY</strong></div>
          <div><small>本年已入账</small><strong>${escapeHtml(selected.spent_year_cny ?? "-")} CNY</strong></div>
        </div>
        <p>这里合计该员工名下所有调用凭证的正式入账成本。预算限制按组织、项目、应用和单把凭证设置；如有多把凭证，请逐一查看其适用限制。</p>
        ${selected.keys?.length ? `<div class="budget-person-keys">${selected.keys.map((key) => `<button type="button" data-budget-person-key="${escapeAttribute(key.virtual_key_id)}" class="${state.selectedVirtualKeyId === key.virtual_key_id ? "is-selected" : ""}">${escapeHtml(displayVirtualKeyName(key.name))}<small>${escapeHtml(key.environment_name ?? "")}</small></button>`).join("")}</div>` : "<p>此员工当前没有可选调用凭证；历史成本仍计入上方汇总。</p>"}
      </div>` : "<p class=\"budget-person-hint\">先选择员工；下方暂显示组织层预算概览。</p>"}
    </section>
  `;
}

/**
 * 渲染后端返回的可信 scope chain 及正式账本、预留和有效消耗拆分。
 */
function renderScopeChain(statuses, aggregate, selectedPerson) {
  const scopeChain = aggregate.scope_chain ?? [];
  if (!scopeChain.length) {
    return `<div class="empty-state budget-empty">当前运行环境尚无适用预算。</div>`;
  }
  return `
    <section class="budget-chain-section" aria-label="适用的预算限制">
      <h3>${selectedPerson ? "这次调用会受到哪些预算限制" : "组织预算限制"}</h3>
      <p>从组织到调用凭证逐级检查；任一级设置为“耗尽后阻断”且额度已用完，请求就会被阻断。各层金额可能覆盖同一笔调用，不能相加。</p>
      <ol class="budget-chain-cards">
        ${scopeChain.map((scope) => renderScopeChainRow(scope, statuses)).join("")}
      </ol>
    </section>
  `;
}

/**
 * 渲染一层预算状态；无规则的权威 scope 仍保留在链中并标记“未配置”。
 */
function renderScopeChainRow(scope, statuses) {
  const item = statuses.find((status) => (
    status.scope_type === scope.scope_type && status.scope_id === scope.scope_id
  ));
  return `
    <li class="budget-chain-card" data-budget-scope-id="${escapeAttribute(scope.budget_scope_id ?? scope.scope_id ?? "")}">
      <div class="budget-chain-card-heading"><strong>${escapeHtml(SCOPE_LABELS[scope.scope_type] ?? scope.scope_type)} · ${escapeHtml(scopeDisplayName(scope))}</strong>
        <mark class="status-mark status-${escapeAttribute(item?.status ?? "unconfigured")}">
          ${escapeHtml(STATUS_LABELS[item?.status] ?? item?.status ?? "未配置")}
        </mark>
      </div>
      ${item ? `<div class="budget-chain-card-values">
        <span>周期：<strong>${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")}</strong></span>
        <span>预算：<strong>${escapeHtml(item.budget_cny)} CNY</strong></span>
        <span>已用：<strong>${escapeHtml(item.effective_spend_cny ?? item.spent_cny)} CNY</strong></span>
        <span>剩余：<strong>${escapeHtml(item.effective_remaining_cny ?? item.estimated_remaining_cny)} CNY</strong></span>
      </div><p>已用金额含正式入账 ${escapeHtml(item.ledger_spent_cny ?? item.spent_cny)} CNY 和待结算预留 ${escapeHtml(item.reserved_cny ?? "0")} CNY；本层耗尽后${escapeHtml(item.exhausted_action === "block" ? "阻断" : "仅告警")}。</p>${renderBlockNotice(item)}`
      : "<p>本层未设预算，不单独限制调用。</p>"}
    </li>
  `;
}

/**
 * 返回安全的 scope 展示名；未知或空限制层级不制造虚假的前端推断。
 */
function scopeDisplayName(scope) {
  if (!scope) {
    return "-";
  }
  const label = scope.scope_label
    ?? `${SCOPE_LABELS[scope.scope_type] ?? scope.scope_type ?? "scope"} · ${scope.scope_id ?? "-"}`;
  return cleanScopeLabel(label);
}

function cleanScopeLabel(label) {
  // Key prefixes are useful for diagnostics but add no meaning to a budget decision.
  // Keep the opaque id in data attributes and show only the administrator's label.
  const cleaned = String(label).replace(/\s*\((?:mhv|mh|sk|pk|key)[a-z0-9_*:-]*\)\s*$/iu, "").trim();
  if (/^local development virtual key$/iu.test(cleaned)) {
    return /^en(?:-|$)/iu.test(globalThis.document?.documentElement?.lang ?? "")
      ? "Default organization KEY"
      : "默认调用 KEY";
  }
  return cleaned || "-";
}

function displayVirtualKeyName(name) {
  return cleanScopeLabel(name || "未命名调用 KEY");
}
/**
 * 渲染 active BudgetBlock 提示，使用后端调用链字段帮助管理员定位首次阻断。
 */
function renderBlockNotice(item) {
  if (item.block_status !== "active" || !item.budget_block_id) {
    return "";
  }
  return `
    <div class="budget-block-notice" role="status" data-budget-block-id="${escapeAttribute(item.budget_block_id)}">
      <strong>请求已阻断</strong>
      <span>${escapeHtml(formatDateTime(item.blocked_at))}</span>
    </div>
  `;
}

/**
 * 渲染管理员预算写入表单。
 * 表单同时覆盖租户级和 Virtual Key 级规则，scope_id 只在 Key 预算时提交。
 */
function renderBudgetForm(selectedPerson, projectState, environmentState, rules) {
  const virtualKeys = selectedPerson?.keys ?? [];
  const projects = projectState?.projects ?? [];
  const applications = projectState?.applications ?? [];
  const environments = environmentState?.state === "ok"
    ? (environmentState.environments ?? []).filter((item) => item.status === "active")
    : [];
  return `
    <details class="budget-create-disclosure">
      <summary>新增或修改成本预算</summary>
      <p>按运行环境和对象设置一个预算周期。员工可能有多把调用凭证；当前单把凭证预算不等于该员工的总额限制，请先在上方查找员工并选择凭证。</p>
      <form class="budget-rule-form" id="budget-rule-form">
      <label>
        <span>环境</span>
        <select name="environmentId" ${environments.length ? "" : "disabled"}>
          ${environments.length
            ? environments.map((item) => `<option value="${escapeAttribute(item.environment_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.code)}</option>`).join("")
            : `<option value="">请先创建可用运行环境</option>`}
        </select>
      </label>
      <label>
        <span>维度</span>
        <select name="scopeType">
          <option value="tenant">租户</option>
          <option value="project">项目</option>
          <option value="application">应用</option>
          <option value="virtual_key" ${virtualKeys.length ? "" : "disabled"}>员工调用凭证</option>
        </select>
      </label>
      <label>
        <span>具体对象</span>
        <select name="scopeId">
          <option value="" data-budget-scope-option="tenant">租户预算不需要选择</option>
          ${projects
            .map((item) => `<option value="${escapeAttribute(item.project_id)}" data-budget-scope-option="project" hidden disabled>项目 · ${escapeHtml(item.name)} (${escapeHtml(item.project_code ?? "-")})</option>`)
            .join("")}
          ${applications
            .map((item) => `<option value="${escapeAttribute(item.application_id)}" data-budget-scope-option="application" hidden disabled>应用 · ${escapeHtml(item.name)} (${escapeHtml(item.project_name ?? "-")})</option>`)
            .join("")}
          ${virtualKeys
            .map((item) => `<option value="${escapeAttribute(item.virtual_key_id)}" data-budget-scope-option="virtual_key" data-environment-id="${escapeAttribute(item.environment_id ?? "")}" hidden disabled>${escapeHtml(selectedPerson.display_name)} · ${escapeHtml(displayVirtualKeyName(item.name))}</option>`)
            .join("")}
        </select>
      </label>
      <label>
        <span>名称</span>
        <input name="name" placeholder="例如：研发团队月预算" />
      </label>
      <label>
        <span>预算周期</span>
        <select name="budgetPeriod"><option value="day">每天</option><option value="month">每月</option><option value="year">每年</option></select>
      </label>
      <label>
        <span>预算金额 CNY</span>
        <input name="dailyBudgetCny" inputmode="decimal" placeholder="1000" required />
      </label>
      <label>
        <span>告警阈值 %</span>
        <input name="warningThresholdPercent" type="number" min="1" max="99" value="80" required />
      </label>
      <label>
        <span>100% 动作</span>
        <select name="exhaustedAction">
          <option value="alert_only">仅告警</option>
          <option value="block">阻断</option>
        </select>
      </label>
      <label>
        <span>状态</span>
        <select name="status">
          <option value="active">启用</option>
          <option value="disabled">停用</option>
        </select>
      </label>
      <div class="budget-actions">
        <button type="submit" ${environments.length ? "" : "disabled"}>保存预算</button>
        <span>${escapeHtml(rules.length ? "已配置规则可直接更新" : "当前没有预算规则")}</span>
      </div>
      </form>
    </details>
  `;
}

/**
 * 渲染开发者只读提示，避免误以为前端隐藏表单等同于权限控制。
 */
function renderReadOnlyNotice() {
  return `
    <div class="budget-readonly">
      当前角色可查看预算状态；预算金额和阻断策略只能由管理员修改。
    </div>
  `;
}

/**
 * 渲染预算规则表，展示配置和今日状态的对应关系。
 */
function renderRuleTable(rules, canWrite) {
  return `
    <div class="budget-rule-table" role="table" aria-label="预算规则">
      <div class="budget-rule-row budget-rule-head" role="row">
        <span>规则</span>
        <span>维度</span>
        <span>周期预算</span>
        <span>告警</span>
        <span>100% 动作</span>
        <span>本周期状态</span>
        <span>更新时间</span>
      </div>
      ${rules.length ? rules.map((item) => renderRuleRow(item, canWrite)).join("") : renderEmptyRuleRow()}
    </div>
  `;
}

/**
 * 渲染单条预算规则，金额和状态均使用后端返回的规范化字段。
 */
function renderRuleRow(item, canWrite) {
  const today = item.today_status ?? {};
  return `
    <div class="budget-rule-row" role="row" data-budget-rule-id="${escapeAttribute(item.budget_rule_id)}">
      <span class="budget-rule-name" data-label="规则">
        <strong>${escapeHtml(item.name)}</strong>
      </span>
      <span class="budget-rule-scope" data-label="维度">${escapeHtml(item.environment_name ?? item.environment ?? "-")} · ${escapeHtml(cleanScopeLabel(item.scope_label ?? item.scope_id))}</span>
      <span data-label="周期预算">${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")} ${escapeHtml(item.daily_budget_cny)} CNY</span>
      <span data-label="告警">${escapeHtml(item.warning_threshold_percent)}%</span>
      <span class="budget-mode-cell" data-label="100% 动作">
        ${canWrite ? renderModeControl(item) : escapeHtml(ACTION_LABELS[item.exhausted_action] ?? item.exhausted_action)}
      </span>
      <span data-label="本周期状态">
        <mark class="status-mark status-${escapeAttribute(today.status ?? "ok")}">
          ${escapeHtml(STATUS_LABELS[today.status] ?? today.status ?? "-")}
        </mark>
        <em>${escapeHtml(today.usage_percent ?? 0)}%</em>
        ${item.active_block ? `<strong class="budget-block-label">已阻断</strong>` : ""}
      </span>
      <time data-label="更新时间" datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
    </div>
  `;
}

/**
 * 渲染管理员快捷模式选择器；后端仍会按 tenant_id 和 admin 角色重新校验规则。
 */
function renderModeControl(item) {
  return `
    <select
      data-budget-mode-rule-id="${escapeAttribute(item.budget_rule_id)}"
      aria-label="${escapeAttribute(`${item.name} 耗尽模式`)}"
    >
      <option value="alert_only" ${item.exhausted_action === "alert_only" ? "selected" : ""}>仅告警</option>
      <option value="block" ${item.exhausted_action === "block" ? "selected" : ""}>阻断</option>
    </select>
  `;
}

/**
 * 渲染空预算规则表行。
 * 当前租户尚未配置规则时仍保持表格结构稳定，避免用户误以为页面加载失败。
 */
function renderEmptyRuleRow() {
  return `
    <div class="budget-rule-row budget-rule-empty" role="row">
      <span>尚未配置成本预算。</span>
    </div>
  `;
}

/**
 * 渲染预算写入错误提示。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="budget-action-error" role="alert">
      ${escapeHtml(error.message ?? "预算操作失败，请稍后重试")}
      ${error.requestId ? `<small>请求 ID：${escapeHtml(error.requestId)}</small>` : ""}
    </div>
  `;
}

/**
 * 渲染预算操作反馈。
 */
function renderFeedback(feedback) {
  return feedback ? `<div class="budget-feedback">${escapeHtml(feedback)}</div>` : "";
}

/**
 * 渲染详情键值对。
 */
function renderPair(label, value) {
  return `
    <dt>${escapeHtml(label)}</dt>
    <dd>${escapeHtml(value ?? "-")}</dd>
  `;
}

/**
 * 限制进度条宽度在 0-100 之间，避免异常数据撑破布局。
 */
function progressWidth(value) {
  const parsed = Number(value ?? 0);
  if (!Number.isFinite(parsed)) {
    return 0;
  }
  return Math.max(0, Math.min(100, parsed));
}

/**
 * 格式化 ISO 时间，避免表格中出现过长的小数秒。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义，防止预算名称或后端错误消息破坏页面结构。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * HTML 属性转义，额外处理反引号以保护 data-* 和 style 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
