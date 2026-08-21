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
  if (state.view === "workspaces") return workspacesView(state, permissions);
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

function workspacesView(state, permissions) {
  if (state.workspaceDetail) return workspaceDetailView(state, permissions);
  const items = state.workspaces?.items ?? [];
  const filters = state.workspaceFilters ?? {};
  return `
    <header class="platform-page-heading"><div><p>Workspace operations</p><h1>Workspace 目录</h1></div><span>当前页 ${number(items.length)} 条</span></header>
    <section class="platform-band platform-workspace-tools">
      <form class="platform-filter-form" data-workspace-filter-form autocomplete="off">
        <label class="platform-filter-search"><span>精确搜索</span><input name="search" value="${escapeAttribute(filters.search ?? "")}" placeholder="Workspace ID 或组织 slug" maxlength="80" /></label>
        <label><span>状态</span><select name="operational_status">${filterOptions(filters.operational_status, [["", "全部"], ["active", "Active"], ["suspended", "Suspended"], ["deletion_pending", "Deletion pending"], ["deleted", "Deleted"], ["migration_conflict", "Migration conflict"]])}</select></label>
        <label><span>Region</span><input name="region" value="${escapeAttribute(filters.region ?? "")}" placeholder="cn-north-1" maxlength="40" /></label>
        <label><span>风险标签</span><select name="risk_tag">${filterOptions(filters.risk_tag, [["", "全部"], ["security_review", "Security review"], ["compliance_hold", "Compliance hold"], ["payment_attention", "Payment attention"], ["adoption_attention", "Adoption attention"]])}</select></label>
        <label><span>健康状态</span><select name="health_status">${filterOptions(filters.health_status, [["", "全部"], ["fresh", "Fresh"], ["stale", "Stale"], ["not_ready", "Not ready"], ["unavailable", "Unavailable"]])}</select></label>
        <label><span>健康风险</span><select name="health_risk">${filterOptions(filters.health_risk, [["", "全部"], ["healthy", "Healthy"], ["watch", "Watch"], ["at_risk", "At risk"], ["critical", "Critical"]])}</select></label>
        <label><span>类型</span><select name="kind">${filterOptions(filters.kind, [["", "全部"], ["organization", "Organization"], ["personal", "Personal"]])}</select></label>
        <button class="platform-primary" type="submit">筛选</button>
      </form>
    </section>
    <section class="platform-band platform-workspace-directory">
      <div class="platform-table-wrap"><table class="platform-workspace-table"><thead><tr><th>Workspace</th><th>状态</th><th>Region</th><th>健康</th><th>风险</th><th>成员 / Session</th><th><span class="platform-visually-hidden">操作</span></th></tr></thead><tbody>${items.map(workspaceRow).join("") || emptyRow("没有符合条件的 Workspace", 7)}</tbody></table></div>
      <div class="platform-pagination"><span>第 ${number((state.workspaceCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="workspace-previous" ${state.workspaceCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="workspace-next" ${state.workspaces?.has_more ? "" : "disabled"}>下一页</button></div></div>
    </section>`;
}

function workspaceRow(workspace) {
  const health = workspace.health ?? {};
  const tags = workspace.platform_risk_tags;
  return `<tr>
    <td data-label="Workspace"><strong>${escapeHtml(workspace.display_name)}</strong><small>${escapeHtml(workspace.workspace_id)}${workspace.slug ? ` · ${escapeHtml(workspace.slug)}` : ""}</small></td>
    <td data-label="状态">${status(workspace.operational_status)}<small>Rev ${number(workspace.operational_revision)}</small></td>
    <td data-label="Region"><strong>${escapeHtml(workspace.region_code ?? "未配置")}</strong><small>${escapeHtml(kindLabel(workspace.kind))}</small></td>
    <td data-label="健康">${status(health.score_band ?? health.status)}<small>${escapeHtml(statusLabel(health.status ?? "not_ready"))}</small></td>
    <td data-label="风险">${Array.isArray(tags) ? riskTagList(tags) : '<span class="platform-restricted">受限</span>'}</td>
    <td data-label="成员 / Session"><strong>${number(workspace.member_count)} / ${number(workspace.active_session_count)}</strong></td>
    <td data-label="操作"><div class="platform-row-actions"><button type="button" data-platform-action="open-workspace" data-workspace-id="${escapeAttribute(workspace.workspace_id)}">查看详情</button></div></td>
  </tr>`;
}

