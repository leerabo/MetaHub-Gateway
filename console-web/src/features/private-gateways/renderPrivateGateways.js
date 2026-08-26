/** PRD-120 safe Workspace deployment projection and one-use registration reveal. */

import { privateGatewayPageOwner } from "./privateGatewayPageState.js";

export function renderPrivateGateways(state, authContext, consoleShellState) {
  const owner = privateGatewayPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  return `
    <div class="private-gateway-workspace" data-private-gateway-root>
      <header class="private-gateway-toolbar">
        <div><p class="eyebrow">PRD-120 · Customer-owned runtime</p><h3 id="private-gateways-title">私有 Gateway</h3><p>设备身份、短期证书与连接状态</p></div>
        <div class="private-gateway-commands">
          <button type="button" class="private-gateway-icon" data-private-gateway-refresh title="刷新" aria-label="刷新私有 Gateway"${state.busy ? " disabled" : ""}>&#8635;</button>
          ${owner.canManage ? `<button type="button" class="primary" data-private-gateway-open${state.busy ? " disabled" : ""}>创建 Deployment</button>` : ""}
        </div>
      </header>
      ${renderFeedback(state.feedback)}
      ${renderList(state, owner)}
      ${renderDialog(state, owner)}
    </div>`;
}

function renderList(state, owner) {
  if (state.state === "loading") return '<div class="private-gateway-empty" role="status">正在读取私有 Gateway 状态</div>';
  if (state.state === "error") return renderError(state.error);
  if (!state.deployments.length) return '<div class="private-gateway-empty">尚未创建私有 Gateway Deployment</div>';
  return `<div class="private-gateway-list">${state.deployments.map((item) => renderDeployment(item, state, owner)).join("")}</div>`;
}

function renderDeployment(item, state, owner) {
  const certificate = item.current_certificate;
  const active = !new Set(["revoked", "retired"]).has(item.status);
  return `
    <article class="private-gateway-deployment">
      <header>
        <div><span class="private-gateway-status tone-${escapeAttribute(item.health_code)}">${healthLabel(item.health_code)}</span><h4>${escapeHtml(item.display_name)}</h4><code>${escapeHtml(item.deployment_id)}</code></div>
        <dl><div><dt>区域</dt><dd>${escapeHtml(item.region_code)}</dd></div><div><dt>模式</dt><dd>${modeLabel(item.mode)}</dd></div><div><dt>Revision</dt><dd>${integer(item.revision)}</dd></div></dl>
      </header>
      <div class="private-gateway-facts">
        ${fact("设备", item.identity_generation ? `generation ${integer(item.identity_generation)}` : "待注册")}
        ${fact("Agent", item.agent_version ?? "-")}
        ${fact("最近连接", formatTime(item.last_connected_at))}
        ${fact("证书到期", formatTime(item.certificate_expires_at))}
        ${fact("证书状态", certificate ? certificateStatusLabel(certificate.status) : "未签发")}
        ${fact("吊销水位", integer(item.revocation_generation))}
      </div>
      ${renderTimeline(item.recent_events)}
      ${owner.canManage && active ? `
        <div class="private-gateway-actions">
          <label><span>吊销原因</span><select data-private-gateway-reason="${escapeAttribute(item.deployment_id)}"${state.busy ? " disabled" : ""}>
            <option value="owner_requested">主动停用</option><option value="device_lost">设备遗失</option><option value="security_incident">安全事件</option><option value="deployment_retired">Deployment 退役</option>
          </select></label>
          <button type="button" class="danger" data-private-gateway-revoke="${escapeAttribute(item.deployment_id)}"${state.busy ? " disabled" : ""}>${state.revokingId === item.deployment_id ? "正在吊销" : "吊销"}</button>
        </div>` : ""}
    </article>`;
}

function renderTimeline(events) {
  if (!events.length) return '<div class="private-gateway-timeline-empty">尚无设备事件</div>';
  return `<ol class="private-gateway-timeline" aria-label="最近设备事件">${events.map((event) => `
    <li><span class="tone-${escapeAttribute(event.outcome_code)}"></span><div><strong>${eventLabel(event.event_type)}</strong><small>${escapeHtml(event.reason_code)} · generation ${integer(event.generation)}</small></div><time>${formatTime(event.occurred_at)}</time></li>
  `).join("")}</ol>`;
}

