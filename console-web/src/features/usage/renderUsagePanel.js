/**
 * 模块职责：渲染当前租户全量用量、成本与关联预算摘要。
 * 安全边界：账单权限和 tenant_id 由后端强制校验；Developer 缺少 billing 权限时返回零 DOM。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 预算状态标签仅解释后端聚合结论，不在用量页面重新计算阈值或阻断动作。
const BUDGET_STATUS_LABELS = {
  ok: "正常",
  threshold_reached: "达到告警",
  exhausted: "已耗尽",
};

// 预算动作标签用于解释治理决策，不能在前端自行推导是否允许调用。
const BUDGET_ACTION_LABELS = {
  alert_only: "仅告警",
  block: "阻断",
  allow: "放行",
  warn: "告警",
};

/**
 * 渲染 Token 用量与成本面板。
 * 面板只对管理员和开发者可见；后端 /usage 仍必须执行 RBAC 和 tenant_id 隔离校验。
 */
export function renderUsagePanel(usageState, authContext, budgetState) {
  if (hasPermission(authContext, PERMISSIONS.VIRTUAL_KEY_READ)
    && !hasPermission(authContext, PERMISSIONS.BILLING_USAGE_READ)) {
    return renderPersonalUsagePanel(usageState);
  }
  // 全租户账单只对后端 billing.usage.read 授权主体可见，Developer 不再因旧角色集合误获数据。
  if (!hasPermission(authContext, PERMISSIONS.BILLING_USAGE_READ)) {
    return "";
  }
  // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
  if (!usageState) {
    return renderLoadingPanel();
  }
  // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
  if (usageState.state === "error") {
    // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
    return renderErrorPanel(usageState);
  }

  const summary = usageState.summary ?? {};
  const exportState = usageState.exportState ?? { status: "idle" };
  const canExport = hasPermission(authContext, PERMISSIONS.BILLING_USAGE_EXPORT);
  return `
    <div class="panel-heading usage-heading">
      <div>
        <h2 id="usage-title">用量与成本</h2>
      </div>
      <div class="usage-heading-actions">
        <span class="usage-scope-label">组织汇总</span>
        ${renderUsageExportButton(canExport, exportState)}
      </div>
    </div>
    <div class="console-page-toolbar usage-toolbar">
      ${renderActionError(usageState.actionError)}
      ${renderFilterForm(usageState.filters ?? {}, summary.by_consumer ?? [])}
    </div>
    ${renderUsageExportFeedback(canExport, exportState)}
    ${renderTotals(summary.totals)}
    ${renderBudgetOverview(budgetState)}
    ${renderSummaryTables(summary, usageState.dimension ?? "overview")}
  `;
}

