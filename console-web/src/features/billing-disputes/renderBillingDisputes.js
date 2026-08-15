/** PAGE-112 compact list, detail and dialog rendering without private free text. */

import { billingDisputePageOwner } from "./billingDisputePageState.js";

const STATUS_LABELS = Object.freeze({
  submitted: "待审核",
  under_review: "审核中",
  approved: "已批准",
  rejected: "已拒绝",
  cancelled: "已取消",
});
const ACTION_LABELS = Object.freeze({
  create: "提交",
  start_review: "开始审核",
  approve: "批准",
  reject: "拒绝",
  cancel: "取消",
});

/** Render only when read permission and backend navigation mirrors are both current. */
export function renderBillingDisputes(state, authContext, consoleShellState, i18n) {
  const owner = billingDisputePageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  const t = translator(i18n);
  return `
    <div class="billing-dispute-workspace" data-billing-disputes-root>
      <header class="billing-dispute-toolbar">
        <div>
          <h2 id="billing-disputes-title">${t("billing_dispute.title", "账单争议")}</h2>
          <span>${t("billing_dispute.scope", "当前租户")}</span>
        </div>
        <div class="billing-dispute-toolbar-actions">
          <button type="button" class="billing-dispute-icon-command" data-billing-dispute-refresh title="${t("common.refresh", "刷新")}" aria-label="${t("common.refresh", "刷新")}"${state.dialog?.open ? " disabled" : ""}>&#8635;</button>
          ${owner.canCreate ? `<button type="button" class="primary" data-billing-dispute-open-create>${t("billing_dispute.create", "提交争议")}</button>` : ""}
        </div>
      </header>
      ${renderFeedback(state)}
      ${renderFilters(state, t)}
      <div class="billing-dispute-layout${state.selectedId ? " has-detail" : ""}">
        ${renderList(state, t)}
        ${renderDetail(state, owner, t)}
      </div>
      ${renderDialog(state, owner, t)}
    </div>
  `;
}

function renderFilters(state, t) {
  const filters = state.filters ?? {};
  const disabled = state.state === "loading" || state.dialog?.open;
  return `
    <form class="billing-dispute-filters" data-billing-dispute-filters novalidate>
      <label><span>${t("billing_dispute.status", "状态")}</span>
        <select name="status"${disabled ? " disabled" : ""}>
          <option value="">${t("billing_dispute.all", "全部")}</option>
          ${Object.entries(STATUS_LABELS).map(([value, label]) => `<option value="${value}"${filters.status === value ? " selected" : ""}>${t(`billing_dispute.status.${value}`, label)}</option>`).join("")}
        </select>
      </label>
      <label><span>${t("billing_dispute.scope_type", "范围")}</span>
        <select name="scope_type"${disabled ? " disabled" : ""}>
          <option value="">${t("billing_dispute.all", "全部")}</option>
          <option value="request_ids"${filters.scope_type === "request_ids" ? " selected" : ""}>Request IDs</option>
          <option value="time_range"${filters.scope_type === "time_range" ? " selected" : ""}>${t("billing_dispute.time_range", "时间段")}</option>
        </select>
      </label>
      <label><span>${t("billing_dispute.created_from", "创建开始")}</span><input type="datetime-local" name="created_from" step="1" value="${escapeAttribute(toLocalTime(filters.created_from))}"${disabled ? " disabled" : ""}></label>
      <label><span>${t("billing_dispute.created_to", "创建结束")}</span><input type="datetime-local" name="created_to" step="1" value="${escapeAttribute(toLocalTime(filters.created_to))}"${disabled ? " disabled" : ""}></label>
      <div class="billing-dispute-filter-actions">
        <button type="reset" data-billing-dispute-filter-reset${disabled ? " disabled" : ""}>${t("billing_dispute.reset", "重置")}</button>
        <button type="submit" class="primary"${disabled ? " disabled" : ""}>${t("billing_dispute.apply", "应用筛选")}</button>
      </div>
      <div class="billing-dispute-live" role="status" aria-live="polite"></div>
    </form>
  `;
}

