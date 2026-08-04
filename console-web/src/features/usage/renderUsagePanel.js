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

  const records = usageState.records?.usage_records ?? [];
  const summary = usageState.summary ?? {};
  const exportState = usageState.exportState ?? { status: "idle" };
  const canExport = hasPermission(authContext, PERMISSIONS.BILLING_USAGE_EXPORT);
  return `
    <div class="panel-heading usage-heading">
      <div>
        <h2 id="usage-title">Token 用量与成本</h2>
        <p>按天、模型、Virtual Key、Project 和 Application 追踪用量；真实 usage 与本地估算会明确区分。</p>
      </div>
      <div class="usage-heading-actions">
        <span class="usage-count">${escapeHtml(records.length)} / ${escapeHtml(usageState.records?.total ?? 0)}</span>
        ${renderUsageExportButton(canExport, exportState)}
      </div>
    </div>
    ${renderActionError(usageState.actionError)}
    ${renderFilterForm(usageState.filters ?? {})}
    ${renderUsageExportFeedback(canExport, exportState)}
    ${renderTotals(summary.totals)}
    ${renderBudgetOverview(budgetState)}
    ${renderSummaryTables(summary)}
    ${renderRecordTable(records)}
    ${renderPager(usageState.records ?? {})}
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
  // 预算摘要读取失败不能影响用量账本展示，但要保留 request_id 便于排查预算接口。
  if (budgetState.state === "error") {
    return `
      <section class="usage-budget-summary usage-budget-error" aria-label="预算状态">
        <div>
          <strong>预算状态暂不可用</strong>
          <span>${escapeHtml(budgetState.message ?? "预算接口读取失败")} request_id: ${escapeHtml(budgetState.requestId ?? "-")}</span>
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
        <strong>${escapeHtml(item.scope_label ?? item.scope_type ?? "-")}</strong>
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
      <h2 id="usage-title">Token 用量与成本</h2>
      <p>正在读取当前租户的用量账本。</p>
    </div>
    <div class="empty-state">等待 /usage/summary 和 /usage/records 响应。</div>
  `;
}

/**
 * 渲染接口失败状态，并展示 request_id 方便定位控制面日志。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="usage-title">Token 用量与成本</h2>
      <p>用量账本暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "用量接口读取失败")} request_id: ${escapeHtml(state.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染用量筛选表单，字段名与后端 Query 参数保持一一对应。
 */
function renderFilterForm(filters) {
  return `
    <form class="usage-filter-form" id="usage-filter-form">
      <label>
        <span>开始日期</span>
        <input type="date" name="startDate" value="${escapeAttribute(filters.startDate ?? "")}" />
      </label>
      <label>
        <span>结束日期</span>
        <input type="date" name="endDate" value="${escapeAttribute(filters.endDate ?? "")}" />
      </label>
      <label>
        <span>模型 ID</span>
        <input name="modelId" value="${escapeAttribute(filters.modelId ?? "")}" placeholder="model_xxx / default-chat" />
      </label>
      <label>
        <span>Virtual Key</span>
        <input name="virtualKeyId" value="${escapeAttribute(filters.virtualKeyId ?? "")}" placeholder="virtual_key_id" />
      </label>
      <label>
        <span>Environment ID</span>
        <input name="environmentId" value="${escapeAttribute(filters.environmentId ?? "")}" placeholder="environment_id" />
      </label>
      <label>
        <span>Project</span>
        <input name="projectId" value="${escapeAttribute(filters.projectId ?? "")}" placeholder="project_id" />
      </label>
      <label>
        <span>Application</span>
        <input name="applicationId" value="${escapeAttribute(filters.applicationId ?? "")}" placeholder="application_id" />
      </label>
      <label>
        <span>Provider</span>
        <input name="providerId" value="${escapeAttribute(filters.providerId ?? "")}" placeholder="provider_id" />
      </label>
      <label>
        <span>usage 来源</span>
        <select name="estimated">
          ${renderOption("", "全部", filters.estimated)}
          ${renderOption("false", "真实 usage", filters.estimated)}
          ${renderOption("true", "估算 usage", filters.estimated)}
        </select>
      </label>
      <label>
        <span>request_id</span>
        <input name="requestId" value="${escapeAttribute(filters.requestId ?? "")}" placeholder="req_xxx" />
      </label>
      <div class="usage-filter-actions">
        <button type="submit">筛选</button>
        <button type="button" id="usage-filter-reset">清空</button>
      </div>
    </form>
  `;
}

/**
 * 渲染总量指标卡，成本按 CNY/USD 分开展示，不做汇率折算。
 */
function renderTotals(totals = {}) {
  return `
    <div class="usage-total-grid" aria-label="用量总览">
      ${renderMetric("请求数", totals.request_count ?? 0, "条")}
      ${renderMetric("总 Token", totals.total_tokens ?? 0, "tokens")}
      ${renderMetric("缓存 Token", totals.cache_tokens ?? 0, "tokens")}
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
 * 渲染按天、模型和 Virtual Key 的聚合表，帮助快速定位主要成本来源。
 */