function renderPersonalUsagePanel(usageState) {
  if (!usageState) {
    return '<div class="panel-heading"><h2 id="usage-title">我的用量</h2><p>正在读取本人调用统计。</p></div><div class="empty-state" role="status">正在加载...</div>';
  }
  if (usageState.state === "error") {
    return `<div class="panel-heading"><h2 id="usage-title">我的用量</h2><p>个人用量暂不可用。</p></div><div class="empty-state error-state" role="alert">${escapeHtml(usageState.message ?? "用量接口读取失败")}</div>`;
  }
  const totals = usageState.personalSummary?.totals ?? {};
  const days = usageState.personalSummary?.by_day ?? [];
  return `
    <div class="panel-heading usage-heading"><div><h2 id="usage-title">我的用量</h2><p>只显示本人通过个人 KEY 的统计。</p></div><span class="usage-scope-label">个人汇总</span></div>
    ${renderActionError(usageState.actionError)}
    <details class="usage-filter-panel usage-personal-filter-panel"${hasUsageFilters(usageState.filters) ? " open" : ""}>
      <summary><span>筛选我的用量</span>${hasUsageFilters(usageState.filters) ? "<small>已设置筛选条件</small>" : ""}</summary>
      <form class="usage-filter-form usage-personal-filter" id="usage-filter-form">
      <label><span>开始日期</span><input type="date" name="startDate" aria-label="开始日期" value="${escapeAttribute(usageState.filters?.startDate ?? "")}" /></label>
      <label><span>结束日期</span><input type="date" name="endDate" aria-label="结束日期" value="${escapeAttribute(usageState.filters?.endDate ?? "")}" /></label>
      <div class="usage-filter-actions"><button type="submit">查询</button><button type="button" id="usage-filter-reset">清除筛选</button></div>
      </form>
    </details>
    <section class="usage-total-grid" aria-label="个人令牌汇总">
      <div><strong>${escapeHtml(totals.request_count ?? 0)}</strong><span>请求次数</span></div>
      <div><strong>${escapeHtml(totals.input_tokens ?? 0)}</strong><span>输入令牌</span></div>
      <div><strong>${escapeHtml(totals.output_tokens ?? 0)}</strong><span>输出令牌</span></div>
      <div><strong>${escapeHtml(totals.total_tokens ?? 0)}</strong><span>总令牌</span></div>
    </section>
    ${days.length ? `<section class="usage-personal-days"><h3>每日用量</h3><div class="usage-record-table" role="table"><div class="usage-record-row usage-record-head" role="row"><span>日期</span><span>请求次数</span><span>输入 Token</span><span>输出 Token</span><span>总 Token</span></div>${days.map((item) => `<div class="usage-record-row" role="row"><time>${escapeHtml(item.day)}</time><span>${escapeHtml(item.request_count)}</span><span>${escapeHtml(item.input_tokens)}</span><span>${escapeHtml(item.output_tokens)}</span><span>${escapeHtml(item.total_tokens)}</span></div>`).join("")}</div></section>` : ""}
    <p class="usage-detail-handoff">需要查看单次调用、失败原因或路由尝试时，请到“治理 → 调用记录”。</p>
  `;
}

/**
 * 渲染用量概览里的预算摘要。
 * 这里不提供编辑入口，只把 /budget-status 的当日状态放在用量页，便于财务和管理员把成本趋势与预算阈值放在一起判断。
 */
function renderBudgetOverview(budgetState) {
  if (!budgetState) {
    return "";
  }
  // 预算摘要读取失败不能影响用量账本展示；关联号仍保留在状态对象中。
  if (budgetState.state === "error") {
    return `
      <section class="usage-budget-summary usage-budget-error" aria-label="预算状态">
        <div>
          <strong>预算状态暂不可用</strong>
          <span>${escapeHtml(budgetState.message ?? "预算接口读取失败")}</span>
        </div>
      </section>
    `;
  }

  const statuses = budgetState.status?.statuses ?? [];
  if (!statuses.length) {
    return `
      <section class="usage-budget-summary" aria-label="预算状态">
        <div>
          <strong>今日预算状态</strong>
          <span>当前租户尚未启用日预算规则。</span>
        </div>
      </section>
    `;
  }

  return `
    <section class="usage-budget-summary" aria-label="预算状态">
      <div>
        <strong>今日预算状态</strong>
        <span>${escapeHtml(budgetState.status?.budget_date ?? "-")}，共 ${escapeHtml(statuses.length)} 条启用规则。</span>
      </div>
      <div class="usage-budget-metrics">
        ${statuses.slice(0, 3).map(renderUsageBudgetChip).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染用量概览中的单条预算状态摘要。
 * 金额以控制面返回的字符串为准，前端不重新计算财务口径，避免与后端账本出现展示偏差。
 */
function renderUsageBudgetChip(item) {
  return `
    <article class="usage-budget-chip status-${escapeAttribute(item.status)}">
      <div>
        <strong>${escapeHtml(displayUsageScope(item.scope_label ?? item.scope_type ?? "-"))}</strong>
        <mark>${escapeHtml(BUDGET_STATUS_LABELS[item.status] ?? item.status ?? "-")}</mark>
      </div>
      <span>${escapeHtml(item.spent_cny ?? "0")} / ${escapeHtml(item.budget_cny ?? "0")} CNY</span>
      <em>${escapeHtml(item.usage_percent ?? 0)}% · ${escapeHtml(BUDGET_ACTION_LABELS[item.next_action] ?? item.next_action ?? "-")}</em>
    </article>
  `;
}

/**
 * 渲染加载状态，避免首次进入控制台时用量区域空白。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="usage-title">用量与成本</h2>
      <p>正在读取当前租户的用量账本。</p>
    </div>
    <div class="empty-state">等待 /usage/summary 和 /usage/records 响应。</div>
  `;
}

