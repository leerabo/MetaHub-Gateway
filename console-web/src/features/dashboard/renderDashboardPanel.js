import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// Dashboard 指标单位和状态文案只用于前端展示，真实聚合口径必须以后端 /dashboard/overview 为准。
const UNIT_FORMATTERS = {
  count: (value) => formatInteger(value),
  percent: (value) => `${formatNumber(value, 2)}%`,
  ms: (value) => `${formatInteger(value)} ms`,
  cny: (value) => `¥${formatNumber(value, 4)}`,
  tokens: (value) => formatInteger(value),
};

// Dashboard 状态标签只负责把后端状态枚举翻译为中文展示，不在前端重新判断业务阈值。
const STATUS_LABELS = {
  ok: "正常",
  warning: "关注",
  critical: "异常",
  empty: "待接入",
};

/**
 * 渲染 MVP 总览 Dashboard。
 * 页面只展示当前租户聚合指标和安全摘要，不能出现 Prompt、Response、Authorization、Provider Key 或 Virtual Key 明文。
 */
export function renderDashboardPanel(dashboardState, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.DASHBOARD_READ)) {
    return "";
  }
  if (!dashboardState) {
    return renderLoadingPanel();
  }
  // 后端聚合失败时只展示统一错误摘要，定位信息进入后台日志。
  if (dashboardState.state === "error") {
    return renderErrorPanel(dashboardState);
  }

  const canOpenLogs = hasPermission(authContext, PERMISSIONS.REQUEST_LOG_READ);
  const overview = dashboardState.overview ?? dashboardState;
  return `
    <section class="dashboard-panel" aria-labelledby="dashboard-title">
      <div class="panel-heading dashboard-heading">
        <div>
          <h2 id="dashboard-title">总览 Dashboard</h2>
          <p>${escapeHtml(formatWindow(overview.window))}</p>
        </div>
        <span class="dashboard-refresh">刷新 ${escapeHtml(formatDateTime(overview.window?.generated_at))}</span>
      </div>
      ${renderEmptyState(overview.empty_state)}
      <div class="dashboard-metric-grid">
        ${(overview.metrics ?? []).map(renderMetric).join("")}
      </div>
      <div class="dashboard-insight-grid">
        ${renderTopModels(overview.top_models ?? [])}
        ${renderTopVirtualKeys(overview.top_virtual_keys ?? [])}
      </div>
      ${renderRecentErrors(overview.recent_errors ?? [], canOpenLogs)}
    </section>
  `;
}

/**
 * 渲染 Dashboard 加载态，避免刷新过程中 overview 出现空白。
 */
function renderLoadingPanel() {
  return `
    <section class="dashboard-panel" aria-labelledby="dashboard-title">
      <div class="panel-heading">
        <h2 id="dashboard-title">总览 Dashboard</h2>
        <p>正在读取今日请求、成本和错误摘要。</p>
      </div>
      <div class="empty-state">等待 /dashboard/overview 响应。</div>
    </section>
  `;
}

/**
 * 渲染 Dashboard 错误态。
 */
function renderErrorPanel(state) {
  return `
    <section class="dashboard-panel" aria-labelledby="dashboard-title">
      <div class="panel-heading">
        <h2 id="dashboard-title">总览 Dashboard</h2>
        <p>今日指标暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(state.message ?? "Dashboard 读取失败，请稍后重试")}
      </div>
    </section>
  `;
}

/**
 * 渲染空数据接入引导。
 * 推荐动作来自后端白名单，不在前端拼接外部链接或敏感参数。
 */
function renderEmptyState(emptyState) {
  if (!emptyState?.is_empty) {
    return "";
  }
  return `
    <div class="dashboard-empty-state">
      <div>
        <strong>${escapeHtml(emptyState.title || "今天还没有调用数据")}</strong>
        <span>${escapeHtml(emptyState.description || "完成一次调用后即可看到真实指标。")}</span>
      </div>
      <div class="dashboard-empty-actions">
        ${(emptyState.recommended_actions ?? []).map(renderRecommendedAction).join("")}
      </div>
    </div>
  `;
}

/**
 * 渲染单个空状态推荐动作。
 */
function renderRecommendedAction(action) {
  const target = escapeAttribute(action.target ?? "overview");
  return `
    <button type="button" class="dashboard-empty-action" data-console-nav="${target}" data-console-target="${target}">
      <strong>${escapeHtml(action.label ?? "操作")}</strong>
      <span>${escapeHtml(action.description ?? "")}</span>
    </button>
  `;
}

/**
 * 渲染单个 KPI 指标块。
 * 指标值、单位和状态均来自后端聚合结果，前端只做格式化与 HTML 转义，避免展示层改写治理口径。
 */
function renderMetric(metric) {
  const formatter = UNIT_FORMATTERS[metric.unit] ?? ((value) => escapeHtml(value));
  const status = metric.status ?? "empty";
  return `
    <article class="dashboard-metric status-${escapeAttribute(status)}">
      <div>
        <span>${escapeHtml(metric.label)}</span>
        <mark>${escapeHtml(STATUS_LABELS[status] ?? status)}</mark>
      </div>
      <strong>${escapeHtml(formatter(metric.value ?? 0))}</strong>
      <em>${escapeHtml(metric.description ?? "")}</em>
    </article>
  `;
}

/**
 * 渲染 Top 模型列表，成本和请求量均以后端聚合值为准。
 */
