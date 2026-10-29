/**
 * 模块职责：把 PRD-053 成本异常安全 DTO 渲染为 PAGE-077 列表/详情和 PAGE-078 告警卡片。
 * 安全边界：只逐字段读取冻结合同，所有名称、ID 和错误文本均转义；未知响应字段、cursor、
 * 幂等键、Token、Provider Key、Prompt、Response 和原始异常正文永远不会进入 DOM。
 */

import { formatCostMicros, formatCostRatioMillis } from "../../lib/costAnomalies.js";
import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 标签映射既保证中文可读性，也避免把任意枚举文本直接拼入 class 或按钮命令。
const RULE_LABELS = Object.freeze({
  tenant_spend_spike: "组织成本突增",
  virtual_key_spend_spike: "KEY 成本突增",
  virtual_key_off_hours: "非工作时段消费",
});
const STATUS_LABELS = Object.freeze({
  open: "待处理",
  acknowledged: "已确认",
  dismissed: "已忽略",
  mitigated: "已临时限流",
});
// 严重级别标签只接受后端冻结枚举，不把未知文本用作 CSS token。
const SEVERITY_LABELS = Object.freeze({ warning: "警告", critical: "严重" });
// 治理动作标签与 PATCH 白名单一致，按钮文本本身不承担权限判断。
const ACTION_LABELS = Object.freeze({
  acknowledge: "确认告警",
  dismiss: "忽略告警",
  apply_temporary_rate_limit: "应用临时限流",
  release_temporary_rate_limit: "解除临时限流",
});
// 忽略原因使用有限枚举，避免把管理员自由文本写入审计或页面历史。
const REASON_LABELS = Object.freeze({
  false_positive: "误报",
  expected_activity: "预期活动",
  resolved_without_action: "已自行恢复",
  other: "其他",
});
// 检测器状态只表达可用性，不把降级误展示为“没有异常”。
const DETECTOR_LABELS = Object.freeze({ healthy: "检测正常", degraded: "检测降级", idle: "等待检测" });
// 事件派发标签只展示持久状态，不暴露 Endpoint、Secret 或失败正文。
const EVENT_LABELS = Object.freeze({
  pending: "待派发",
  processing: "派发中",
  dispatched: "已持久化投递",
  failed: "派发失败",
});

/**
 * 渲染成本异常完整工作区。
 *
 * @param {object|null} state 页面安全快照或错误状态。
 * @param {object|null} authContext 当前认证上下文，仅用于 admin DOM 门禁。
 * @returns {string} 已完成转义的 HTML；非 admin 返回空字符串且不暴露页面结构。
 */
export function renderCostAnomalies(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.BILLING_ANOMALY_READ)) {
    return "";
  }
  const canWrite = hasPermission(authContext, PERMISSIONS.BILLING_ANOMALY_WRITE);
  if (!state || state.state === "loading") {
    return renderLoading();
  }
  const filters = state.filters ?? {};
  // 读取失败保留筛选和公开 request_id，但绝不回显原始响应或上一次租户列表。
  if (state.state === "error") {
    return `
      ${renderHeading()}
      ${renderFilters(filters)}
      <div class="cost-anomaly-error" role="alert">
        <strong>${escapeHtml(state.code ?? "cost_anomaly.unavailable")}</strong>
        <span>${escapeHtml(state.message ?? "成本异常读取失败")}</span>
      </div>
    `;
  }

  const anomalies = Array.isArray(state.cost_anomalies) ? state.cost_anomalies : [];
  return `
    ${renderHeading(state)}
    ${renderSummary(state.summary)}
    ${renderFeedback(state.feedback)}
    ${renderActionError(state.actionError)}
    ${renderAlertCards(anomalies)}
    <section class="cost-anomaly-page" aria-labelledby="cost-anomaly-list-title">
      <div class="cost-anomaly-section-heading">
        <div><h3 id="cost-anomaly-list-title">成本异常记录</h3></div>
      </div>
      ${renderFilters(filters)}
      <div class="cost-anomaly-workspace">
        ${renderAnomalyList(anomalies, state.selectedAnomaly?.cost_anomaly_id)}
        ${renderDetail(state.selectedAnomaly, canWrite)}
      </div>
      ${renderPager(state, anomalies.length)}
    </section>
    ${renderActionDialog(state.actionDialog, canWrite)}
  `;
}