function renderSummaryTables(summary = {}) {
  return `
    <div class="usage-summary-grid">
      ${renderDaySummary(summary.by_day ?? [])}
      ${renderModelSummary(summary.by_model ?? [])}
      ${renderKeySummary(summary.by_virtual_key ?? [])}
      ${renderEnvironmentSummary(summary.by_environment ?? [])}
      ${renderProjectSummary(summary.by_project ?? [])}
      ${renderApplicationSummary(summary.by_application ?? [])}
    </div>
  `;
}

/**
 * 渲染按天聚合表，展示趋势所需的核心字段。
 */
function renderDaySummary(rows) {
  return `
    <section class="usage-summary-block">
      <h3>按天</h3>
      <div class="usage-mini-table" role="table" aria-label="按天用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>日期</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length ? rows.map((row) => renderMiniRow(row.day, row.request_count, row.total_tokens, formatDualCost(row))).join("") : renderMiniEmpty()}
      </div>
    </section>
  `;
}

/**
 * 渲染按模型聚合表，展示模型 ID、请求数、Token 和成本。
 */
function renderModelSummary(rows) {
  return `
    <section class="usage-summary-block">
      <h3>按模型</h3>
      <div class="usage-mini-table" role="table" aria-label="按模型用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>模型</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length ? rows.map((row) => renderMiniRow(row.resolved_model ?? row.model_id ?? "-", row.request_count, row.total_tokens, formatDualCost(row))).join("") : renderMiniEmpty()}
      </div>
    </section>
  `;
}

/**
 * 渲染按 Virtual Key 聚合表，只展示 key_prefix 或 ID，不展示密钥明文。
 */
function renderKeySummary(rows) {
  return `
    <section class="usage-summary-block">
      <h3>按 Key</h3>
      <div class="usage-mini-table" role="table" aria-label="按 Virtual Key 用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>Key</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length ? rows.map((row) => renderMiniRow(row.virtual_key_prefix ?? row.virtual_key_id ?? "-", row.request_count, row.total_tokens, formatDualCost(row))).join("") : renderMiniEmpty()}
      </div>
    </section>
  `;
}

/**
 * 渲染按 Environment 聚合表，帮助管理员核对各环境请求量、Token 和成本。
 */