function renderList(state, t) {
  if (state.state === "loading") {
    return `<section class="billing-dispute-list" aria-label="${t("billing_dispute.list", "争议列表")}"><div class="billing-dispute-empty" role="status">${t("billing_dispute.loading", "正在读取账单争议")}</div></section>`;
  }
  if (state.state === "error") {
    return `<section class="billing-dispute-list" aria-label="${t("billing_dispute.list", "争议列表")}">${renderError(state.error)}</section>`;
  }
  const items = Array.isArray(state.disputes) ? state.disputes : [];
  return `
    <section class="billing-dispute-list" aria-labelledby="billing-dispute-list-title">
      <div class="billing-dispute-section-heading"><h3 id="billing-dispute-list-title">${t("billing_dispute.list", "争议列表")}</h3><span>${items.length}</span></div>
      ${items.length ? `
        <div class="billing-dispute-table-scroll">
          <table class="billing-dispute-table">
            <thead><tr><th>${t("billing_dispute.id", "争议 ID")}</th><th>${t("billing_dispute.status", "状态")}</th><th>${t("billing_dispute.scope_type", "范围")}</th><th>${t("billing_dispute.items", "项目")}</th><th>${t("billing_dispute.total", "争议金额")}</th><th>${t("billing_dispute.created_at", "创建时间")}</th><th><span class="sr-only">${t("billing_dispute.action", "操作")}</span></th></tr></thead>
            <tbody>${items.map((item) => renderListRow(item, state.selectedId, t)).join("")}</tbody>
          </table>
        </div>
      ` : `<div class="billing-dispute-empty">${t("billing_dispute.empty", "暂无账单争议")}</div>`}
      <div class="billing-dispute-pagination">
        ${state.filters?.cursor ? `<button type="button" data-billing-dispute-first>${t("billing_dispute.first_page", "返回首页")}</button>` : ""}
        <button type="button" data-billing-dispute-next${state.nextCursor && !state.dialog?.open ? "" : " disabled"}>${t("billing_dispute.next_page", "下一页")}</button>
      </div>
    </section>
  `;
}

function renderListRow(item, selectedId, t) {
  const selected = item.billing_dispute_id === selectedId;
  return `
    <tr${selected ? " class=\"is-selected\"" : ""}>
      <td data-label="${t("billing_dispute.id", "争议 ID")}"><code>${escapeHtml(item.billing_dispute_id)}</code></td>
      <td data-label="${t("billing_dispute.status", "状态")}"><span class="billing-dispute-status tone-${escapeAttribute(item.status)}">${statusLabel(item.status, t)}</span></td>
      <td data-label="${t("billing_dispute.scope_type", "范围")}">${scopeLabel(item.scope_type, t)}</td>
      <td data-label="${t("billing_dispute.items", "项目")}">${integerText(item.item_count)}</td>
      <td data-label="${t("billing_dispute.total", "争议金额")}">${renderCurrencyTotals(item.disputed_currency_totals)}</td>
      <td data-label="${t("billing_dispute.created_at", "创建时间")}">${formatTime(item.created_at)}</td>
      <td class="billing-dispute-row-action"><button type="button" data-billing-dispute-select="${escapeAttribute(item.billing_dispute_id)}">${t("billing_dispute.view", "查看")}</button></td>
    </tr>
  `;
}