/** 渲染加载占位，保持页头和主工作区高度稳定。 */
function renderLoading() {
  return `
    ${renderHeading()}
    <div class="cost-anomaly-loading" role="status" aria-live="polite">
      <span class="status-dot state-checking"></span>
      <span>正在加载成本异常</span>
    </div>
  `;
}

/** 渲染 PAGE-077 主标题；数量只接受安全整数，不从列表长度推导全量总数。 */
function renderHeading(state = {}) {
  return `
    <header class="cost-anomaly-heading">
      <div>
        <h2 id="cost-anomaly-title">成本异常检测</h2>
      </div>
      <div class="cost-anomaly-heading-count"><strong>${safeCount(state.total)}</strong><span>条异常</span></div>
    </header>
  `;
}

/** 渲染同一 GET 快照内的检测器和处置汇总，不在前端重算后端全量统计。 */
function renderSummary(summary = {}) {
  const detectorStatus = knownToken(summary.detector_status, DETECTOR_LABELS, "idle");
  return `
    <section class="cost-anomaly-summary" aria-label="成本异常汇总">
      ${renderSummaryMetric("待处理", safeCount(summary.open_count))}
      ${renderSummaryMetric("严重", safeCount(summary.critical_count), "critical")}
      ${renderSummaryMetric("已限流", safeCount(summary.mitigated_count))}
      ${renderSummaryMetric("生效限流", safeCount(summary.active_override_count))}
      <div class="cost-anomaly-detector detector-${detectorStatus}">
        <span>${escapeHtml(DETECTOR_LABELS[detectorStatus])}</span>
        <strong>${escapeHtml(formatDateTime(summary.latest_window_end))}</strong>
        ${summary.detector_error_code
          ? `<code>${escapeHtml(summary.detector_error_code)}</code>`
          : ""}
      </div>
    </section>
  `;
}