function renderDialog(state, owner) {
  if (!owner.canManage || !state.dialog?.open) return "";
  const registration = state.registration;
  const submitting = state.dialog.state === "submitting";
  return `
    <dialog class="private-gateway-dialog" data-private-gateway-dialog aria-labelledby="private-gateway-dialog-title" aria-busy="${submitting}">
      <header><div><h3 id="private-gateway-dialog-title">${registration ? "一次性注册材料" : "创建 Deployment"}</h3><span>${registration ? `有效至 ${formatTime(registration.expiresAt)}` : "绑定当前 Workspace"}</span></div><button type="button" class="private-gateway-icon" data-private-gateway-close title="关闭" aria-label="关闭"${submitting ? " disabled" : ""}>&times;</button></header>
      ${state.dialog.error ? renderError(state.dialog.error) : ""}
      ${registration ? renderRegistration(registration) : renderCreateForm(submitting)}
    </dialog>`;
}

function renderCreateForm(submitting) {
  return `<form data-private-gateway-create autocomplete="off" novalidate>
    <div class="private-gateway-form-grid">
      <label><span>显示名称</span><input name="display_name" maxlength="120" minlength="2" required${submitting ? " disabled" : ""}></label>
      <label><span>区域</span><input name="region_code" value="cn-east-1" maxlength="40" pattern="[a-z0-9][a-z0-9-]+" required${submitting ? " disabled" : ""}></label>
    </div>
    <fieldset class="private-gateway-segmented"${submitting ? " disabled" : ""}><legend>连接模式</legend><label><input type="radio" name="mode" value="connected" checked><span>Connected</span></label><label><input type="radio" name="mode" value="isolated"><span>Isolated</span></label></fieldset>
    <label class="private-gateway-ttl"><span>注册窗口</span><select name="token_ttl_seconds"${submitting ? " disabled" : ""}><option value="600">10 分钟</option><option value="300">5 分钟</option><option value="60">1 分钟</option></select></label>
    <p class="private-gateway-form-error" data-private-gateway-form-error role="alert"></p>
    <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-close${submitting ? " disabled" : ""}>取消</button><button type="submit" class="primary"${submitting ? " disabled" : ""}>${submitting ? "正在创建" : "创建"}</button></div>
  </form>`;
}

function renderRegistration(value) {
  return `<section class="private-gateway-registration" data-private-gateway-one-use>
    <dl><div><dt>Deployment ID</dt><dd><code>${escapeHtml(value.deploymentId)}</code></dd></div><div><dt>Registration token</dt><dd><code>${escapeHtml(value.token)}</code></dd></div><div><dt>Challenge</dt><dd><code>${escapeHtml(value.challenge)}</code></dd></div></dl>
    <div class="private-gateway-dialog-actions"><button type="button" data-private-gateway-close>关闭并清除</button><button type="button" class="primary" data-private-gateway-copy>复制并清除</button></div>
  </section>`;
}

function renderFeedback(feedback) {
  if (!feedback) return "";
  const labels = {
    "edge.registration_created": "Deployment 已创建",
    "edge.registration_replayed": "已恢复 Deployment 结果",
    "edge.registration_copied": "注册材料已复制并从页面清除",
    "edge.deployment_revoked": "Deployment 已吊销",
  };
  if (feedback.kind === "error") return renderError(feedback);
  return `<div class="private-gateway-feedback" role="status"><strong>${escapeHtml(labels[feedback.code] ?? feedback.code)}</strong><span>request_id: ${escapeHtml(feedback.requestId ?? "-")}</span></div>`;
}

function renderError(error) {
  return `<div class="private-gateway-error" role="alert"><strong>${escapeHtml(error?.message ?? "私有 Gateway 请求失败")}</strong><span><code>${escapeHtml(error?.code ?? "edge.unavailable")}</code> · request_id: ${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function fact(label, value) { return `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`; }
function healthLabel(value) { return ({ not_registered: "待注册", healthy: "健康", certificate_expiring: "证书临期", revoked: "已吊销", offline: "离线", unknown: "未知" })[value] ?? "未知"; }
function modeLabel(value) { return value === "isolated" ? "隔离" : "联网"; }
function certificateStatusLabel(value) { return ({ active: "有效", draining: "轮换窗口", superseded: "已替换", revoked: "已吊销", expired: "已过期" })[value] ?? "未知"; }
function eventLabel(value) { return ({ "edge.registered": "设备注册", "edge.certificate_rotated": "证书轮换", "edge.revoked": "Deployment 吊销", "edge.authentication_failed": "认证失败" })[value] ?? value; }
function formatTime(value) { if (!value) return "-"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date); }
function integer(value) { return Number.isSafeInteger(value) ? String(value) : "-"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
