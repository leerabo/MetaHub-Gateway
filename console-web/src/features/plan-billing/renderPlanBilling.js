/** PAGE-110/111 的无敏感正文渲染。 */

import { planPageOwner, quotaPageOwner } from "./planBillingPageState.js";

// 额度类型标签只用于受控页面展示，权限与额度事实仍由后端响应决定。
const QUOTA_LABELS = Object.freeze({
  paid: "付费",
  free: "免费",
  trial: "试用",
  gift: "赠送",
});

/** 仅在完整 plan.read 导航 owner 下渲染套餐页。 */
export function renderPlanManagement(state, authContext, consoleShellState, i18n) {
  const owner = planPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  const translate = translator(i18n);
  const draft = state.draft ?? {
    expected_revision: 0,
    display_name: "",
    base_tier: "free",
    status: "active",
    trial_ends_at: null,
  };
  return `
    <div class="plan-billing-workspace" data-plan-management-root>
      <header class="plan-billing-toolbar">
        <div>
          <h2 id="plans-title">${translate("plan.title", "套餐管理")}</h2>
          <span>${translate("plan.scope", "当前租户")}</span>
        </div>
        <button type="button" class="plan-billing-icon-command" data-plan-refresh title="${translate("common.refresh", "刷新")}" aria-label="${translate("common.refresh", "刷新")}"${state.dirty || state.confirmOpen || state.saveState === "saving" ? " disabled" : ""}>&#8635;</button>
      </header>
      ${renderFeedback(state)}
      ${state.state === "loading" ? `<div class="plan-billing-empty" role="status">${translate("plan.loading", "正在读取套餐")}</div>` : ""}
      ${state.state === "error" ? renderError(state.error, translate("plan.unavailable", "套餐暂不可用")) : ""}
      ${state.state === "ready" ? `
        ${renderPlanSummary(state.plan, translate)}
        ${renderPlanEditor(state, draft, owner, translate)}
        ${renderQuotaTable(state.quotaBuckets, { showActions: false, translate })}
      ` : ""}
      ${renderPlanConfirmation(state, draft, owner, translate)}
    </div>
  `;
}

/** 仅在完整 quota.read 导航 owner 下渲染额度页。 */
export function renderQuotaManagement(state, authContext, consoleShellState, i18n) {
  const owner = quotaPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  const translate = translator(i18n);
  return `
    <div class="plan-billing-workspace" data-quota-management-root>
      <header class="plan-billing-toolbar">
        <div>
          <h2 id="quota-buckets-title">${translate("quota.title", "额度管理")}</h2>
          <span>${translate("quota.scope", "当前租户")}</span>
        </div>
        <button type="button" class="plan-billing-icon-command" data-quota-refresh title="${translate("common.refresh", "刷新")}" aria-label="${translate("common.refresh", "刷新")}"${state.dialog?.open ? " disabled" : ""}>&#8635;</button>
      </header>
      ${renderFeedback(state)}
      ${state.state === "loading" ? `<div class="plan-billing-empty" role="status">${translate("quota.loading", "正在读取额度")}</div>` : ""}
      ${state.state === "error" ? renderError(state.error, translate("quota.unavailable", "额度暂不可用")) : ""}
      ${state.state === "ready" ? `
        ${renderQuotaPlanLine(state.plan, translate)}
        ${renderQuotaTable(state.quotaBuckets, { showActions: owner.canWrite, translate })}
        ${renderOverage(state.overage, translate)}
        ${renderRecentAdjustments(state.recentAdjustments, translate)}
      ` : ""}
      ${renderQuotaDialog(state, owner, translate)}
    </div>
  `;
}

/** 渲染套餐事实摘要；无套餐时仅展示当前租户的空状态。 */
function renderPlanSummary(plan, translate) {
  if (!plan) {
    return `<div class="plan-billing-empty plan-empty-state">${translate("plan.empty", "当前租户尚未创建套餐")}</div>`;
  }
  return `
    <dl class="plan-summary-strip">
      <div><dt>${translate("plan.base_tier", "基础层级")}</dt><dd>${tierLabel(plan.base_tier, translate)}</dd></div>
      <div><dt>${translate("plan.effective_tier", "有效层级")}</dt><dd>${tierLabel(plan.effective_tier, translate)}</dd></div>
      <div><dt>${translate("plan.status", "状态")}</dt><dd>${statusLabel(plan.status, translate)}</dd></div>
      <div><dt>${translate("plan.trial_status", "试用")}</dt><dd>${trialLabel(plan.trial_status, translate)}</dd></div>
      <div><dt>Revision</dt><dd>${plan.state_revision}</dd></div>
      <div><dt>${translate("plan.trial_end", "试用结束")}</dt><dd>${formatTime(plan.trial_ends_at)}</dd></div>
    </dl>
  `;
}

