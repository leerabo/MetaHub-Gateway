/** PAGE-108 审批中心与 PAGE-109 同页移动单任务视图。 */
import { approvalActionAccess, approvalPageOwner } from "./approvalPageState.js";

// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const TARGET_LABELS = Object.freeze({
  budget_block_release: "预算阻断释放",
  session_risk_action: "会话风险处置",
});
const ACTION_LABELS = Object.freeze({
  release: "释放预算阻断",
  freeze_session: "冻结会话",
  freeze_virtual_key: "冻结虚拟 Key",
  dismiss: "忽略风险",
  release_session: "解除会话冻结",
  release_virtual_key: "解除虚拟 Key 冻结",
});
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const STATUS_LABELS = Object.freeze({ pending: "待审批", approved: "已批准", rejected: "已拒绝" });
const WRITEBACK_LABELS = Object.freeze({ pending: "待回写", completed: "已回写", failed: "回写失败" });
const REASON_LABELS = Object.freeze({
  approved: "已批准",
  user_rejected: "人工拒绝",
  timeout: "超时自动拒绝",
  approved_business_need: "业务需要已确认",
  risk_unacceptable: "风险不可接受",
  budget_not_approved: "预算未获批准",
  no_longer_needed: "操作已不再需要",
});

/** 仅在后端 approvals 导航 owner 精确成立时输出页面 DOM。 */
export function renderApprovals(state, authContext, consoleShellState) {
  const owner = approvalPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? emptyState(owner);
  const mode = view.mobileFocus ? "mobile-card" : "center";
  return `
    <div class="approval-workspace mode-${mode}" data-approvals-root data-approval-view="${mode}">
      <header class="approval-toolbar">
        <div>
          <h2 id="approvals-title">${view.mobileFocus ? "移动审批卡片" : "审批中心"}</h2>
          <p>${view.mobileFocus ? "当前任务" : "预算和高风险操作的待审批事项"}</p>
        </div>
        <div class="approval-toolbar-actions">
          ${view.mobileFocus ? `<button type="button" class="icon-command" data-approval-back title="返回审批中心" aria-label="返回审批中心">←</button>` : ""}
          <button type="button" class="icon-command" data-approval-refresh title="刷新审批任务" aria-label="刷新审批任务">↻</button>
        </div>
      </header>
      ${view.mobileFocus ? "" : renderFilters(view)}
      <div class="approval-feedback${view.actionError ? " is-error" : ""}" role="status" aria-live="polite">
        ${renderActionFeedback(view)}
      </div>
      <div class="approval-layout">
        ${view.mobileFocus ? "" : renderList(view)}
        ${renderDetail(view, authContext)}
      </div>
      ${renderActionDialog(view)}
    </div>
  `;
}

