import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 状态标签只解释后端预算状态，不在浏览器内重算阈值或替代账本判定。
const STATUS_LABELS = {
  unconfigured: "未配置",
  scheduled: "待启用",
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
  const canWrite = hasPermission(authContext, PERMISSIONS.BILLING_BUDGET_WRITE);
  return `
    <p class="budget-page-intro">设置模型调用成本上限；账户余额与付款请到<a href="console.html#workspace.finance.quota">额度管理</a>查看。</p>
    <div class="console-page-toolbar budget-toolbar">
      ${renderActionError(budgetState.actionError)}
      ${renderFeedback(budgetState.feedback)}
    </div>
    ${canWrite ? renderBudgetForm(projectState, environmentState) : renderReadOnlyNotice()}
    ${renderRuleTable(rules, canWrite, rulesPayload.total ?? rules.length, budgetState.ruleOffset ?? 0)}
    ${renderPeoplePicker(budgetState)}
    ${budgetState.selectedPerson ? renderScopeChain(statuses, aggregate, budgetState.selectedPerson, budgetState.statusError) : ""}
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
        <div><h3>查询员工用量</h3><p>按姓名或邮箱查找。选中员工后可查看入账成本和调用凭证。</p></div>
        <form id="budget-person-search-form" role="search">
          <label for="budget-person-search">员工姓名或邮箱</label>
          <div><input id="budget-person-search" name="search" value="${escapeAttribute(state.peopleSearch ?? "")}" placeholder="输入姓名或邮箱" maxlength="160" />
          <button type="submit">查找</button></div>
        </form>
      </div>
      <div class="budget-person-results" aria-label="员工查找结果">
        ${people.map((person) => `<button type="button" data-budget-person-id="${escapeAttribute(person.user_id)}" class="${selected?.user_id === person.user_id ? "is-selected" : ""}">
          <strong>${escapeHtml(person.display_name)}</strong><span>${escapeHtml(person.email)}</span><small>${escapeHtml(person.key_count)} 个调用凭证</small>
        </button>`).join("") || (state.peopleSearch ? "<p>没有找到员工。请核对姓名或邮箱。</p>" : "<p>输入姓名或邮箱后查找；每页最多显示 20 人。</p>")}
      </div>
      ${total > 20 ? `<div class="budget-person-pagination">
        <button type="button" data-budget-person-page="-1" ${offset === 0 ? "disabled" : ""}>上一页</button>
        <span>${escapeHtml(offset + 1)}–${escapeHtml(Math.min(offset + 20, total))} / ${escapeHtml(total)}</span>
        <button type="button" data-budget-person-page="1" ${offset + 20 >= total ? "disabled" : ""}>下一页</button>
      </div>` : ""}
      ${selected ? `<div class="budget-selected-person"><strong>${escapeHtml(selected.display_name)}的调用成本</strong><span>${escapeHtml(selected.email)}</span>
        <div class="budget-person-spend">
          <div><small>今日已入账</small><strong>${renderPersonSpend(selected.spent_day_cny)}</strong></div>
          <div><small>本月已入账</small><strong>${renderPersonSpend(selected.spent_month_cny)}</strong></div>
          <div><small>本年已入账</small><strong>${renderPersonSpend(selected.spent_year_cny)}</strong></div>
        </div>
        ${[selected.spent_day_cny, selected.spent_month_cny, selected.spent_year_cny].some((value) => value == null)
          ? "<p role=\"status\">部分历史调用缺少可核实的价格，对应周期不显示金额；已核实的其他周期仍可查看。</p>"
          : ""}
        <p>金额合计该员工名下所有调用凭证的正式入账成本。选择一把凭证可查看它受到的预算限制。</p>
        ${selected.keys?.length ? `<div class="budget-person-keys">${selected.keys.map((key) => `<button type="button" data-budget-person-key="${escapeAttribute(key.virtual_key_id)}" class="${state.selectedVirtualKeyId === key.virtual_key_id ? "is-selected" : ""}">${escapeHtml(displayVirtualKeyName(key.name))}<small>${escapeHtml(key.environment_name ?? "")}</small></button>`).join("")}</div>` : "<p>此员工当前没有可选调用凭证；历史成本仍计入上方汇总。</p>"}
      </div>` : ""}
    </section>
  `;
}

function renderPersonSpend(value) {
  return value == null ? "无法核实" : `${escapeHtml(value)} CNY`;
}

/**
 * 渲染后端返回的可信 scope chain 及正式账本、预留和有效消耗拆分。
 */