/** 渲染套餐 CAS 编辑器；没有实时写权限时仅返回只读提示。 */
function renderPlanEditor(state, draft, owner, translate) {
  if (!owner.canWrite) {
    return `<div class="plan-billing-readonly" role="status">${translate("plan.read_only", "当前为只读套餐视图")}</div>`;
  }
  const disabled = state.saveState === "saving" || state.conflict;
  return `
    <section class="plan-editor" aria-labelledby="plan-editor-title">
      <div class="plan-billing-section-heading">
        <h3 id="plan-editor-title">${state.plan ? translate("plan.edit", "套餐设置") : translate("plan.create", "创建套餐")}</h3>
        <span>revision ${draft.expected_revision}</span>
      </div>
      <form data-plan-form novalidate>
        <label class="plan-billing-field span-2">
          <span>${translate("plan.display_name", "套餐名称")}</span>
          <input name="display_name" maxlength="80" value="${escapeAttribute(draft.display_name)}" autocomplete="off" required${disabled ? " disabled" : ""}>
        </label>
        <label class="plan-billing-field">
          <span>${translate("plan.base_tier", "基础层级")}</span>
          <select name="base_tier"${disabled ? " disabled" : ""}>
            <option value="free"${draft.base_tier === "free" ? " selected" : ""}>${translate("tier.free", "免费")}</option>
            <option value="paid"${draft.base_tier === "paid" ? " selected" : ""}>${translate("tier.paid", "付费")}</option>
          </select>
        </label>
        <label class="plan-billing-field">
          <span>${translate("plan.trial_end", "试用结束")}</span>
          <input type="datetime-local" name="trial_ends_at" step="1" value="${escapeAttribute(toDateTimeLocal(draft.trial_ends_at))}"${disabled ? " disabled" : ""}>
        </label>
        <fieldset class="plan-status-segmented span-2"${disabled ? " disabled" : ""}>
          <legend>${translate("plan.status", "状态")}</legend>
          <label><input type="radio" name="status" value="active"${draft.status === "active" ? " checked" : ""}><span>${translate("status.active", "启用")}</span></label>
          <label><input type="radio" name="status" value="suspended"${draft.status === "suspended" ? " checked" : ""}><span>${translate("status.suspended", "暂停")}</span></label>
        </fieldset>
        <div class="plan-billing-live span-2${state.saveError ? " is-error" : ""}" role="status" aria-live="polite">
          ${state.saveError ? renderErrorText(state.saveError) : state.dirty ? translate("plan.dirty", "有未保存更改") : ""}
        </div>
        <div class="plan-billing-actions span-2">
          ${state.conflict ? `<button type="button" data-plan-rebase>${translate("plan.reload", "重新读取")}</button>` : ""}
          <button type="button" data-plan-discard${state.dirty && !state.conflict ? "" : " disabled"}>${translate("common.discard", "放弃更改")}</button>
          <button type="submit" class="primary"${state.conflict || state.saveState === "saving" ? " disabled" : ""}>${translate("plan.save", "保存套餐")}</button>
        </div>
      </form>
    </section>
  `;
}

