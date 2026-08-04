/**
 * 模块职责：渲染 PAGE-088 内部 Provider 状态页。
 * 安全边界：页面只消费后端脱敏快照；权限、租户隔离、错误码白名单和 Endpoint 清洗仍由 Control API 决定。
 */

import { hasEveryPermission, hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/** Provider effective 状态与可用性的固定中文映射；这里只翻译枚举，不根据指标重新推导健康结论。 */
const STATUS_LABELS = Object.freeze({
  healthy: "健康",
  unhealthy: "异常",
  observing: "观察中",
  unknown: "未知",
  disabled: "已禁用",
});

/** 后端冻结的可用性枚举中文标签。 */
const AVAILABILITY_LABELS = Object.freeze({
  available: "可用",
  degraded: "降级",
  unavailable: "不可用",
  unknown: "未知",
  disabled: "已禁用",
});

/** Provider 类型筛选只提供后端白名单中的稳定选项，不允许页面拼接任意上游类型。 */
const PROVIDER_TYPE_LABELS = Object.freeze({
  zhipu: "智谱",
  dashscope: "通义",
  wenxin: "文心",
  volcengine: "火山引擎",
  hunyuan: "腾讯混元",
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI-compatible",
  custom: "客户本地 / 自定义",
  migration_compatible: "迁移兼容",
});

/** 故障来源和分类只翻译固定枚举，未知值仍以转义文本展示，便于发现合同漂移。 */
const FAILURE_SOURCE_LABELS = Object.freeze({
  non_stream_call: "非流式调用",
  stream_call: "流式调用",
  probe: "探测",
});
const FAILURE_KIND_LABELS = Object.freeze({
  provider: "Provider 故障",
  credential: "凭据故障",
  excluded: "排除项",
  probe: "探测故障",
});

/** PAGE-088 读取真实调用级故障，因此前端可见性必须同时具备健康与调用日志权限。 */
const STATUS_READ_PERMISSIONS = Object.freeze([
  PERMISSIONS.PROVIDER_HEALTH_READ,
  PERMISSIONS.REQUEST_LOG_READ,
]);

/**
 * 渲染内部 Provider 状态页。
 *
 * 参数：state 为 `/status/providers` 的安全响应或 loading/error 状态；authContext 为后端权限快照。
 * 返回：可直接放入控制台 shell 的 HTML；缺少任一读取权限时返回空字符串并保持零业务 DOM。
 */
export function renderProviderStatusPanel(state, authContext) {
  if (!hasEveryPermission(authContext, STATUS_READ_PERMISSIONS)) {
    return "";
  }
  const warning = renderEventWarning(state?.eventWarning);
  if (!state) {
    return `<div class="provider-status-page" data-provider-status-root>
      ${renderHeading()}
      ${warning}
      <div class="empty-state provider-status-loading" aria-live="polite">正在读取 Provider 状态。</div>
    </div>`;
  }
  // 错误态只展示统一 message/request_id，绝不回显响应正文、SQL 或原始上游错误。
  if (state.state === "error") {
    return `<div class="provider-status-page" data-provider-status-root>
      ${renderHeading()}
      ${warning}
      <div class="empty-state error-state provider-status-loading" role="alert">${escapeHtml(state.message ?? "Provider 状态读取失败")} · request_id: ${escapeHtml(state.requestId ?? "-")}</div>
    </div>`;
  }

  const statuses = Array.isArray(state.provider_statuses) ? state.provider_statuses : [];
  return `<div class="provider-status-page" data-provider-status-root>
    ${renderHeading()}
    ${warning}
    ${renderActionError(state.actionError)}
    ${renderStatusSummary(state.summary)}
    <div class="provider-status-window" aria-label="统计时间窗口">
      <span>统计口径：过去 24 小时</span>
      <span>生成时间：${formatDate(state.window_ended_at)}</span>
    </div>
    ${renderFilters(state.filters)}
    <div class="provider-status-list" aria-live="polite">
      ${statuses.length
        ? statuses.map((item) => renderStatusItem(
            item,
            state.probingProviderId,
            hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_PROBE),
          )).join("")
        : `<div class="empty-state provider-status-empty">当前筛选条件下没有 Provider。</div>`}
    </div>
    ${renderPagination(state, statuses.length)}
  </div>`;
}

/** 渲染页面标题；标题属于已授权 shell，不依赖状态接口是否成功。 */
function renderHeading() {
  return `<header class="provider-status-heading">
    <div><h2 id="provider-health-title">Provider 状态</h2><p>当前可用性、真实调用与独立探测</p></div>
  </header>`;
}

/** 渲染非阻断访问事件告警；始终保留节点以便异步事件失败时原位更新。 */
function renderEventWarning(warning) {
  const hasWarning = Boolean(warning?.message);
  return `<div class="provider-status-event-warning empty-state error-state" data-provider-status-event-warning ${hasWarning ? "" : "hidden"}>${hasWarning
    ? `访问记录暂不可用：${escapeHtml(warning.message)} · request_id: ${escapeHtml(warning.requestId ?? "-")}`
    : ""}</div>`;
}

/** 渲染互斥状态汇总；总数来自分页前筛选结果，不能由当前页条数替代。 */
function renderStatusSummary(summary = {}) {
  const cells = [
    ["Provider 总数", summary.total, "total"],
    ["健康", summary.healthy, "healthy"],
    ["异常", summary.unhealthy, "unhealthy"],
    ["观察中", summary.observing, "observing"],
    ["未知", summary.unknown, "unknown"],
    ["已禁用", summary.disabled, "disabled"],
  ];
  return `<div class="provider-status-summary" aria-label="Provider 状态汇总">${cells.map(
    ([label, value, status]) => `<div class="provider-status-summary-item status-${escapeAttribute(status)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(safeCount(value))}</strong></div>`,
  ).join("")}</div>`;
}

/** 渲染固定枚举筛选；label/for 关系保证键盘和辅助技术可以准确操作控件。 */
function renderFilters(filters = {}) {
  return `<form id="provider-health-filters" class="provider-health-filters provider-status-filters">
    <label for="provider-status-health-filter">健康状态<select id="provider-status-health-filter" name="health_status"><option value="">全部</option>${optionsFor(STATUS_LABELS, filters.health_status)}</select></label>
    <label for="provider-status-config-filter">配置状态<select id="provider-status-config-filter" name="provider_status"><option value="">全部</option>${optionsFor({ active: "启用", disabled: "禁用" }, filters.provider_status)}</select></label>
    <label for="provider-status-type-filter">Provider 类型<select id="provider-status-type-filter" name="provider_type"><option value="">全部</option>${optionsFor(PROVIDER_TYPE_LABELS, filters.provider_type)}</select></label>
    <button type="submit">筛选</button>
    <button type="reset" data-provider-health-reset>重置</button>
  </form>`;
}

/** 渲染单个 Provider 状态卡；所有数据库文本都在对应 HTML 上下文转义。 */
function renderStatusItem(item = {}, probingProviderId, canProbe) {
  const effectiveStatus = STATUS_LABELS[item.effective_health_status]
    ? item.effective_health_status
    : "unknown";
  const failures = Array.isArray(item.recent_failures) ? item.recent_failures : [];
  const probing = probingProviderId === item.provider_id;
  return `<article class="provider-status-item status-${escapeAttribute(effectiveStatus)}">
    <header class="provider-status-item-heading">
      <div class="provider-status-identity">
        <strong>${escapeHtml(item.name ?? "-")}</strong>
        <span class="break-anywhere">${escapeHtml(item.provider_code ?? "-")} · ${escapeHtml(item.provider_type ?? "-")}</span>
        <span class="break-anywhere">${escapeHtml(item.base_url ?? "-")}</span>
      </div>
      <div class="provider-status-current" aria-label="当前状态：${escapeAttribute(STATUS_LABELS[effectiveStatus])}">
        <strong>${escapeHtml(STATUS_LABELS[effectiveStatus])}</strong>
        <span>${escapeHtml(AVAILABILITY_LABELS[item.availability_status] ?? "未知")} · ${item.is_health_stale ? "探测已过期" : "探测有效"}</span>
      </div>
      ${canProbe ? `<button type="button" class="icon-button" data-provider-probe="${escapeAttribute(item.provider_id ?? "")}" title="${probing ? "探测中" : "立即探测"}" aria-label="${probing ? "正在探测" : "立即探测"} ${escapeAttribute(item.name ?? "Provider")}" ${probing ? "disabled" : ""}>${probing ? "…" : "↻"}</button>` : ""}
    </header>
    <div class="provider-status-metrics" aria-label="过去 24 小时指标">
      ${renderMetric("Provider 归因成功率", formatRate(item.success_rate_bps, "暂无调用样本"), `${safeCount(item.eligible_call_count)} 个资格样本`)}
      ${renderMetric("非流式调用 P95", formatMilliseconds(item.call_latency_p95_ms), `${safeCount(item.call_latency_sample_count)} 个延迟样本`)}
      ${renderMetric("流式首块 P95", formatMilliseconds(item.stream_first_chunk_p95_ms), `${safeCount(item.stream_first_chunk_sample_count)} 个首块样本`)}
      ${renderMetric("探测成功率 / P95", `${formatRate(item.probe_success_rate_bps, "暂无确定性探测")} / ${formatMilliseconds(item.probe_latency_p95_ms)}`, `${safeCount(item.probe_total)} 次完整探测`)}
    </div>
    ${renderCallCounts(item)}
    ${renderProbeCounts(item)}
    <div class="provider-status-context">
      <div><span>最近错误</span><strong>${escapeHtml(item.last_error?.error_code ?? "-")}</strong><small>${formatDate(item.last_error?.observed_at)}</small></div>
      <div><span>维护备注</span><strong class="provider-maintenance-note">${escapeHtml(item.maintenance_note ?? "无维护备注")}</strong></div>
      <div><span>最近调用 / 探测</span><strong>${formatDate(item.latest_call_at)} / ${formatDate(item.latest_probe_at)}</strong></div>
    </div>
    ${renderFailures(failures)}
  </article>`;
}

/** 手工探测失败只展示统一错误字段，不覆盖仍可读取的状态快照。 */
function renderActionError(error) {
  return error?.message
    ? `<div class="provider-status-action-error empty-state error-state" role="alert">${escapeHtml(error.message)} · request_id: ${escapeHtml(error.requestId ?? "-")}</div>`
    : "";
}

/** 渲染一个固定尺寸指标单元；主值与样本说明分层，异步刷新不会改变网格轨道。 */
function renderMetric(label, value, detail) {
  return `<div class="provider-status-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