function renderScopeChain(statuses, aggregate, selectedPerson, statusError) {
  if (!selectedPerson?.keys?.length) return "";
  if (statusError) {
    return `<div class="budget-action-error" role="status">所选凭证的预算判断暂不可用：${escapeHtml(statusError.message ?? "请稍后重试")} ${statusError.requestId ? `<small>请求 ID：${escapeHtml(statusError.requestId)}</small>` : ""}</div>`;
  }
  return `
    <section class="budget-chain-section" aria-label="适用的预算限制">
      <h3>所选凭证适用的预算</h3>
      <p>${statuses.length ? `${statuses.length} 条规则；当前调用${escapeHtml(ACTION_LABELS[aggregate.effective_action] ?? "放行")}。` : "当前没有适用的预算规则。"} 多条规则同时生效时，不能将剩余额度相加。</p>
      ${statuses.length ? `<ul class="budget-applicable-list">${statuses.map((item) => `<li><strong>${escapeHtml(SCOPE_LABELS[item.scope_type] ?? item.scope_type)} · ${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")}</strong><span>剩余 ${escapeHtml(item.effective_remaining_cny ?? "-")} CNY</span><span>${escapeHtml(STATUS_LABELS[item.status] ?? item.status)}</span></li>`).join("")}</ul>` : ""}
    </section>
  `;
}

/**
 * 渲染一层预算状态；无规则的权威 scope 仍保留在链中并标记“未配置”。
 */
