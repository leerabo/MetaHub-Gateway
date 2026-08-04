/**
 * 模块职责：把 PRD-070 安全 DTO 渲染为 PAGE-098 高危会话列表、详情、overlay 与处置 Dialog。
 * 安全边界：renderer 只逐字段读取 strict client 投影；所有动态文本均转义，Prompt、Response、
 * Session Header、完整 HMAC、cursor、幂等键、Token 和授权指纹永远不会进入 DOM。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import {
  availableSessionRiskActions,
  sessionRiskReasonCodes,
} from "../../lib/sessionRisks.js";
import { sessionRiskPageOwner } from "./sessionRiskPageState.js";

// 固定中文标签避免把未知服务端枚举拼入 class、按钮命令或可执行属性。
const STATUS_LABELS = Object.freeze({
  high_risk: "高危待复核",
  frozen: "已冻结",
  resolved: "已处置",
  expired: "已过期",
});
// scope 与运行状态标签分别来自独立枚举，禁止未知 token 被拼入展示文本。
const SCOPE_LABELS = Object.freeze({
  enterprise_chat: "企业 Chat",
  api_session: "API 会话",
});
const RUNTIME_LABELS = Object.freeze({
  active: "运行正常",
  degraded: "降级运行",
  unavailable: "依赖不可用",
  disabled: "当前未启用",
});
// 时间线动作使用固定命令标签；按钮是否可见由状态机和实时后端权限共同决定。
const ACTION_LABELS = Object.freeze({
  high_risk: "进入高危",
  freeze_session: "冻结会话",
  freeze_virtual_key: "冻结 Virtual Key",
  dismiss: "标记误报",
  release_session: "解除会话冻结",
  release_virtual_key: "解除 Key 冻结",
  expire: "窗口过期",
});
// 原因码只显示产品文案，不渲染后端 message 或管理员自由输入。
const REASON_LABELS = Object.freeze({
  threshold_reached: "达到风险阈值",
  confirmed_attack: "确认攻击",
  false_positive: "误报",
  verified_safe: "已核实安全",
  incident_contained: "事件已控制",
  window_expired: "观察窗口过期",
});
// overlay target/status 使用独立固定标签，避免将内部 scope 或 Key 摘要暴露到 DOM。
const TARGET_LABELS = Object.freeze({ session: "会话", virtual_key: "Virtual Key" });
const FREEZE_STATUS_LABELS = Object.freeze({ active: "生效中", released: "已解除" });

/**
 * 渲染 PAGE-098 完整工作区。
 *
 * @param {object|null} state 页面安全快照。
 * @param {object|null} authContext 当前实时认证与权限快照。
 * @param {object|null} consoleShellState 后端授权导航快照。
 * @returns {string} 已转义 HTML；缺少 read 权限或导航 owner 时返回空字符串。
 */
export function renderSessionRisks(state, authContext, consoleShellState) {
  const owner = sessionRiskPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const canManage = owner.canManage === true && state?.canManage === true;
  const filters = state?.filters ?? {};
  if (!state || state.state === "loading") {
    return `
      ${renderHeading("unavailable")}
      <div class="session-risk-loading" role="status" aria-live="polite">
        <span class="status-dot state-checking"></span><span>正在加载高危会话</span>
      </div>
    `;
  }
  // 错误页只呈现状态机清洗后的固定 code/message/request_id，不复用失败响应正文。
  if (state.state === "error") {
    return `
      ${renderHeading("unavailable")}
      ${renderFilters(filters)}
      ${renderError(state.loadError, "高危会话读取失败")}
    `;
  }
  const risks = Array.isArray(state.session_risks) ? state.session_risks : [];
  return `
    ${renderHeading(state.runtime_status)}
    ${renderSummary(state.summary, state.runtime_status)}
    ${renderFeedback(state.feedback)}
    ${state.pageError ? renderError(state.pageError, "高危会话操作失败") : ""}
    <section class="session-risk-page" aria-labelledby="session-risk-list-title">
      <div class="session-risk-section-heading">
        <h3 id="session-risk-list-title">会话风险记录</h3>
        <code>request_id: ${escapeHtml(state.request_id ?? "-")}</code>
      </div>
      ${renderFilters(filters)}
      <div class="session-risk-workspace">
        ${renderRiskList(risks, state.selectedRisk?.session_risk_score_id)}
        ${renderDetail(
          state.selectedRisk,
          canManage,
          hasPermission(authContext, PERMISSIONS.REQUEST_LOG_READ),
        )}
      </div>
      ${renderPager(state, risks.length)}
    </section>
    ${renderActionDialog(state.actionDialog, state.selectedRisk, canManage)}
  `;
}