/** 渲染套餐变更确认框；只允许当前可写所有者确认现有草稿。 */
function renderPlanConfirmation(state, draft, owner, translate) {
  if (!owner.canWrite || !state.confirmOpen) return "";
  const busy = state.saveState === "saving";
  return `
    <dialog class="plan-billing-dialog" data-plan-confirm-dialog aria-labelledby="plan-confirm-title" aria-busy="${busy}">
      <header>
        <h3 id="plan-confirm-title">${translate("plan.confirm_title", "确认套餐变更")}</h3>
        <button type="button" class="plan-billing-icon-command" data-plan-confirm-close title="${translate("common.close", "关闭")}" aria-label="${translate("common.close", "关闭")}"${busy ? " disabled" : ""}>&times;</button>
      </header>
      <dl class="plan-confirm-summary">
        <div><dt>${translate("plan.display_name", "套餐名称")}</dt><dd>${escapeHtml(draft.display_name)}</dd></div>
        <div><dt>${translate("plan.base_tier", "基础层级")}</dt><dd>${tierLabel(draft.base_tier, translate)}</dd></div>
        <div><dt>${translate("plan.status", "状态")}</dt><dd>${statusLabel(draft.status, translate)}</dd></div>
        <div><dt>${translate("plan.trial_end", "试用结束")}</dt><dd>${formatTime(draft.trial_ends_at)}</dd></div>
      </dl>
      <div class="plan-billing-live${state.saveError ? " is-error" : ""}" role="status" aria-live="polite">
        ${busy ? translate("plan.saving", "正在保存套餐") : state.saveError ? renderErrorText(state.saveError) : ""}
      </div>
      <div class="plan-billing-dialog-actions">
        <button type="button" data-plan-confirm-cancel${busy ? " disabled" : ""}>${translate("common.cancel", "取消")}</button>
        <button type="button" class="primary" data-plan-confirm-submit${busy ? " disabled" : ""}>${translate("common.confirm", "确认")}</button>
      </div>
    </dialog>
  `;
}

/** 渲染额度页的当前套餐摘要，不从页面参数推导租户信息。 */
function renderQuotaPlanLine(plan, translate) {
  if (!plan) return `<div class="plan-billing-empty">${translate("quota.no_plan", "当前租户尚未创建套餐")}</div>`;
  return `
    <div class="quota-plan-line">
      <strong>${escapeHtml(plan.display_name)}</strong>
      <span>${tierLabel(plan.effective_tier, translate)}</span>
      <span>${statusLabel(plan.status, translate)}</span>
      <code>revision ${plan.state_revision}</code>
    </div>
  `;
}

/** 渲染已校验额度桶列表，调整命令只在调用方授权时出现。 */
function renderQuotaTable(buckets, { showActions, translate }) {
  if (!Array.isArray(buckets) || !buckets.length) return "";
  const rows = [...buckets]
    .sort((left, right) => ["paid", "free", "trial", "gift"].indexOf(left.quota_type) - ["paid", "free", "trial", "gift"].indexOf(right.quota_type))
    .map((bucket) => `
      <tr data-quota-type="${bucket.quota_type}">
        <th scope="row" data-label="${translate("quota.type", "类型")}"><span class="quota-type-swatch tone-${bucket.quota_type}" aria-hidden="true"></span>${quotaLabel(bucket.quota_type, translate)}</th>
        <td data-label="${translate("quota.granted", "授予")}">${escapeHtml(bucket.granted)}</td>
        <td data-label="${translate("quota.consumed", "已消费")}">${escapeHtml(bucket.consumed)}</td>
        <td data-label="${translate("quota.reserved", "预留")}">${escapeHtml(bucket.reserved)}</td>
        <td data-label="${translate("quota.available", "可用")}"><strong>${escapeHtml(bucket.available)}</strong></td>
        <td data-label="${translate("quota.allocation", "分类消费")}">${escapeHtml(bucket.allocation_consumed)} <small>(${bucket.allocation_count})</small></td>
        <td data-label="${translate("quota.status", "状态")}">${bucketStatusLabel(bucket.status, translate)}</td>
        ${showActions ? `<td class="quota-action-cell"><button type="button" data-quota-adjust="${bucket.quota_type}">${translate("quota.adjust", "调整")}</button></td>` : ""}
      </tr>
    `).join("");
  return `
    <section class="quota-table-section" aria-labelledby="quota-table-title">
      <div class="plan-billing-section-heading">
        <h3 id="quota-table-title">${translate("quota.buckets", "额度分类")}</h3>
        <span>CNY</span>
      </div>
      <div class="quota-table-scroll">
        <table class="quota-table">
          <thead><tr>
            <th>${translate("quota.type", "类型")}</th>
            <th>${translate("quota.granted", "授予")}</th>
            <th>${translate("quota.consumed", "已消费")}</th>
            <th>${translate("quota.reserved", "预留")}</th>
            <th>${translate("quota.available", "可用")}</th>
            <th>${translate("quota.allocation", "分类消费")}</th>
            <th>${translate("quota.status", "状态")}</th>
            ${showActions ? `<th><span class="sr-only">${translate("quota.action", "操作")}</span></th>` : ""}
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </section>
  `;
}

