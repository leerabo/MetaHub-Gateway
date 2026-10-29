/** PRD-132 compact Workspace ticket queue, timeline, SLA, and diagnostic evidence UI. */

import { supportTicketPageOwner } from "./supportTicketPageState.js";

const STATUS_LABELS = Object.freeze({
  open: "待受理", triaged: "已分诊", waiting_customer: "待客户回复",
  waiting_platform: "待平台回复", resolved: "已解决", closed: "已关闭",
});
const SEVERITY_LABELS = Object.freeze({ p1: "P1 紧急", p2: "P2 高", p3: "P3 中", p4: "P4 低" });
const CATEGORY_LABELS = Object.freeze({
  account: "账号", billing: "账单", integration: "集成", migration: "迁移",
  reliability: "可靠性", security: "安全",
});
const ATTACHMENT_STATUS = Object.freeze({
  quarantined: "隔离扫描中", available: "扫描通过", rejected: "已拒绝", scan_timeout: "扫描超时",
});

export function renderSupportTickets(state, authContext, consoleShellState) {
  const owner = supportTicketPageOwner(authContext, consoleShellState);
  if (!owner) return "";
  const view = state ?? emptyView();
  const selected = view.items?.find((item) => item.ticket_id === view.selectedTicketId)
    ?? view.items?.[0] ?? null;
  return `<div class="support-tickets" data-support-ticket-root>
    <header class="support-ticket-heading">
      <div><p>平台支持</p><h2 id="support-tickets-title">平台支持</h2><span>向 MetaHub 提交问题并查看处理进度</span></div>
      <div class="support-ticket-heading-meta"><strong>${view.items?.length ?? 0}</strong><span>当前工单</span></div>
    </header>
    ${feedback(view.feedback)}
    ${owner.canCreate ? createForm(view.busy?.mutation) : ""}
    ${filters(view)}
    <div class="support-ticket-layout${selected ? "" : " is-empty"}">
      <aside class="support-ticket-list" aria-label="工单列表">
        ${renderTicketList(view.items ?? [], selected?.ticket_id, view.busy?.list)}
      </aside>
      <section class="support-ticket-detail" aria-live="polite">
        ${selected ? renderTicketDetail(selected, owner, view.busy?.mutation) : emptyDetail(owner)}
      </section>
    </div>
  </div>`;
}

function filters(view) {
  return `<form class="support-ticket-filters" data-support-ticket-filter-form>
    <label><span>状态</span><select name="status">
      ${option("", "全部状态", view.filters?.status)}
      ${Object.entries(STATUS_LABELS).map(([value, label]) => option(value, label, view.filters?.status)).join("")}
    </select></label>
    <label><span>优先级</span><select name="severity">
      ${option("", "全部优先级", view.filters?.severity)}
      ${Object.entries(SEVERITY_LABELS).map(([value, label]) => option(value, label, view.filters?.severity)).join("")}
    </select></label>
    <button type="submit"${view.busy?.list ? " disabled" : ""}>筛选</button>
    <button type="button" data-support-ticket-refresh${view.busy?.list ? " disabled" : ""} aria-label="刷新工单">刷新</button>
  </form>`;
}

function renderTicketList(items, selectedId, busy) {
  if (busy && !items.length) return '<p class="support-ticket-empty" role="status">正在读取工单</p>';
  if (!items.length) return '<p class="support-ticket-empty">当前筛选下没有工单</p>';
  return items.map((ticket) => `<button type="button" class="support-ticket-list-item${ticket.ticket_id === selectedId ? " is-active" : ""}" data-support-ticket-select="${escapeAttribute(ticket.ticket_id)}" aria-pressed="${ticket.ticket_id === selectedId}">
    <span class="support-ticket-list-top"><strong>${escapeHtml(ticket.subject)}</strong><b data-severity="${escapeAttribute(ticket.severity)}">${escapeHtml(ticket.severity.toUpperCase())}</b></span>
    <span>${escapeHtml(CATEGORY_LABELS[ticket.category] ?? ticket.category)} · ${escapeHtml(STATUS_LABELS[ticket.status] ?? ticket.status)}</span>
    <small>${formatTime(ticket.updated_at)}</small>
  </button>`).join("");
}