/** 渲染紧凑页头与运行状态，不把 disabled 解释为无历史风险。 */
function renderHeading(runtimeStatus) {
  const runtime = knownToken(runtimeStatus, RUNTIME_LABELS, "unavailable");
  return `
    <header class="session-risk-heading">
      <div><h2 id="session-risk-title">高危会话</h2><span>PAGE-098</span></div>
      <mark class="session-risk-runtime runtime-${runtime}">${escapeHtml(RUNTIME_LABELS[runtime])}</mark>
    </header>
  `;
}

/** 渲染服务端同快照汇总；计数不从当前分页列表推导。 */
function renderSummary(summary = {}, runtimeStatus) {
  const runtime = knownToken(runtimeStatus, RUNTIME_LABELS, "unavailable");
  return `
    <section class="session-risk-summary" aria-label="会话风险汇总">
      ${renderSummaryMetric("待复核", safeCount(summary.high_risk_count), "danger")}
      ${renderSummaryMetric("高危冻结", safeCount(summary.frozen_count), "frozen")}
      ${renderSummaryMetric("已处置", safeCount(summary.resolved_count), "resolved")}
      ${renderSummaryMetric("运行状态", RUNTIME_LABELS[runtime], `runtime-${runtime}`)}
    </section>
  `;
}