/** 渲染独立超额结算摘要，非零金额使用告警语义提示。 */
function renderOverage(overage, translate) {
  if (!overage) return "";
  const alert = overage.amount !== "0.000000";
  return `
    <div class="quota-overage${alert ? " is-alert" : ""}" role="${alert ? "alert" : "status"}">
      <span>${translate("quota.overage", "未分配 / Overage")}</span>
      <strong>${escapeHtml(overage.amount)} CNY</strong>
      <small>${overage.settlement_count} ${translate("quota.settlements", "笔结算")}</small>
    </div>
  `;
}

/** 渲染最近额度调整的公开摘要，不包含私有原因或幂等键。 */
function renderRecentAdjustments(items, translate) {
  if (!Array.isArray(items) || !items.length) {
    return `<section class="quota-adjustment-history"><div class="plan-billing-section-heading"><h3>${translate("quota.recent", "最近调整")}</h3></div><div class="plan-billing-empty">${translate("quota.no_adjustments", "暂无额度调整")}</div></section>`;
  }
  return `
    <section class="quota-adjustment-history" aria-labelledby="quota-adjustment-history-title">
      <div class="plan-billing-section-heading"><h3 id="quota-adjustment-history-title">${translate("quota.recent", "最近调整")}</h3><span>${items.length}</span></div>
      <div class="quota-adjustment-list">
        ${items.map((item) => `
          <div class="quota-adjustment-row">
            <span class="quota-type-swatch tone-${item.quota_type}" aria-hidden="true"></span>
            <strong>${quotaLabel(item.quota_type, translate)}</strong>
            <span>${item.direction === "credit" ? "+" : "-"}${escapeHtml(item.amount)} CNY</span>
            <span>${formatTime(item.created_at)}</span>
            <code>revision ${item.resulting_bucket_revision}</code>
          </div>
        `).join("")}
      </div>
    </section>
  `;
}

/** 渲染额度调整模态框；未知结果仅暴露重试同一意图的命令。 */
function renderQuotaDialog(state, owner, translate) {
  if (!owner.canWrite || !state.dialog?.open) return "";
  const busy = state.adjustmentState === "submitting";
  const unknown = state.adjustmentState === "unknown_result";
  const bucket = state.quotaBuckets?.find((item) => item.quota_type === state.dialog.quotaType);
  return `
    <dialog class="plan-billing-dialog quota-adjustment-dialog" data-quota-adjustment-dialog data-quota-type="${state.dialog.quotaType}" aria-labelledby="quota-adjustment-title" aria-busy="${busy}">
      <header>
        <h3 id="quota-adjustment-title">${translate("quota.adjust_title", "调整额度")}</h3>
        <button type="button" class="plan-billing-icon-command" data-quota-adjustment-close title="${translate("common.close", "关闭")}" aria-label="${translate("common.close", "关闭")}"${busy ? " disabled" : ""}>&times;</button>
      </header>
      ${unknown ? `
        <div class="quota-unknown-result" role="status">
          <strong>${translate("quota.unknown_result", "调整结果待确认")}</strong>
          <span>${quotaLabel(state.dialog.quotaType, translate)} · ${state.dialog.direction === "credit" ? "+" : "-"}${escapeHtml(state.dialog.amount)} CNY</span>
        </div>
      ` : `
        <form data-quota-adjustment-form novalidate>
          <label class="plan-billing-field">
            <span>${translate("quota.type", "类型")}</span>
            <select name="quota_type"${busy ? " disabled" : ""}>
              ${["paid", "free", "trial", "gift"].map((type) => `<option value="${type}"${state.dialog.quotaType === type ? " selected" : ""}>${quotaLabel(type, translate)}</option>`).join("")}
            </select>
          </label>
          <label class="plan-billing-field">
            <span>${translate("quota.direction", "方向")}</span>
            <select name="direction"${busy ? " disabled" : ""}>
              <option value="credit"${state.dialog.direction === "credit" ? " selected" : ""}>${translate("quota.credit", "增加")}</option>
              <option value="debit"${state.dialog.direction === "debit" ? " selected" : ""}>${translate("quota.debit", "减少")}</option>
            </select>
          </label>
          <label class="plan-billing-field">
            <span>${translate("quota.amount", "金额（CNY）")}</span>
            <input name="amount" inputmode="decimal" maxlength="26" value="${escapeAttribute(state.dialog.amount)}" placeholder="0.000000" autocomplete="off" required${busy ? " disabled" : ""}>
          </label>
          <label class="plan-billing-field">
            <span>${translate("quota.reason", "原因")}</span>
            <textarea name="reason" maxlength="500" rows="4" autocomplete="off" required${busy ? " disabled" : ""}></textarea>
          </label>
          <input type="hidden" name="expected_bucket_revision" value="${bucket?.state_revision ?? 0}">
        </form>
      `}
      <div class="plan-billing-live${state.adjustmentError ? " is-error" : ""}" role="status" aria-live="polite">
        ${busy ? translate("quota.submitting", "正在提交额度调整") : state.adjustmentError ? renderErrorText(state.adjustmentError) : ""}
      </div>
      <div class="plan-billing-dialog-actions">
        <button type="button" data-quota-adjustment-cancel${busy ? " disabled" : ""}>${translate("common.cancel", "取消")}</button>
        ${unknown
          ? `<button type="button" class="primary" data-quota-adjustment-retry>${translate("quota.retry", "重试同一调整")}</button>`
          : `<button type="button" class="primary" data-quota-adjustment-submit${busy ? " disabled" : ""}>${translate("common.confirm", "确认")}</button>`}
      </div>
    </dialog>
  `;
}

