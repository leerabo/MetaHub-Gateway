/**
 * PAGE-099 输出安全事件页。
 *
 * 页面复用 RequestLog 的 tenant owner、列表与详情 API，只展示扫描元数据和暴露计数。
 * Provider Key、Token、Prompt、Response、DLP pattern 与命中文本均不属于本模块输入合同。
 */

import {
  renderRequestLogDetail,
  requestLogDateTimeLocalValue,
} from "../request-logs/renderRequestLogPanel.js";
import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 扫描状态标签严格对应后端二维状态机，未知值由渲染函数按安全回退文案处理。
const STATUS_LABELS = Object.freeze({
  pending: "处理中",
  not_configured: "未配置",
  completed: "已完成",
  unavailable: "扫描不可用",
  not_applicable: "不适用",
  legacy_unavailable: "历史证据不可用",
});
// 扫描模式标签只解释冻结执行方式，不根据响应正文或 Provider 类型推断模式。
const MODE_LABELS = Object.freeze({
  non_stream_full: "非流式全文",
  stream_window: "流式滑窗",
});
// 暴露状态来自后端持久证据，前端不能用 DOM 内容或事件数量自行升级结论。
const EXPOSURE_LABELS = Object.freeze({
  none: "未暴露",
  partial: "部分已发送",
  complete: "完整已发送",
  unknown: "未知",
});
// 账单标签只展示人工复核意图，不在控制台自动生成冲正或修改正式分录。
const BILLING_LABELS = Object.freeze({
  not_required: "无需复核",
  dispute_pending: "待争议",
});

/**
 * 渲染输出安全事件工作区。
 *
 * 参数：同一 RequestLog owner 接纳的列表/详情状态和实时认证快照。返回值：页面 HTML。
 * 权限约束：缺少 request_log.read 时返回零 DOM；这只是前端裁剪，Control API 仍逐请求执行最终校验。
 */
export function renderOutputSafety(state, authContext, i18n = null) {
  if (!hasPermission(authContext, PERMISSIONS.REQUEST_LOG_READ)) return "";
  if (!state) return renderLoading();
  // 后端或 DTO 校验失败时只显示固定错误态，禁止继续渲染可能属于旧 owner 的列表详情。
  if (state.state === "error") return renderError(state);
  const logs = Array.isArray(state.request_logs) ? state.request_logs : [];
  const summary = summarize(logs);
  return `
    <div class="output-safety-heading">
      <div>
        <h2 id="output-safety-title">输出安全事件</h2>
        <p>当前租户被阻断或扫描不可用的模型输出事件。</p>
      </div>
      <span>${escapeHtml(logs.length)} / ${escapeHtml(state.total ?? 0)}</span>
    </div>
    ${renderSummary(summary)}
    ${renderFilters(state.filters ?? {})}
    <div class="output-safety-workspace">
      <div class="output-safety-list-pane">
        ${renderEventTable(logs)}
        ${renderPager(state)}
      </div>
      <aside class="output-safety-detail-pane" aria-label="输出安全日志详情">
        ${renderRequestLogDetail(state.detail, state.detailError, null, authContext, i18n)}
      </aside>
    </div>
  `;
}

/** 按当前页安全摘要计数；总量仍以后端 tenant-qualified total 为准。 */
function summarize(logs) {
  return logs.reduce((result, item) => {
    const scan = safeScan(item);
    if (scan.decision === "blocked") result.blocked += 1;
    if (scan.scan_status === "unavailable") result.unavailable += 1;
    if (scan.exposure_state === "partial") result.partial += 1;
    if (scan.billing_disposition === "dispute_pending") result.dispute += 1;
    return result;
  }, { blocked: 0, unavailable: 0, partial: 0, dispute: 0 });
}

/** 渲染紧凑统计带，数字只来自当前响应，不从错误码猜测安全状态。 */
function renderSummary(summary) {
  return `
    <div class="output-safety-summary" aria-label="当前页输出安全摘要">
      ${renderMetric("已阻断", summary.blocked)}
      ${renderMetric("扫描不可用", summary.unavailable)}
      ${renderMetric("部分暴露", summary.partial)}
      ${renderMetric("待账单复核", summary.dispute)}
    </div>
  `;
}

/** 渲染单个固定尺寸摘要指标，避免动态数字造成布局跳动。 */
function renderMetric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/**
 * 渲染 PAGE-099 筛选。
 * tenant_id 从不进入表单；隐藏 terminalStage 也不作为授权，主应用会强制覆盖为 output_safety。
 */
