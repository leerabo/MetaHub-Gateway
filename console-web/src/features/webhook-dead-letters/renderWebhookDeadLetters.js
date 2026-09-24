/**
 * 模块职责：把 PAGE-083 的安全死信 DTO 渲染为筛选、列表、状态反馈和归档确认界面。
 * 安全边界：只逐字段读取页面状态白名单；所有名称、ID、错误码和时间均经过 HTML/属性转义，
 * 不渲染 payload、Endpoint URL、tenant、幂等键、Secret、签名、claim、lease 或接收端正文。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { localizeConsoleStaticHtml } from "../../i18n/localizeConsoleStaticHtml.js";

// 筛选选项冻结死信状态机公开枚举，页面不能提交未知状态探测其他租户记录。
const STATUS_OPTIONS = [
  ["all", "全部"],
  ["dead_letter", "待处理"],
  ["replay_pending", "重放中"],
  ["replayed", "已恢复"],
  ["archived", "已归档"],
  ["expired", "已过期"],
];
// 状态标签由冻结选项派生，避免筛选值与列表展示出现两套含义。
const STATUS_LABELS = Object.fromEntries(STATUS_OPTIONS);

/**
 * 渲染 Webhook 死信工作区。
 *
 * @param {object|null} state 页面安全快照。
 * @param {object|null} authContext 当前认证上下文。
 * @returns {string} manager 可见 HTML；viewer 或身份缺失时返回空串。
 *
 * 前端角色判断只用于实现 viewer 零 DOM，真实 GET/POST 仍必须由后端权限守卫独立拒绝。
 */
export function renderWebhookDeadLetters(state, authContext, i18n = null) {
  if (!hasPermission(authContext, PERMISSIONS.WEBHOOK_MANAGE)) {
    return "";
  }
  const view = normalizeView(state);
  return localizeConsoleStaticHtml(`
    <div class="webhook-dead-letter-page" data-state="${escapeAttribute(view.state)}">
      ${renderHeading(view)}
      ${renderFeedback(view)}
      ${renderFilters(view)}
      ${renderContent(view)}
      ${renderPager(view)}
      ${renderArchiveDialog(view.archiveDialog)}
    </div>
  `, i18n);
}

/** 渲染标题、可见数量和当前请求链路；request_id 是故障排查证据而不是权限依据。 */
function renderHeading(view) {
  return `
    <header class="webhook-dead-letter-heading">
      <div>
        <h2 id="webhook-dead-letters-title">Webhook 死信</h2>
        <p>${view.deadLetterEvents.length} 条可见记录 · request_id: <code>${escapeHtml(view.requestId)}</code></p>
      </div>
      <button
        type="button"
        class="webhook-dead-letter-icon-button"
        data-webhook-dead-letter-refresh
        title="刷新死信列表"
        aria-label="刷新死信列表"
      ><span aria-hidden="true">↻</span></button>
    </header>
  `;
}

/** 渲染列表错误、动作反馈和轮询封顶状态；所有分支保留公开 request_id。 */
function renderFeedback(view) {
  const entries = [];
  if (view.error) {
    entries.push(`
      <div class="webhook-dead-letter-notice is-error" role="alert">
        <strong>${escapeHtml(view.error.message)}</strong>
        <span>code: <code>${escapeHtml(view.error.code)}</code> · request_id: <code>${escapeHtml(view.error.requestId)}</code></span>
      </div>
    `);
  }
  if (view.feedback) {
    entries.push(`
      <div class="webhook-dead-letter-notice is-${view.feedback.kind === "success" ? "success" : "error"}" role="status" aria-live="polite">
        <strong>${escapeHtml(view.feedback.message)}</strong>
        ${view.feedback.code ? `<span>code: <code>${escapeHtml(view.feedback.code)}</code></span>` : ""}
        <span>request_id: <code>${escapeHtml(view.feedback.requestId)}</code></span>
      </div>
    `);
  }
  if (view.pollExhausted) {
    entries.push(`
      <div class="webhook-dead-letter-notice is-neutral" role="status">
        <strong>重放状态轮询已暂停</strong>
        <span>当前记录仍在处理中，可使用刷新按钮重新确认。</span>
      </div>
    `);
  }
  return entries.join("");
}