/**
 * 普通用户只需要知道读取失败；关联号保留在客户端错误对象和服务端日志中。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="usage-title">用量与成本</h2>
      <p>用量账本暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "用量接口读取失败")}
    </div>
  `;
}

/**
 * 渲染用量筛选表单，字段名与后端 Query 参数保持一一对应。
 */
function renderFilterForm(filters, consumers) {
  const activeFilters = countActiveUsageFilters(filters);
  const selectedConsumerId = String(filters.consumerId ?? "").trim();
  const consumerOptions = consumers
    .filter((row) => row.consumer_id && row.consumer_name)
    .map((row) => `<option value="${escapeAttribute(row.consumer_id)}"${String(row.consumer_id) === selectedConsumerId ? " selected" : ""}>${escapeHtml(row.consumer_name)}${row.consumer_department ? ` / ${escapeHtml(row.consumer_department)}` : ""}</option>`)
    .join("");
  const selectedConsumerFallback = selectedConsumerId && !consumers.some((row) => String(row.consumer_id) === selectedConsumerId)
    ? `<option value="${escapeAttribute(selectedConsumerId)}" selected>当前使用者</option>`
    : "";
  return `
    <details class="usage-filter-panel"${activeFilters ? " open" : ""}>
        <summary><span>筛选统计</span>${activeFilters ? `<small>已设置 ${activeFilters} 项</small>` : ""}</summary>
      <form class="usage-filter-form" id="usage-filter-form">
      <label>
        <span>使用者</span>
        <select name="consumerId" aria-label="使用者">
          <option value="">全部使用者</option>
          ${selectedConsumerFallback}
          ${consumerOptions}
        </select>
      </label>
      <label>
        <span>开始日期</span>
        <input type="date" name="startDate" aria-label="开始日期" value="${escapeAttribute(filters.startDate ?? "")}" />
      </label>
      <label>
        <span>结束日期</span>
        <input type="date" name="endDate" aria-label="结束日期" value="${escapeAttribute(filters.endDate ?? "")}" />
      </label>
      <label>
        <span>模型</span>
        <input name="modelId" aria-label="模型" value="${escapeAttribute(filters.modelId ?? "")}" placeholder="输入模型名称" />
      </label>
      <label>
        <span>调用凭据</span>
        <input name="virtualKeyId" aria-label="调用凭据" value="${escapeAttribute(filters.virtualKeyId ?? "")}" placeholder="输入 KEY 名称或前缀" />
      </label>
      <label>
        <span>环境</span>
        <input name="environmentId" aria-label="环境" value="${escapeAttribute(filters.environmentId ?? "")}" placeholder="输入环境名称" />
      </label>
      <label>
        <span>项目</span>
        <input name="projectId" aria-label="项目" value="${escapeAttribute(filters.projectId ?? "")}" placeholder="输入项目名称" />
      </label>
      <label>
        <span>应用</span>
        <input name="applicationId" aria-label="应用" value="${escapeAttribute(filters.applicationId ?? "")}" placeholder="输入应用名称" />
      </label>
      <label>
        <span>模型服务</span>
        <input name="providerId" aria-label="模型服务" value="${escapeAttribute(filters.providerId ?? "")}" placeholder="输入服务名称" />
      </label>
      <label>
        <span>用量来源</span>
        <select name="estimated" aria-label="用量来源">
          ${renderOption("", "全部", filters.estimated)}
          ${renderOption("false", "真实用量", filters.estimated)}
          ${renderOption("true", "估算用量", filters.estimated)}
        </select>
      </label>
      <label>
      </label>
      <div class="usage-filter-actions">
        <button type="submit">筛选</button>
        <button type="button" id="usage-filter-reset">清空</button>
      </div>
      </form>
    </details>
  `;
}

