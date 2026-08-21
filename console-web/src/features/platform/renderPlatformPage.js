const ROLE_OPTIONS = [
  "platform_owner",
  "platform_ops",
  "platform_security",
  "platform_billing",
  "platform_support",
  "platform_verification",
  "platform_compliance",
  "platform_privacy",
  "release_manager",
  "platform_provider_engineer",
  "platform_channel",
  "platform_customer_success",
  "platform_growth",
];

export function renderPlatformGate(container, state = {}) {
  container.innerHTML = `
    <div class="platform-gate">
      <header class="platform-gate-brand">
        <span class="platform-mark" aria-hidden="true">M</span>
        <div><strong>MetaHub</strong><span>Platform Operations</span></div>
      </header>
      <section class="platform-gate-panel" aria-labelledby="platform-gate-title">
        <p>平台身份</p>
        <h1 id="platform-gate-title">进入工作台</h1>
        ${feedback(state)}
        <form data-platform-session-form autocomplete="off">
          <label><span>动态验证码</span><input name="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required autofocus /></label>
          <button class="platform-primary" type="submit">验证并进入</button>
        </form>
        <a href="/identity.html">返回账号安全</a>
      </section>
    </div>`;
}

export function renderPlatformWorkspace(container, state) {
  const context = state.session?.context;
  const operator = context?.operator ?? {};
  const permissions = new Set(context?.permissions ?? []);
  container.innerHTML = `
    <div class="platform-shell">
      <header class="platform-topbar">
        <a class="platform-wordmark" href="/platform.html"><span class="platform-mark" aria-hidden="true">M</span><strong>MetaHub</strong></a>
        <div class="platform-operator-summary"><span>${escapeHtml(operator.display_name ?? "-")}</span><strong>${escapeHtml((operator.roles ?? []).map(roleLabel).join(" · "))}</strong></div>
        <div class="platform-topbar-actions">
          <button class="platform-icon-button" type="button" data-platform-action="refresh" title="刷新" aria-label="刷新">↻</button>
          <a href="/identity.html">账号安全</a>
        </div>
      </header>
      <div class="platform-body">
        <aside class="platform-sidebar" aria-label="平台导航">
          ${(state.navigation ?? []).map((item) => navItem(item, state.view)).join("")}
        </aside>
        <section class="platform-content" aria-live="polite">
          ${feedback(state)}
          ${pendingConfirmation(state)}
          ${renderView(state, permissions)}
        </section>
      </div>
    </div>`;
}

function renderView(state, permissions) {
  if (state.view === "overview") return overviewView(state);
  if (state.view === "operators") return operatorsView(state, permissions);
  if (state.view === "support") return supportView(state, permissions);
  if (state.view === "audit") return auditView(state);
  return domainPlaceholderView(state);
}

function overviewView(state) {
  const operatorCount = state.operators?.total ?? 0;
  const grants = state.grants?.grants ?? [];
  const active = grants.filter((item) => item.status === "active").length;
  const pending = grants.filter((item) => item.status === "pending_approvals").length;
  return `
    <header class="platform-page-heading"><div><p>运营概览</p><h1>平台工作台</h1></div><span>会话至 ${formatTime(state.session?.context?.expires_at)}</span></header>
    <section class="platform-metrics" aria-label="平台摘要">
      ${metric("操作员", operatorCount, "已纳入平台 IAM")}
      ${metric("有效支持授权", active, "短时诊断访问")}
      ${metric("待审批", pending, "客户与平台双域")}
    </section>
    <section class="platform-band"><div class="platform-section-heading"><div><p>权限</p><h2>当前授权</h2></div></div><div class="platform-chip-row">${(state.session?.context?.operator?.roles ?? []).map((role) => `<span>${escapeHtml(roleLabel(role))}</span>`).join("")}</div></section>`;
}

function operatorsView(state, permissions) {
  const operators = state.operators?.operators ?? [];
  return `
    <header class="platform-page-heading"><div><p>Platform IAM</p><h1>操作员</h1></div><span>${operators.length} 个账号</span></header>
    ${permissions.has("platform.operator.manage") ? operatorForm() : ""}
    <section class="platform-band"><div class="platform-table-wrap"><table><thead><tr><th>操作员</th><th>角色</th><th>状态</th><th>Revision</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${operators.map(operatorRow).join("") || emptyRow("暂无操作员")}</tbody></table></div></section>`;
}