/**
 * 渲染状态分段控件和两个当前页安全摘要菜单。
 * 菜单值只从当前页死信摘要派生并用于收窄当前租户查询；tenant_id 和 cursor 从不进入表单。
 */
function renderFilters(view) {
  return `
    <form id="webhook-dead-letter-filter-form" class="webhook-dead-letter-filters">
      <div class="webhook-dead-letter-segments" role="group" aria-label="死信状态">
        ${STATUS_OPTIONS.map(([value, label]) => `
          <button
            type="button"
            data-webhook-dead-letter-status="${escapeAttribute(value)}"
            aria-pressed="${view.filters.status === value ? "true" : "false"}"
            class="${view.filters.status === value ? "is-active" : ""}"
          >${escapeHtml(label)}</button>
        `).join("")}
      </div>
      <label>
        <span>事件类型</span>
        <select name="eventType">
          <option value="">全部事件</option>
          ${view.eventTypes.map((eventType) => `
            <option value="${escapeAttribute(eventType)}" ${selected(view.filters.eventType, eventType)}>${escapeHtml(eventType)}</option>
          `).join("")}
        </select>
      </label>
      <label>
        <span>Endpoint</span>
        <select name="webhookEndpointId">
          <option value="">全部 Endpoint</option>
          ${view.webhookEndpoints.map((endpoint) => `
            <option value="${escapeAttribute(endpoint.webhook_endpoint_id)}" ${selected(view.filters.webhookEndpointId, endpoint.webhook_endpoint_id)}>
              ${escapeHtml(endpoint.name)}
            </option>
          `).join("")}
        </select>
      </label>
      <button type="submit" class="webhook-dead-letter-apply">应用筛选</button>
    </form>
  `;
}

/** 根据 loading/error/empty/ok 状态渲染主内容，partial 状态仍保留最近安全快照。 */
function renderContent(view) {
  if (view.state === "loading") {
    return `<div class="webhook-dead-letter-empty" role="status">正在读取 Webhook 死信。</div>`;
  }
  // 初次读取失败时没有可保留证据，只显示统一错误状态，禁止拼接后端原始异常。
  if (view.state === "error" && view.deadLetterEvents.length === 0) {
    return `<div class="webhook-dead-letter-empty is-error">死信列表暂不可用。</div>`;
  }
  if (view.deadLetterEvents.length === 0) {
    return `<div class="webhook-dead-letter-empty">当前筛选下没有 Webhook 死信。</div>`;
  }
  return `
    <div class="webhook-dead-letter-table-wrap">
      <div class="webhook-dead-letter-table" role="table" aria-label="Webhook 死信列表">
        <div class="webhook-dead-letter-row webhook-dead-letter-head" role="row">
          <span role="columnheader">Endpoint</span>
          <span role="columnheader">事件</span>
          <span role="columnheader">死信时间</span>
          <span role="columnheader">失败</span>
          <span role="columnheader">投递</span>
          <span role="columnheader">状态</span>
          <span role="columnheader">最近重放</span>
          <span role="columnheader">操作</span>
        </div>
        ${view.deadLetterEvents.map((event) => renderRow(event, view)).join("")}
      </div>
    </div>
  `;
}