function renderFilters(filters) {
  return `
    <form id="output-safety-filter-form" class="output-safety-filter-form">
      <label><span>request_id</span><input name="requestId" value="${escapeAttribute(filters.requestId ?? "")}" placeholder="req_xxx" /></label>
      <label><span>状态</span><select name="outputSafetyStatus">${renderOptions([
        ["", "全部"], ["completed", "已完成"], ["unavailable", "扫描不可用"], ["pending", "处理中"],
      ], filters.outputSafetyStatus)}</select></label>
      <label><span>模式</span><select name="outputSafetyMode">${renderOptions([
        ["", "全部"], ["non_stream_full", "非流式全文"], ["stream_window", "流式滑窗"],
      ], filters.outputSafetyMode)}</select></label>
      <label><span>暴露</span><select name="outputSafetyExposure">${renderOptions([
        ["", "全部"], ["none", "未暴露"], ["partial", "部分已发送"], ["complete", "完整已发送"], ["unknown", "未知"],
      ], filters.outputSafetyExposure)}</select></label>
      <label><span>账单状态</span><select name="outputSafetyBilling">${renderOptions([
        ["", "全部"], ["not_required", "无需复核"], ["dispute_pending", "待争议"],
      ], filters.outputSafetyBilling)}</select></label>
      <label><span>开始时间</span><input type="datetime-local" name="createdFrom" value="${escapeAttribute(requestLogDateTimeLocalValue(filters.createdFrom))}" /></label>
      <label><span>结束时间</span><input type="datetime-local" name="createdTo" value="${escapeAttribute(requestLogDateTimeLocalValue(filters.createdTo))}" /></label>
      <div class="output-safety-filter-actions">
        <button type="submit">查询</button>
        <button type="button" id="output-safety-filter-reset">重置</button>
      </div>
    </form>
  `;
}

/** 只从固定选项构造 select，未知后端值不会生成新的可提交选项。 */
function renderOptions(options, selected) {
  return options.map(([value, label]) => (
    `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`
  )).join("");
}

/** 渲染安全事件列表；列表项只有严格 output_safety 摘要和 RequestLog 固有标识。 */
function renderEventTable(logs) {
  return `
    <div class="output-safety-table" role="table" aria-label="当前租户输出安全事件">
      <div class="output-safety-row output-safety-row-head" role="row">
        <span>时间</span><span>模式 / 状态</span><span>暴露</span><span>已输出</span><span>账单复核</span><span>request_id</span><span>操作</span>
      </div>
      ${logs.length ? logs.map(renderEventRow).join("") : '<div class="output-safety-empty">当前条件下没有输出安全事件。</div>'}
    </div>
  `;
}

/** 渲染单条事件；缺失摘要时明确显示证据不可用，不把它降级为 clean。 */
function renderEventRow(item) {
  const scan = safeScan(item);
  const emitted = `${nonnegative(scan.emitted_token_count)} Token / ${nonnegative(scan.emitted_sse_event_count)} SSE`;
  return `
    <div class="output-safety-row" role="row">
      <time data-label="时间" datetime="${escapeAttribute(item.created_at ?? "")}">${escapeHtml(formatDateTime(item.created_at))}</time>
      <span data-label="模式 / 状态"><strong>${escapeHtml(MODE_LABELS[scan.scan_mode] ?? scan.scan_mode ?? "证据不可用")}</strong><em>${escapeHtml(STATUS_LABELS[scan.scan_status] ?? scan.scan_status ?? "证据不可用")}</em></span>
      <span data-label="暴露">${escapeHtml(EXPOSURE_LABELS[scan.exposure_state] ?? scan.exposure_state ?? "未知")}</span>
      <span data-label="已输出">${escapeHtml(emitted)}</span>
      <span data-label="账单复核">${escapeHtml(BILLING_LABELS[scan.billing_disposition] ?? scan.billing_disposition ?? "未知")}</span>
      <code data-label="request_id">${escapeHtml(item.request_id ?? "-")}</code>
      <span data-label="操作"><button type="button" data-request-log-detail="${escapeAttribute(item.request_id ?? "")}">查看详情</button></span>
    </div>
  `;
}

/** 输出安全列表摘要缺失时返回空对象，任何未知字段都不会自动穿透到模板。 */
function safeScan(item) {
  const value = item?.output_safety;
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

/** 分页沿用 RequestLog 的 offset 合同与按钮 ID，由同一 owner 事件处理器执行。 */
function renderPager(state) {
  const limit = nonnegative(state.limit) || 20;
  const offset = nonnegative(state.offset);
  const total = nonnegative(state.total);
  const canNext = state.has_more === true || offset + limit < total;
  return `
    <div class="request-log-pager">
      <button type="button" id="request-log-prev" ${offset > 0 ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(total ? offset + 1 : 0)}-${escapeHtml(Math.min(offset + limit, total))} / ${escapeHtml(total)}</span>
      <button type="button" id="request-log-next" ${canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/** 加载态不创建列表或详情按钮，避免接口返回前产生无 owner 交互。 */
function renderLoading() {
  return '<div class="output-safety-heading"><div><h2 id="output-safety-title">输出安全事件</h2><p>正在读取当前租户安全事件。</p></div></div><div class="empty-state">等待 /request-logs 响应。</div>';
}

/** 错误态只展示统一消息与 request_id，原始响应正文和异常栈不会进入 DOM。 */
function renderError(state) {
  return `<div class="output-safety-heading"><div><h2 id="output-safety-title">输出安全事件</h2><p>安全事件暂不可用。</p></div></div><div class="empty-state error-state">${escapeHtml(state.message ?? "输出安全事件读取失败")} request_id: ${escapeHtml(state.requestId ?? "-")}</div>`;
}

/** 把不可信数值收敛为非负安全整数，异常值显示为零而不影响布局。 */
function nonnegative(value) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0;
}

/** 时间仅用于展示；解析失败时保留固定占位，不输出浏览器异常文本。 */
function formatDateTime(value) {
  const date = new Date(value ?? "");
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

/** 统一转义文本节点，防止后端标识或未来枚举形成 HTML。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值在文本转义基础上额外处理反引号，禁止退出 data/datetime/value 属性。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