function renderTicketDetail(ticket, owner, busy) {
  const participant = ticket.reported_by_me || owner.canManage;
  return `<article data-support-ticket-detail>
    <header class="support-ticket-detail-heading">
      <div><span>${escapeHtml(SEVERITY_LABELS[ticket.severity] ?? ticket.severity)} · ${escapeHtml(CATEGORY_LABELS[ticket.category] ?? ticket.category)}</span><h3>${escapeHtml(ticket.subject)}</h3></div>
      <span class="support-ticket-status" data-status="${escapeAttribute(ticket.status)}">${escapeHtml(STATUS_LABELS[ticket.status] ?? ticket.status)}</span>
    </header>
    ${slaBand(ticket)}
    <section class="support-ticket-section"><div class="support-ticket-section-heading"><h4>消息时间线</h4><span>${ticket.messages.length} 条</span></div>
      <ol class="support-ticket-timeline">${ticket.messages.map(message).join("") || '<li class="support-ticket-empty">暂无消息</li>'}</ol>
      ${participant && !["closed"].includes(ticket.status) ? replyForm(ticket, busy) : ""}
    </section>
    <section class="support-ticket-section"><div class="support-ticket-section-heading"><h4>安全附件</h4><span>${ticket.attachments.length} 个</span></div>
      <div class="support-ticket-evidence">${ticket.attachments.map(attachment).join("") || '<p class="support-ticket-empty">尚未关联诊断证据</p>'}</div>
      ${participant && !["closed"].includes(ticket.status) ? attachmentForm(ticket, busy) : ""}
    </section>
    <section class="support-ticket-section"><div class="support-ticket-section-heading"><h4>升级记录</h4><span>${ticket.escalations.length} 次</span></div>
      <ol class="support-ticket-escalations">${ticket.escalations.map(escalation).join("") || '<li class="support-ticket-empty">暂无升级</li>'}</ol>
      ${participant && !["resolved", "closed"].includes(ticket.status) ? escalationForm(ticket, busy) : ""}
    </section>
    ${participant ? lifecycleActions(ticket, busy) : ""}
  </article>`;
}

function slaBand(ticket) {
  const sla = ticket.sla;
  const risk = Date.parse(sla.risk_at) <= Date.now() && sla.state === "running";
  return `<dl class="support-ticket-sla${risk ? " is-risk" : ""}">
    <div><dt>SLA 状态</dt><dd>${escapeHtml(slaState(sla.state))}</dd></div>
    <div><dt>风险阈值</dt><dd>${formatTime(sla.risk_at)}</dd></div>
    <div><dt>解决时限</dt><dd>${formatTime(sla.resolution_deadline_at)}</dd></div>
    <div><dt>升级次数</dt><dd>${sla.escalation_generation ?? 0}</dd></div>
  </dl>`;
}

function message(item) {
  const author = item.author_domain === "customer" ? "Workspace" : "平台支持";
  return `<li><div><strong>${author}</strong><span>${formatTime(item.created_at)}</span></div><p>${escapeHtml(item.body)}</p></li>`;
}

function attachment(item) {
  return `<div class="support-ticket-attachment" data-status="${escapeAttribute(item.status)}">
    <div><strong>${escapeHtml(ATTACHMENT_STATUS[item.status] ?? item.status)}</strong><span>${escapeHtml(intentLabel(item.intent_code))}</span></div>
    <small>${escapeHtml(item.media_type)} · ${formatBytes(item.size_bytes)}</small>
    ${item.scan_outcome ? `<span>扫描结果：${escapeHtml(scanOutcome(item.scan_outcome))}</span>` : ""}
  </div>`;
}

function escalation(item) {
  return `<li><strong>${escapeHtml(escalationSource(item.source_code))}</strong><span>${escapeHtml(item.reason_code)} · ${escapeHtml(item.status)}</span><time>${formatTime(item.created_at)}</time></li>`;
}

function replyForm(ticket, busy) {
  return `<form class="support-ticket-inline-form" data-support-ticket-reply-form autocomplete="off">
    <input name="ticket_id" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" />
    <input name="revision" type="hidden" value="${ticket.revision}" />
    <label><span>回复客户支持</span><textarea name="message" minlength="1" maxlength="8000" rows="3" required></textarea></label>
    <button type="submit"${busy ? " disabled" : ""}>发送回复</button>
  </form>`;
}

function attachmentForm(ticket, busy) {
  return `<form class="support-ticket-inline-form support-ticket-attachment-form" data-support-ticket-attachment-form autocomplete="off">
    <input name="ticket_id" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" />
    <input name="revision" type="hidden" value="${ticket.revision}" />
    <label><span>已上传文件 ID</span><input name="file_id" pattern="file_[0-9a-f]{32}" maxlength="37" required /></label>
    <label><span>证据用途</span><select name="intent_code"><option value="diagnostic_evidence">诊断证据</option><option value="log_bundle">日志包</option><option value="reproduction">复现材料</option></select></label>
    <label class="support-ticket-check"><input name="confirmed" type="checkbox" required /><span>确认进入隔离扫描</span></label>
    <button type="submit"${busy ? " disabled" : ""}>提交扫描</button>
  </form>`;
}

function escalationForm(ticket, busy) {
  return `<form class="support-ticket-inline-form support-ticket-escalation-form" data-support-ticket-escalation-form autocomplete="off">
    <input name="ticket_id" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" />
    <input name="revision" type="hidden" value="${ticket.revision}" />
    <label><span>升级原因</span><select name="reason_code"><option value="customer_impact">客户影响扩大</option><option value="security_impact">安全影响</option><option value="sla_attention">SLA 关注</option></select></label>
    <label class="support-ticket-check"><input name="confirmed" type="checkbox" required /><span>确认请求升级</span></label>
    <button type="submit"${busy ? " disabled" : ""}>请求升级</button>
  </form>`;
}