function renderDetail(state, owner, t) {
  if (!state.selectedId) return "";
  if (state.detailState === "loading") {
    return `<section class="billing-dispute-detail" aria-label="${t("billing_dispute.detail", "争议详情")}"><div class="billing-dispute-empty" role="status">${t("billing_dispute.detail_loading", "正在读取争议详情")}</div></section>`;
  }
  if (state.detailState === "error") {
    return `<section class="billing-dispute-detail" aria-label="${t("billing_dispute.detail", "争议详情")}">${renderError(state.detailError)}<button type="button" data-billing-dispute-close-detail>${t("common.close", "关闭")}</button></section>`;
  }
  const detail = state.detail;
  if (!detail) return "";
  const self = detail.created_by_user_id === owner.userId;
  return `
    <section class="billing-dispute-detail" aria-labelledby="billing-dispute-detail-title">
      <header class="billing-dispute-detail-heading">
        <div><h3 id="billing-dispute-detail-title">${t("billing_dispute.detail", "争议详情")}</h3><code>${escapeHtml(detail.billing_dispute_id)}</code></div>
        <button type="button" class="billing-dispute-icon-command" data-billing-dispute-close-detail title="${t("common.close", "关闭")}" aria-label="${t("common.close", "关闭")}">&times;</button>
      </header>
      <dl class="billing-dispute-summary">
        <div><dt>${t("billing_dispute.status", "状态")}</dt><dd><span class="billing-dispute-status tone-${escapeAttribute(detail.status)}">${statusLabel(detail.status, t)}</span></dd></div>
        <div><dt>Revision</dt><dd>${integerText(detail.state_revision)}</dd></div>
        <div><dt>${t("billing_dispute.scope_type", "范围")}</dt><dd>${scopeLabel(detail.scope_type, t)}</dd></div>
        <div><dt>${t("billing_dispute.items", "项目")}</dt><dd>${integerText(detail.item_count)}</dd></div>
        <div><dt>${t("billing_dispute.created_by", "申请人")}</dt><dd><code>${escapeHtml(detail.created_by_user_id)}</code></dd></div>
        <div><dt>${t("billing_dispute.reviewer", "审核人")}</dt><dd><code>${escapeHtml(detail.reviewer_user_id ?? "-")}</code></dd></div>
      </dl>
      ${renderPrivateText("reason", state.revealed?.reason, t("billing_dispute.reason", "申请原因"), t)}
      ${state.privateTextAvailable?.decisionReason ? renderPrivateText("decision", state.revealed?.decisionReason, t("billing_dispute.decision_reason", "决定原因"), t) : ""}
      ${renderDetailActions(detail, owner, self, t)}
      ${renderItems(detail.items, t)}
      ${renderTimeline(detail, t)}
    </section>
  `;
}

function renderPrivateText(kind, expanded, label, t) {
  const key = kind === "decision" ? "decisionReason" : "reason";
  return `
    <section class="billing-dispute-private-text" aria-labelledby="billing-dispute-${kind}-title">
      <div class="billing-dispute-section-heading">
        <h4 id="billing-dispute-${kind}-title">${escapeHtml(label)}</h4>
        <button type="button" data-billing-dispute-${expanded ? "collapse" : "reveal"}="${key}">${expanded ? t("billing_dispute.collapse", "收起") : t("billing_dispute.reveal", "临时展开")}</button>
      </div>
      ${expanded ? `<p data-billing-dispute-private-text="${key}"></p>` : `<p class="billing-dispute-redacted">${t("billing_dispute.collapsed", "内容已折叠")}</p>`}
    </section>
  `;
}

function renderDetailActions(detail, owner, self, t) {
  const buttons = [];
  if (detail.status === "submitted" && owner.canReview && !self) {
    buttons.push(actionButton("start_review", t("billing_dispute.start_review", "开始审核")));
  }
  if (detail.status === "submitted" && owner.canCreate && self) {
    buttons.push(actionButton("cancel", t("billing_dispute.cancel", "取消争议")));
  }
  if (detail.status === "under_review" && owner.canApprove && !self) {
    buttons.push(actionButton("approve", t("billing_dispute.approve", "批准"), "primary"));
  }
  if (detail.status === "under_review" && owner.canReview && !self) {
    buttons.push(actionButton("reject", t("billing_dispute.reject", "拒绝"), "danger"));
  }
  if (!buttons.length) return "";
  return `<div class="billing-dispute-actions" aria-label="${t("billing_dispute.actions", "争议操作")}">${buttons.join("")}</div>`;
}

