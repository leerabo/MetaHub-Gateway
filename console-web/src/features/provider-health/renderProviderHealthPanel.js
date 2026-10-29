import { hasEveryPermission, hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/** Provider 健康 effective 状态中文标签，真实路由判断仍以后端原始字段为准。 */
const STATUS_LABELS = {
  healthy: "健康",
  unhealthy: "异常",
  observing: "观察中",
  unknown: "未知",
  disabled: "已禁用",
};

const PROVIDER_TYPE_LABELS = Object.freeze({
  zhipu: "智谱",
  dashscope: "通义",
  wenxin: "文心",
  volcengine: "火山引擎",
  hunyuan: "腾讯混元",
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI 兼容",
  custom: "自定义服务",
  migration_compatible: "迁移兼容",
});

/** 渲染 PAGE-051 Provider 健康页。 */
export function renderProviderHealthPanel(state, authContext) {
  // 缺少后端健康读权限时零 DOM；OpsAdmin 等角色不再被旧角色集合拦截。
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_READ)) {
    return "";
  }
  if (!state) {
    return `<div class="empty-state">正在读取 Provider 健康状态。</div>`;
  }
  // 统一错误只展示安全 message 与 request_id，不渲染底层异常或响应正文。
  if (state.state === "error") {
    return `<div class="empty-state error-state">${escapeHtml(state.message ?? "Provider 健康读取失败")} request_id: ${escapeHtml(state.requestId ?? "-")}</div>`;
  }
  const summary = state.summary ?? {};
  return `
    <div class="provider-health-panel">
      ${state.actionError ? `<div class="empty-state error-state">${escapeHtml(state.actionError.message)} request_id: ${escapeHtml(state.actionError.requestId ?? "-")}</div>` : ""}
      <div class="provider-health-summary" aria-label="Provider 健康汇总">
        ${renderSummary("健康", summary.healthy, "healthy")}
        ${renderSummary("异常", summary.unhealthy, "unhealthy")}
        ${renderSummary("观察中", summary.observing, "observing")}
        ${renderSummary("未知", summary.unknown, "unknown")}
        ${renderSummary("已禁用", summary.disabled, "disabled")}
      </div>
      <form id="provider-health-filters" class="provider-health-filters">
        <label>健康状态<select name="health_status"><option value="">全部</option>${statusOptions(state.filters?.health_status)}</select></label>
        <label>配置状态<select name="provider_status"><option value="">全部</option><option value="active">启用</option><option value="disabled">禁用</option></select></label>
        <button type="submit">筛选</button>
        <button type="reset" data-provider-health-reset>重置</button>
      </form>
      <div class="provider-health-list">
        ${(state.provider_health ?? []).length
          ? state.provider_health.map((item) => renderHealthRow(
              item,
              state.probingProviderId,
              hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_PROBE),
            )).join("")
          : `<div class="empty-state">当前筛选条件下没有 Provider。</div>`}
      </div>
    </div>
  `;
}

/**
 * 渲染 Dashboard 的独立健康摘要卡，接口失败不影响原有指标。
 * 详情链接必须同时满足双权限和后端导航 owner，只有健康读取权限的自定义角色仍可看摘要但不会看到死链接。
 */
export function renderProviderHealthDashboardCard(state, authContext, consoleShellState = null) {
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_READ)) {
    return "";
  }
  // Dashboard 卡片独立降级，健康接口失败不能破坏原请求、成本和错误指标。
  if (!state || state.state === "error") {
    return `<section class="dashboard-card provider-health-dashboard"><div class="dashboard-card-heading"><h3>Provider 健康</h3></div><div class="empty-state">健康摘要暂不可用${state?.requestId ? ` request_id: ${escapeHtml(state.requestId)}` : ""}</div></section>`;
  }
  const summary = state.summary ?? {};
  const healthyRatio = summary.total ? Math.round((Number(summary.healthy ?? 0) / summary.total) * 100) : 0;
  const exceptions = (state.provider_health ?? []).filter((item) => ["unhealthy", "observing"].includes(item.effective_health_status)).slice(0, 3);
  const canOpenStatus = canOpenProviderStatus(authContext, consoleShellState);
  return `<section class="dashboard-card provider-health-dashboard">
    <div class="dashboard-card-heading"><h3>Provider 健康</h3>${canOpenStatus ? '<button type="button" class="dashboard-card-action" data-console-nav="provider-health" data-console-route="workspace.gateway.provider-health" data-console-target="provider-health">查看详情</button>' : ""}</div>
    <strong class="provider-health-ratio">${escapeHtml(healthyRatio)}%</strong>
    <span>异常 ${escapeHtml(summary.unhealthy ?? 0)} · 观察中 ${escapeHtml(summary.observing ?? 0)}</span>
    <div class="dashboard-list">${exceptions.length ? exceptions.map((item) => `<div class="dashboard-list-row"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(STATUS_LABELS[item.effective_health_status])}</span></div>`).join("") : `<div class="empty-state">暂无异常 Provider</div>`}</div>
  </section>`;
}