function lifecycleActions(ticket, busy) {
  const statusAction = ticket.status === "resolved" ? "open"
    : ticket.status === "closed" ? null : "resolved";
  return `<section class="support-ticket-section support-ticket-lifecycle">
    <div class="support-ticket-section-heading"><h4>工单状态</h4></div>
    ${statusAction ? `<form data-support-ticket-status-form>
      <input name="ticket_id" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" />
      <input name="revision" type="hidden" value="${ticket.revision}" />
      <input name="status" type="hidden" value="${statusAction}" />
      <button type="submit"${busy ? " disabled" : ""}>${statusAction === "open" ? "重新打开" : "标记已解决"}</button>
    </form>` : ""}
    ${ticket.status === "resolved" && !ticket.survey ? surveyForm(ticket, busy) : ""}
  </section>`;
}

function surveyForm(ticket, busy) {
  return `<form class="support-ticket-survey" data-support-ticket-survey-form autocomplete="off">
    <input name="ticket_id" type="hidden" value="${escapeAttribute(ticket.ticket_id)}" />
    <input name="revision" type="hidden" value="${ticket.revision}" />
    <label><span>满意度</span><select name="survey_rating"><option value="5">5 · 非常满意</option><option value="4">4 · 满意</option><option value="3">3 · 一般</option><option value="2">2 · 不满意</option><option value="1">1 · 非常不满意</option></select></label>
    <label><span>补充评价</span><textarea name="survey_comment" maxlength="2000" rows="2"></textarea></label>
    <button type="submit"${busy ? " disabled" : ""}>提交评价</button>
  </form>`;
}

function createForm(busy, open = false) {
  return `<details class="support-ticket-create"${open ? " open" : ""}><summary><span>创建工单</span><small>固定分类与优先级</small></summary><div class="support-ticket-create-body">
    <form data-support-ticket-create-form autocomplete="off">
      <label><span>分类</span><select name="category">${Object.entries(CATEGORY_LABELS).map(([value, label]) => option(value, label)).join("")}</select></label>
      <label><span>优先级</span><select name="severity">${Object.entries(SEVERITY_LABELS).map(([value, label]) => option(value, label, "p3")).join("")}</select></label>
      <label class="span-2"><span>主题</span><input name="subject" minlength="3" maxlength="200" required /></label>
      <label class="span-2"><span>问题说明</span><textarea name="message" minlength="1" maxlength="8000" rows="4" required></textarea></label>
      <label><span>关联对象</span><select name="related_kind" data-support-related-kind><option value="none">不关联</option><option value="request">请求</option><option value="incident">事件</option><option value="edge_deployment">边缘部署</option><option value="migration">迁移</option></select></label>
      <label><span>关联引用</span><input name="related_reference" minlength="3" maxlength="256" disabled /></label>
      <button type="submit"${busy ? " disabled" : ""}>创建工单</button>
    </form>
  </div></details>`;
}

function emptyDetail(owner) {
  return `<div class="support-ticket-empty support-ticket-empty-detail"><strong>没有可查看的工单</strong><span>${owner.canCreate ? "点击上方创建工单" : "当前权限为只读"}</span></div>`;
}
function feedback(value) {
  if (!value) return "";
  const labels = {
    "support.ticket_created": "工单已创建",
    "support.ticket_updated": "工单已更新",
    "support.attachment_quarantined": "附件已进入隔离扫描",
    "support.ticket_escalated": "升级请求已提交",
  };
  return `<div class="support-ticket-feedback is-${value.kind}" role="${value.kind === "error" ? "alert" : "status"}"><strong>${escapeHtml(labels[value.code] ?? (value.kind === "error" ? "操作未完成，请稍后重试" : "操作已完成"))}</strong></div>`;
}
function option(value, label, selected) { return `<option value="${escapeAttribute(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`; }
function slaState(value) { return ({ running: "计时中", paused_customer: "等待客户", paused_policy: "策略暂停", stopped: "已停止" })[value] ?? value; }
function intentLabel(value) { return ({ diagnostic_evidence: "诊断证据", log_bundle: "日志包", reproduction: "复现材料" })[value] ?? value; }
function scanOutcome(value) { return ({ clean: "安全", malicious: "恶意内容", structure_invalid: "结构无效", owner_changed: "文件所有权变化", timeout: "超时", unavailable: "不可用" })[value] ?? value; }
function escalationSource(value) { return ({ customer_requested: "客户请求", sla_at_risk: "SLA 风险", sla_breached: "SLA 超时", platform_manual: "平台升级" })[value] ?? value; }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(date); }
function formatBytes(value) { return value >= 1_048_576 ? `${(value / 1_048_576).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`; }
function emptyView() { return { items: [], selectedTicketId: null, filters: {}, busy: {}, requestId: "-" }; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value).replace(/`/gu, "&#96;"); }