/** 渲染一个汇总指标，critical 使用独立高对比色而非整页单色主题。 */
function renderSummaryMetric(label, value, tone = "neutral") {
  return `<div class="cost-anomaly-summary-metric tone-${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/**
 * 渲染 PAGE-078 重复告警卡片。
 * 只展示当前快照内最多三条未终结事实，避免在首屏复制完整详情和动作历史。
 */
function renderAlertCards(anomalies) {
  const alerts = anomalies
    .filter((item) => item?.status === "open" || item?.status === "acknowledged")
    .slice(0, 3);
  return `
    <section class="cost-anomaly-alerts" aria-labelledby="cost-anomaly-alerts-title">
      <div class="cost-anomaly-section-heading">
        <div><h3 id="cost-anomaly-alerts-title">待处理告警</h3></div>
      </div>
      <div class="cost-anomaly-alert-grid">
        ${alerts.length
          ? alerts.map(renderAlertCard).join("")
          : `<div class="cost-anomaly-alert-empty">当前筛选下没有待处理告警。</div>`}
      </div>
    </section>
  `;
}

/** 渲染一张告警摘要卡；命令只携带当前页不透明 anomaly ID。 */
function renderAlertCard(anomaly) {
  const severity = knownToken(anomaly?.severity, SEVERITY_LABELS, "warning");
  const ruleType = knownToken(anomaly?.rule_type, RULE_LABELS, "tenant_spend_spike");
  return `
    <article class="cost-anomaly-alert-card severity-${severity}">
      <div class="cost-anomaly-alert-meta">
        <mark>${escapeHtml(SEVERITY_LABELS[severity])}</mark>
        <time datetime="${escapeAttribute(validDateTimeAttribute(anomaly?.window_end))}">${escapeHtml(formatDateTime(anomaly?.window_end))}</time>
      </div>
      <h4>${escapeHtml(RULE_LABELS[ruleType])}</h4>
      <p>${escapeHtml(displayScope(anomaly))}</p>
      <div class="cost-anomaly-alert-cost"><strong>${escapeHtml(formatCostMicros(anomaly?.current_cost_micros))}</strong><span>CNY / 5 分钟</span></div>
      <button type="button" data-cost-anomaly-select="${escapeAttribute(anomaly?.cost_anomaly_id ?? "")}">查看证据</button>
    </article>
  `;
}

/** 渲染固定筛选表单；时间使用 datetime-local，提交时由 binder 转成 ISO 字符串。 */
function renderFilters(filters = {}) {
  return `
    <form class="cost-anomaly-filter-form" id="cost-anomaly-filter-form">
      <label><span>状态</span><select name="status">${renderOptions(STATUS_LABELS, filters.status)}</select></label>
      <label><span>规则</span><select name="ruleType">${renderOptions(RULE_LABELS, filters.ruleType)}</select></label>
      <label><span>级别</span><select name="severity">${renderOptions(SEVERITY_LABELS, filters.severity)}</select></label>
      <label><span>环境</span><input name="environmentId" value="${escapeAttribute(filters.environmentId ?? "")}" placeholder="全部环境" /></label>
      <label><span>KEY</span><input name="virtualKeyId" value="${escapeAttribute(filters.virtualKeyId ?? "")}" placeholder="全部 KEY" /></label>
      <label><span>开始时间</span><input type="datetime-local" name="fromTime" value="${escapeAttribute(toDateTimeLocal(filters.fromTime))}" /></label>
      <label><span>结束时间</span><input type="datetime-local" name="toTime" value="${escapeAttribute(toDateTimeLocal(filters.toTime))}" /></label>
      <div class="cost-anomaly-filter-actions">
        <button type="submit">筛选</button>
        <button type="button" id="cost-anomaly-filter-reset">清空</button>
      </div>
    </form>
  `;
}

/** 渲染当前页异常列表；每条卡片字段固定且不枚举原始响应对象。 */
function renderAnomalyList(anomalies, selectedId) {
  return `
    <div class="cost-anomaly-list" aria-label="成本异常列表">
      ${anomalies.length
        ? anomalies.map((item) => renderAnomalyListItem(item, selectedId)).join("")
        : `<div class="cost-anomaly-empty">当前筛选下没有成本异常。</div>`}
    </div>
  `;
}

/** 渲染一条可扫描列表项；金额只使用 BigInt formatter，绝不以 Number 做比率或金额计算。 */
function renderAnomalyListItem(anomaly, selectedId) {
  const id = anomaly?.cost_anomaly_id ?? "";
  const severity = knownToken(anomaly?.severity, SEVERITY_LABELS, "warning");
  const status = knownToken(anomaly?.status, STATUS_LABELS, "open");
  const ruleType = knownToken(anomaly?.rule_type, RULE_LABELS, "tenant_spend_spike");
  const selected = id && id === selectedId;
  return `
    <article class="cost-anomaly-list-item${selected ? " is-selected" : ""}">
      <div class="cost-anomaly-list-topline">
        <mark class="severity-${severity}">${escapeHtml(SEVERITY_LABELS[severity])}</mark>
        <span class="status-${status}">${escapeHtml(STATUS_LABELS[status])}</span>
        <time datetime="${escapeAttribute(validDateTimeAttribute(anomaly?.window_end))}">${escapeHtml(formatDateTime(anomaly?.window_end))}</time>
      </div>
      <h4>${escapeHtml(RULE_LABELS[ruleType])}</h4>
      <p>${escapeHtml(displayScope(anomaly))}</p>
      <div class="cost-anomaly-list-evidence">
        <span><small>当前成本</small><strong>${escapeHtml(formatCostMicros(anomaly?.current_cost_micros))} CNY</strong></span>
        <span><small>观察倍率</small><strong>${escapeHtml(formatCostRatioMillis(anomaly?.observed_ratio_millis))}</strong></span>
      </div>
      <button type="button" data-cost-anomaly-select="${escapeAttribute(id)}" aria-pressed="${selected ? "true" : "false"}">详情</button>
    </article>
  `;
}

/** 渲染 PAGE-077 详情和不可变证据；没有选择时保持稳定占位。 */
function renderDetail(anomaly, canWrite) {
  if (!anomaly) {
    return `
      <aside class="cost-anomaly-detail is-empty" aria-label="异常详情">
        <strong>异常详情</strong><p>选择一条记录查看检测证据和治理历史。</p>
      </aside>
    `;
  }
  const ruleType = knownToken(anomaly.rule_type, RULE_LABELS, "tenant_spend_spike");
  const severity = knownToken(anomaly.severity, SEVERITY_LABELS, "warning");
  const status = knownToken(anomaly.status, STATUS_LABELS, "open");
  return `
    <aside class="cost-anomaly-detail" aria-labelledby="cost-anomaly-detail-title">
      <div class="cost-anomaly-detail-heading">
        <div><span>${escapeHtml(SEVERITY_LABELS[severity])} · ${escapeHtml(STATUS_LABELS[status])}</span><h3 id="cost-anomaly-detail-title">${escapeHtml(RULE_LABELS[ruleType])}</h3></div>
        <button type="button" id="cost-anomaly-close-detail" class="cost-anomaly-icon-button" aria-label="关闭详情" title="关闭详情">×</button>
      </div>
      ${renderInputQuality(anomaly)}
      <dl class="cost-anomaly-detail-list">
        ${renderPair("环境", safeText(anomaly.environment_name))}
        ${renderPair("作用范围", displayScope(anomaly))}
        ${renderPair("个人 KEY", displayVirtualKey(anomaly))}
        ${renderPair("项目 / 应用", `${safeText(anomaly.project_name)} / ${safeText(anomaly.application_name)}`)}
        ${renderPair("检测窗口", `${formatDateTime(anomaly.window_start)} - ${formatDateTime(anomaly.window_end)}`)}
        ${renderPair("基线窗口", `${formatDateTime(anomaly.baseline_start)} - ${formatDateTime(anomaly.baseline_end)}`)}
        ${renderPair("当前成本", `${formatCostMicros(anomaly.current_cost_micros)} CNY`)}
        ${renderPair("24 小时基线总额", `${formatCostMicros(anomaly.baseline_total_micros)} CNY`)}
        ${renderPair("基线 5 分钟均值", `${formatCostMicros(anomaly.baseline_average_micros)} CNY`)}
        ${renderPair("观察 / 阈值", `${formatCostRatioMillis(anomaly.observed_ratio_millis)} / ${formatCostRatioMillis(anomaly.threshold_multiplier_millis)}`)}
        ${renderPair("最小触发金额", `${formatCostMicros(anomaly.minimum_cost_micros)} CNY`)}
        ${renderPair("来源分录", `${safeCount(anomaly.source_entry_count)} 条 / ${safeCount(anomaly.baseline_bucket_count)} 个基线桶`)}
        ${renderPair("影响 Key 数", safeCount(anomaly.affected_virtual_key_count))}
        ${renderPair("建议动作", safeRecommendedAction(anomaly.recommended_action))}
        ${renderPair("事件状态", EVENT_LABELS[knownToken(anomaly.event_dispatch_status, EVENT_LABELS, "pending")])}
        ${renderPair("检测时间", formatDateTime(anomaly.detected_at))}
        ${renderPair("处置截止", formatDateTime(anomaly.mitigation_eligible_until))}
      </dl>
      ${renderMitigation(anomaly.temporary_rate_limit_override)}
      ${canWrite ? renderActionCommands(anomaly) : ""}
      ${renderActionHistory(anomaly.actions)}
    </aside>
  `;
}

/** 渲染输入质量证据，明确估算和 legacy 行不能伪装成最终厂商结算价。 */
function renderInputQuality(anomaly) {
  const estimated = safeCount(anomaly.estimated_entry_count);
  const legacy = safeCount(anomaly.legacy_entry_count);
  const tone = estimated === "0" && legacy === "0" ? "clean" : "review";
  return `
    <div class="cost-anomaly-input-quality quality-${tone}">
      <strong>输入质量</strong>
      <span>估算分录 ${escapeHtml(estimated)}</span>
      <span>历史金额证据 ${escapeHtml(legacy)}</span>
    </div>
  `;
}

/** 渲染当前临时限流的有效状态和各维度，不推导或覆盖后端有效值。 */
function renderMitigation(override) {
  if (!override) {
    return `<section class="cost-anomaly-mitigation is-empty"><h4>临时限流</h4><span>未应用</span></section>`;
  }
  const effective = ["active", "released", "expired"].includes(override.effective_status)
    ? override.effective_status
    : "expired";
  const labels = { active: "生效中", released: "已解除", expired: "已到期" };
  return `
    <section class="cost-anomaly-mitigation mitigation-${effective}">
      <div><h4>临时限流</h4><strong>${escapeHtml(labels[effective])}</strong></div>
      <dl>
        ${renderPair("RPM", safeNullableLimit(override.rpm_limit))}
        ${renderPair("TPM", safeNullableLimit(override.tpm_limit))}
        ${renderPair("并发", safeNullableLimit(override.concurrency_limit))}
        ${renderPair("开始", formatDateTime(override.starts_at))}
        ${renderPair("到期", formatDateTime(override.expires_at))}
      </dl>
    </section>
  `;
}

/** 根据后端当前事实渲染可尝试命令；最终状态迁移和资源归属仍由 PATCH 后端校验。 */
function renderActionCommands(anomaly) {
  const status = anomaly?.status;
  const activeOverride = anomaly?.temporary_rate_limit_override?.effective_status === "active";
  const commands = [];
  if (status === "open") {
    commands.push(renderActionButton(anomaly.cost_anomaly_id, "acknowledge", false));
  }
  if (status === "open" || status === "acknowledged") {
    commands.push(renderActionButton(anomaly.cost_anomaly_id, "dismiss", false));
  }
  if (
    anomaly?.scope_type === "virtual_key"
    && anomaly?.mitigation_eligible === true
    && !activeOverride
    && (status === "open" || status === "acknowledged")
  ) {
    commands.push(renderActionButton(anomaly.cost_anomaly_id, "apply_temporary_rate_limit", true));
  }
  if (activeOverride) {
    commands.push(renderActionButton(anomaly.cost_anomaly_id, "release_temporary_rate_limit", false));
  }
  return `<div class="cost-anomaly-actions">${commands.join("") || `<span>当前状态无可用动作</span>`}</div>`;
}

/** 渲染一个固定动作按钮，primary 只用于推荐的临时限流命令。 */
function renderActionButton(anomalyId, action, primary) {
  return `<button type="button" class="${primary ? "is-primary" : ""}" data-cost-anomaly-action="${escapeAttribute(action)}" data-cost-anomaly-id="${escapeAttribute(anomalyId ?? "")}">${escapeHtml(ACTION_LABELS[action])}</button>`;
}

/** 渲染 append-only 动作历史，只读取公开 DTO 白名单字段。 */
function renderActionHistory(actions) {
  const safeActions = Array.isArray(actions) ? actions : [];
  return `
    <section class="cost-anomaly-history" aria-labelledby="cost-anomaly-history-title">
      <h4 id="cost-anomaly-history-title">治理历史</h4>
      ${safeActions.length
        ? `<ol>${safeActions.map(renderHistoryItem).join("")}</ol>`
        : `<p>暂无治理动作。</p>`}
    </section>
  `;
}

/** 渲染一条治理动作，原因和有效限制均使用固定字段与枚举标签。 */
function renderHistoryItem(action) {
  const actionType = knownToken(action?.action_type, ACTION_LABELS, "acknowledge");
  const before = knownToken(action?.status_before, STATUS_LABELS, "open");
  const after = knownToken(action?.status_after, STATUS_LABELS, "open");
  const reason = action?.dismiss_reason && REASON_LABELS[action.dismiss_reason]
    ? ` · ${REASON_LABELS[action.dismiss_reason]}`
    : "";
  return `
    <li>
      <div><strong>${escapeHtml(ACTION_LABELS[actionType])}</strong><time>${escapeHtml(formatDateTime(action?.created_at))}</time></div>
      <p>${escapeHtml(STATUS_LABELS[before])} → ${escapeHtml(STATUS_LABELS[after])}${escapeHtml(reason)}</p>
      <span>操作者 ${escapeHtml(action?.actor_display_name ?? "组织管理员")}</span>
    </li>
  `;
}

/** 渲染动作确认表单；幂等键和 operation IDs 不创建任何 DOM 节点。 */
function renderActionDialog(dialog, canWrite) {
  if (!canWrite || !dialog?.open) {
    return "";
  }
  const action = knownToken(dialog.action, ACTION_LABELS, "acknowledge");
  return `
    <dialog class="cost-anomaly-action-dialog" id="cost-anomaly-action-dialog" open aria-labelledby="cost-anomaly-action-title">
      <form id="cost-anomaly-action-form">
        <div class="cost-anomaly-dialog-heading">
          <div><span>治理动作</span><h3 id="cost-anomaly-action-title">${escapeHtml(ACTION_LABELS[action])}</h3></div>
          <button type="button" id="cost-anomaly-action-close" class="cost-anomaly-icon-button" aria-label="关闭动作" title="关闭动作" ${dialog.submitting ? "disabled" : ""}>×</button>
        </div>
        ${dialog.error ? renderDialogError(dialog.error) : ""}
        ${renderActionFields(action, dialog)}
        <div class="cost-anomaly-dialog-actions">
          <button type="button" id="cost-anomaly-action-cancel" ${dialog.submitting ? "disabled" : ""}>取消</button>
          <button type="submit" ${dialog.submitting ? "disabled aria-busy=\"true\"" : ""}>${dialog.submitting ? "正在提交" : "确认"}</button>
        </div>
      </form>
    </dialog>
  `;
}

/** 按动作渲染精确字段集合，避免 acknowledge/release 产生无关输入。 */
function renderActionFields(action, dialog) {
  if (action === "dismiss") {
    return `
      <label><span>忽略原因</span><select name="dismissReason" required>${renderOptions(REASON_LABELS, dialog.dismissReason, false)}</select></label>
    `;
  }
  if (action === "apply_temporary_rate_limit") {
    return `
      <div class="cost-anomaly-limit-grid">
        <label><span>时长（分钟）</span><input type="number" name="durationMinutes" min="5" max="1440" step="1" value="${escapeAttribute(dialog.durationMinutes ?? "30")}" required /></label>
        <label><span>RPM</span><input type="number" name="rpmLimit" min="1" max="100000000" step="1" value="${escapeAttribute(dialog.rpmLimit ?? "")}" /></label>
        <label><span>TPM</span><input type="number" name="tpmLimit" min="1" max="100000000" step="1" value="${escapeAttribute(dialog.tpmLimit ?? "")}" /></label>
        <label><span>并发</span><input type="number" name="concurrencyLimit" min="1" max="100000000" step="1" value="${escapeAttribute(dialog.concurrencyLimit ?? "")}" /></label>
      </div>
    `;
  }
  const copy = action === "release_temporary_rate_limit"
    ? "确认解除当前异常创建的临时限流。"
    : "确认已查看并接手该成本异常。";
  return `<p class="cost-anomaly-confirmation">${escapeHtml(copy)}</p>`;
}

/** 渲染公开动作错误，不反射请求正文、限制草稿或原始服务端响应。 */
function renderDialogError(error) {
  return `
    <div class="cost-anomaly-dialog-error" role="alert">
      <strong>成本异常动作未完成</strong>
      <span>${escapeHtml(error.message ?? "请稍后重试")}</span>
    </div>
  `;
}

/** 渲染 cursor 分页控件；opaque cursor 本身只存在页面闭包内存。 */
function renderPager(state, pageCount) {
  return `
    <div class="cost-anomaly-pager">
      <button type="button" id="cost-anomaly-prev" ${state.canPrevious ? "" : "disabled"}>上一页</button>
      <span>${escapeHtml(safeCount(pageCount))} 条 / 当前快照共 ${escapeHtml(safeCount(state.total))} 条</span>
      <button type="button" id="cost-anomaly-next" ${state.canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/** 渲染动作成功反馈，不展示幂等键原文。 */
function renderFeedback(feedback) {
  if (!feedback) {
    return "";
  }
  return `<div class="cost-anomaly-feedback" role="status">${escapeHtml(feedback.message ?? "治理动作已提交")}</div>`;
}

/** 渲染列表级安全错误，如 DOM 注入 ID 或旧选择失效。 */
function renderActionError(error) {
  if (!error) {
    return "";
  }
  return `<div class="cost-anomaly-action-error" role="alert">${escapeHtml(error.message ?? "成本异常操作失败，请稍后重试")}</div>`;
}

/** 渲染非空详情键值对；空值不创建 DOM，避免把未授权字段误解为已授权空值。 */
function renderPair(label, value) {
  if (value === null || value === undefined || value === "" || value === "- / -") {
    return "";
  }
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

/** 渲染固定枚举 options；筛选表单可包含“全部”，必填动作表单不包含空选项。 */
function renderOptions(labels, selectedValue, includeAll = true) {
  const values = includeAll ? [["", "全部"], ...Object.entries(labels)] : Object.entries(labels);
  return values.map(([value, label]) => {
    const selected = value === (selectedValue ?? "") ? " selected" : "";
    return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
  }).join("");
}

/** 从固定枚举表返回可用于 class 的 token；未知值收敛为指定安全默认值。 */
function knownToken(value, labels, fallback) {
  return typeof value === "string" && Object.hasOwn(labels, value) ? value : fallback;
}

/** 组合可展示作用域，名称和 ID 均会在最终 HTML 写入时统一转义。 */
function displayScope(anomaly) {
  const label = safeText(anomaly?.scope_label);
  return label;
}

/** 组合 Key 名称、前缀和不透明 ID；不接受或展示 Virtual Key 明文。 */
function displayVirtualKey(anomaly) {
  const name = safeText(anomaly?.virtual_key_name);
  const prefix = safeText(anomaly?.virtual_key_prefix);
  return [name, prefix].filter((part) => part !== "-").join(" / ") || "-";
}

/** 建议动作只接受固定公开命令，未知服务端文本不会原样进入页面。 */
function safeRecommendedAction(value) {
  const labels = {
    review_impacted_keys: "复核受影响 Key",
    apply_temporary_rate_limit: "应用临时限流",
  };
  return labels[value] ?? "人工复核";
}

/** 把可空限制值收敛为安全整数或“无限制”，不对限制做任何前端合并。 */
function safeNullableLimit(value) {
  return Number.isSafeInteger(value) && value > 0 ? String(value) : "无限制";
}

/** 数量只接受非负安全整数，避免任意对象的 toString 或超范围数字进入 DOM。 */
function safeCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : "0";
}

/** 可展示文本仅接受字符串、有限数字和布尔值，其他对象统一为占位。 */
function safeText(value) {
  if (typeof value === "string" && value) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return "-";
}

/** 把 ISO 时间格式化为中国用户习惯；非法值不回显原始文本。 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

/** datetime 属性只接受可解析时间，非法输入返回空值。 */
function validDateTimeAttribute(value) {
  const date = new Date(value ?? "");
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/** 把已保存 ISO 时间投影为 datetime-local；筛选值仍会在提交时重新转成 ISO。 */
function toDateTimeLocal(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

/** 对动态文本执行完整 HTML 转义，阻止名称、ID 或错误消息闭合节点。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值沿用 HTML 转义并额外处理反引号，保护 data-*、value 和 datetime。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