/** 渲染单个汇总指标；tone 只来自调用方固定枚举。 */
function renderSummaryMetric(label, value, tone) {
  return `<div class="session-risk-summary-item tone-${escapeAttribute(tone)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/** 渲染冻结筛选栏；表单不包含 tenant、actor、排序字段、Prompt 开关或 cursor。 */
function renderFilters(filters) {
  return `
    <form class="session-risk-filters" id="session-risk-filter-form">
      <label><span>状态</span><select name="status">${renderOptions(STATUS_LABELS, filters.status)}</select></label>
      <label><span>会话类型</span><select name="scopeType">${renderOptions(SCOPE_LABELS, filters.scopeType)}</select></label>
      <label><span>Virtual Key</span><input name="virtualKeyId" value="${escapeAttribute(filters.virtualKeyId ?? "")}" placeholder="Key ID" autocomplete="off" /></label>
      <label><span>开始时间</span><input type="datetime-local" name="fromTime" value="${escapeAttribute(toDateTimeLocal(filters.fromTime))}" /></label>
      <label><span>结束时间</span><input type="datetime-local" name="toTime" value="${escapeAttribute(toDateTimeLocal(filters.toTime))}" /></label>
      <div class="session-risk-filter-actions">
        <button type="submit">筛选</button>
        <button type="button" id="session-risk-filter-reset">清空</button>
      </div>
    </form>
  `;
}

/** 渲染当前页可扫描列表；空态仍保留详情区域稳定尺寸。 */
function renderRiskList(risks, selectedId) {
  return `
    <div class="session-risk-list" aria-label="高危会话列表">
      ${risks.length
        ? risks.map((risk) => renderRiskListItem(risk, selectedId)).join("")
        : `<div class="session-risk-empty">当前筛选下没有高危会话记录。</div>`}
    </div>
  `;
}

/** 渲染单条会话摘要；scope_reference 已由 client 验证为 Chat ID 或短安全引用。 */
function renderRiskListItem(risk, selectedId) {
  const status = knownToken(risk?.status, STATUS_LABELS, "high_risk");
  const scopeType = knownToken(risk?.scope_type, SCOPE_LABELS, "api_session");
  const selected = risk?.session_risk_score_id === selectedId;
  return `
    <article class="session-risk-list-item status-${status}${selected ? " is-selected" : ""}">
      <div class="session-risk-list-topline">
        <mark class="session-risk-status status-${status}">${escapeHtml(STATUS_LABELS[status])}</mark>
        <time datetime="${escapeAttribute(validDateTimeAttribute(risk?.updated_at))}">${escapeHtml(formatDateTime(risk?.updated_at))}</time>
      </div>
      <div class="session-risk-scope">
        <span>${escapeHtml(SCOPE_LABELS[scopeType])}</span>
        <code>${escapeHtml(risk?.scope_reference ?? "-")}</code>
      </div>
      <div class="session-risk-score-line">
        <strong>${escapeHtml(safeScore(risk?.score))}</strong><span>/ 10000</span>
        <em>${escapeHtml(safeCount(risk?.risky_turn_count))} 个风险 turn</em>
      </div>
      ${renderSignals(risk?.signal_counts)}
      <button type="button" data-session-risk-select="${escapeAttribute(risk?.session_risk_score_id ?? "")}" aria-pressed="${selected ? "true" : "false"}">查看详情</button>
    </article>
  `;
}

/** 渲染固定三类信号计数，不枚举服务端对象 key。 */
function renderSignals(signals = {}) {
  return `
    <div class="session-risk-signals" aria-label="风险信号">
      <span>越狱 <strong>${escapeHtml(safeCount(signals.jailbreak))}</strong></span>
      <span>敏感 <strong>${escapeHtml(safeCount(signals.sensitive))}</strong></span>
      <span>工具诱导 <strong>${escapeHtml(safeCount(signals.tool_inducement))}</strong></span>
    </div>
  `;
}

/** 渲染会话详情、时间线、overlay 与 append-only 处置记录。 */
function renderDetail(risk, canManage, canOpenRequest) {
  if (!risk) {
    return `
      <aside class="session-risk-detail is-empty" aria-label="会话风险详情">
        <strong>会话风险详情</strong><p>选择一条记录查看冻结覆盖和处置时间线。</p>
      </aside>
    `;
  }
  const status = knownToken(risk.status, STATUS_LABELS, "high_risk");
  const scopeType = knownToken(risk.scope_type, SCOPE_LABELS, "api_session");
  return `
    <aside class="session-risk-detail" aria-labelledby="session-risk-detail-title">
      <div class="session-risk-detail-heading">
        <div><span>${escapeHtml(SCOPE_LABELS[scopeType])} · ${escapeHtml(STATUS_LABELS[status])}</span><h3 id="session-risk-detail-title">${escapeHtml(risk.scope_reference)}</h3></div>
        <button type="button" id="session-risk-close-detail" class="session-risk-icon-button" aria-label="关闭详情" title="关闭详情">×</button>
      </div>
      <dl class="session-risk-detail-grid">
        ${renderPair("风险记录 ID", risk.session_risk_score_id)}
        ${renderPair("Virtual Key", risk.virtual_key_id)}
        ${renderPair("风险分数", `${safeScore(risk.score)} / ${safeScore(risk.score_scale)}`)}
        ${renderPair("风险 turn", safeCount(risk.risky_turn_count))}
        ${renderPair("状态 revision", safeCount(risk.state_revision))}
        ${renderPair("阈值版本", risk.threshold_version)}
        ${renderRequestPair("最后 request_id", risk.last_source_request_id, canOpenRequest)}
        ${renderPair("首次高危", formatDateTime(risk.first_high_risk_at))}
        ${renderPair("冻结时间", formatDateTime(risk.frozen_at))}
        ${renderPair("窗口到期", formatDateTime(risk.window_expires_at))}
        ${renderPair("最后更新", formatDateTime(risk.updated_at))}
      </dl>
      ${renderSignals(risk.signal_counts)}
      ${renderFreezes(risk.freezes)}
      ${canManage ? renderActionCommands(risk) : ""}
      ${renderActionHistory(risk.actions, canOpenRequest)}
    </aside>
  `;
}

/** 渲染当前 score 来源下的 session/Key overlay，已释放记录仍保留。 */
function renderFreezes(freezes) {
  const values = Array.isArray(freezes) ? freezes : [];
  return `
    <section class="session-risk-freezes" aria-labelledby="session-risk-freezes-title">
      <h4 id="session-risk-freezes-title">冻结覆盖</h4>
      ${values.length
        ? `<ul>${values.map(renderFreezeItem).join("")}</ul>`
        : `<p>当前记录没有冻结覆盖。</p>`}
    </section>
  `;
}

/** 渲染单个 overlay，target/status 只使用固定标签。 */
function renderFreezeItem(freeze) {
  const target = knownToken(freeze?.target_type, TARGET_LABELS, "session");
  const status = knownToken(freeze?.status, FREEZE_STATUS_LABELS, "released");
  return `
    <li class="freeze-${status}">
      <div><strong>${escapeHtml(TARGET_LABELS[target])}</strong><span>${escapeHtml(FREEZE_STATUS_LABELS[status])}</span></div>
      <code>${escapeHtml(freeze?.session_risk_freeze_id ?? "-")}</code>
      <time>${escapeHtml(formatDateTime(freeze?.released_at ?? freeze?.created_at))}</time>
    </li>
  `;
}

/** 按当前后端事实生成可尝试动作；实际授权、状态和 target 仍由 POST 后端复核。 */
function renderActionCommands(risk) {
  const actions = availableSessionRiskActions(risk);
  return `
    <div class="session-risk-actions" aria-label="风险处置">
      ${actions.length
        ? actions.map((action) => renderActionButton(risk.session_risk_score_id, action)).join("")
        : `<span>当前状态无可用处置。</span>`}
    </div>
  `;
}

/** 渲染固定动作按钮；data 属性只承载当前页不透明 score ID 与动作枚举。 */
function renderActionButton(sessionRiskScoreId, action) {
  const primary = action === "freeze_session" || action === "freeze_virtual_key";
  return `<button type="button" class="${primary ? "is-danger" : ""}" data-session-risk-action="${escapeAttribute(action)}" data-session-risk-score-id="${escapeAttribute(sessionRiskScoreId)}">${escapeHtml(ACTION_LABELS[action])}</button>`;
}

/** 渲染不可变处置记录；actor display 与链路 ID 均逐字段转义。 */
function renderActionHistory(actions, canOpenRequest) {
  const values = Array.isArray(actions) ? actions : [];
  return `
    <section class="session-risk-history" aria-labelledby="session-risk-history-title">
      <h4 id="session-risk-history-title">处置记录</h4>
      ${values.length
        ? `<ol>${values.map((action) => renderHistoryItem(action, canOpenRequest)).join("")}</ol>`
        : `<p>暂无处置记录。</p>`}
    </section>
  `;
}

/** 渲染单条 action 时间线，不输出 Audit 网络元数据或幂等摘要。 */
function renderHistoryItem(action, canOpenRequest) {
  const actionName = knownToken(action?.action, ACTION_LABELS, "high_risk");
  const reason = knownToken(action?.reason_code, REASON_LABELS, "threshold_reached");
  const before = knownToken(action?.status_before, { monitoring: "监测中", ...STATUS_LABELS }, "monitoring");
  const after = knownToken(action?.status_after, { monitoring: "监测中", ...STATUS_LABELS }, "high_risk");
  return `
    <li>
      <div><strong>${escapeHtml(ACTION_LABELS[actionName])}</strong><time>${escapeHtml(formatDateTime(action?.created_at))}</time></div>
      <p>${escapeHtml(REASON_LABELS[reason])} · ${escapeHtml(({ monitoring: "监测中", ...STATUS_LABELS })[before])} → ${escapeHtml(({ monitoring: "监测中", ...STATUS_LABELS })[after])}</p>
      <span>${action?.actor_type === "system" ? "系统" : "操作者"} ${escapeHtml(action?.actor_reference ?? "-")}</span>
      ${renderRequestCommand(action?.source_request_id, canOpenRequest)}
    </li>
  `;
}

/** 渲染固定处置 Dialog；双权限不成立时整个 Dialog DOM 都不存在。 */
function renderActionDialog(dialog, risk, canManage) {
  if (!canManage || !dialog?.open || !risk) return "";
  const action = knownToken(dialog.action, ACTION_LABELS, "dismiss");
  const reasons = sessionRiskReasonCodes(action);
  if (!reasons.length) return "";
  return `
    <dialog class="session-risk-action-dialog" id="session-risk-action-dialog" open aria-modal="true" aria-labelledby="session-risk-action-title">
      <form id="session-risk-action-form">
        <div class="session-risk-dialog-heading">
          <div><span>风险处置</span><h3 id="session-risk-action-title">${escapeHtml(ACTION_LABELS[action])}</h3></div>
          <button type="button" id="session-risk-action-close" class="session-risk-icon-button" aria-label="关闭处置" title="关闭处置" ${dialog.submitting ? "disabled" : ""}>×</button>
        </div>
        ${dialog.error ? renderError(dialog.error, "风险处置提交失败") : ""}
        <dl class="session-risk-dialog-summary">
          ${renderPair("会话", `${SCOPE_LABELS[risk.scope_type]} / ${risk.scope_reference}`)}
          ${renderPair("当前状态", STATUS_LABELS[risk.status])}
          ${renderPair("风险分数", `${safeScore(risk.score)} / ${safeScore(risk.score_scale)}`)}
          ${renderPair("处置结果", actionResultLabel(action))}
        </dl>
        <label class="session-risk-reason-field"><span>原因</span><select name="reasonCode" required>${renderReasonOptions(reasons, dialog.reasonCode)}</select></label>
        <div class="session-risk-dialog-actions">
          <button type="button" id="session-risk-action-cancel" ${dialog.submitting ? "disabled" : ""}>取消</button>
          <button type="submit" ${dialog.submitting ? "disabled aria-busy=\"true\"" : ""}>${dialog.submitting ? "正在提交" : "确认处置"}</button>
        </div>
      </form>
    </dialog>
  `;
}

/** 把固定动作映射为 Dialog 中的明确结果，不显示任意 target 输入。 */
function actionResultLabel(action) {
  return {
    freeze_session: "为当前会话创建冻结覆盖",
    freeze_virtual_key: "为关联 Virtual Key 创建本来源冻结覆盖",
    dismiss: "将当前记录标记为已处置",
    release_session: "只解除当前记录的会话冻结覆盖",
    release_virtual_key: "只解除当前记录的 Key 冻结覆盖",
  }[action] ?? "提交固定风险处置";
}

/** 渲染当前动作允许的原因选项，不提供自由文本。 */
function renderReasonOptions(reasons, selectedReason) {
  return reasons.map((reason) => {
    const selected = reason === selectedReason ? " selected" : "";
    return `<option value="${escapeAttribute(reason)}"${selected}>${escapeHtml(REASON_LABELS[reason])}</option>`;
  }).join("");
}

/** 渲染 cursor 翻页命令；opaque cursor 原文不进入 DOM。 */
function renderPager(state, pageCount) {
  return `
    <div class="session-risk-pager">
      <button type="button" id="session-risk-prev" ${state.canPrevious ? "" : "disabled"}>上一页</button>
      <span>当前页 ${escapeHtml(safeCount(pageCount))} 条</span>
      <button type="button" id="session-risk-next" ${state.canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
}

/** 渲染成功反馈，不展示幂等键或 operation trace。 */
function renderFeedback(feedback) {
  if (!feedback) return "";
  return `<div class="session-risk-feedback" role="status">${escapeHtml(feedback.message ?? "风险处置已提交")}${feedback.idempotentReplay ? "（幂等恢复）" : ""} · request_id: ${escapeHtml(feedback.requestId ?? "-")}</div>`;
}

/** 渲染固定统一错误；code/message/request_id 已由状态层收敛并再次转义。 */
function renderError(error, fallback) {
  return `
    <div class="session-risk-error" role="alert" tabindex="-1">
      <strong>${escapeHtml(error?.code ?? "session_risk.unavailable")}</strong>
      <span>${escapeHtml(error?.message ?? fallback)}</span>
      <code>request_id: ${escapeHtml(error?.requestId ?? "-")}</code>
    </div>
  `;
}

/** 渲染详情 request_id；无日志权限时只显示 code，不生成跳转命令。 */
function renderRequestPair(label, requestId, canOpenRequest) {
  const value = requestId || "-";
  return `<div><dt>${escapeHtml(label)}</dt><dd>${canOpenRequest ? renderRequestButton(value) : `<code>${escapeHtml(value)}</code>`}</dd></div>`;
}

/** 渲染时间线内的 request_id 命令或只读 code。 */
function renderRequestCommand(requestId, canOpenRequest) {
  const value = requestId || "-";
  return canOpenRequest ? renderRequestButton(value) : `<code>request_id: ${escapeHtml(value)}</code>`;
}

/** 生成 Request Logs 跳转按钮；目标接口仍按当前 Session tenant 和权限校验。 */
function renderRequestButton(requestId) {
  return `<button type="button" class="session-risk-request-link" data-session-risk-request-id="${escapeAttribute(requestId)}">${escapeHtml(requestId)}</button>`;
}

/** 渲染非空详情键值对。 */
function renderPair(label, value) {
  if (value === null || value === undefined || value === "" || value === "-") return "";
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`;
}

/** 渲染固定筛选枚举，可选“全部”。 */
function renderOptions(labels, selectedValue) {
  return [["", "全部"], ...Object.entries(labels)].map(([value, label]) => {
    const selected = value === (selectedValue ?? "") ? " selected" : "";
    return `<option value="${escapeAttribute(value)}"${selected}>${escapeHtml(label)}</option>`;
  }).join("");
}

/** 从固定表中返回可用于 CSS 的 token，未知值使用安全默认。 */
function knownToken(value, labels, fallback) {
  return typeof value === "string" && Object.hasOwn(labels, value) ? value : fallback;
}

/** 数量只接受非负安全整数。 */
function safeCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : "0";
}

/** 分数只接受 0..10000 安全整数。 */
function safeScore(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= 10_000 ? String(value) : "-";
}

/** 把 UTC 时间格式化为中国用户习惯；非法值不回显原始文本。 */
function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "-" : date.toLocaleString("zh-CN", { hour12: false });
}

/** datetime 属性只接受可解析时间。 */
function validDateTimeAttribute(value) {
  const date = new Date(value ?? "");
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/** 把 UTC 筛选投影到 datetime-local 控件，不改变闭包保存的规范值。 */
function toDateTimeLocal(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

/** 对所有动态文本执行 HTML 转义。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值额外转义反引号，保护 data-*、value 和 datetime 边界。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