function renderScopeChainRow(scope, statuses) {
  const items = statuses.filter((status) => (
    status.scope_type === scope.scope_type && status.scope_id === scope.scope_id
  ));
  return (items.length ? items : [null]).map((item) => `
    <li class="budget-chain-card" data-budget-scope-id="${escapeAttribute(scope.budget_scope_id ?? scope.scope_id ?? "")}">
      <div class="budget-chain-card-heading"><strong>${escapeHtml(SCOPE_LABELS[scope.scope_type] ?? scope.scope_type)} · ${escapeHtml(scopeDisplayName(scope))}${item ? ` · ${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")}` : ""}</strong>
        <mark class="status-mark status-${escapeAttribute(item?.status ?? "unconfigured")}">
          ${escapeHtml(STATUS_LABELS[item?.status] ?? item?.status ?? "未配置")}
        </mark>
      </div>
      ${item ? `<div class="budget-chain-card-values">
        <span>周期：<strong>${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")}</strong></span>
        <span>预算：<strong>${escapeHtml(item.budget_cny)} CNY</strong></span>
        <span>已用：<strong>${escapeHtml(item.effective_spend_cny ?? item.spent_cny)} CNY</strong></span>
        <span>剩余：<strong>${escapeHtml(item.effective_remaining_cny ?? item.estimated_remaining_cny)} CNY</strong></span>
      </div><p>已用金额含正式入账 ${escapeHtml(item.ledger_spent_cny ?? item.spent_cny)} CNY 和待结算预留 ${escapeHtml(item.reserved_cny ?? "0")} CNY；本层耗尽后${escapeHtml(item.exhausted_action === "block" ? "阻断" : "仅告警")}。</p>${item.effective_from_utc ? `<p class="budget-partial-period">本周期从 ${escapeHtml(formatDateTime(item.effective_from_utc))} 起算；此前价格无法确认的调用未计入此规则。</p>` : ""}${renderBlockNotice(item)}`
      : "<p>本层未设预算，不单独限制调用。</p>"}
    </li>
  `).join("");
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
function renderBudgetForm(projectState, environmentState) {
  const projects = projectState?.projects ?? [];
  const applications = projectState?.applications ?? [];
  const environments = environmentState?.state === "ok"
    ? (environmentState.environments ?? []).filter((item) => item.status === "active")
    : [];
  return `
    <button type="button" class="budget-add-button" data-budget-open-form>＋ 新增预算</button>
    <dialog class="budget-rule-dialog" id="budget-rule-dialog" aria-labelledby="budget-dialog-title">
      <div class="budget-dialog-header"><h3 id="budget-dialog-title">设置成本预算</h3><button type="button" data-budget-close-form aria-label="关闭预算设置">关闭</button></div>
      <p>选择适用对象和周期，再设置金额与启用时间。同一对象可分别设置日、月、年上限。</p>
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
          <option value="virtual_key">员工调用凭证</option>
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
        </select>
      </label>
      <div class="budget-employee-picker" id="budget-employee-picker" hidden>
        <label for="budget-employee-search">搜索员工姓名或邮箱</label>
        <div class="budget-employee-search-line"><input id="budget-employee-search" type="search" placeholder="输入姓名或邮箱" maxlength="160" autocomplete="off" /><button type="button" data-budget-employee-search>查找</button></div>
        <div id="budget-employee-results" class="budget-employee-results" aria-live="polite">输入姓名或邮箱后查找员工。</div>
        <div id="budget-employee-selected" class="budget-employee-selected" hidden></div>
        <label id="budget-employee-key-wrap" hidden>调用凭证<select id="budget-employee-key"></select></label>
        <small>员工可以有多把调用凭证；此规则作用于选定的这一把。</small>
      </div>
      <label>
        <span>预算周期</span>
        <select name="budgetPeriod"><option value="day">每天</option><option value="month">每月</option><option value="year">每年</option></select>
      </label>
      <label>
        <span>预算金额 CNY</span>
        <input name="dailyBudgetCny" inputmode="decimal" placeholder="1000" required />
      </label>
      <label>
        <span>启用时间</span>
        <select name="activationMode"><option value="immediate">保存后立即启用</option><option value="scheduled">指定时间启用</option><option value="keep" hidden disabled>保持原启用设置</option></select>
      </label>
      <label>
        <span>指定启用时间（北京时间）</span>
        <input name="activationLocal" type="datetime-local" />
        <small>填写日期和时间会自动切换为定时启用。</small>
      </label>
      <p class="budget-current-activation" id="budget-current-activation" hidden>当前启用设置</p>
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
      <label>
        <span>名称</span>
        <input name="name" placeholder="例如：研发团队月预算" />
      </label>
      <div class="budget-actions">
        <button type="submit" ${environments.length ? "" : "disabled"}>保存预算</button>
        <span class="budget-dialog-message" id="budget-dialog-message" role="status" aria-live="polite">填写完成后点击保存。未到启用时间前不会限制调用。</span>
      </div>
      </form>
    </dialog>
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
function renderRuleTable(rules, canWrite, total, offset) {
  return `
    <section class="budget-rules-section" aria-label="已设置的预算">
      <div class="budget-rules-heading"><h3>已设置的预算</h3><p>共 ${escapeHtml(total)} 条规则，每页最多 10 条；停用和待启用的规则不会限制调用。</p></div>
    <div class="budget-rule-table" role="table" aria-label="预算规则">
      <div class="budget-rule-row budget-rule-head" role="row">
        <span>适用范围</span>
        <span>预算与使用</span>
        <span>启用时间 / 状态</span>
        <span>达到上限时</span>
      </div>
      ${rules.length ? rules.map((item) => renderRuleRow(item, canWrite)).join("") : renderEmptyRuleRow()}
    </div>
    ${total > 10 ? `<div class="budget-rule-pagination">
      <button type="button" data-budget-rule-page="-1" ${offset === 0 ? "disabled" : ""}>上一页</button>
      <span>${escapeHtml(offset + 1)}–${escapeHtml(Math.min(offset + 10, total))} / ${escapeHtml(total)}</span>
      <button type="button" data-budget-rule-page="1" ${offset + 10 >= total ? "disabled" : ""}>下一页</button>
    </div>` : ""}
    </section>
  `;
}

/**
 * 渲染单条预算规则，金额和状态均使用后端返回的规范化字段。
 */
function renderRuleRow(item, canWrite) {
  const today = item.today_status ?? {};
  const state = item.status === "disabled" ? "已停用" : STATUS_LABELS[today.status] ?? "正常";
  const scopeName = item.scope_type === "virtual_key" && item.scope_owner_display_name
    ? `${item.scope_owner_display_name} · ${item.scope_owner_email ?? ""}`
    : cleanScopeLabel(item.scope_label ?? item.scope_id);
  const scopeDetail = `${item.environment_name ?? item.environment ?? "-"} · ${item.name}`;
  const activation = item.activation_at_utc
    ? `${today.status === "scheduled" ? "将于" : "自"} ${formatDateTime(item.activation_at_utc)} 启用（北京时间）`
    : "未设置定时启用";
  return `
    <div class="budget-rule-row" role="row" data-budget-rule-id="${escapeAttribute(item.budget_rule_id)}">
      <span class="budget-rule-name" data-label="适用范围">
        <strong>${escapeHtml(scopeName)}</strong>
        <small>${escapeHtml(scopeDetail)}</small>
      </span>
      <span class="budget-rule-amount" data-label="预算与使用">
        <strong>${escapeHtml(PERIOD_LABELS[item.budget_period] ?? "每日")} ${escapeHtml(item.daily_budget_cny)} CNY</strong>
        <small>${today.status === "scheduled" ? "启用后开始统计" : `已用 ${escapeHtml(today.effective_spend_cny ?? "-")} · 剩余 ${escapeHtml(today.effective_remaining_cny ?? "-")} CNY`}</small>
      </span>
      <span class="budget-rule-activation" data-label="启用时间 / 状态">
        <strong>${escapeHtml(state)}</strong><small>${escapeHtml(activation)}</small>
        ${item.active_block ? "<small class=\"budget-block-label\">当前已阻断调用</small>" : ""}
        ${item.effective_from_utc ? `<small>本周期计费从 ${escapeHtml(formatDateTime(item.effective_from_utc))} 起算</small>` : ""}
      </span>
      <span class="budget-rule-action" data-label="达到上限时">
        <strong>${escapeHtml(ACTION_LABELS[item.exhausted_action] ?? item.exhausted_action)}</strong>
        <small>${escapeHtml(item.warning_threshold_percent)}% 时告警</small>
        ${canWrite ? `<button type="button" data-budget-edit-rule-id="${escapeAttribute(item.budget_rule_id)}">编辑</button>` : ""}
      </span>
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
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(parsed);
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