/** 渲染可核对的六项调用计数，恒等式由后端验证，前端不自行修正异常数据。 */
function renderCallCounts(item) {
  const counts = [
    ["终态", item.terminal_call_count],
    ["资格", item.eligible_call_count],
    ["成功", item.success_count],
    ["Provider 失败", item.provider_failure_count],
    ["凭据失败", item.credential_failure_count],
    ["排除", item.excluded_call_count],
  ];
  return `<dl class="provider-status-counts" aria-label="真实调用计数">${counts.map(
    ([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(safeCount(value))}</dd></div>`,
  ).join("")}</dl>`;
}

/** 渲染完整探测组计数，成功、失败与不确定三项可与 total 直接核对。 */
function renderProbeCounts(item) {
  const counts = [
    ["探测总数", item.probe_total],
    ["探测成功", item.probe_success_count],
    ["探测失败", item.probe_failure_count],
    ["探测不确定", item.probe_inconclusive_count],
    ["探测延迟样本", item.probe_latency_sample_count],
  ];
  return `<dl class="provider-status-probe-counts" aria-label="探测计数">${counts.map(
    ([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(safeCount(value))}</dd></div>`,
  ).join("")}</dl>`;
}

/** 使用原生 details 展示最近故障，避免自定义 disclosure 产生额外键盘状态。 */
function renderFailures(failures) {
  return `<details class="provider-status-failures"><summary>最近故障（${escapeHtml(failures.length)}）</summary>
    <div class="provider-status-failure-list">${failures.length
      ? failures.map(renderFailure).join("")
      : `<div class="empty-state">最近 7 天无故障记录。</div>`}</div>
  </details>`;
}

/** 渲染一条脱敏故障事实；只显示固定分类、错误码和可追踪标识。 */
function renderFailure(failure = {}) {
  return `<div class="provider-status-failure">
    <div><strong>${escapeHtml(FAILURE_SOURCE_LABELS[failure.source] ?? failure.source ?? "-")}</strong><span>${escapeHtml(FAILURE_KIND_LABELS[failure.failure_kind] ?? failure.failure_kind ?? "-")} · ${failure.eligible_for_success_rate ? "计入成功率" : "不计入成功率"}</span></div>
    <div><strong>${escapeHtml(failure.error_code ?? "-")}</strong><span>${formatDate(failure.observed_at)} · ${formatMilliseconds(failure.latency_ms)}</span></div>
    <div class="break-anywhere"><span>request_id: ${escapeHtml(failure.request_id ?? "-")}</span><span>trace_id: ${escapeHtml(failure.trace_id ?? "-")}</span></div>
  </div>`;
}

/** 根据后端分页字段渲染上一页/下一页；按钮只提交受控 offset，不接收任意 URL 参数。 */
function renderPagination(state, visibleCount) {
  const limit = safePositiveInteger(state.limit, 50);
  const offset = safeNonNegativeInteger(state.offset, 0);
  const total = safeNonNegativeInteger(state.total, 0);
  if (total <= limit && offset === 0) return "";
  const previousOffset = Math.max(0, offset - limit);
  const nextOffset = offset + limit;
  return `<nav class="provider-status-pagination" aria-label="Provider 状态分页">
    <span>显示 ${escapeHtml(visibleCount)} / ${escapeHtml(total)}</span>
    <div>
      <button type="button" class="icon-button" data-provider-status-offset="${escapeAttribute(previousOffset)}" title="上一页" aria-label="上一页" ${offset === 0 ? "disabled" : ""}>←</button>
      <button type="button" class="icon-button" data-provider-status-offset="${escapeAttribute(nextOffset)}" title="下一页" aria-label="下一页" ${nextOffset >= total ? "disabled" : ""}>→</button>
    </div>
  </nav>`;
}

/** 从固定映射生成 select option，selected 只与规范字符串比较。 */
function optionsFor(labels, selected) {
  return Object.entries(labels).map(([value, label]) => (
    `<option value="${escapeAttribute(value)}" ${String(selected ?? "") === value ? "selected" : ""}>${escapeHtml(label)}</option>`
  )).join("");
}

/** 把万分比格式化为百分比；null 明确保留无样本语义。 */
function formatRate(value, emptyLabel) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 10_000) return emptyLabel;
  const percentage = value / 100;
  return `${Number.isInteger(percentage) ? percentage : percentage.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")}%`;
}

/** 格式化有界毫秒值；null 或非法值不得被伪造成 0ms。 */
function formatMilliseconds(value) {
  return Number.isSafeInteger(value) && value >= 0 ? `${value} ms` : "暂无样本";
}

/** 格式化服务端时间；空值或异常时间统一显示短横线，避免 Invalid Date 污染运维页面。 */
function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "-"
    : escapeHtml(date.toLocaleString("zh-CN", { hour12: false }));
}

/** 计数只接受 JavaScript 安全非负整数；异常响应失败关闭为短横线而不是猜测。 */
function safeCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : "-";
}

/** 分页 limit 只接受正安全整数，异常响应使用页面固定默认值。 */
function safePositiveInteger(value, fallback) {
  return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** 分页 offset/total 只接受非负安全整数，异常响应使用固定回退值。 */
function safeNonNegativeInteger(value, fallback) {
  return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

/** HTML 文本转义覆盖引号，维护备注和错误码只能作为文本进入 DOM。 */
function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;").replaceAll("`", "&#096;");
}

/** HTML 属性转义额外处理反引号，阻止 Provider 名称或 ID 突破属性边界。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