function operatorForm() {
  return `
    <section class="platform-band platform-form-band"><div class="platform-section-heading"><div><p>委派</p><h2>新增操作员</h2></div></div>
      <form class="platform-inline-form" data-operator-form autocomplete="off">
        <label><span>Global Account ID</span><input name="accountId" pattern="gac_[0-9a-f]{32}" maxlength="36" required /></label>
        <fieldset><legend>角色</legend><div class="platform-role-grid">${ROLE_OPTIONS.map((role) => `<label><input type="checkbox" name="roles" value="${role}" /><span>${escapeHtml(roleLabel(role))}</span></label>`).join("")}</div></fieldset>
        <button class="platform-primary" type="submit">创建操作员</button>
      </form>
    </section>`;
}

function operatorRow(operator) {
  const disabled = operator.status !== "active";
  return `<tr><td><strong>${escapeHtml(operator.display_name)}</strong><small>${escapeHtml(operator.operator_id)}</small></td><td>${(operator.roles ?? []).map((role) => `<span class="platform-role">${escapeHtml(roleLabel(role))}</span>`).join(" ")}</td><td>${status(operator.status)}</td><td>${number(operator.revision)}</td><td><button class="platform-icon-button" type="button" data-platform-action="toggle-operator" data-operator-id="${escapeAttribute(operator.operator_id)}" data-revision="${number(operator.revision)}" data-status="${disabled ? "active" : "disabled"}" data-roles="${escapeAttribute((operator.roles ?? []).join(","))}" title="${disabled ? "启用" : "停用"}" aria-label="${disabled ? "启用" : "停用"}">${disabled ? "▷" : "×"}</button></td></tr>`;
}

function supportView(state, permissions) {
  const grants = state.grants?.grants ?? [];
  return `
    <header class="platform-page-heading"><div><p>Controlled Support</p><h1>支持授权</h1></div><span>${grants.length} 条记录</span></header>
    ${permissions.has("platform.support.request") ? supportForms(permissions) : ""}
    ${state.diagnostics ? diagnostics(state.diagnostics) : ""}
    <section class="platform-band"><div class="platform-table-wrap"><table><thead><tr><th>Workspace</th><th>状态</th><th>待审批</th><th>到期</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${grants.map((grant) => grantRow(grant, permissions)).join("") || emptyRow("暂无支持授权")}</tbody></table></div></section>`;
}