function countActiveUsageFilters(filters = {}) {
  return [
    "consumerId", "startDate", "endDate", "modelId", "virtualKeyId", "environmentId",
    "projectId", "applicationId", "providerId", "estimated",
  ].filter((key) => String(filters[key] ?? "").trim() !== "").length;
}

function hasUsageFilters(filters = {}) {
  return countActiveUsageFilters(filters) > 0;
}

/**
 * 渲染总量指标卡，成本按 CNY/USD 分开展示，不做汇率折算。
 */
function renderTotals(totals = {}) {
  return `
    <div class="usage-total-grid" aria-label="用量总览">
      ${renderMetric("请求数", totals.request_count ?? 0, "条")}
      ${renderMetric("总令牌", totals.total_tokens ?? 0, "条")}
      ${renderMetric("缓存令牌", totals.cache_tokens ?? 0, "条")}
      ${renderMetric("成本 CNY", formatCost(totals.cost_cny, "CNY"), "")}
      ${renderMetric("成本 USD", formatCost(totals.cost_usd, "USD"), "")}
      ${renderMetric("估算记录", totals.estimated_records ?? 0, `真实 ${totals.real_usage_records ?? 0}`)}
    </div>
  `;
}

/**
 * 渲染单个指标卡，统一处理数值和辅助单位的转义。
 */
function renderMetric(label, value, unit) {
  return `
    <article class="usage-metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${unit ? `<em>${escapeHtml(unit)}</em>` : ""}
    </article>
  `;
}

/**
 * 渲染按维度切换的聚合表；项目视图同时承载其下的应用明细。
 */
function renderSummaryTables(summary = {}, activeDimension = "overview") {
  const dimensions = [
    ["overview", "总览", "当前筛选范围的汇总指标。", ""],
    ["day", "按日期", "按日期查看请求量、令牌和成本变化。", renderDaySummary(summary.by_day ?? [])],
    ["consumer", "按使用者", "查看每位成员的请求量和成本。", renderConsumerSummary(summary.by_consumer ?? [])],
    ["model", "按模型", "查看各模型的请求量和成本。", renderModelSummary(summary.by_model ?? [])],
    ["project", "按项目", "查看项目总量，应用会缩进显示在所属项目下。", renderProjectSummary(summary.by_project ?? [], summary.by_application ?? [])],
    ["environment", "按环境", "查看各环境的请求量和成本。", renderEnvironmentSummary(summary.by_environment ?? [])],
  ];
  const selected = dimensions.some(([value]) => value === activeDimension) ? activeDimension : "overview";
  return `
    <section class="usage-view-switcher" aria-labelledby="usage-view-label">
      <div class="usage-view-switcher-heading">
        <label id="usage-view-label" for="usage-dimension">统计视图</label>
        <span>单次调用记录请到“治理 → 调用记录”查看。</span>
      </div>
      <select id="usage-dimension" name="usageDimension" aria-label="统计视图">
        ${dimensions.map(([value, label]) => `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`).join("")}
      </select>
    </section>
    <div class="usage-summary-views">
      ${dimensions.map(([value, label, description, content]) => `
        <section class="usage-summary-view" data-usage-dimension="${escapeAttribute(value)}"${value === selected && value !== "overview" ? "" : " hidden"} aria-labelledby="usage-dimension-${escapeAttribute(value)}">
          <div class="usage-summary-view-heading">
            <div><h3 id="usage-dimension-${escapeAttribute(value)}">${escapeHtml(label)}</h3><p>${escapeHtml(description)}</p></div>
          </div>
          ${content || (value === "overview" ? "" : `<div class="usage-summary-empty">当前筛选范围暂无${escapeHtml(label)}数据。</div>`)}
        </section>
      `).join("")}
    </div>
  `;
}

/**
 * 渲染按天聚合表，展示趋势所需的核心字段。
 */
function renderConsumerSummary(rows) {
  if (!rows.length) return "";
  return `
    <section class="usage-summary-block">
      <div class="usage-mini-table" role="table" aria-label="按使用者用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>使用者</span><span>请求</span><span>令牌</span><span>成本</span>
        </div>
        ${rows.map((row) => renderMiniRow(row.consumer_name ? `${row.consumer_name}${row.consumer_department ? ` / ${row.consumer_department}` : ""}` : "未绑定使用者", row.request_count, row.total_tokens, formatDualCost(row))).join("")}
      </div>
    </section>
  `;
}