/** 后端已授权导航与当前权限快照必须同时确认 PAGE-088 可进入。 */
function canOpenProviderStatus(authContext, consoleShellState) {
  if (!hasEveryPermission(authContext, [
    PERMISSIONS.PROVIDER_HEALTH_READ,
    PERMISSIONS.REQUEST_LOG_READ,
  ])) {
    return false;
  }
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.some((item) => (
    item?.id === "provider-health"
    && item?.target_section === "provider-health"
    && item?.visible !== false
    && item?.disabled !== true
  ));
}

/** 渲染一个稳定宽度的状态汇总单元。 */
function renderSummary(label, value, status) {
  return `<div class="health-summary-item status-${escapeAttribute(status)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? 0)}</strong></div>`;
}

/** 渲染单个 Provider；状态文字与颜色同时存在，满足可访问性要求。 */
function renderHealthRow(item, probingProviderId, canProbe) {
  const probing = probingProviderId === item.provider_id;
  return `<article class="provider-health-row status-${escapeAttribute(item.effective_health_status)}">
    <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(PROVIDER_TYPE_LABELS[item.provider_type] ?? "模型服务")}</span><span class="break-anywhere">${escapeHtml(item.base_url)}</span></div>
    <dl><div><dt>状态</dt><dd>${escapeHtml(STATUS_LABELS[item.effective_health_status] ?? item.effective_health_status)}</dd></div><div><dt>最近成功</dt><dd>${formatDate(item.last_success_at)}</dd></div><div><dt>延迟</dt><dd>${item.last_probe_latency_ms == null ? "-" : `${escapeHtml(item.last_probe_latency_ms)} ms`}</dd></div><div><dt>错误</dt><dd>${escapeHtml(item.last_probe_error_code ?? "-")}</dd></div></dl>
    ${renderHealthCaveat(item)}
    ${canProbe ? `<button type="button" class="icon-button" data-provider-probe="${escapeAttribute(item.provider_id)}" title="立即探测" aria-label="立即探测 ${escapeAttribute(item.name)}" ${probing ? "disabled" : ""}>${probing ? "探测中" : "↻"}</button>` : ""}
  </article>`;
}

function renderHealthCaveat(item) {
  const probeTotal = Number.isSafeInteger(item.probe_total) ? item.probe_total : 0;
  const probeRate = Number.isSafeInteger(item.probe_success_rate_bps) ? item.probe_success_rate_bps : null;
  const notices = [];
  if (Number.isSafeInteger(item.eligible_call_count) && item.eligible_call_count === 0) {
    notices.push("无真实调用样本，状态主要来自探测");
  }
  if (probeTotal > 0 && probeRate !== null && probeRate < 10000) {
    notices.push(`探测成功率 ${formatRate(probeRate)}`);
  }
  return notices.length ? `<small class="provider-health-caveat" role="note">证据范围：${escapeHtml(notices.join("；"))}</small>` : "";
}

function formatRate(value) {
  if (!Number.isSafeInteger(value) || value < 0) return "暂无数据";
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}%`;
}

/** 生成健康状态筛选项。 */
function statusOptions(selected) {
  return Object.entries(STATUS_LABELS).map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

/** 格式化服务端 ISO 时间，空值保持短横线。 */
function formatDate(value) {
  return value ? escapeHtml(new Date(value).toLocaleString("zh-CN", { hour12: false })) : "-";
}

/** 转义所有数据库展示文本，防止 Provider 名称或 URL 形成 XSS。 */
function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

/** 转义 HTML 属性值，并额外处理反引号。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