function supportForms(permissions) {
  return `<section class="platform-band platform-form-split">
    <form data-support-grant-form autocomplete="off"><div class="platform-section-heading"><div><p>JIT Grant</p><h2>申请诊断访问</h2></div></div><label><span>Workspace ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required /></label><label><span>原因代码</span><select name="reasonCode"><option value="workspace_diagnostics">Workspace diagnostics</option><option value="incident_response">Incident response</option></select></label><label><span>时限</span><input name="durationMinutes" type="number" min="1" max="30" value="15" required /></label><button class="platform-primary" type="submit">提交申请</button></form>
    ${permissions.has("platform.break_glass.use") ? `<form data-break-glass-form autocomplete="off"><div class="platform-section-heading"><div><p>Emergency</p><h2>Break-glass</h2></div></div><label><span>Workspace ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required /></label><label><span>原因代码</span><select name="reasonCode"><option value="incident_containment">Incident containment</option><option value="service_recovery">Service recovery</option></select></label><label><span>动态验证码</span><input name="totpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认启动紧急访问</span></label><button class="platform-danger" type="submit">启动 Break-glass</button></form>` : ""}
  </section>`;
}

function grantRow(grant, permissions) {
  const canApprove = permissions.has("platform.support.approve") && grant.status === "pending_approvals" && grant.missing_approvals?.includes("platform");
  const canOpen = grant.status === "active";
  const canRevoke = ["active", "pending_approvals"].includes(grant.status);
  return `<tr><td><strong>${escapeHtml(grant.workspace_id)}</strong><small>${escapeHtml(grant.grant_id)}</small></td><td>${status(grant.status)}</td><td>${escapeHtml((grant.missing_approvals ?? []).map(domainLabel).join("、") || "无")}</td><td>${formatTime(grant.expires_at)}</td><td><div class="platform-row-actions">${canApprove ? `<button type="button" data-platform-action="approve-grant" data-grant-id="${escapeAttribute(grant.grant_id)}">批准</button>` : ""}${canOpen ? `<button type="button" data-platform-action="open-support" data-grant-id="${escapeAttribute(grant.grant_id)}">打开诊断</button>` : ""}${canRevoke ? `<button class="is-danger" type="button" data-platform-action="prepare-revoke" data-grant-id="${escapeAttribute(grant.grant_id)}">撤销</button>` : ""}</div></td></tr>`;
}

function diagnostics(value) {
  return `<section class="platform-band platform-diagnostics"><div class="platform-section-heading"><div><p>SupportSession</p><h2>Workspace 诊断</h2></div><span>${status(value.operational_status)}</span></div><dl><div><dt>Workspace</dt><dd>${escapeHtml(value.workspace_id)}</dd></div><div><dt>Region</dt><dd>${escapeHtml(value.region_code ?? "-")}</dd></div><div><dt>Provisioning</dt><dd>${escapeHtml(value.provisioning_status)}</dd></div><div><dt>Health</dt><dd>${escapeHtml(value.health_band ?? "-")}</dd></div><div><dt>Active memberships</dt><dd>${number(value.active_memberships)}</dd></div></dl></section>`;
}

function auditView(state) {
  const entries = state.audit?.entries ?? [];
  return `<header class="platform-page-heading"><div><p>Immutable Audit</p><h1>平台审计</h1></div><span>${entries.length} 条记录</span></header><section class="platform-band"><ol class="platform-timeline">${entries.map((entry) => `<li><time>${formatTime(entry.occurred_at)}</time><div><strong>${escapeHtml(actionLabel(entry.action))}</strong><span>${escapeHtml(entry.object_type)} · ${escapeHtml(entry.object_id)}</span><small>${escapeHtml(entry.reason_code)}</small></div></li>`).join("") || '<li class="platform-empty">暂无审计记录</li>'}</ol></section>`;
}

function domainPlaceholderView(state) {
  const item = (state.navigation ?? []).find((candidate) => candidate.id === state.view);
  return `
    <header class="platform-page-heading"><div><p>Platform domain</p><h1>${escapeHtml(item?.label ?? "平台域")}</h1></div></header>
    <section class="platform-band platform-domain-empty"><strong>暂无数据</strong></section>`;
}

function pendingConfirmation(state) {
  if (!state.pendingAction) return "";
  return `<section class="platform-confirm" role="alert"><div><strong>撤销支持授权</strong><span>${escapeHtml(state.pendingAction.id)}</span></div><div><button type="button" data-platform-action="cancel-confirm">取消</button><button class="platform-danger" type="button" data-platform-action="confirm-revoke" data-grant-id="${escapeAttribute(state.pendingAction.id)}">确认撤销</button></div></section>`;
}

function feedback(state) {
  if (state.error) return `<div class="platform-feedback is-error" role="alert"><strong>${escapeHtml(state.error.message)}</strong><span>Request ID ${escapeHtml(state.error.requestId ?? "-")}</span></div>`;
  if (state.feedback) return `<div class="platform-feedback is-ok" role="status">${escapeHtml(state.feedback)}</div>`;
  if (state.loading) return '<div class="platform-feedback" role="status">正在刷新…</div>';
  return "";
}

function navItem(item, current) {
  const view = item.id;
  return `<button type="button" data-platform-view="${escapeAttribute(view)}" class="${view === current ? "is-active" : ""}"><span aria-hidden="true">${navSymbol(view)}</span>${escapeHtml(item.label)}</button>`;
}

function metric(label, value, detail) {
  return `<div><span>${escapeHtml(label)}</span><strong>${number(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = /active|healthy|ready|reviewed/u.test(text) ? "is-ok" : /pending|watch/u.test(text) ? "is-warn" : /revoked|expired|disabled|critical/u.test(text) ? "is-danger" : "";
  return `<span class="platform-status ${kind}">${escapeHtml(statusLabel(text))}</span>`;
}

function roleLabel(value) { return String(value ?? "").replace(/^platform_/u, "").replaceAll("_", " "); }
function domainLabel(value) { return value === "workspace" ? "客户" : value === "platform" ? "平台" : value; }
function statusLabel(value) { return String(value).replaceAll("_", " "); }
function actionLabel(value) { return String(value).replaceAll("_", " ").replaceAll(".", " / "); }
function navSymbol(view) { return ({ overview: "⌂", operators: "◎", support: "◇", audit: "≡" })[view] ?? "·"; }
function emptyRow(label) { return `<tr><td colspan="5" class="platform-empty">${escapeHtml(label)}</td></tr>`; }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date); }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