function renderDaySummary(rows) {
  if (!rows.length) return "";
  return `
    <section class="usage-summary-block">
      <div class="usage-mini-table" role="table" aria-label="按天用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>日期</span><span>请求</span><span>令牌</span><span>成本</span>
        </div>
        ${rows.map((row) => renderMiniRow(row.day, row.request_count, row.total_tokens, formatDualCost(row))).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染按模型聚合表，展示模型 ID、请求数、Token 和成本。
 */
function renderModelSummary(rows) {
  if (!rows.length) return "";
  return `
    <section class="usage-summary-block">
      <div class="usage-mini-table" role="table" aria-label="按模型用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>模型</span><span>请求</span><span>令牌</span><span>成本</span>
        </div>
        ${rows.map((row) => renderMiniRow(row.resolved_model ?? row.model_name ?? "-", row.request_count, row.total_tokens, formatDualCost(row))).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染按环境聚合表，帮助管理员核对各环境请求量、Token 和成本。
 */
function renderEnvironmentSummary(rows) {
  if (!rows.length) return "";
  /** 按环境展示请求、Token 和成本，名称缺失时回退到代码或稳定 ID。 */
  return `
    <section class="usage-summary-block">
      <div class="usage-mini-table" role="table" aria-label="按环境用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>环境</span><span>请求</span><span>令牌</span><span>成本</span>
        </div>
        ${rows.map((row) => renderMiniRow(row.environment_name ?? row.environment ?? "未知", row.request_count, row.total_tokens, formatDualCost(row))).join("")}
      </div>
    </section>
  `;
}

/**
 * 渲染 admin-only CSV 导出命令。
 * 当前 RBAC 尚无 finance 角色，批量账本导出由 admin 承担；前端隐藏只是体验优化，后端仍必须执行 admin 守卫。
 */
function renderUsageExportButton(canExport, exportState) {
  if (!canExport) {
    return "";
  }
  const busy = ["requesting", "pending", "processing", "downloading"].includes(exportState.status);
  const commandLabel = exportState.jobId
    ? "继续查询"
    : exportState.operationRequestId
      ? "重试导出"
      : "导出 CSV";
  return `
    <button
      type="button"
      id="usage-export-csv"
      class="usage-export-button"
      aria-busy="${busy ? "true" : "false"}"
      ${busy ? "disabled" : ""}
    >${commandLabel}</button>
  `;
}

/**
 * 渲染独立导出反馈，避免 CSV 失败把整个用量概览替换为错误页。
 * message 只包含固定状态和导出进度，关联号仍保留在状态对象中而不进入页面。
 */
function renderUsageExportFeedback(canExport, exportState) {
  if (!canExport) {
    return "";
  }
  const state = exportState ?? { status: "idle", message: "" };
  const hidden = state.status === "idle";
  const isError = state.status === "error";
  return `
    <div
      class="usage-export-feedback status-${escapeAttribute(state.status)}"
      data-usage-export-feedback
      role="${isError ? "alert" : "status"}"
      aria-live="polite"
      ${hidden ? "hidden" : ""}
    >${escapeHtml(state.message ?? "正在处理用量导出")}</div>
  `;
}

/**
 * 渲染按项目聚合表，帮助财务按一级业务系统核对成本。
 */
function renderProjectSummary(rows, applicationRows) {
  if (!rows.length && !applicationRows.length) return "";
  const groups = new Map();
  for (const row of rows) {
    const key = String(row.project_id ?? row.project_name ?? "unbound");
    groups.set(key, { project: row, applications: [] });
  }
  for (const row of applicationRows) {
    const key = String(row.project_id ?? row.project_name ?? "unbound");
    const group = groups.get(key) ?? { project: null, applications: [] };
    group.applications.push(row);
    groups.set(key, group);
  }
  const projectRows = [...groups.values()].map(({ project, applications }) => {
    if (project) return renderMiniRow(project.project_name ?? "未绑定项目", project.request_count, project.total_tokens, formatDualCost(project), "usage-project-row")
      + applications.map((row) => renderMiniRow(row.application_name ?? "未命名应用", row.request_count, row.total_tokens, formatDualCost(row), "usage-project-application")).join("");
    const aggregate = applications.reduce((total, row) => ({
      request_count: total.request_count + Number(row.request_count ?? 0),
      total_tokens: total.total_tokens + Number(row.total_tokens ?? 0),
      cost_cny: total.cost_cny + Number(row.cost_cny ?? 0),
      cost_usd: total.cost_usd + Number(row.cost_usd ?? 0),
    }), { request_count: 0, total_tokens: 0, cost_cny: 0, cost_usd: 0 });
    return renderMiniRow("未绑定项目", aggregate.request_count, aggregate.total_tokens, formatDualCost(aggregate), "usage-project-row")
      + applications.map((row) => renderMiniRow(row.application_name ?? "未命名应用", row.request_count, row.total_tokens, formatDualCost(row), "usage-project-application")).join("");
  }).join("");
  return `
    <section class="usage-summary-block">
      <div class="usage-mini-table" role="table" aria-label="按项目用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>项目</span><span>请求</span><span>令牌</span><span>成本</span>
        </div>
        ${projectRows}
      </div>
    </section>
  `;
}

/**
 * 渲染聚合小表的一行，保持各列稳定对齐。
 */
function renderMiniRow(label, requestCount, totalTokens, cost, rowClass = "") {
  return `
    <div class="usage-mini-row${rowClass ? ` ${escapeAttribute(rowClass)}` : ""}" role="row">
      <span title="${escapeAttribute(label)}">${escapeHtml(label)}</span>
      <span data-label="请求">${escapeHtml(requestCount ?? 0)}</span>
      <span data-label="令牌">${escapeHtml(totalTokens ?? 0)}</span>
      <span data-label="成本">${escapeHtml(cost)}</span>
    </div>
  `;
}

/**
 * 渲染聚合表空状态，避免表格区域塌陷。
 */
function renderMiniEmpty() {
  return `<div class="usage-mini-row usage-mini-empty" role="row"><span>暂无数据</span></div>`;
}

function displayUsageScope(value) {
  const label = String(value ?? "").trim();
  const cleaned = label.replace(/\s*\((?:mhv|mh|sk|pk|key)[a-z0-9_*:-]*\)\s*$/iu, "").trim();
  if (/^local development virtual key$/iu.test(cleaned) || /^mhv_dev_seed(?:_|$)/iu.test(cleaned)) {
    return "默认调用 KEY";
  }
  return cleaned || "-";
}

/**
 * 格式化双币种成本，M0 不做 CNY/USD 互转，只显示原币种拆分结果。
 */
function formatDualCost(item) {
  const cny = Number(item.cost_cny ?? 0);
  const usd = Number(item.cost_usd ?? 0);
  if (cny > 0 && usd > 0) {
    // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
    return `${formatCost(cny, "CNY")} / ${formatCost(usd, "USD")}`;
  }
  if (usd > 0) {
    // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
    return formatCost(usd, "USD");
  }
  // 用量和成本分支直接影响账本追踪，异常值要在这里归一化。
  return formatCost(cny, "CNY");
}

/**
 * 格式化成本金额，保留最多 6 位小数以匹配后端账本精度。
 */
function formatCost(amount, currency) {
  const value = Number(amount ?? 0).toFixed(6).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
  return `${value} ${currency}`;
}

/**
 * 渲染操作级错误提示，例如筛选或分页失败。
 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `
    <div class="usage-action-error" role="alert">
      ${escapeHtml(error.message ?? "用量操作失败")}
    </div>
  `;
}

/**
 * 渲染 select option，并保持当前筛选条件选中态。
 */
function renderOption(value, label, selectedValue) {
  const selected = value === (selectedValue ?? "") ? " selected" : "";
  return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
}

/**
 * HTML 文本转义，防止账本字段里的特殊字符破坏页面结构。
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
 * HTML 属性转义，额外处理反引号以保护 data-* 和 title 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