/** 渲染单条安全死信摘要；命令可见性完全由 allowed_actions 决定。 */
function renderRow(event, view) {
  const busy = view.busyEventId === event.dead_letter_event_id;
  const isPending = event.status === "replay_pending";
  const canReplay = event.allowed_actions.includes("replay") && !busy && !isPending;
  const canArchive = event.allowed_actions.includes("archive") && !busy && !isPending;
  return `
    <div class="webhook-dead-letter-row" role="row" data-dead-letter-id="${escapeAttribute(event.dead_letter_event_id)}">
      <span role="cell" data-label="Endpoint" class="webhook-dead-letter-identity">
        <strong>${escapeHtml(event.webhook_endpoint_name || "未命名 Endpoint")}</strong>
        <code>${escapeHtml(event.webhook_endpoint_id || "-")}</code>
      </span>
      <span role="cell" data-label="事件" class="webhook-dead-letter-identity">
        <strong>${escapeHtml(event.event_type || "-")}</strong>
        <code>${escapeHtml(event.event_id || "-")}</code>
      </span>
      <span role="cell" data-label="死信时间" class="webhook-dead-letter-times">
        <time datetime="${escapeAttribute(event.failed_at)}">${escapeHtml(formatTime(event.failed_at))}</time>
        <small>到期 ${escapeHtml(formatTime(event.expires_at))}</small>
      </span>
      <span role="cell" data-label="失败" class="webhook-dead-letter-failure">
        <code>${escapeHtml(event.failure_error_code || "-")}</code>
        <small>${event.failure_http_status ?? "无 HTTP 状态"}</small>
      </span>
      <span role="cell" data-label="投递">
        <strong>${event.attempt_count}</strong>
        <small>${escapeHtml(latestAttemptLabel(event.latest_attempt))}</small>
      </span>
      <span role="cell" data-label="状态">
        <mark class="webhook-dead-letter-status status-${escapeAttribute(event.status)}">
          ${escapeHtml(STATUS_LABELS[event.status] ?? event.status)}
        </mark>
      </span>
      <span role="cell" data-label="最近重放" class="webhook-dead-letter-replay-summary">
        ${renderReplaySummary(event)}
      </span>
      <span role="cell" data-label="操作" class="webhook-dead-letter-actions">
        ${canReplay ? `
          <button
            type="button"
            data-webhook-dead-letter-replay="${escapeAttribute(event.dead_letter_event_id)}"
            title="重放此死信"
          ><span aria-hidden="true">↻</span><span>重放</span></button>
        ` : ""}
        ${canArchive ? `
          <button
            type="button"
            data-webhook-dead-letter-archive="${escapeAttribute(event.dead_letter_event_id)}"
            title="归档此死信"
          ><span aria-hidden="true">□</span><span>归档</span></button>
        ` : ""}
        ${busy ? `<span class="webhook-dead-letter-busy" role="status">${view.busyAction === "archive" ? "归档中" : "提交中"}</span>` : ""}
        ${!canReplay && !canArchive && !busy ? `<span class="webhook-dead-letter-no-action">-</span>` : ""}
      </span>
    </div>
  `;
}

/** 渲染 replay delivery、安全错误码和时间，不展示幂等键或 worker 字段。 */
function renderReplaySummary(event) {
  if (!event.current_replay_delivery_id && !event.last_replay_error_code) {
    return `<span>-</span>`;
  }
  return `
    ${event.current_replay_delivery_id ? `<code>${escapeHtml(event.current_replay_delivery_id)}</code>` : ""}
    ${event.last_replay_error_code ? `<small>${escapeHtml(event.last_replay_error_code)}</small>` : ""}
    ${event.replayed_at || event.replay_requested_at
      ? `<time datetime="${escapeAttribute(event.replayed_at || event.replay_requested_at)}">${escapeHtml(formatTime(event.replayed_at || event.replay_requested_at))}</time>`
      : ""}
  `;
}

/** 渲染 opaque cursor 前后翻页命令；cursor 原文从不进入 data 属性。 */
function renderPager(view) {
  if (view.state === "loading" || (view.state === "error" && view.deadLetterEvents.length === 0)) {
    return "";
  }
  return `
    <nav class="webhook-dead-letter-pager" aria-label="死信分页">
      <button type="button" id="webhook-dead-letter-prev" ${view.canPrevious ? "" : "disabled"}>上一页</button>
      <span>每页最多 ${view.filters.limit} 条</span>
      <button type="button" id="webhook-dead-letter-next" ${view.canNext ? "" : "disabled"}>下一页</button>
    </nav>
  `;
}