function workspaceDetailView(state, permissions) {
  const workspace = state.workspaceDetail.workspace;
  const health = state.workspaceHealth?.health ?? workspace.health ?? {};
  const actions = state.workspaceDetail.recent_actions ?? [];
  const events = state.workspaceEvents?.events ?? [];
  const canLifecycle = permissions.has("platform.workspace.lifecycle.manage");
  const riskOptions = allowedRiskOptions(permissions);
  const canContact = permissions.has("platform.workspace.lifecycle.manage")
    || permissions.has("platform.customer_success.read");
  return `
    <header class="platform-page-heading platform-workspace-heading"><div><button class="platform-back-button" type="button" data-platform-action="close-workspace">← 返回目录</button><p>${escapeHtml(kindLabel(workspace.kind))}</p><h1>${escapeHtml(workspace.display_name)}</h1><small>${escapeHtml(workspace.workspace_id)}${workspace.slug ? ` · ${escapeHtml(workspace.slug)}` : ""}</small></div><div class="platform-detail-status">${status(workspace.operational_status)}<span>Operational rev ${number(workspace.operational_revision)}</span></div></header>
    ${canLifecycle ? lifecycleCommands(workspace) : ""}
    <section class="platform-band platform-workspace-summary">
      <div class="platform-section-heading"><div><p>Operational facts</p><h2>运营摘要</h2></div><span>更新于 ${formatTime(workspace.updated_at)}</span></div>
      <dl class="platform-fact-grid">
        ${fact("Region", workspace.region_code ?? "未配置")}
        ${fact("Residency policy", workspace.residency_policy_version ?? "未配置")}
        ${fact("成员", number(workspace.member_count))}
        ${fact("活跃 Session", number(workspace.active_session_count))}
        ${fact("数据生命周期", statusLabel(workspace.data_lifecycle_status))}
        ${fact("Gateway", statusLabel(workspace.gateway_status))}
        ${fact("部署", statusLabel(workspace.deployment_status))}
        ${workspace.plan_status ? fact("套餐", statusLabel(workspace.plan_status)) : ""}
      </dl>
    </section>
    <section class="platform-band platform-health-detail">
      <div class="platform-section-heading"><div><p>Health projection</p><h2>健康投影</h2></div>${status(health.status)}</div>
      <dl class="platform-fact-grid">
        ${fact("Score band", statusLabel(health.score_band ?? "not_ready"))}
        ${fact("Risk level", statusLabel(health.risk_level ?? "not_ready"))}
        ${fact("评估时间", formatTime(health.evaluated_at))}
        ${fact("健康风险", Array.isArray(health.risk_tags) ? health.risk_tags.map(statusLabel).join("、") || "无" : "受限")}
      </dl>
    </section>
    ${riskOptions.length && Array.isArray(workspace.platform_risk_tags) ? riskControls(workspace, riskOptions) : ""}
    ${canContact && workspace.owner_contact_status ? notificationControls(workspace) : ""}
    <section class="platform-band platform-detail-history">
      <div class="platform-history-column"><div class="platform-section-heading"><div><p>Action intents</p><h2>最近动作</h2></div></div>${actionTimeline(actions)}</div>
      <div class="platform-history-column"><div class="platform-section-heading"><div><p>Operational events</p><h2>事件时间线</h2></div></div>${eventTimeline(events)}</div>
    </section>`;
}