function renderEnvironmentSummary(rows) {
  /** 按 Environment 展示请求、Token 和成本，名称缺失时回退到代码或稳定 ID。 */
  return `
    <section class="usage-summary-block">
      <h3>按 Environment</h3>
      <div class="usage-mini-table" role="table" aria-label="按 Environment 用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>环境</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length
          ? rows.map((row) => renderMiniRow(row.environment_name ?? row.environment ?? row.environment_id ?? "未知", row.request_count, row.total_tokens, formatDualCost(row))).join("")
          : renderMiniEmpty()}
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
 * message 只包含固定状态、文件名、job ID 和 request_id，仍统一转义以阻止异常响应写入 HTML。
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
 * 渲染按 Project 聚合表，帮助财务按一级业务系统核对成本。
 */
function renderProjectSummary(rows) {
  return `
    <section class="usage-summary-block">
      <h3>按 Project</h3>
      <div class="usage-mini-table" role="table" aria-label="按 Project 用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>Project</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length ? rows.map((row) => renderMiniRow(row.project_name ?? row.project_id ?? "未绑定", row.request_count, row.total_tokens, formatDualCost(row))).join("") : renderMiniEmpty()}
      </div>
    </section>
  `;
}

/**
 * 渲染按 Application 聚合表，帮助定位同一项目下的具体调用来源。
 */
function renderApplicationSummary(rows) {
  return `
    <section class="usage-summary-block">
      <h3>按 Application</h3>
      <div class="usage-mini-table" role="table" aria-label="按 Application 用量">
        <div class="usage-mini-row usage-mini-head" role="row">
          <span>Application</span><span>请求</span><span>Token</span><span>成本</span>
        </div>
        ${rows.length ? rows.map((row) => renderMiniRow(row.application_name ?? row.application_id ?? "未绑定", row.request_count, row.total_tokens, formatDualCost(row))).join("") : renderMiniEmpty()}
      </div>
    </section>
  `;
}

/**
 * 渲染聚合小表的一行，保持各列稳定对齐。
 */
function renderMiniRow(label, requestCount, totalTokens, cost) {
  return `
    <div class="usage-mini-row" role="row">
      <span title="${escapeAttribute(label)}">${escapeHtml(label)}</span>
      <span>${escapeHtml(requestCount ?? 0)}</span>
      <span>${escapeHtml(totalTokens ?? 0)}</span>
      <span>${escapeHtml(cost)}</span>
    </div>
  `;
}

/**
 * 渲染聚合表空状态，避免表格区域塌陷。
 */
function renderMiniEmpty() {
  return `<div class="usage-mini-row usage-mini-empty" role="row"><span>暂无数据</span></div>`;
}

/**
 * 渲染用量明细表，只展示规范化 usage 和脱敏元数据。
 */
function renderRecordTable(records) {
  return `
    <div class="usage-record-table" role="table" aria-label="当前租户用量记录">
      <div class="usage-record-row usage-record-head" role="row">
        <span>request_id</span>
        <span>模型</span>
        <span>Provider</span>
        <span>Key</span>
        <span>归属</span>
        <span>Token</span>
        <span>成本</span>
        <span>来源</span>
        <span>时间</span>
      </div>
      ${records.length ? records.map(renderRecordRow).join("") : renderRecordEmpty()}
    </div>
  `;
}

/**
 * 渲染单条 UsageRecord，估算记录会用 mark 明确标识。
 */
function renderRecordRow(item) {
  return `
    <div class="usage-record-row" role="row">
      <span class="usage-record-id" data-label="request_id">
        <code>${escapeHtml(item.request_id)}</code>
        <em>${escapeHtml(item.price_snapshot_id ?? "-")}</em>
      </span>
      <span class="usage-record-model" data-label="模型">
        <strong>${escapeHtml(item.request_model ?? "-")}</strong>
        <em>${escapeHtml(item.resolved_model ?? item.model_id ?? "-")}</em>
      </span>
      <span data-label="Provider">${escapeHtml(item.provider_code ?? "-")}</span>
      <span data-label="Key">${escapeHtml(item.virtual_key_prefix ?? item.virtual_key_id ?? "-")}</span>
      <span class="usage-record-scope" data-label="归属">
        <strong>${escapeHtml(item.project_name ?? item.project_id ?? "未绑定项目")}</strong>
        <em>${escapeHtml(item.application_name ?? item.application_id ?? "未绑定应用")}</em>
      </span>
      <span data-label="Token">${escapeHtml(formatTokens(item))}</span>
      <span data-label="成本">${escapeHtml(formatDualCost(item))}</span>
      <span data-label="来源">${renderUsageSource(item)}</span>
      <time data-label="时间" datetime="${escapeAttribute(item.created_at)}">${escapeHtml(formatDateTime(item.created_at))}</time>
      ${renderNormalizationDetails(item)}
    </div>
  `;
}

/**
 * 渲染用量明细空状态，提示当前筛选条件下没有记录。
 */
function renderRecordEmpty() {
  return `
    <div class="usage-record-row usage-record-empty" role="row">
      <span>当前条件下没有用量记录。</span>
    </div>
  `;
}

/**
 * 渲染分页状态和上一页/下一页按钮。
 */
function renderPager(recordsState) {
  const limit = Number(recordsState.limit ?? 20);
  const offset = Number(recordsState.offset ?? 0);
  const total = Number(recordsState.total ?? 0);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;
  return `
    <div class="usage-pager" data-limit="${escapeAttribute(limit)}" data-offset="${escapeAttribute(offset)}">
      <button type="button" id="usage-prev" ${canPrev ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(total ? offset + 1 : 0)}-${escapeHtml(Math.min(offset + limit, total))} / ${escapeHtml(total)}</span>
      <button type="button" id="usage-next" ${canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/**
 * 渲染 usage 来源标签，估算记录用醒目标记提醒用户不要当成厂商账单。
 */
function renderUsageSource(item) {
  const label = item.estimated ? "估算" : "真实";
  const status = item.estimated ? "estimated" : "real";
  return `<mark class="usage-source usage-source-${status}">${escapeHtml(label)}</mark>`;
}

/**
 * 渲染可展开归一化详情，展示内部成本与已脱敏 Provider usage 双口径。
 */
function renderNormalizationDetails(item) {
  const normalized = item.normalized_usage ?? {};
  const providerCost = normalized.provider_cost_amount
    ? `${normalized.provider_cost_amount} ${normalized.provider_cost_currency ?? "未知币种"}`
    : "未上报";
  const warnings = Array.isArray(normalized.normalization_warnings)
    ? normalized.normalization_warnings.join(", ") || "无"
    : "无";
  return `
    <details class="usage-normalization-details">
      <summary>归一化详情</summary>
      <dl>
        <div><dt>Provider 成本</dt><dd>${escapeHtml(providerCost)}</dd></div>
        <div><dt>归一警告</dt><dd>${escapeHtml(warnings)}</dd></div>
      </dl>
      <strong>Normalized Usage</strong>
      <pre>${escapeHtml(safeJson(normalized))}</pre>
      <strong>Provider Raw Usage</strong>
      <pre>${escapeHtml(safeJson(item.provider_raw_usage ?? {}))}</pre>
    </details>
  `;
}

/**
 * 把安全用量对象格式化为稳定 JSON，序列化失败时返回空对象占位。
 */
function safeJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return "{}";
  }
}

/**
 * 格式化 Token 摘要，缓存 Token 单独展示以免被重复计入总量。
 */
function formatTokens(item) {
  return `${item.total_tokens ?? 0} total / ${item.cache_tokens ?? 0} cache`;
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
      ${escapeHtml(error.message ?? "用量操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
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
 * 格式化 ISO 时间字符串，避免表格中出现过长的小数秒。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
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