/**
 * 渲染 renderFilters 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderFilters(state) {
  return `
    <div class="approval-filters">
      <label>状态
        <select data-approval-status-filter>
          ${[["pending", "待审批"], ["approved", "已批准"], ["rejected", "已拒绝"], ["all", "全部"]]
            .map(([value, label]) => `<option value="${value}"${state.status === value ? " selected" : ""}>${label}</option>`)
            .join("")}
        </select>
      </label>
      <label>目标类型
        <select data-approval-target-filter>
          <option value=""${state.targetType ? "" : " selected"}>全部类型</option>
          ${Object.entries(TARGET_LABELS).map(([value, label]) => (
            `<option value="${value}"${state.targetType === value ? " selected" : ""}>${label}</option>`
          )).join("")}
        </select>
      </label>
    </div>
  `;
}

/**
 * 渲染 renderList 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderList(state) {
  if (state.state === "error") return renderError(state.error, "审批列表读取失败");
  const approvals = Array.isArray(state.approvals) ? state.approvals : [];
  return `
    <section class="approval-list" aria-labelledby="approval-list-title">
      <div class="approval-section-heading"><h3 id="approval-list-title">任务</h3><span>${approvals.length} 项</span></div>
      ${state.state === "loading"
        ? `<div class="approval-empty">正在读取审批任务</div>`
        : approvals.length
          ? `<div class="approval-list-items">${approvals.map((item) => renderListItem(item, state.selectedApprovalId)).join("")}</div>`
          : `<div class="approval-empty">当前筛选下没有审批任务</div>`}
      <div class="approval-pager">
        <button type="button" data-approval-previous${state.canPrevious ? "" : " disabled"}>上一页</button>
        <button type="button" data-approval-next${state.canNext ? "" : " disabled"}>下一页</button>
      </div>
    </section>
  `;
}

/**
 * 渲染 renderListItem 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderListItem(item, selectedId) {
  return `
    <button type="button" class="approval-list-item${item.approval_id === selectedId ? " is-selected" : ""}" data-approval-select="${escapeAttribute(item.approval_id)}">
      <span><strong>${escapeHtml(TARGET_LABELS[item.target_type] ?? "审批任务")}</strong></span>
      <span class="approval-badges"><b class="tone-${escapeAttribute(item.status)}">${escapeHtml(STATUS_LABELS[item.status])}</b><b>${item.risk_level === "high" ? "高风险" : "标准"}</b></span>
      <span>${escapeHtml(ACTION_LABELS[item.target_action] ?? item.target_action)}</span>
      <span data-approval-countdown data-expires-at="${escapeAttribute(item.expires_at)}" data-status="${escapeAttribute(item.status)}">${escapeHtml(countdownText(item.expires_at, item.status))}</span>
    </button>
  `;
}

/**
 * 渲染 renderDetail 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderDetail(state, authContext) {
  if (!state.selectedApprovalId) {
    return `<section class="approval-detail"><div class="approval-empty">选择一项任务查看审批事实</div></section>`;
  }
  // 错误路径只保留可行动的普通用户提示，定位信息进入后台日志。
  if (state.detailState === "error") return renderError(state.detailError, "审批详情读取失败");
  if (state.detailState !== "ready" || !state.detail) {
    return `<section class="approval-detail"><div class="approval-empty">正在读取审批详情</div></section>`;
  }
  const { approval, target_summary: target, allowed_actions: actions, decisions } = state.detail;
  const canApprove = actions.includes("approve") && approvalActionAccess(approval, authContext, "approve");
  const canReject = actions.includes("reject") && approvalActionAccess(approval, authContext, "reject");
  return `
    <section class="approval-detail" aria-labelledby="approval-detail-title">
      <div class="approval-detail-heading">
        <div>
          <p class="eyebrow">${escapeHtml(TARGET_LABELS[approval.target_type])}</p>
          <h3 id="approval-detail-title">${escapeHtml(ACTION_LABELS[approval.target_action] ?? approval.target_action)}</h3>
        </div>
        <span class="approval-status tone-${escapeAttribute(approval.status)}">${escapeHtml(STATUS_LABELS[approval.status])}</span>
      </div>
      <div class="approval-card-summary">
        <div><span>风险</span><strong>${approval.risk_level === "high" ? "高风险 · 需要二次验证" : "标准"}</strong></div>
        <div><span>剩余时间</span><strong data-approval-countdown data-expires-at="${escapeAttribute(approval.expires_at)}" data-status="${escapeAttribute(approval.status)}">${escapeHtml(countdownText(approval.expires_at, approval.status))}</strong></div>
        <div><span>结果回写</span><strong>${escapeHtml(WRITEBACK_LABELS[approval.writeback_status])}</strong></div>
      </div>
      ${renderActions(canApprove, canReject, state.actionState, approval.approval_id)}
      <dl class="approval-target-summary">
        <div><dt>目标类型</dt><dd>${escapeHtml(TARGET_LABELS[target.target_type])}</dd></div>
        <div><dt>创建时间</dt><dd><time datetime="${escapeAttribute(approval.created_at)}">${escapeHtml(formatTime(approval.created_at))}</time></dd></div>
      </dl>
      ${approval.resolution_reason ? `<p class="approval-resolution">${escapeHtml(REASON_LABELS[approval.resolution_reason])}</p>` : ""}
      ${renderTimeline(decisions)}
    </section>
  `;
}

/**
 * 渲染 renderActions 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderActions(canApprove, canReject, actionState, approvalId) {
  if (!canApprove && !canReject) return "";
  const disabled = actionState === "submitting" ? " disabled" : "";
  const task = escapeAttribute(approvalId);
  return `
    <div class="approval-actions" aria-label="审批动作">
      ${canApprove ? `<button type="button" class="approval-command approve" data-approval-action="approve" data-approval-task="${task}"${disabled}><span aria-hidden="true">✓</span><span>批准</span></button>` : ""}
      ${canReject ? `<button type="button" class="approval-command reject" data-approval-action="reject" data-approval-task="${task}"${disabled}><span aria-hidden="true">×</span><span>拒绝</span></button>` : ""}
    </div>
  `;
}

/**
 * 渲染 renderTimeline 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderTimeline(decisions) {
  const items = Array.isArray(decisions) ? decisions : [];
  return `
    <section class="approval-timeline" aria-labelledby="approval-timeline-title">
      <div class="approval-section-heading"><h4 id="approval-timeline-title">审批审计</h4><span>${items.length} 条决策</span></div>
      <ol>
        <li><strong>任务已创建</strong><span>等待有权负责人处理</span></li>
        ${items.map((item) => `
          <li>
            <strong>${item.decision === "approve" ? "已批准" : item.decision === "timeout" ? "超时自动拒绝" : "已拒绝"}</strong>
            <span>${escapeHtml(REASON_LABELS[item.reason_code] ?? item.reason_code)} · ${escapeHtml(formatTime(item.created_at))}</span>
            <small>${escapeHtml(item.actor_type === "system" ? "系统自动处理" : "审批人已处理")}</small>
          </li>
        `).join("")}
      </ol>
    </section>
  `;
}

/**
 * 渲染 renderActionDialog 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderActionDialog(state) {
  if (!state.detail?.approval || !state.actionOpen || !["approve", "reject"].includes(state.actionType)) return "";
  const approval = state.detail.approval;
  const approve = state.actionType === "approve";
  const busy = state.actionState === "submitting";
  const disabled = busy ? " disabled" : "";
  const reasons = approve
    ? [["approved_business_need", "业务需要已确认"]]
    : [
        ["risk_unacceptable", "风险不可接受"],
        ["budget_not_approved", "预算未获批准"],
        ["no_longer_needed", "操作已不再需要"],
      ];
  return `
    <dialog class="approval-dialog" data-approval-dialog data-approval-task="${escapeAttribute(approval.approval_id)}" data-approval-dialog-action="${escapeAttribute(state.actionType)}" data-approval-busy="${busy ? "true" : "false"}" aria-busy="${busy ? "true" : "false"}" aria-labelledby="approval-dialog-title" tabindex="-1">
      <form method="dialog" data-approval-action-form autocomplete="off">
        <div class="approval-dialog-heading">
          <h3 id="approval-dialog-title">确认${approve ? "批准" : "拒绝"}</h3>
          <button type="button" class="icon-command" data-approval-dialog-close aria-label="关闭"${disabled}>×</button>
        </div>
        <p>这项审批决定提交后不可撤销，请确认后继续。</p>
        <label>原因
          <select name="reason_code" data-approval-reason${disabled}>
            ${reasons.map(([value, label]) => `<option value="${value}"${state.actionReason === value ? " selected" : ""}>${label}</option>`).join("")}
          </select>
        </label>
        ${approval.requires_step_up ? `
          <label>当前密码
            <input type="password" name="password" data-approval-password required maxlength="1024" autocomplete="current-password"${disabled}>
          </label>
        ` : ""}
        ${state.actionError ? renderError(state.actionError, "审批动作未完成") : ""}
        <div class="approval-dialog-actions">
          <button type="button" data-approval-dialog-close${disabled}>取消</button>
          <button type="submit" class="approval-command ${approve ? "approve" : "reject"}"${disabled}>${busy ? "正在提交" : `确认${approve ? "批准" : "拒绝"}`}</button>
        </div>
      </form>
    </dialog>
  `;
}

/**
 * 渲染 renderActionFeedback 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderActionFeedback(state) {
  if (state.actionState === "submitting") return "正在复核权限并提交审批";
  if (state.actionState === "success") return "审批已提交";
  if (state.actionError && !state.actionOpen) {
    return escapeHtml(state.actionError.message ?? "审批未完成，请稍后重试");
  }
  return "";
}

/**
 * 渲染 renderError 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function renderError(error, fallback) {
  return `
    <div class="approval-error" role="alert">
      <strong>${escapeHtml(error?.message ?? fallback)}</strong>
      <span>请稍后重试；如果问题持续，请联系管理员。</span>
    </div>
  `;
}

/**
 * 渲染 emptyState 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function emptyState(owner) {
  return {
    state: "loading",
    approvals: [],
    status: "pending",
    targetType: "",
    selectedApprovalId: "",
    mobileFocus: false,
    detailState: "idle",
    detail: null,
    canDecide: owner.canDecide,
    actionOpen: false,
    actionState: "idle",
  };
}

/**
 * 渲染 countdownText 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function countdownText(expiresAt, status) {
  if (status !== "pending") return "已结束";
  const seconds = Math.max(0, Math.ceil((Date.parse(expiresAt) - Date.now()) / 1000));
  if (seconds <= 0) return "等待超时收敛";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  return hours > 0 ? `${hours} 小时 ${minutes} 分` : `${minutes} 分钟`;
}

/**
 * 渲染 formatTime 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function formatTime(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "-";
}

/**
 * 渲染 escapeHtml 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * 渲染 escapeAttribute 对应审批视图；动态字段必须转义，显示能力不得替代后端授权。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