function lifecycleCommands(workspace) {
  const canSuspend = workspace.operational_status === "active";
  const canRestore = workspace.operational_status === "suspended";
  if (!canSuspend && !canRestore) return "";
  const action = canSuspend ? "suspend" : "restore";
  return `<section class="platform-command-bar"><div><strong>${canSuspend ? "冻结 Workspace" : "恢复 Workspace"}</strong><span>${canSuspend ? "保留客户数据并撤销活跃 WorkspaceSession" : "旧 Session 不会复活"}</span></div><button class="${canSuspend ? "platform-danger" : "platform-primary"}" type="button" data-platform-action="prepare-workspace-lifecycle" data-lifecycle-action="${action}">${canSuspend ? "⏸ 冻结" : "▶ 恢复"}</button></section>`;
}

function riskControls(workspace, options) {
  const tags = workspace.platform_risk_tags ?? [];
  return `<section class="platform-band platform-operation-grid">
    <div><div class="platform-section-heading"><div><p>Risk operations</p><h2>平台风险标签</h2></div><span>Risk rev ${number(workspace.risk_revision)}</span></div><div class="platform-active-risk-tags">${tags.map((tag) => `<span>${escapeHtml(statusLabel(tag))}<button type="button" data-platform-action="remove-workspace-risk" data-tag-code="${escapeAttribute(tag)}" data-reason-code="${escapeAttribute(riskReason(tag))}" title="移除标签" aria-label="移除 ${escapeAttribute(statusLabel(tag))}">×</button></span>`).join("") || '<span class="platform-restricted">暂无标签</span>'}</div></div>
    <form data-workspace-risk-form autocomplete="off"><label><span>标签</span><select name="tagCode">${filterOptions("", options)}</select></label><label><span>原因</span><select name="reasonCode">${filterOptions("", [["security_review", "Security review"], ["compliance_review", "Compliance review"], ["payment_follow_up", "Payment follow up"], ["adoption_follow_up", "Adoption follow up"]])}</select></label><button class="platform-primary" type="submit">添加标签</button></form>
  </section>`;
}

function notificationControls(workspace) {
  return `<section class="platform-band platform-operation-grid">
    <div><div class="platform-section-heading"><div><p>Owner contact</p><h2>联系 Owner</h2></div></div><dl class="platform-fact-grid platform-fact-grid-compact">${fact("联系状态", statusLabel(workspace.owner_contact_status))}${fact("最近通知", statusLabel(workspace.last_notification_status ?? "none"))}</dl></div>
    <form data-workspace-notification-form autocomplete="off"><label><span>模板</span><select name="templateCode">${filterOptions("", [["workspace_operational_notice", "Operational notice"], ["workspace_risk_follow_up", "Risk follow up"], ["workspace_service_update", "Service update"]])}</select></label><label><span>原因</span><select name="reasonCode">${filterOptions("", [["operational_notice", "Operational notice"], ["risk_follow_up", "Risk follow up"], ["service_update", "Service update"]])}</select></label><button class="platform-primary" type="submit" ${workspace.owner_contact_status === "available" ? "" : "disabled"}>加入投递队列</button></form>
  </section>`;
}

function actionTimeline(actions) {
  return `<ol class="platform-timeline">${actions.map((action) => `<li><time>${formatTime(action.created_at)}</time><div><strong>${escapeHtml(actionLabel(action.action_type))}</strong><span>${escapeHtml(action.reason_code)}</span><small>Result rev ${number(action.result_revision)}</small></div></li>`).join("") || '<li class="platform-empty">暂无平台动作</li>'}</ol>`;
}