/** 渲染固定操作反馈及链路标识，不输出服务端原始错误正文。 */
function renderFeedback(state) {
  if (!state.feedback) return "";
  return `<div class="plan-billing-feedback${state.feedback.error ? " is-error" : ""}" role="status">${escapeHtml(state.feedback.message)} · request_id: ${escapeHtml(state.feedback.requestId ?? "-")}</div>`;
}

/** 渲染经过状态层收敛的错误卡片，并使用固定后备文案。 */
function renderError(error, fallback) {
  return `<div class="plan-billing-error" role="alert"><strong>${escapeHtml(error?.message ?? fallback)}</strong><code>${escapeHtml(error?.code ?? "quota.unavailable")}</code><span>request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

/** 将固定错误对象转换为安全行内文本，保留请求标识供排障。 */
function renderErrorText(error) {
  return `${escapeHtml(error.message)} · ${escapeHtml(error.code)} · request_id: ${escapeHtml(error.requestId)}`;
}

/** 将额度类型映射为受控本地化标签，未知值保持可审计原值。 */
function quotaLabel(value, translate) {
  return translate(`quota.type.${value}`, QUOTA_LABELS[value] ?? value);
}

/** 将套餐层级映射为受控本地化标签，不改变后端层级事实。 */
function tierLabel(value, translate) {
  return translate(`tier.${value}`, value === "trial" ? "试用" : value === "paid" ? "付费" : "免费");
}

/** 将套餐状态映射为受控本地化标签，不推断额外状态。 */
function statusLabel(value, translate) {
  return translate(`status.${value}`, value === "active" ? "启用" : "暂停");
}

/** 将试用状态映射为受控本地化标签，不计算新的试用周期。 */
function trialLabel(value, translate) {
  const fallbacks = { not_started: "未开始", active: "进行中", expired: "已结束" };
  return translate(`trial.${value}`, fallbacks[value] ?? value);
}

/** 将额度桶状态映射为受控本地化标签，不修改额度可用性。 */
function bucketStatusLabel(value, translate) {
  const fallbacks = { active: "可用", exhausted: "已用尽", expired: "已到期", suspended: "已暂停" };
  return translate(`quota.status.${value}`, fallbacks[value] ?? value);
}

/** 构造带固定后备文案的翻译函数，缺少词条时不返回空文本。 */
function translator(i18n) {
  return (key, fallback) => i18n?.t?.(key, {}, fallback) ?? fallback;
}

/** 将后端时间转换为当前区域显示文本，非法时间显示占位符。 */
function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString([], { hour12: false }) : "-";
}

/** 将有效 UTC 时间转换为本地表单值，非法时间不会进入输入框。 */
function toDateTimeLocal(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  /** 将本地日期字段补齐为两位数字，仅服务当前表单格式化。 */
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 转义进入 HTML 正文的动态值，阻断标签与属性边界注入。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 在正文转义基础上处理模板属性分隔符，供受控属性值使用。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