function renderTopModels(models) {
  return `
    <section class="dashboard-card" aria-labelledby="dashboard-top-models">
      <div class="dashboard-card-heading">
        <h3 id="dashboard-top-models">Top 模型</h3>
        <span>${escapeHtml(models.length)} 条</span>
      </div>
      <div class="dashboard-list">
        ${models.length ? models.map(renderTopModel).join("") : renderMiniEmpty("暂无模型调用")}
      </div>
    </section>
  `;
}

/**
 * 渲染单条模型排行。
 */
function renderTopModel(item) {
  return `
    <article class="dashboard-list-row">
      <div>
        <strong>${escapeHtml(item.model_display_name ?? item.resolved_model ?? "默认路由")}</strong>
        <span>${escapeHtml(item.provider_name ?? "已接入模型服务")}</span>
      </div>
      <dl>
        <div><dt>请求</dt><dd>${escapeHtml(formatInteger(item.request_count))}</dd></div>
        <div><dt>成功</dt><dd>${escapeHtml(formatInteger(item.success_count))}</dd></div>
        <div><dt>Token</dt><dd>${escapeHtml(formatInteger(item.total_tokens))}</dd></div>
        <div><dt>成本</dt><dd>¥${escapeHtml(formatNumber(item.cost_cny, 4))}</dd></div>
      </dl>
    </article>
  `;
}

/**
 * 渲染 Top Virtual Key 列表，只显示脱敏前缀和名称。
 */
function renderTopVirtualKeys(keys) {
  return `
    <section class="dashboard-card" aria-labelledby="dashboard-top-keys">
      <div class="dashboard-card-heading">
        <h3 id="dashboard-top-keys">Top Virtual Key</h3>
        <span>${escapeHtml(keys.length)} 条</span>
      </div>
      <div class="dashboard-list">
        ${keys.length ? keys.map(renderTopVirtualKey).join("") : renderMiniEmpty("暂无 Key 调用")}
      </div>
    </section>
  `;
}

/**
 * 渲染单条 Virtual Key 排行。
 * 这里只展示名称和脱敏前缀，任何明文 Key、哈希或密文字段都不能进入 Dashboard DOM。
 */
function renderTopVirtualKey(item) {
  return `
    <article class="dashboard-list-row">
      <div>
        <strong>${escapeHtml(item.virtual_key_name ?? "调用 KEY")}</strong>
        <span>${escapeHtml(item.virtual_key_prefix ?? "-")}</span>
      </div>
      <dl>
        <div><dt>请求</dt><dd>${escapeHtml(formatInteger(item.request_count))}</dd></div>
        <div><dt>成功</dt><dd>${escapeHtml(formatInteger(item.success_count))}</dd></div>
        <div><dt>Token</dt><dd>${escapeHtml(formatInteger(item.total_tokens))}</dd></div>
        <div><dt>成本</dt><dd>¥${escapeHtml(formatNumber(item.cost_cny, 4))}</dd></div>
      </dl>
    </article>
  `;
}

/**
 * 渲染最近错误列表，viewer 不展示调用日志跳转。
 */
function renderRecentErrors(errors, canOpenLogs) {
  return `
    <section class="dashboard-card dashboard-errors" aria-labelledby="dashboard-errors-title">
      <div class="dashboard-card-heading">
        <h3 id="dashboard-errors-title">最近错误</h3>
        <span>${escapeHtml(errors.length)} 条</span>
      </div>
      <div class="dashboard-error-list">
        ${errors.length ? errors.map((item) => renderRecentError(item, canOpenLogs)).join("") : renderMiniEmpty("暂无失败请求")}
      </div>
    </section>
  `;
}

/**
 * 渲染单条最近错误摘要。
 */
function renderRecentError(item, canOpenLogs) {
  const requestId = item.request_id ?? "-";
  return `
    <article class="dashboard-error-row">
      <div>
        <strong>调用失败</strong>
        <span>${escapeHtml(item.diagnosis_title ?? "未命中特定错误归因规则。")}</span>
      </div>
      <dl>
        <div><dt>模型</dt><dd>${escapeHtml(item.model ?? "-")}</dd></div>
        <div><dt>模型服务</dt><dd>${escapeHtml(item.provider_name ?? "已接入模型服务")}</dd></div>
        <div><dt>延迟</dt><dd>${escapeHtml(item.latency_ms == null ? "-" : `${formatInteger(item.latency_ms)} ms`)}</dd></div>
        <div><dt>时间</dt><dd>${escapeHtml(formatDateTime(item.created_at))}</dd></div>
      </dl>
      ${canOpenLogs ? `<button type="button" data-console-nav="request-logs" data-console-route="workspace.observability.requests" data-console-target="request-logs" data-request-id="${escapeAttribute(requestId)}">查看日志</button>` : ""}
    </article>
  `;
}

/**
 * 渲染小型空状态文本。
 */
function renderMiniEmpty(label) {
  return `<div class="dashboard-mini-empty">${escapeHtml(label)}</div>`;
}

/**
 * 格式化 Dashboard 时间窗口。
 */
function formatWindow(windowInfo) {
  if (!windowInfo?.start_at || !windowInfo?.end_at) {
    return "今日窗口待加载";
  }
  return `${formatDateTime(windowInfo.start_at)} - ${formatDateTime(windowInfo.end_at)} · ${windowInfo.timezone ?? "Asia/Shanghai"}`;
}

/**
 * 格式化日期时间展示，去掉毫秒保持 Dashboard 紧凑。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * 格式化整数指标。
 */
function formatInteger(value) {
  return Number(value ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

/**
 * 格式化小数指标。
 */
function formatNumber(value, digits) {
  return Number(value ?? 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

/**
 * HTML 文本转义，所有来自 API 的展示文本都必须经过这里。
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
 * HTML 属性转义，避免 request_id、target 等字段破坏属性边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