function eventTimeline(events) {
  return `<ol class="platform-timeline">${events.map((event) => `<li><time>${formatTime(event.occurred_at)}</time><div><strong>${escapeHtml(actionLabel(event.event_type))}</strong><span>${escapeHtml(event.reason_code)}</span><small>Operational ${number(event.operational_revision)} · Risk ${number(event.risk_revision)}</small></div></li>`).join("") || '<li class="platform-empty">暂无运营事件</li>'}</ol>`;
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
  if (/^workspace-(?:suspend|restore)$/u.test(state.pendingAction.kind)) {
    const suspend = state.pendingAction.kind === "workspace-suspend";
    const reasons = suspend
      ? [["security_containment", "Security containment"], ["terms_enforcement", "Terms enforcement"], ["operational_protection", "Operational protection"]]
      : [["security_cleared", "Security cleared"], ["terms_resolved", "Terms resolved"], ["operational_recovery", "Operational recovery"]];
    return `<section class="platform-confirm platform-lifecycle-confirm" role="alert"><div><strong>${suspend ? "确认冻结 Workspace" : "确认恢复 Workspace"}</strong><span>${escapeHtml(state.pendingAction.id)} · Rev ${number(state.pendingAction.revision)}</span></div><form data-workspace-lifecycle-form autocomplete="off"><label><span>原因</span><select name="reasonCode">${filterOptions("", reasons)}</select></label><button type="button" data-platform-action="cancel-confirm">取消</button><button class="${suspend ? "platform-danger" : "platform-primary"}" type="submit">${suspend ? "确认冻结" : "确认恢复"}</button></form></section>`;
  }
  return `<section class="platform-confirm" role="alert"><div><strong>撤销支持授权</strong><span>${escapeHtml(state.pendingAction.id)}</span></div><div><button type="button" data-platform-action="cancel-confirm">取消</button><button class="platform-danger" type="button" data-platform-action="confirm-revoke" data-grant-id="${escapeAttribute(state.pendingAction.id)}">确认撤销</button></div></section>`;
}

function feedback(state) {
  if (state.error) {
    const conflict = /platform\.workspace_(?:revision|risk)_conflict/u.test(state.error.code ?? "")
      ? "状态已被其他操作更新，请刷新后重试。"
      : "";
    return `<div class="platform-feedback is-error" role="alert"><strong>${escapeHtml(state.error.message)}</strong>${conflict ? `<span>${escapeHtml(conflict)}</span>` : ""}<span>Request ID ${escapeHtml(state.error.requestId ?? "-")}</span></div>`;
  }
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
  const kind = /active|healthy|ready|reviewed|fresh/u.test(text)
    ? "is-ok"
    : /pending|watch|stale|at_risk/u.test(text)
      ? "is-warn"
      : /revoked|expired|disabled|critical|suspended|deleted|conflict|unavailable/u.test(text)
        ? "is-danger"
        : "";
  return `<span class="platform-status ${kind}">${escapeHtml(statusLabel(text))}</span>`;
}

function roleLabel(value) { return String(value ?? "").replace(/^platform_/u, "").replaceAll("_", " "); }
function domainLabel(value) { return value === "workspace" ? "客户" : value === "platform" ? "平台" : value; }
function statusLabel(value) { return String(value).replaceAll("_", " "); }
function actionLabel(value) { return String(value).replaceAll("_", " ").replaceAll(".", " / "); }
function kindLabel(value) { return value === "organization" ? "Organization" : value === "personal" ? "Personal" : value; }
function navSymbol(view) { return ({ overview: "⌂", operators: "◎", workspaces: "▦", support: "◇", audit: "≡" })[view] ?? "·"; }
function emptyRow(label, columns = 5) { return `<tr><td colspan="${number(columns)}" class="platform-empty">${escapeHtml(label)}</td></tr>`; }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date); }
function fact(label, value) { return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`; }
function filterOptions(current, options) { return options.map(([value, label]) => `<option value="${escapeAttribute(value)}" ${value === current ? "selected" : ""}>${escapeHtml(label)}</option>`).join(""); }
function riskTagList(tags) { return tags.length ? `<div class="platform-tag-list">${tags.map((tag) => `<span>${escapeHtml(statusLabel(tag))}</span>`).join("")}</div>` : '<span class="platform-restricted">无</span>'; }
function allowedRiskOptions(permissions) {
  const options = [];
  if (permissions.has("platform.workspace.risk.manage")) options.push(["security_review", "Security review"], ["compliance_hold", "Compliance hold"]);
  if (permissions.has("platform.customer_success.read")) options.push(["payment_attention", "Payment attention"], ["adoption_attention", "Adoption attention"]);
  return options;
}
function riskReason(tag) { return ({ security_review: "security_review", compliance_hold: "compliance_review", payment_attention: "payment_follow_up", adoption_attention: "adoption_follow_up" })[tag] ?? "security_review"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]); }
function escapeAttribute(value) { return escapeHtml(value); }