function actionButton(action, label, className = "") {
  return `<button type="button"${className ? ` class="${className}"` : ""} data-billing-dispute-open-action="${action}">${escapeHtml(label)}</button>`;
}

function renderItems(items, t) {
  if (!Array.isArray(items) || !items.length) return "";
  return `
    <section class="billing-dispute-items" aria-labelledby="billing-dispute-items-title">
      <div class="billing-dispute-section-heading"><h4 id="billing-dispute-items-title">${t("billing_dispute.items", "项目")}</h4><span>${items.length}</span></div>
      <div class="billing-dispute-item-list">
        ${items.map((item) => `
          <div class="billing-dispute-item">
            <code>${escapeHtml(item.source_request_id ?? item.billing_dispute_item_id)}</code>
            <span>${escapeHtml(item.currency ?? "-")} ${escapeHtml(item.disputable_amount ?? item.disputable ?? item.amount ?? "-")}</span>
            <span>${formatTime(item.created_at)}</span>
            <small>${escapeHtml(item.price_evidence_status ?? "-")}</small>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

function renderTimeline(detail, t) {
  const transitions = Array.isArray(detail.transitions) ? detail.transitions : [];
  const holds = Array.isArray(detail.holds) ? detail.holds : [];
  const links = Array.isArray(detail.adjustment_links) ? detail.adjustment_links : [];
  return `
    <section class="billing-dispute-timeline" aria-labelledby="billing-dispute-timeline-title">
      <div class="billing-dispute-section-heading"><h4 id="billing-dispute-timeline-title">${t("billing_dispute.timeline", "处理时间线")}</h4><span>${transitions.length}</span></div>
      <ol>
        ${transitions.map((transition) => `
          <li>
            <span class="billing-dispute-timeline-mark" aria-hidden="true"></span>
            <div><strong>${escapeHtml(ACTION_LABELS[transition.action] ?? transition.action ?? "-")}</strong><span>${escapeHtml(transition.from_status ?? "-")} → ${escapeHtml(transition.to_status ?? "-")}</span><time>${formatTime(transition.created_at)}</time></div>
          </li>
        `).join("")}
      </ol>
      <div class="billing-dispute-fact-strip">
        <span>${t("billing_dispute.holds", "结算暂缓")} <strong>${holds.filter((item) => item.status === "active").length}</strong> / ${holds.length}</span>
        <span>${t("billing_dispute.adjustments", "调整记录")} <strong>${links.length}</strong></span>
        <span>${t("billing_dispute.event", "事件投递")} <strong>${escapeHtml(detail.event_delivery_status ?? "-")}</strong></span>
      </div>
    </section>
  `;
}

function renderDialog(state, owner, t) {
  if (!state.dialog?.open) return "";
  return state.dialog.kind === "create"
    ? renderCreateDialog(state, owner, t)
    : renderActionDialog(state, owner, t);
}

function renderCreateDialog(state, owner, t) {
  if (!owner.canCreate) return "";
  const busy = state.mutationState === "submitting";
  const unknown = state.mutationState === "unknown_result";
  return `
    <dialog class="billing-dispute-dialog" data-billing-dispute-dialog data-dialog-kind="create" aria-labelledby="billing-dispute-create-title" aria-busy="${busy}">
      <header><h3 id="billing-dispute-create-title">${t("billing_dispute.create", "提交争议")}</h3><button type="button" class="billing-dispute-icon-command" data-billing-dispute-dialog-close title="${t("common.close", "关闭")}" aria-label="${t("common.close", "关闭")}"${busy ? " disabled" : ""}>&times;</button></header>
      ${unknown ? renderUnknownResult(state, t) : `
        <form data-billing-dispute-create-form novalidate>
          <fieldset class="billing-dispute-segmented"${busy ? " disabled" : ""}>
            <legend>${t("billing_dispute.scope_type", "范围")}</legend>
            <label><input type="radio" name="scope_type" value="request_ids"${state.dialog.scopeType === "request_ids" ? " checked" : ""}><span>Request IDs</span></label>
            <label><input type="radio" name="scope_type" value="time_range"${state.dialog.scopeType === "time_range" ? " checked" : ""}><span>${t("billing_dispute.time_range", "时间段")}</span></label>
          </fieldset>
          <div data-billing-dispute-request-scope${state.dialog.scopeType === "request_ids" ? "" : " hidden"}>
            <label class="billing-dispute-field"><span>Request IDs</span><textarea name="request_ids" rows="5" maxlength="16100" autocomplete="off" placeholder="req_..."${busy ? " disabled" : ""}></textarea></label>
          </div>
          <div class="billing-dispute-time-fields" data-billing-dispute-time-scope${state.dialog.scopeType === "time_range" ? "" : " hidden"}>
            <label class="billing-dispute-field"><span>${t("billing_dispute.period_start", "开始时间")}</span><input type="datetime-local" name="period_start" step="1"${busy ? " disabled" : ""}></label>
            <label class="billing-dispute-field"><span>${t("billing_dispute.period_end", "结束时间")}</span><input type="datetime-local" name="period_end" step="1"${busy ? " disabled" : ""}></label>
          </div>
          <label class="billing-dispute-field"><span>${t("billing_dispute.reason", "申请原因")}</span><textarea name="reason" rows="5" maxlength="2000" autocomplete="off" required${busy ? " disabled" : ""}></textarea></label>
          <output class="billing-dispute-scope-summary" data-billing-dispute-scope-summary>${t("billing_dispute.scope_pending", "范围待确认")}</output>
        </form>
      `}
      ${renderDialogStatus(state, t)}
      <div class="billing-dispute-dialog-actions">
        <button type="button" data-billing-dispute-dialog-cancel${busy ? " disabled" : ""}>${t("common.cancel", "取消")}</button>
        ${unknown
          ? `<button type="button" class="primary" data-billing-dispute-retry>${t("billing_dispute.retry", "重试同一操作")}</button>`
          : `<button type="button" class="primary" data-billing-dispute-submit-create${busy ? " disabled" : ""}>${t("common.confirm", "确认")}</button>`}
      </div>
    </dialog>
  `;
}

function renderActionDialog(state, owner, t) {
  const action = state.dialog.action;
  const allowed = action === "cancel" ? owner.canCreate : action === "approve" ? owner.canApprove : owner.canReview;
  if (!allowed) return "";
  const busy = state.mutationState === "submitting";
  const unknown = state.mutationState === "unknown_result";
  return `
    <dialog class="billing-dispute-dialog" data-billing-dispute-dialog data-dialog-kind="action" data-action="${escapeAttribute(action)}" aria-labelledby="billing-dispute-action-title" aria-busy="${busy}">
      <header><h3 id="billing-dispute-action-title">${escapeHtml(ACTION_LABELS[action] ?? action)}</h3><button type="button" class="billing-dispute-icon-command" data-billing-dispute-dialog-close title="${t("common.close", "关闭")}" aria-label="${t("common.close", "关闭")}"${busy ? " disabled" : ""}>&times;</button></header>
      ${unknown ? renderUnknownResult(state, t) : `
        <form data-billing-dispute-action-form novalidate>
          <input type="hidden" name="action" value="${escapeAttribute(action)}">
          <input type="hidden" name="expected_revision" value="${integerText(state.dialog.expectedRevision)}">
          ${action === "approve" ? renderApprovalInputs(state.detail?.items ?? [], t, busy) : ""}
          ${action !== "start_review" ? `<label class="billing-dispute-field"><span>${t("billing_dispute.decision_reason", "决定原因")}</span><textarea name="decision_reason" rows="5" maxlength="2000" autocomplete="off" required${busy ? " disabled" : ""}></textarea></label>` : `<p>${t("billing_dispute.start_review_confirm", "确认开始审核该争议。")}</p>`}
        </form>
      `}
      ${renderDialogStatus(state, t)}
      <div class="billing-dispute-dialog-actions">
        <button type="button" data-billing-dispute-dialog-cancel${busy ? " disabled" : ""}>${t("common.cancel", "取消")}</button>
        ${unknown
          ? `<button type="button" class="primary" data-billing-dispute-retry>${t("billing_dispute.retry", "重试同一操作")}</button>`
          : `<button type="button" class="${action === "reject" ? "danger" : "primary"}" data-billing-dispute-submit-action${busy ? " disabled" : ""}>${t("common.confirm", "确认")}</button>`}
      </div>
    </dialog>
  `;
}

function renderApprovalInputs(items, t, busy) {
  return `
    <fieldset class="billing-dispute-approval-items"${busy ? " disabled" : ""}>
      <legend>${t("billing_dispute.approve_items", "批准调整项目")}</legend>
      ${items.map((item) => {
        const maximum = item.current_disputable_amount ?? item.disputable_amount ?? item.disputable ?? item.amount ?? "";
        return `
          <label>
            <span><code>${escapeHtml(item.source_request_id ?? item.billing_dispute_item_id)}</code><small>${escapeHtml(item.currency ?? "-")} · ${t("billing_dispute.maximum", "上限")} ${escapeHtml(maximum || "-")}</small></span>
            <input name="adjustment:${escapeAttribute(item.billing_dispute_item_id)}" inputmode="decimal" maxlength="24" placeholder="0.000000" autocomplete="off">
          </label>
        `;
      }).join("")}
    </fieldset>
  `;
}

function renderUnknownResult(state, t) {
  return `<div class="billing-dispute-unknown" role="status"><strong>${t("billing_dispute.unknown_result", "提交结果待确认")}</strong><span>${escapeHtml(state.dialog.action ? ACTION_LABELS[state.dialog.action] : t("billing_dispute.create", "提交争议"))}</span></div>`;
}

function renderDialogStatus(state, t) {
  const busy = state.mutationState === "submitting";
  return `<div class="billing-dispute-live${state.mutationError ? " is-error" : ""}" role="status" aria-live="polite">${busy ? t("billing_dispute.submitting", "正在提交账单争议操作") : state.mutationError ? renderErrorText(state.mutationError) : ""}</div>`;
}

function renderFeedback(state) {
  if (!state.feedback) return "";
  return `<div class="billing-dispute-feedback${state.feedback.error ? " is-error" : ""}" role="status">${escapeHtml(state.feedback.message)} · request_id: ${escapeHtml(state.feedback.requestId ?? "-")}</div>`;
}

function renderError(error) {
  return `<div class="billing-dispute-error" role="alert"><strong>${escapeHtml(error?.message ?? "账单争议暂不可用")}</strong><code>${escapeHtml(error?.code ?? "billing_dispute.unavailable")}</code><span>request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function renderErrorText(error) {
  return `${escapeHtml(error.message)} · ${escapeHtml(error.code)} · request_id: ${escapeHtml(error.requestId)}`;
}

function renderCurrencyTotals(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "-";
  const entries = Object.entries(value).filter(([currency, amount]) => /^[A-Z]{3}$/.test(currency) && typeof amount === "string");
  return entries.length ? entries.map(([currency, amount]) => `${escapeHtml(amount)} ${currency}`).join(" / ") : "-";
}

function statusLabel(value, t) {
  return t(`billing_dispute.status.${value}`, STATUS_LABELS[value] ?? value);
}

function scopeLabel(value, t) {
  return value === "request_ids" ? "Request IDs" : t("billing_dispute.time_range", "时间段");
}

function translator(i18n) {
  return (key, fallback) => i18n?.t?.(key, {}, fallback) ?? fallback;
}

function integerText(value) {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : "0";
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString([], { hour12: false }) : "-";
}

function toLocalTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