/**
 * 渲染原生 dialog 归档确认。
 * 原生对话框提供 Escape、键盘与模态语义；绑定层在关闭后恢复原按钮焦点。
 */
function renderArchiveDialog(archiveDialog) {
  if (!archiveDialog?.event) {
    return "";
  }
  const event = archiveDialog.event;
  return `
    <dialog class="webhook-dead-letter-dialog" id="webhook-dead-letter-archive-dialog" aria-labelledby="webhook-dead-letter-dialog-title">
      <form method="dialog" id="webhook-dead-letter-archive-form">
        <header>
          <h3 id="webhook-dead-letter-dialog-title">归档死信</h3>
          <button type="button" data-webhook-dead-letter-archive-cancel aria-label="关闭归档确认" title="关闭">×</button>
        </header>
        <dl>
          <div><dt>事件类型</dt><dd>${escapeHtml(event.event_type || "-")}</dd></div>
          <div><dt>事件 ID</dt><dd><code>${escapeHtml(event.event_id || "-")}</code></dd></div>
          <div><dt>Endpoint</dt><dd>${escapeHtml(event.webhook_endpoint_name || event.webhook_endpoint_id || "-")}</dd></div>
        </dl>
        <div class="webhook-dead-letter-dialog-actions">
          <button type="button" data-webhook-dead-letter-archive-cancel>取消</button>
          <button type="submit" class="is-danger" autofocus>确认归档</button>
        </div>
      </form>
    </dialog>
  `;
}

/** 把外部状态规范化为渲染所需固定结构，避免模板访问未知属性。 */
function normalizeView(state) {
  return {
    state: ["loading", "ok", "partial", "error"].includes(state?.state) ? state.state : "loading",
    filters: {
      status: String(state?.filters?.status ?? "all"),
      eventType: String(state?.filters?.eventType ?? ""),
      webhookEndpointId: String(state?.filters?.webhookEndpointId ?? ""),
      limit: Number.isSafeInteger(state?.filters?.limit) ? state.filters.limit : 50,
    },
    deadLetterEvents: Array.isArray(state?.dead_letter_events) ? state.dead_letter_events : [],
    eventTypes: Array.isArray(state?.event_types) ? state.event_types : [],
    webhookEndpoints: Array.isArray(state?.webhook_endpoints) ? state.webhook_endpoints : [],
    requestId: String(state?.request_id ?? "-"),
    error: state?.error ?? null,
    feedback: state?.feedback ?? null,
    archiveDialog: state?.archiveDialog ?? null,
    busyEventId: String(state?.busyEventId ?? ""),
    busyAction: String(state?.busyAction ?? ""),
    canPrevious: Boolean(state?.canPrevious),
    canNext: Boolean(state?.canNext),
    pollExhausted: Boolean(state?.pollExhausted),
  };
}

/** 返回 select option 的固定 selected 属性；动态值不直接拼接属性。 */
function selected(current, value) {
  return current === value ? "selected" : "";
}

/** 把最新 attempt 摘要转换为简短运营标签。 */
function latestAttemptLabel(attempt) {
  if (!attempt) {
    return "暂无尝试摘要";
  }
  const status = attempt.http_status ?? attempt.error_code ?? attempt.status ?? "-";
  return `最近 ${status}`;
}

/** 以稳定 UTC 格式展示 ISO 时间；非法值原样安全展示，不猜测本地时区。 */
function formatTime(value) {
  const normalized = String(value ?? "");
  if (!normalized) {
    return "-";
  }
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime())
    ? normalized
    : parsed.toISOString().replace("T", " ").replace(".000Z", " UTC");
}

/** 对文本节点执行完整 HTML 转义，阻断名称、ID 和错误码中的标签注入。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值在 HTML 转义基础上额外处理反引号，避免模板属性边界混淆。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
