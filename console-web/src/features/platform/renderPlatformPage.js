// 平台渲染器只消费严格安全 DTO；所有服务端字符串转义后进入 DOM，不展示内部地址、Provider 或签名。
import { NOTIFICATION_EVENT_PARAMETERS } from "../../lib/notifications.js";

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
  "platform_communications",
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
  if (state.view === "infrastructure") return infrastructureView(state, permissions);
  if (state.view === "gateway") return gatewayView(state, permissions);
  if (state.view === "job-runtime") return jobRuntimeView(state, permissions);
  if (state.view === "feature-rollouts") return featureRolloutsView(state, permissions);
  if (state.view === "config-distribution") return configDistributionView(state, permissions);
  if (state.view === "observability") return observabilityView(state, permissions);
  if (state.view === "abuse-risk") return abuseRiskView(state, permissions);
  if (state.view === "operators") return operatorsView(state, permissions);
  if (state.view === "workspaces") return workspacesView(state, permissions);
  if (state.view === "support") return supportView(state, permissions);
  if (state.view === "audit") return auditView(state);
  if (state.view === "billing") return billingView(state, permissions);
  if (state.view === "compliance") return complianceView(state, permissions);
  if (state.view === "verification") return verificationView(state, permissions);
  if (state.view === "notification-templates") return notificationTemplatesView(state, permissions);
  return domainPlaceholderView(state);
}

function abuseRiskView(state, permissions) {
  const response = state.riskCases ?? {};
  const cases = Array.isArray(response.items) ? response.items : [];
  const support = response.projection === "support";
  const canDecide = !support && permissions.has("platform.risk.decide");
  return `
    <header class="platform-page-heading">
      <div><p>Abuse / Risk</p><h1>滥用与风险控制</h1></div>
      <span>${support ? "支持投影" : "安全投影"}</span>
    </header>
    <section class="platform-metrics" aria-label="风险案件概览">
      ${metric("Cases", response.total ?? cases.length, "当前筛选")}
      ${metric("Open", cases.filter((item) => item.status === "open").length, "待分诊")}
      ${metric("Restricted", cases.filter((item) => item.status === "restricted").length, "限制生效")}
      ${metric("Appeals", cases.reduce((total, item) => total + item.appeals.length, 0), "申诉记录")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Case queue</p><h2>风险案件</h2></div>
        <form class="platform-risk-filter" data-platform-risk-filter-form>
          <label><span>状态</span><select name="status">${riskStatusOptions(state.riskStatus)}</select></label>
          <button type="submit" ${state.loading ? "disabled" : ""}>筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-risk-table">
          <thead><tr><th>案件 / Workspace</th><th>状态 / 风险</th><th>信号</th><th>限制 / 申诉</th><th>决策</th></tr></thead>
          <tbody>${cases.map((item) => riskCaseRow(item, { support, canDecide, loading: state.loading })).join("") || emptyRow("暂无风险案件", 5)}</tbody>
        </table>
      </div>
      <div class="platform-pagination platform-risk-pagination">
        <button type="button" data-platform-action="risk-previous" ${state.riskCursorHistory?.length ? "" : "disabled"}>上一页</button>
        <span>${number(cases.length)} 条</span>
        <button type="button" data-platform-action="risk-next" ${response.next_cursor ? "" : "disabled"}>下一页</button>
      </div>
    </section>
  `;
}

function riskCaseRow(item, { support, canDecide, loading }) {
  const score = support ? "已隐藏" : statusLabel(item.score_band ?? "unknown");
  const signals = support
    ? '<span class="platform-restricted">支持投影不显示</span>'
    : item.signal_families.map((value) => `<span>${escapeHtml(statusLabel(value))}</span>`).join("")
      || '<span class="platform-restricted">无</span>';
  return `
    <tr>
      <td data-label="案件 / Workspace"><strong>${escapeHtml(shortReference(item.risk_case_id))}</strong><span>${escapeHtml(shortWorkspace(item.workspace_id))} · rev ${number(item.revision)}</span><small>${escapeHtml(formatTime(item.updated_at))}</small></td>
      <td data-label="状态 / 风险">${status(item.status)}<span>${escapeHtml(score)}</span><small>${escapeHtml(statusLabel(item.safe_reason_family))}</small></td>
      <td data-label="信号"><div class="platform-tag-list platform-risk-signals">${signals}</div></td>
      <td data-label="限制 / 申诉">${riskRestriction(item.restriction)}${riskAppealTimeline(item.appeals)}</td>
      <td data-label="决策">${canDecide ? riskDecisionControls(item, loading) : '<span class="platform-restricted">只读</span>'}</td>
    </tr>
  `;
}

function riskRestriction(restriction) {
  if (!restriction) return '<div class="platform-risk-restriction"><strong>无限制</strong></div>';
  return `<dl class="platform-risk-restriction">
    <div><dt>类型</dt><dd>${escapeHtml(statusLabel(restriction.restriction_kind))}</dd></div>
    <div><dt>状态</dt><dd>${status(restriction.status)}</dd></div>
    <div><dt>截止</dt><dd>${escapeHtml(formatTime(restriction.expires_at))}</dd></div>
  </dl>`;
}

function riskAppealTimeline(appeals) {
  if (!appeals.length) return '<p class="platform-restricted">无申诉</p>';
  return `<ol class="platform-risk-appeals" aria-label="申诉时间线">${appeals.map((appeal) => `
    <li><div><strong>${escapeHtml(statusLabel(appeal.category))}</strong>${status(appeal.status)}</div><small>${escapeHtml(formatTime(appeal.created_at))}${appeal.resolution_reason ? ` · ${escapeHtml(statusLabel(appeal.resolution_reason))}` : ""}</small></li>
  `).join("")}</ol>`;
}

function riskDecisionControls(item, loading) {
  if (new Set(["cleared", "closed"]).has(item.status)) {
    return '<span class="platform-restricted">已终结</span>';
  }
  const hidden = `<input type="hidden" name="caseId" value="${escapeAttribute(item.risk_case_id)}" /><input type="hidden" name="revision" value="${number(item.revision)}" />`;
  const review = item.status === "open" ? `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="required_review" /><input type="hidden" name="reasonCode" value="manual_review" /><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认复核</span></label><button type="submit" ${loading ? "disabled" : ""}>进入复核</button></form>` : "";
  const restrict = item.status !== "restricted" ? `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="restricted" /><input type="hidden" name="reasonCode" value="automation_confirmed" /><label><span>限制</span><select name="restrictionKind"><option value="api_calls">API 调用</option><option value="resource_creation">资源创建</option><option value="workspace_access">Workspace 访问</option></select></label><label><span>分钟</span><input name="restrictionMinutes" type="number" min="5" max="10080" value="30" required /></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认限制</span></label><button class="platform-danger" type="submit" ${loading ? "disabled" : ""}>限制</button></form>` : "";
  const clear = `
    <form data-platform-risk-decision-form>${hidden}<input type="hidden" name="decision" value="cleared" /><label><span>清除原因</span><select name="reasonCode"><option value="false_positive">误报</option><option value="appeal_approved">申诉通过</option></select></label><label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认清除</span></label><button type="submit" ${loading ? "disabled" : ""}>清除</button></form>`;
  return `<div class="platform-risk-actions">${review}${restrict}${clear}</div>`;
}

function jobRuntimeView(state, permissions) {
  const response = state.jobQueues ?? {};
  const queues = Array.isArray(response.queues) ? response.queues : [];
  const jobs = Array.isArray(response.recent_jobs) ? response.recent_jobs : [];
  const types = Array.isArray(response.job_types) ? response.job_types : [];
  const canManage = permissions.has("platform.job.manage");
  const canManageTypes = permissions.has("platform.job.type_manage");
  return `
    <header class="platform-page-heading">
      <div><p>Scheduler / Worker</p><h1>任务运行时</h1></div>
      <span>${number(jobs.length)} 个任务</span>
    </header>
    <section class="platform-band platform-job-queues" aria-label="队列状态">
      <div class="platform-job-queue-grid">
        ${queues.map(jobQueueSummary).join("") || '<p class="platform-empty">暂无队列状态</p>'}
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Durable jobs</p><h2>任务</h2></div>
        <form class="platform-job-filter" data-platform-job-filter-form>
          <label><span>状态</span><select name="status">${jobStatusOptions(state.jobStatus)}</select></label>
          <button type="submit">筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-job-table">
          <thead><tr><th>任务</th><th>队列 / 优先级</th><th>状态</th><th>尝试</th><th>调度</th><th>操作</th></tr></thead>
          <tbody>${jobs.map(jobRow).join("") || emptyRow("暂无任务", 6)}</tbody>
        </table>
      </div>
    </section>
    ${state.selectedJob ? jobDetailView(state.selectedJob, canManage, state.loading) : ""}
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Signed catalog</p><h2>任务类型</h2></div>
        <span>${number(types.length)} 个类型</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-job-type-table">
          <thead><tr><th>类型</th><th>队列</th><th>资源</th><th>执行门限</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>${types.map((item) => jobTypeRow(item, canManageTypes, state.loading)).join("") || emptyRow("暂无任务类型", 6)}</tbody>
        </table>
      </div>
    </section>`;
}

function jobQueueSummary(queue) {
  return `<div>
    <span>${escapeHtml(queueLabel(queue.queue_code))}</span>
    <strong>${number(queue.queued_count)}</strong>
    <small>运行 ${number(queue.active_lease_count)} · 死信 ${number(queue.dead_letter_count)}</small>
    <small>最久 ${queue.oldest_wait_seconds === null ? "-" : `${number(queue.oldest_wait_seconds)}s`}</small>
  </div>`;
}

function jobRow(job) {
  return `<tr>
    <td data-label="任务"><strong>${escapeHtml(job.type_code)}</strong><small>${escapeHtml(job.job_id)}</small></td>
    <td data-label="队列 / 优先级"><strong>${escapeHtml(queueLabel(job.queue_code))}</strong><small>P${number(job.priority)}</small></td>
    <td data-label="状态">${status(job.status)}<small>${escapeHtml(job.safe_outcome_code ?? "-")}</small></td>
    <td data-label="尝试"><strong>${number(job.attempt_count)} / ${number(job.max_attempts)}</strong><small>generation ${number(job.generation)}</small></td>
    <td data-label="调度"><strong>${formatTime(job.scheduled_at)}</strong><small>rev ${number(job.revision)}</small></td>
    <td data-label="操作"><button type="button" data-platform-action="open-job" data-job-id="${escapeAttribute(job.job_id)}">查看</button></td>
  </tr>`;
}

function jobDetailView(detail, canManage, loading) {
  const job = detail.job;
  const attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
  const checkpoints = Array.isArray(detail.checkpoints) ? detail.checkpoints : [];
  const retryable = new Set(["failed", "dead_lettered"]).has(job.status);
  const cancellable = new Set(["queued", "leased", "running"]).has(job.status);
  return `<section class="platform-band platform-job-detail">
    <div class="platform-section-heading">
      <div><p>${escapeHtml(job.job_id)}</p><h2>${escapeHtml(job.type_code)}</h2></div>
      <button class="platform-icon-button" type="button" data-platform-action="close-job" title="关闭" aria-label="关闭">×</button>
    </div>
    <div class="platform-job-detail-grid">
      <div>
        <h3>Attempt 时间线</h3>
        <ol class="platform-job-timeline">${attempts.map(jobAttempt).join("") || "<li>暂无 Attempt</li>"}</ol>
      </div>
      <div>
        <h3>Checkpoint</h3>
        <ol class="platform-job-checkpoints">${checkpoints.map(jobCheckpoint).join("") || "<li>暂无 Checkpoint</li>"}</ol>
      </div>
    </div>
    ${canManage && (retryable || cancellable) ? `<form class="platform-job-command" data-platform-job-command-form>
      <input type="hidden" name="jobId" value="${escapeAttribute(job.job_id)}" />
      <input type="hidden" name="revision" value="${number(job.revision)}" />
      <input type="hidden" name="action" value="${retryable ? "retry" : "cancel"}" />
      <label><span>原因</span><select name="reasonCode">${retryable
        ? '<option value="operator_retry">操作员重试</option><option value="incident_recovery">事件恢复</option>'
        : '<option value="operator_cancelled">操作员取消</option><option value="security_containment">安全处置</option>'}</select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认操作</span></label>
      <button class="${retryable ? "platform-primary" : "platform-danger"}" type="submit" ${loading ? "disabled" : ""}>${retryable ? "重试" : "取消任务"}</button>
    </form>` : ""}
  </section>`;
}

function jobAttempt(attempt) {
  return `<li><div>${status(attempt.status)}<strong>#${number(attempt.attempt_number)}</strong></div>
    <span>${formatTime(attempt.leased_at)}</span>
    <small>${escapeHtml(attempt.safe_outcome_code ?? "-")} · generation ${number(attempt.generation)}</small></li>`;
}

function jobCheckpoint(checkpoint) {
  return `<li><div><strong>#${number(checkpoint.sequence_number)}</strong><span>generation ${number(checkpoint.generation)}</span></div>
    <small>${escapeHtml(checkpoint.checkpoint_sha256.slice(0, 12))} · ${formatTime(checkpoint.created_at)}</small></li>`;
}

function jobTypeRow(item, canManage, loading) {
  const action = item.status === "active" ? "disable" : "activate";
  return `<tr>
    <td data-label="类型"><strong>${escapeHtml(item.type_code)}</strong><small>contract v${number(item.contract_version)}</small></td>
    <td data-label="队列">${escapeHtml(queueLabel(item.queue_code))}</td>
    <td data-label="资源">${escapeHtml(item.resource_class)}</td>
    <td data-label="执行门限"><strong>${number(item.max_attempts)} 次</strong><small>${number(item.timeout_seconds)}s</small></td>
    <td data-label="状态">${status(item.status)}<small>rev ${number(item.revision)}</small></td>
    <td data-label="操作">${canManage ? `<form class="platform-job-type-command" data-platform-job-type-form>
      <input type="hidden" name="typeCode" value="${escapeAttribute(item.type_code)}" />
      <input type="hidden" name="revision" value="${number(item.revision)}" />
      <input type="hidden" name="action" value="${action}" />
      <input type="hidden" name="reasonCode" value="${action === "activate" ? "release_activation" : "operational_pause"}" />
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${loading ? "disabled" : ""}>${action === "activate" ? "启用" : "停用"}</button>
    </form>` : "-"}</td>
  </tr>`;
}

function jobStatusOptions(selected) {
  return [
    ["", "全部"], ["queued", "排队"], ["leased", "已领取"], ["running", "运行中"],
    ["cancel_requested", "取消中"], ["succeeded", "成功"], ["failed", "失败"],
    ["cancelled", "已取消"], ["dead_lettered", "死信"],
  ].map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function featureRolloutsView(state, permissions) {
  const response = state.featureFlags ?? {};
  const flags = Array.isArray(response.flags) ? response.flags : [];
  const canWrite = permissions.has("platform.feature.write");
  const canApprove = permissions.has("platform.feature.approve");
  const canKill = permissions.has("platform.feature.kill");
  return `
    <header class="platform-page-heading">
      <div><p>Server-authoritative delivery</p><h1>功能开关与渐进发布</h1></div>
      <span>${number(response.total ?? 0)} 个 Flag</span>
    </header>
    <section class="platform-metrics" aria-label="发布概览">
      ${metric("Active", flags.filter((item) => item.status === "active").length, "可发布")}
      ${metric("P0", flags.filter((item) => item.risk_level === "p0").length, "双人审批")}
      ${metric("Paused", flags.filter((item) => item.latest_rollout?.status === "paused").length, "健康门禁")}
      ${metric("Exposures", flags.reduce((sum, item) => sum + item.exposure_count, 0), "不可变事实")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Immutable versions</p><h2>发布目录</h2></div>
        <form class="platform-feature-filter" data-platform-feature-filter-form>
          <label><span>状态</span><select name="status">${featureStatusOptions(state.featureStatus)}</select></label>
          <label><span>风险</span><select name="riskLevel">${featureRiskOptions(state.featureRiskLevel)}</select></label>
          <button type="submit">筛选</button>
        </form>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-feature-table">
          <thead><tr><th>Flag / Head</th><th>版本与规则</th><th>发布阶梯</th><th>审批 / 健康</th><th>曝光</th><th>操作</th></tr></thead>
          <tbody>${flags.map((item) => featureFlagRow(
    item,
    { canWrite, canApprove, canKill, loading: state.loading },
  )).join("") || emptyRow("暂无功能开关", 6)}</tbody>
        </table>
      </div>
    </section>
    ${canWrite ? featureCreateForm() : ""}`;
}

function configDistributionView(state, permissions) {
  const response = state.configDistribution ?? {};
  const items = Array.isArray(response.items) ? response.items : [];
  const canRebuild = permissions.has("platform.config_distribution.rebuild");
  const gatewayCount = items.reduce((sum, item) => sum + item.gateway_count, 0);
  const staleCount = items.reduce((sum, item) => sum + item.stale_gateway_count, 0);
  const laggingCount = items.filter(
    (item) => item.minimum_applied_generation < item.generation,
  ).length;
  return `
    <header class="platform-page-heading">
      <div><p>DB-authoritative snapshots</p><h1>配置分发</h1></div>
      <span>${number(response.total ?? 0)} 个工作区</span>
    </header>
    <section class="platform-metrics" aria-label="配置分发概览">
      ${metric("Workspaces", items.length, "已分配 Cell")}
      ${metric("Gateways", gatewayCount, "已观测实例")}
      ${metric("ACK lag", laggingCount, "落后工作区")}
      ${metric("Stale", staleCount, "显式 stale / LKG")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Monotonic watermarks</p><h2>工作区水位</h2></div>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-config-table">
          <thead><tr><th>工作区 / Cell</th><th>Head</th><th>ACK 水位</th><th>LKG / Stale</th><th>最近重建</th><th>操作</th></tr></thead>
          <tbody>${items.map((item) => configDistributionRow(
    item,
    { canRebuild, loading: state.loading },
  )).join("") || emptyRow("暂无配置分发水位", 6)}</tbody>
        </table>
      </div>
    </section>`;
}

function configDistributionRow(item, access) {
  const rebuild = item.latest_rebuild;
  const ackLag = Math.max(0, item.generation - item.minimum_applied_generation);
  const stale = item.stale_gateway_count > 0;
  return `<tr>
    <td data-label="工作区 / Cell"><strong>${escapeHtml(item.workspace_id)}</strong><small>${escapeHtml(item.cell_code)}</small><small>${number(item.gateway_count)} 个 Gateway</small></td>
    <td data-label="Head"><strong>generation ${number(item.generation)}</strong><small>revocation ${number(item.revocation_watermark)}</small></td>
    <td data-label="ACK 水位"><strong>${number(item.minimum_applied_generation)}</strong><small>lag ${number(ackLag)} · ${formatTime(item.latest_ack_at)}</small></td>
    <td data-label="LKG / Stale"><strong>LKG ${number(item.minimum_lkg_generation)}</strong>${status(stale ? "stale" : "current")}<small>${number(item.stale_gateway_count)} 个 stale</small></td>
    <td data-label="最近重建">${rebuild ? `<strong>generation ${number(rebuild.target_generation)}</strong>${status(rebuild.status)}<small>rev ${number(rebuild.revision)} · ${formatTime(rebuild.created_at)}</small><small>result ${rebuild.result_manifest_sha256 ? escapeHtml(rebuild.result_manifest_sha256.slice(0, 12)) : "-"}</small>` : '<span class="platform-restricted">尚无重建</span>'}</td>
    <td data-label="操作">${access.canRebuild ? `<form class="platform-config-rebuild" data-platform-config-rebuild-form>
      <input type="hidden" name="workspaceId" value="${escapeAttribute(item.workspace_id)}" />
      <label><span>原因</span><select name="reasonCode"><option value="operator_rebuild">运维重建</option><option value="consistency_repair">一致性修复</option></select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${access.loading ? "disabled" : ""}>重建</button>
    </form>` : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function observabilityView(state, permissions) {
  const serviceResponse = state.observabilityServices ?? {};
  const sloResponse = state.serviceLevelObjectives ?? {};
  const budgetResponse = state.errorBudgets ?? {};
  const services = Array.isArray(serviceResponse.items) ? serviceResponse.items : [];
  const slos = Array.isArray(sloResponse.items) ? sloResponse.items : [];
  const budgets = Array.isArray(budgetResponse.items) ? budgetResponse.items : [];
  const alerts = Array.isArray(budgetResponse.alerts) ? budgetResponse.alerts : [];
  const canManage = permissions.has("platform.observability.manage");
  const canAck = permissions.has("platform.observability.alert_ack");
  const projection = serviceResponse.projection === "security" ? "安全投影" : "运维投影";
  return `
    <header class="platform-page-heading">
      <div><p>Metrics / Logs / Traces</p><h1>服务可观测性</h1></div>
      <span>${escapeHtml(projection)}</span>
    </header>
    <section class="platform-metrics" aria-label="可观测性概览">
      ${metric("Services", services.length, "服务目录")}
      ${metric("Healthy", services.filter((item) => item.health_status === "healthy").length, "黄金信号")}
      ${metric("SLO", slos.length, "当前版本")}
      ${metric("Alerts", alerts.filter((item) => item.state !== "resolved").length, "待处理")}
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Golden signals</p><h2>服务目录</h2></div>
        <span>watermark ${number(Math.max(0, ...services.map((item) => item.source_watermark)))}</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-observability-table">
          <thead><tr><th>服务</th><th>健康</th><th>流量</th><th>错误率</th><th>P95 延迟</th></tr></thead>
          <tbody>${services.map(observableServiceRow).join("") || emptyRow("暂无服务遥测", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Immutable definitions</p><h2>SLO</h2></div>
        <span>${number(sloResponse.total ?? 0)} 个定义</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-slo-table">
          <thead><tr><th>SLO / 版本</th><th>SLI</th><th>窗口 / 目标</th><th>燃烧门限</th><th>操作</th></tr></thead>
          <tbody>${slos.map((item) => sloRow(item, canManage, state.loading)).join("") || emptyRow("暂无 SLO", 5)}</tbody>
        </table>
      </div>
    </section>
    ${canManage ? sloEditor(null, services, state.loading) : ""}
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Integer error budgets</p><h2>错误预算</h2></div>
        <span>${number(budgets.length)} 个窗口</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-budget-table">
          <thead><tr><th>SLO / 窗口</th><th>请求</th><th>允许 / 已消耗</th><th>剩余</th><th>燃烧率</th></tr></thead>
          <tbody>${budgets.map(errorBudgetRow).join("") || emptyRow("等待聚合作业", 5)}</tbody>
        </table>
      </div>
    </section>
    <section class="platform-band platform-alert-timeline">
      <div class="platform-section-heading">
        <div><p>Append-only timeline</p><h2>告警时间线</h2></div>
        <span>${number(alerts.length)} 条</span>
      </div>
      <ol>${alerts.map((item) => alertTimelineItem(item, canAck, state.loading)).join("")
        || "<li class=\"platform-empty\">暂无告警</li>"}</ol>
    </section>`;
}

function observableServiceRow(item) {
  return `<tr>
    <td data-label="服务"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.service_code)} · Tier ${number(item.service_tier)}</small><small>${escapeHtml(item.golden_signals.join(" · "))}</small></td>
    <td data-label="健康">${status(item.health_status)}<small>${escapeHtml(item.owner_team_code)}</small></td>
    <td data-label="流量"><strong>${number(item.total_count)}</strong><small>当前分钟聚合</small></td>
    <td data-label="错误率"><strong>${bps(item.error_rate_bps)}</strong><small>${number(item.error_rate_bps)} bps</small></td>
    <td data-label="P95 延迟"><strong>${number(item.latency_p95_ms)} ms</strong><small>watermark ${number(item.source_watermark)}</small></td>
  </tr>`;
}

function sloRow(item, canManage, loading) {
  return `<tr>
    <td data-label="SLO / 版本"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.slo_code)}</small><small>v${number(item.version_number)} · ${item.is_current ? "HEAD" : "历史"}</small></td>
    <td data-label="SLI"><strong>${escapeHtml(item.sli_kind)}</strong><small>${escapeHtml(item.operation_code)} · ${escapeHtml(item.region_code)}</small><small>${escapeHtml(item.signal_class)}</small></td>
    <td data-label="窗口 / 目标"><strong>${number(item.window_minutes)} min</strong><small>${bps(item.target_bps)} 目标${item.latency_threshold_ms === null ? "" : ` · ≤ ${number(item.latency_threshold_ms)} ms`}</small></td>
    <td data-label="燃烧门限"><strong>${bps(item.burn_warning_bps)}</strong><small>critical ${bps(item.burn_critical_bps)}</small></td>
    <td data-label="操作">${canManage && item.is_current
    ? sloEditor(item, [], loading)
    : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function sloEditor(item, services, loading) {
  const creating = item === null;
  const serviceOptions = creating
    ? services.map((service) => `<option value="${escapeAttribute(service.service_code)}">${escapeHtml(service.display_name)}</option>`).join("")
    : `<option value="${escapeAttribute(item.service_code)}">${escapeHtml(item.service_code)}</option>`;
  const content = `<form data-platform-slo-form>
    <input type="hidden" name="previousVersion" value="${creating ? 0 : Number(item.version_number)}" />
    <label><span>服务</span><select name="serviceCode" required>${serviceOptions}</select></label>
    <label><span>SLO code</span><input name="sloCode" value="${escapeAttribute(item?.slo_code ?? "")}" required /></label>
    <label><span>名称</span><input name="displayName" value="${escapeAttribute(item?.display_name ?? "")}" maxlength="120" required /></label>
    <label><span>信号分类</span><select name="signalClass"><option value="reliability" ${item?.signal_class === "security" ? "" : "selected"}>可靠性</option><option value="security" ${item?.signal_class === "security" ? "selected" : ""}>安全</option></select></label>
    <label><span>SLI</span><select name="sliKind"><option value="availability" ${item?.sli_kind === "latency" ? "" : "selected"}>可用性</option><option value="latency" ${item?.sli_kind === "latency" ? "selected" : ""}>延迟</option></select></label>
    <label><span>Operation</span><input name="operationCode" value="${escapeAttribute(item?.operation_code ?? "chat.completions")}" required /></label>
    <label><span>Region</span><input name="regionCode" value="${escapeAttribute(item?.region_code ?? "global")}" required /></label>
    <label><span>窗口分钟</span><input name="windowMinutes" type="number" min="5" max="43200" value="${Number(item?.window_minutes ?? 5)}" required /></label>
    <label><span>目标 bps</span><input name="targetBps" type="number" min="1" max="10000" value="${Number(item?.target_bps ?? 9990)}" required /></label>
    <label><span>延迟门限 ms</span><select name="latencyThresholdMs">${[10, 50, 100, 250, 500, 1000, 2500, 5000].map((value) => `<option value="${value}" ${value === item?.latency_threshold_ms ? "selected" : ""}>${value}</option>`).join("")}</select></label>
    <label><span>Warning burn bps</span><input name="burnWarningBps" type="number" min="10000" value="${Number(item?.burn_warning_bps ?? 20000)}" required /></label>
    <label><span>Critical burn bps</span><input name="burnCriticalBps" type="number" min="10001" value="${Number(item?.burn_critical_bps ?? 50000)}" required /></label>
    <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认版本定义</span></label>
    <button class="platform-primary" type="submit" ${loading ? "disabled" : ""}>${creating ? "创建 SLO" : "追加版本"}</button>
  </form>`;
  if (creating) {
    return `<section class="platform-band platform-slo-editor"><div class="platform-section-heading"><div><p>CAS head</p><h2>创建 SLO</h2></div></div>${content}</section>`;
  }
  return `<details class="platform-slo-version-editor"><summary>追加版本</summary>${content}</details>`;
}

function errorBudgetRow(item) {
  const max = Math.max(item.allowed_bad_count, 1);
  const value = Math.min(item.consumed_bad_count, max);
  return `<tr>
    <td data-label="SLO / 窗口"><strong>${escapeHtml(item.slo_code)} · v${number(item.slo_version)}</strong><small>${formatTime(item.window_started_at)} → ${formatTime(item.window_ended_at)}</small>${status(item.health_status)}</td>
    <td data-label="请求"><strong>${number(item.good_count)} / ${number(item.total_count)} good</strong><small>watermark ${number(item.source_watermark)}</small></td>
    <td data-label="允许 / 已消耗"><progress max="${max}" value="${value}" aria-label="预算消耗 ${number(item.consumed_bad_count)} / ${number(item.allowed_bad_count)}"></progress><small>${number(item.consumed_bad_count)} / ${number(item.allowed_bad_count)}</small></td>
    <td data-label="剩余"><strong>${number(item.remaining_bad_count)}</strong><small>错误请求额度</small></td>
    <td data-label="燃烧率"><strong>${bps(item.burn_rate_bps)}</strong><small>computed ${formatTime(item.computed_at)}</small></td>
  </tr>`;
}

function alertTimelineItem(item, canAck, loading) {
  return `<li>
    <div><span>${formatTime(item.last_seen_at)}</span>${status(item.state)}${status(item.severity)}</div>
    <strong>${escapeHtml(item.slo_code)}</strong>
    <small>${escapeHtml(item.safe_reason_code)} · ${escapeHtml(item.service_code)} / ${escapeHtml(item.region_code)} · rev ${number(item.revision)}</small>
    <small>${item.latest_acknowledgement_at ? `ACK ${formatTime(item.latest_acknowledgement_at)}` : "尚未确认"}</small>
    ${canAck && item.state !== "resolved" ? `<form data-platform-alert-ack-form>
      <input type="hidden" name="alertId" value="${escapeAttribute(item.alert_id)}" />
      <input type="hidden" name="revision" value="${number(item.revision)}" />
      <label><span>原因</span><select name="reasonCode"><option value="investigating">调查中</option><option value="known_issue">已知问题</option><option value="mitigating">缓解中</option></select></label>
      <label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label>
      <button type="submit" ${loading ? "disabled" : ""}>记录 ACK</button>
    </form>` : ""}
  </li>`;
}

function bps(value) {
  const numeric = Number(value);
  const percent = Number.isFinite(numeric) ? numeric / 100 : 0;
  return `${percent.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
}

function featureFlagRow(flag, access) {
  const rollout = flag.latest_rollout;
  const versions = Array.isArray(flag.versions) ? flag.versions : [];
  const latestVersion = versions[0] ?? {
    version_number: flag.current_version,
    rules: flag.rules,
    exposure_count: 0,
  };
  return `<tr>
    <td data-label="Flag / Head"><strong>${escapeHtml(flag.display_name)}</strong><small>${escapeHtml(flag.flag_code)}</small><small>head v${number(flag.current_version)} · rev ${number(flag.revision)}</small>${status(flag.status)}</td>
    <td data-label="版本与规则">
      <div class="platform-feature-versions">${versions.map((version) => featureVersion(flag, version)).join("")}</div>
      ${access.canWrite && flag.status === "active" ? featureVersionForm(flag, latestVersion, access.loading) : ""}
    </td>
    <td data-label="发布阶梯">${rollout ? `<strong>${number(rollout.target_percentage_bps) / 100}%</strong><small>${escapeHtml(rollout.environment_code)} · ${escapeHtml(rollout.region_code)}</small><small>${escapeHtml(rollout.action)} · ${formatTime(rollout.updated_at)}</small>${status(rollout.status)}` : '<span class="platform-restricted">尚未发布</span>'}</td>
    <td data-label="审批 / 健康">${rollout ? `<strong>${number(rollout.approval_count)} / ${flag.risk_level === "p0" ? "2" : "0"} 票</strong><small>${escapeHtml(rollout.health_status)} · watermark ${rollout.health_source_watermark === null ? "-" : number(rollout.health_source_watermark)}</small><small>rollout rev ${number(rollout.revision)}</small>` : "-"}</td>
    <td data-label="曝光"><strong>${number(flag.exposure_count)}</strong><small>${versions.map((version) => `v${number(version.version_number)} ${number(version.exposure_count)}`).join(" · ") || "-"}</small></td>
    <td data-label="操作"><div class="platform-feature-actions">${featureActions(flag, latestVersion, access)}</div></td>
  </tr>`;
}

function featureVersion(flag, version) {
  const rules = version.rules;
  const label = version.version_number === flag.current_version ? "HEAD" : "CANDIDATE";
  return `<div><strong>v${number(version.version_number)} · ${label}</strong><small>${number(rules.percentage_bps) / 100}% · ${escapeHtml(rules.environments.join(", "))}</small><small>${escapeHtml(rules.regions.join(", ") || "all regions")}${rules.cohort_code ? ` · ${escapeHtml(rules.cohort_code)}` : ""}</small></div>`;
}

function featureActions(flag, latestVersion, access) {
  if (flag.status !== "active") return '<span class="platform-restricted">已关闭</span>';
  const rollout = flag.latest_rollout;
  const active = new Set(["pending_approval", "scheduled", "running"]).has(rollout?.status);
  const forms = [];
  if (access.canWrite && !active) {
    forms.push(featureStartForm(flag, latestVersion, access.loading));
  }
  if (access.canApprove && rollout?.status === "pending_approval") {
    forms.push(featureRolloutCommandForm(flag, rollout, "approve", "审批", access.loading));
  }
  if (access.canWrite && new Set(["scheduled", "running"]).has(rollout?.status)) {
    forms.push(featureRolloutCommandForm(flag, rollout, "pause", "暂停", access.loading));
  }
  if (access.canWrite && rollout?.status === "paused") {
    forms.push(featureRolloutCommandForm(flag, rollout, "resume", "恢复", access.loading));
  }
  if (access.canWrite && rollout?.status === "completed" && flag.current_version > 1) {
    forms.push(featureRolloutCommandForm(flag, rollout, "rollback", "回滚", access.loading));
  }
  if (access.canKill) forms.push(featureKillForm(flag, access.loading));
  return forms.join("") || '<span class="platform-restricted">只读</span>';
}

function featureStartForm(flag, version, loading) {
  const rules = version.rules;
  return `<form data-platform-feature-command-form>
    ${featureCommandFields(flag, null, "start", flag.revision)}
    <input type="hidden" name="versionNumber" value="${number(version.version_number)}" />
    <label><span>阶梯 bps</span><input name="percentageBps" type="number" min="0" max="${number(rules.percentage_bps)}" step="100" value="${number(rules.percentage_bps)}" required /></label>
    <label><span>环境</span><select name="environmentCode">${rules.environments.map((item) => `<option value="${escapeAttribute(item)}">${escapeHtml(item)}</option>`).join("")}</select></label>
    <label><span>Region</span><input name="regionCode" value="${escapeAttribute(rules.regions[0] ?? "global")}" required /></label>
    ${featureConfirm("开始", loading, "platform-primary")}
  </form>`;
}

function featureRolloutCommandForm(flag, rollout, action, label, loading) {
  return `<form data-platform-feature-command-form>
    ${featureCommandFields(flag, rollout.rollout_id, action, rollout.revision)}
    ${featureConfirm(label, loading, action === "rollback" ? "platform-danger" : "")}
  </form>`;
}

function featureCommandFields(flag, rolloutId, action, revision) {
  return `<input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
    <input type="hidden" name="rolloutId" value="${escapeAttribute(rolloutId ?? "")}" />
    <input type="hidden" name="action" value="${escapeAttribute(action)}" />
    <input type="hidden" name="revision" value="${number(revision)}" />`;
}

function featureConfirm(label, loading, className = "") {
  return `<label class="platform-check"><input type="checkbox" name="confirmed" required /><span>确认</span></label><button class="${className}" type="submit" ${loading ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}

function featureKillForm(flag, loading) {
  return `<form data-platform-feature-kill-form>
    <input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
    <input type="hidden" name="revision" value="${number(flag.revision)}" />
    <label><span>Kill 原因</span><select name="reasonCode"><option value="incident_containment">事件处置</option><option value="security_containment">安全处置</option><option value="release_rollback">发布回滚</option></select></label>
    ${featureConfirm("Kill", loading, "platform-danger")}
  </form>`;
}

function featureVersionForm(flag, version, loading) {
  const rules = version.rules;
  return `<details class="platform-feature-version-editor"><summary>追加版本</summary>
    <form data-platform-feature-create-form>
      <input type="hidden" name="action" value="version" />
      <input type="hidden" name="flagId" value="${escapeAttribute(flag.flag_id)}" />
      <input type="hidden" name="revision" value="${number(flag.revision)}" />
      <input type="hidden" name="flagCode" value="${escapeAttribute(flag.flag_code)}" />
      <input type="hidden" name="displayName" value="${escapeAttribute(flag.display_name)}" />
      <input type="hidden" name="riskLevel" value="${escapeAttribute(flag.risk_level)}" />
      ${featureRuleFields(rules)}
      ${featureConfirm("保存 v" + (version.version_number + 1), loading, "platform-primary")}
    </form>
  </details>`;
}

function featureCreateForm() {
  return `<section class="platform-band platform-feature-create">
    <div class="platform-section-heading"><div><p>Closed rule set</p><h2>创建功能开关</h2></div></div>
    <form data-platform-feature-create-form autocomplete="off">
      <input type="hidden" name="action" value="create" />
      <input type="hidden" name="flagId" value="" />
      <input type="hidden" name="revision" value="" />
      <label><span>Flag code</span><input name="flagCode" pattern="[a-z0-9][a-z0-9_.-]{2,79}" required /></label>
      <label><span>显示名称</span><input name="displayName" maxlength="120" required /></label>
      <label><span>风险等级</span><select name="riskLevel"><option value="standard">Standard</option><option value="p0">P0</option></select></label>
      ${featureRuleFields({ environments: ["production"], regions: [], cohort_code: null, percentage_bps: 10000, prerequisite_flag_code: null })}
      ${featureConfirm("创建", false, "platform-primary")}
    </form>
  </section>`;
}

function featureRuleFields(rules) {
  return `<fieldset class="platform-feature-environments"><legend>环境</legend>${["development", "staging", "production"].map((environment) => `<label class="platform-check"><input type="checkbox" name="environments" value="${environment}" ${rules.environments.includes(environment) ? "checked" : ""} /><span>${environment}</span></label>`).join("")}</fieldset>
    <label><span>Regions（逗号分隔）</span><input name="regions" value="${escapeAttribute(rules.regions.join(","))}" /></label>
    <label><span>最大比例 bps</span><input name="percentageBps" type="number" min="0" max="10000" step="100" value="${number(rules.percentage_bps)}" required /></label>
    <label><span>Cohort code</span><input name="cohortCode" value="${escapeAttribute(rules.cohort_code ?? "")}" /></label>
    <label><span>前置 Flag</span><input name="prerequisiteFlagCode" value="${escapeAttribute(rules.prerequisite_flag_code ?? "")}" /></label>`;
}

function featureStatusOptions(selected) {
  return [["", "全部"], ["active", "Active"], ["killed", "Killed"], ["disabled", "Disabled"]]
    .map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function featureRiskOptions(selected) {
  return [["", "全部"], ["standard", "Standard"], ["p0", "P0"]]
    .map(([value, label]) => `<option value="${value}" ${selected === value ? "selected" : ""}>${label}</option>`).join("");
}

function queueLabel(value) {
  return ({ critical: "Critical", standard: "Standard", bulk: "Bulk" })[value] ?? "Unknown";
}

function notificationTemplatesView(state, permissions) {
  const response = state.notificationTemplates ?? {};
  const templates = Array.isArray(response.templates) ? response.templates : [];
  const canWrite = permissions.has("platform.notification_template.write");
  const defaultEvent = "invoice.issued";
  return `
    <header class="platform-page-heading">
      <div><p>Delivery content</p><h1>通知模板</h1></div>
      <span>Catalog revision ${number(response.catalog_revision ?? 0)}</span>
    </header>
    <section class="platform-band">
      <div class="platform-section-heading">
        <div><p>Immutable history</p><h2>模板版本</h2></div>
        <span>${number(templates.length)} 个版本</span>
      </div>
      <div class="platform-table-wrap">
        <table class="platform-notification-template-table">
          <thead><tr><th>模板</th><th>事件</th><th>渠道 / 语言</th><th>版本</th><th>状态</th><th>正文摘要</th></tr></thead>
          <tbody>${templates.map(notificationTemplateRow).join("") || emptyRow("暂无模板版本", 6)}</tbody>
        </table>
      </div>
    </section>
    ${canWrite ? `
      <section class="platform-band platform-notification-editor">
        <div class="platform-section-heading">
          <div><p>Controlled editor</p><h2>创建新版本</h2></div>
        </div>
        <form data-platform-notification-template-form autocomplete="off">
          <label><span>模板代码</span><input name="templateCode" pattern="[a-z0-9][a-z0-9_.-]{2,79}" maxlength="80" value="invoice_issued" required /></label>
          <label><span>事件</span><select name="eventType">${notificationEventOptions(defaultEvent)}</select></label>
          <label><span>渠道</span><select name="channel"><option value="in_app">站内</option><option value="email" selected>邮件</option><option value="webhook">Webhook</option></select></label>
          <label><span>语言</span><select name="locale"><option value="en">English</option><option value="zh-CN">简体中文</option></select></label>
          <label class="platform-notification-parameters"><span>允许参数</span><output data-template-parameters>${escapeHtml(NOTIFICATION_EVENT_PARAMETERS[defaultEvent].join(", "))}</output></label>
          <label class="platform-notification-subject"><span>主题</span><input name="subjectTemplate" maxlength="200" value="Invoice {{invoice_number_masked}} issued" required /></label>
          <label class="platform-notification-body"><span>正文</span><textarea name="bodyTemplate" maxlength="4000" rows="6" required>Invoice {{invoice_number_masked}} for {{total}} {{currency}} was issued at {{issued_at}}.</textarea></label>
          <label><span>版本状态</span><select name="status"><option value="draft">草稿</option><option value="published">发布</option></select></label>
          <button class="platform-primary" type="submit" ${state.loading ? "disabled" : ""}>创建版本</button>
        </form>
      </section>
    ` : ""}
  `;
}

function notificationTemplateRow(template) {
  return `<tr>
    <td data-label="模板"><strong>${escapeHtml(template.template_code)}</strong><small>${escapeHtml(template.notification_template_id)}</small></td>
    <td data-label="事件"><strong>${escapeHtml(notificationEventLabel(template.event_type))}</strong><small>${escapeHtml(template.category)}</small></td>
    <td data-label="渠道 / 语言"><strong>${escapeHtml(template.channel)}</strong><small>${escapeHtml(template.locale)}</small></td>
    <td data-label="版本"><strong>v${number(template.version)}</strong><small>rev ${number(template.revision)}</small></td>
    <td data-label="状态">${status(template.status)}<small>${formatTime(template.published_at ?? template.created_at)}</small></td>
    <td data-label="正文摘要"><strong>${escapeHtml(template.subject_template)}</strong><small>${escapeHtml(template.content_sha256.slice(0, 12))}</small></td>
  </tr>`;
}

function notificationEventOptions(selected) {
  return Object.keys(NOTIFICATION_EVENT_PARAMETERS)
    .map((eventType) => `<option value="${escapeAttribute(eventType)}" ${eventType === selected ? "selected" : ""}>${escapeHtml(notificationEventLabel(eventType))}</option>`)
    .join("");
}

function notificationEventLabel(value) {
  return ({
    "security.sign_in_risk": "登录风险",
    "subscription.status_changed": "订阅状态变更",
    "verification.status_changed": "认证状态变更",
    "payment.status_changed": "支付状态变更",
    "invoice.issued": "发票开具",
    "invoice.reversed": "发票冲红",
    "infrastructure.backup_completed": "备份完成",
    "gateway.health_changed": "Gateway 健康变更",
  })[value] ?? "未知事件";
}

/** 渲染企业认证审核队列，案件投影不显示 Workspace、主体明文或对象引用。 */
function verificationView(state, permissions) {
  const cases = state.verification?.cases ?? [];
  const canReview = permissions.has("platform.verification.review");
  return `
    <header class="platform-page-heading"><div><p>Business identity</p><h1>企业认证审核</h1></div><span>${number(state.verification?.total ?? 0)} 个案件</span></header>
    <section class="platform-band">
      <form class="platform-billing-filter" data-platform-verification-filter-form>
        <label><span>案件状态</span><select name="status"><option value="" ${state.verificationStatus ? "" : "selected"}>全部</option>${verificationStatusOptions(state.verificationStatus)}</select></label>
        <button type="submit">筛选</button>
      </form>
      <div class="platform-table-wrap"><table class="platform-verification-table">
        <thead><tr><th>案件</th><th>主体</th><th>状态</th><th>扫描</th><th>证据</th><th>操作</th></tr></thead>
        <tbody>${cases.map((item) => verificationRow(item, canReview)).join("") || emptyRow("暂无认证案件", 6)}</tbody>
      </table></div>
      <div class="platform-pagination"><span>第 ${number(Math.floor((state.verificationOffset ?? 0) / 20) + 1)} 页</span><div><button type="button" data-platform-action="verification-previous" ${(state.verificationOffset ?? 0) > 0 ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="verification-next" ${(state.verificationOffset ?? 0) + cases.length < (state.verification?.total ?? 0) ? "" : "disabled"}>下一页</button></div></div>
    </section>`;
}

/** 渲染单个安全案件；证据 ID 和审核控件仅对动态 reviewer 权限可见。 */
function verificationRow(item, canReview) {
  const active = new Set(["submitted", "in_review"]).has(item.status);
  const evidence = item.evidence_items ?? [];
  return `<tr>
    <td data-label="案件"><strong>${escapeHtml(item.business_verification_intent_id)}</strong><small>Profile rev ${number(item.profile_revision)} · Case rev ${number(item.revision)}</small></td>
    <td data-label="主体"><strong>${escapeHtml(item.country_code)}</strong><small>${escapeHtml(entityTypeLabel(item.entity_type))}</small></td>
    <td data-label="状态">${status(item.status)}<small>${formatTime(item.submitted_at)}</small></td>
    <td data-label="扫描"><strong>${escapeHtml(scanStateLabel(item.evidence))}</strong><small>${number(item.evidence?.evidence_count ?? 0)} 份</small></td>
    <td data-label="证据"><div class="platform-evidence-list">${evidence.map((entry) => `<div><span>${escapeHtml(evidenceTypeLabel(entry.evidence_type))} · ${escapeHtml(entry.mime_type)} · ${number(entry.size_bytes)} B</span>${canReview && active ? `<button class="platform-icon-button" type="button" data-platform-action="verification-evidence-token" data-intent-id="${escapeAttribute(item.business_verification_intent_id)}" data-evidence-id="${escapeAttribute(entry.verification_evidence_id)}" title="签发证据授权" aria-label="签发证据授权">↓</button>` : ""}</div>`).join("") || '<span class="platform-restricted">无 active 证据</span>'}</div></td>
    <td data-label="操作">${canReview && active ? `<div class="platform-verification-actions"><button class="platform-primary" type="button" data-platform-action="verification-approve" data-intent-id="${escapeAttribute(item.business_verification_intent_id)}" data-revision="${number(item.revision)}">批准</button><form data-platform-verification-reject-form><input name="intentId" type="hidden" value="${escapeAttribute(item.business_verification_intent_id)}" /><input name="revision" type="hidden" value="${number(item.revision)}" /><select name="reasonCode"><option value="document_invalid">证件无效</option><option value="identity_mismatch">主体不一致</option><option value="scan_failed">扫描失败</option><option value="profile_changed">资料已变化</option></select><button type="submit">拒绝</button></form></div>` : '<span class="platform-restricted">只读</span>'}</td>
  </tr>`;
}

function infrastructureView(state, permissions) {
  const value = state.infrastructure;
  const resources = value?.resources ?? [];
  const backup = state.backup?.backup;
  return `
    <header class="platform-page-heading"><div><p>Production runtime</p><h1>基础设施</h1></div><span>${status(value?.overall_status ?? "unavailable")}</span></header>
    <section class="platform-metrics" aria-label="恢复目标">
      ${metric("组件", resources.length, "受控生产依赖")}
      ${metric("RPO", value?.rpo_minutes ?? 0, "分钟")}
      ${metric("RTO", value?.rto_minutes ?? 0, "分钟")}
    </section>
    <section class="platform-band platform-infrastructure-status">
      <div class="platform-section-heading"><div><p>Health projection</p><h2>组件状态</h2></div><span>${number(resources.filter((item) => item.verified).length)} 个已验证</span></div>
      <div class="platform-table-wrap"><table><thead><tr><th>组件</th><th>后端</th><th>状态</th><th>延迟</th><th>最近验证</th></tr></thead><tbody>${resources.map(infrastructureResourceRow).join("") || emptyRow("暂无基础设施状态", 5)}</tbody></table></div>
    </section>
    ${permissions.has("platform.backup.create") ? backupForm() : ""}
    ${backup ? backupPanel(backup) : ""}`;
}

function infrastructureResourceRow(resource) {
  return `<tr>
    <td data-label="组件"><strong>${escapeHtml(infrastructureKindLabel(resource.resource_kind))}</strong><small>${escapeHtml(resource.version_summary ?? "-")}</small></td>
    <td data-label="后端">${escapeHtml(statusLabel(resource.provider_code))}</td>
    <td data-label="状态">${status(resource.status)}${resource.error_code ? `<small>${escapeHtml(resource.error_code)}</small>` : ""}</td>
    <td data-label="延迟">${resource.latency_ms === null ? "-" : `${number(resource.latency_ms)} ms`}</td>
    <td data-label="最近验证">${formatTime(resource.last_verified_at)}</td>
  </tr>`;
}

// Gateway 视图仅展示有界 Cell 摘要，并按实时权限裁剪 placement 写表单。
function gatewayView(state, permissions) {
  const cells = state.gatewayCells?.cells ?? [];
  const active = cells.filter((cell) => cell.status === "active" && cell.health_status === "healthy");
  const placement = state.gatewayPlacement?.placement;
  return `
    <header class="platform-page-heading"><div><p>Regional data plane</p><h1>Gateway</h1></div><span>${number(active.length)} 个可用 Cell</span></header>
    <section class="platform-metrics" aria-label="Gateway fleet summary">
      ${metric("Cells", cells.length, "区域分片")}
      ${metric("可用", active.length, "健康且接流")}
      ${metric("配置水位", Math.max(0, ...cells.map((cell) => cell.config_watermark)), "最新 generation")}
    </section>
    <section class="platform-band platform-gateway-cells">
      <div class="platform-section-heading"><div><p>Bounded fleet projection</p><h2>Cell 状态</h2></div><span>${number(cells.length)} 个 Cell</span></div>
      <div class="platform-table-wrap"><table class="platform-gateway-table"><thead><tr><th>Cell</th><th>Region</th><th>容量</th><th>服务状态</th><th>健康</th><th>Release</th><th>Config</th></tr></thead><tbody>${cells.map(gatewayCellRow).join("") || emptyRow("暂无 Gateway Cell", 7)}</tbody></table></div>
    </section>
    ${permissions.has("platform.gateway.manage") ? gatewayPlacementForm(active) : ""}
    ${placement ? gatewayPlacementResult(placement) : ""}`;
}

// Cell 行保持固定枚举列，避免把拓扑地址或客户信息扩展进运维表格。
function gatewayCellRow(cell) {
  return `<tr>
    <td data-label="Cell"><strong>${escapeHtml(cell.cell_code)}</strong><small>${escapeHtml(statusLabel(cell.capacity_class))}</small></td>
    <td data-label="Region">${escapeHtml(cell.region_code)}</td>
    <td data-label="容量">${status(cell.capacity_band)}</td>
    <td data-label="服务状态">${status(cell.status)}</td>
    <td data-label="健康">${status(cell.health_status)}</td>
    <td data-label="Release">${number(cell.release_watermark)}</td>
    <td data-label="Config">${number(cell.config_watermark)}</td>
  </tr>`;
}

// Placement 表单要求显式 revision、驻留版本和确认，写入仍由服务端 CAS 与权限复核裁决。
function gatewayPlacementForm(cells) {
  const options = cells.map((cell) => `<option value="${escapeAttribute(cell.cell_code)}">${escapeHtml(cell.cell_code)} · ${escapeHtml(cell.region_code)}</option>`).join("");
  return `
    <section class="platform-band platform-gateway-placement">
      <div class="platform-section-heading"><div><p>Signed placement</p><h2>发布 Workspace placement</h2></div><span>Workspace 路由</span></div>
      <form data-platform-gateway-placement-form autocomplete="off">
        <label><span>Workspace ID</span><input name="workspaceId" pattern="wsp_[0-9a-f]{32}" maxlength="36" required /></label>
        <label><span>当前 revision</span><input name="revision" type="number" min="0" value="0" required /></label>
        <label><span>Primary Cell</span><select name="primaryCellCode" required>${options}</select></label>
        <label><span>Failover Cell</span><select name="failoverCellCode"><option value="">禁止 failover</option>${options}</select></label>
        <label><span>Residency policy</span><input name="residencyPolicyVersion" value="" placeholder="cn-residency-v1" maxlength="80" required /></label>
        <label><span>变更原因</span><select name="reasonCode"><option value="initial_placement">Initial placement</option><option value="capacity_rebalance">Capacity rebalance</option><option value="regional_recovery">Regional recovery</option></select></label>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认发布新 generation</span></label>
        <button class="platform-primary" type="submit">发布 placement</button>
      </form>
    </section>`;
}

// 写入结果只回显安全 placement 投影和 generation，不渲染签名制品。
function gatewayPlacementResult(placement) {
  return `
    <section class="platform-band platform-gateway-result">
      <div class="platform-section-heading"><div><p>Current response</p><h2>${escapeHtml(shortWorkspace(placement.workspace_id))}</h2></div>${status("active")}</div>
      <dl class="platform-fact-grid">
        ${fact("Primary", `${placement.primary_cell_code} · ${placement.primary_region_code}`)}
        ${fact("Failover", placement.failover_cell_code ?? "禁止")}
        ${fact("Revision", String(placement.revision))}
        ${fact("Generation", String(placement.snapshot_generation))}
      </dl>
    </section>`;
}

function backupForm() {
  return `
    <section class="platform-band platform-backup-create">
      <div class="platform-section-heading"><div><p>Verified recovery</p><h2>创建备份</h2></div><span>隔离恢复门禁</span></div>
      <form data-platform-backup-form autocomplete="off">
        <label><span>备份类型</span><select name="backupType"><option value="full">完整备份</option><option value="incremental">增量备份</option></select></label>
        <label><span>保留天数</span><input name="retentionDays" type="number" min="1" max="3650" value="30" required /></label>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认创建平台备份</span></label>
        <button class="platform-primary" type="submit">创建备份</button>
      </form>
    </section>`;
}

function backupPanel(backup) {
  const verification = backup.verification ?? {};
  return `
    <section class="platform-band platform-backup-detail">
      <div class="platform-section-heading"><div><p>BackupSet</p><h2>${escapeHtml(backup.backup_id)}</h2></div><div class="platform-row-actions">${status(backup.status)}<button class="platform-icon-button" type="button" data-platform-action="backup-refresh" title="刷新备份状态" aria-label="刷新备份状态">↻</button></div></div>
      <dl>
        ${fact("来源", statusLabel(backup.source_backend))}
        ${fact("水位", backup.source_watermark ?? "-")}
        ${fact("Schema head", backup.schema_head ?? "-")}
        ${fact("内容摘要", shortHash(backup.content_sha256))}
        ${fact("保留至", formatTime(backup.retention_until))}
        ${fact("恢复校验", statusLabel(verification.status ?? "pending"))}
      </dl>
      ${backup.error_code ? `<div class="platform-feedback is-error"><strong>${escapeHtml(backup.error_code)}</strong></div>` : ""}
    </section>`;
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

function billingView(state, permissions) {
  const canReadBilling = permissions.has("platform.billing.read");
  const canReadCatalog = permissions.has("platform.catalog.read");
  const catalogs = state.plans?.plans ?? [];
  const versions = catalogs.flatMap((catalog) => (catalog.versions ?? []).map((version) => ({
    ...version,
    plan_code: catalog.plan_code,
    display_name: catalog.display_name,
    catalog_status: catalog.status,
  })));
  return `
    <header class="platform-page-heading"><div><p>SaaS billing</p><h1>${canReadBilling ? "平台账单" : "套餐目录"}</h1></div><span>${canReadBilling ? `${number(state.billing?.cycles?.length ?? 0)} 个周期` : `${number(catalogs.length)} 个目录 · ${number(versions.length)} 个版本`}</span></header>
    ${canReadBilling ? platformBillingCycles(state, permissions) : ""}
    ${canReadBilling ? platformRefundQueue(state, permissions) : ""}
    ${canReadBilling ? platformInvoiceReconciliation(state, permissions) : ""}
    ${canReadCatalog ? platformCatalog(catalogs, versions, permissions) : ""}
  `;
}

function complianceView(state, permissions) {
  return `
    <header class="platform-page-heading"><div><p>Tax governance</p><h1>合规复核</h1></div><span>账务与合规双域</span></header>
    ${platformInvoiceReconciliation(state, permissions)}
  `;
}

function platformBillingCycles(state, permissions) {
  const cycles = state.billing?.cycles ?? [];
  const adjustable = cycles.flatMap((cycle) => (
    cycle.status === "open" ? [] : cycle.meters
      .filter((meter) => meter.summary_sha256)
      .map((meter) => ({ cycle, meter }))
  ));
  return `
    <section class="platform-band platform-billing-cycles">
      <div class="platform-section-heading"><div><p>Cycle reconciliation</p><h2>周期与差异状态</h2></div><span>${number(cycles.filter((cycle) => cycle.status === "reconciliation_required").length)} 个待对账</span></div>
      <form class="platform-billing-filter" data-platform-billing-filter-form>
        <label><span>周期状态</span><select name="status"><option value="" ${state.billingStatus ? "" : "selected"}>全部</option>${billingStatusOptions(state.billingStatus)}</select></label>
        <button type="submit">筛选</button>
      </form>
      <div class="platform-table-wrap"><table class="platform-billing-table"><thead><tr><th>Workspace</th><th>周期</th><th>状态</th><th>请求</th><th>Token</th><th>有效金额</th><th>更新</th></tr></thead><tbody>${cycles.map(platformBillingCycleRow).join("") || emptyRow("暂无账单周期", 7)}</tbody></table></div>
      <div class="platform-pagination"><span>第 ${number((state.billingCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="billing-previous" ${state.billingCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="billing-next" ${state.billing?.next_cursor ? "" : "disabled"}>下一页</button></div></div>
    </section>
    ${permissions.has("platform.billing.adjust") ? platformBillingAdjustmentForm(adjustable) : ""}
  `;
}

function platformBillingCycleRow(cycle) {
  const requests = cycle.meters.find((meter) => meter.meter_code === "api_requests");
  const tokens = cycle.meters.find((meter) => meter.meter_code === "total_tokens");
  return `<tr>
    <td data-label="Workspace"><strong>${escapeHtml(cycle.workspace_id)}</strong><small>${escapeHtml(cycle.billing_cycle_id)}</small></td>
    <td data-label="周期"><strong>${formatDate(cycle.period_started_at)} - ${formatDate(cycle.period_ends_at)}</strong><small>Rev ${number(cycle.revision)} · watermark ${number(cycle.source_watermark)}</small></td>
    <td data-label="状态">${status(cycle.status)}</td>
    <td data-label="请求"><strong>${number(requests?.usage_quantity)} / ${number(requests?.allocated_quantity)}</strong><small>超额 ${number(requests?.overage_quantity)}</small></td>
    <td data-label="Token"><strong>${number(tokens?.usage_quantity)}</strong><small>调整 ${signedNumber(tokens?.adjustment_quantity)}</small></td>
    <td data-label="有效金额"><strong>${escapeHtml(requests ? `${requests.currency} ${requests.effective_amount}` : "-")}</strong></td>
    <td data-label="更新">${formatTime(cycle.updated_at)}</td>
  </tr>`;
}

function platformBillingAdjustmentForm(targets) {
  return `
    <section class="platform-band platform-billing-adjustment">
      <div class="platform-section-heading"><div><p>Append-only adjustment</p><h2>追加账单调整</h2></div><span>摘要哈希 CAS</span></div>
      ${targets.length ? `<form data-platform-billing-adjustment-form autocomplete="off">
        <label class="platform-billing-target"><span>账单摘要</span><select name="targetKey" required>${targets.map(({ cycle, meter }) => `<option value="${escapeAttribute(`${cycle.billing_cycle_id}:${meter.meter_code}`)}">${escapeHtml(shortWorkspace(cycle.workspace_id))} · ${escapeHtml(meterLabel(meter.meter_code))} · ${escapeHtml(billingStatusLabel(cycle.status))}</option>`).join("")}</select></label>
        <label><span>方向</span><select name="reasonCode"><option value="manual_credit">贷记</option><option value="manual_debit">借记</option></select></label>
        <label><span>数量</span><input name="quantity" type="number" min="0" max="1000000000000" step="1" value="0" required /></label>
        <label><span>金额</span><input name="amount" inputmode="decimal" pattern="(?:0|[1-9][0-9]{0,10})\.[0-9]{2}" value="0.00" required /></label>
        <button class="platform-primary" type="submit">追加调整</button>
      </form>` : '<p class="platform-empty">当前页没有可调整的已冻结摘要。</p>'}
    </section>
  `;
}

function platformRefundQueue(state, permissions) {
  const refunds = state.refunds?.refunds ?? [];
  const canReview = permissions.has("platform.billing.adjust");
  return `
    <section class="platform-band platform-refund-queue">
      <div class="platform-section-heading"><div><p>Refund review</p><h2>退款审核队列</h2></div><span>${number(refunds.length)} 笔</span></div>
      <form class="platform-billing-filter" data-platform-refund-filter-form>
        <label><span>退款状态</span><select name="status">${refundStatusOptions(state.refundStatus)}</select></label>
        <button type="submit">查询</button>
      </form>
      <div class="platform-table-wrap">
        <table class="platform-refund-table">
          <thead><tr><th>退款 / 订单</th><th>金额</th><th>原因</th><th>状态</th><th>申请时间</th><th>审核</th></tr></thead>
          <tbody>${refunds.map((refund) => platformRefundRow(refund, canReview)).join("") || emptyRow("当前筛选下没有退款申请", 6)}</tbody>
        </table>
      </div>
    </section>
  `;
}

function platformRefundRow(refund, canReview) {
  const requested = refund.status === "requested";
  return `
    <tr>
      <td data-label="退款 / 订单"><strong>${escapeHtml(refund.refund_id)}</strong><small>${escapeHtml(refund.order_id)}</small></td>
      <td data-label="金额"><strong>${escapeHtml(formatMinorMoney(refund.amount_minor, refund.currency))}</strong><small>原币最小单位 ${number(refund.amount_minor)}</small></td>
      <td data-label="原因">${escapeHtml(refundReasonLabel(refund.reason_code))}</td>
      <td data-label="状态"><span class="platform-status">${escapeHtml(refundStatusLabel(refund.status))}</span></td>
      <td data-label="申请时间">${formatTime(refund.requested_at)}</td>
      <td data-label="审核">${canReview && requested ? `
        <div class="platform-refund-actions">
          <button class="platform-primary" type="button" data-platform-action="refund-approve" data-refund-id="${escapeAttribute(refund.refund_id)}" data-revision="${number(refund.state_revision)}">批准</button>
          <form data-platform-refund-reject-form autocomplete="off">
            <input name="refundId" type="hidden" value="${escapeAttribute(refund.refund_id)}" />
            <input name="revision" type="hidden" value="${number(refund.state_revision)}" />
            <select name="reasonCode" aria-label="拒绝原因"><option value="amount_invalid">金额不符</option><option value="evidence_missing">证据不足</option></select>
            <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认</span></label>
            <button type="submit">拒绝</button>
          </form>
        </div>` : '<span class="platform-restricted">只读</span>'}</td>
    </tr>
  `;
}

function platformInvoiceReconciliation(state, permissions) {
  const roles = new Set(state.session?.context?.operator?.roles ?? []);
  const canOperate = (roles.has("platform_billing") && permissions.has("platform.billing.read"))
    || (roles.has("platform_compliance") && permissions.has("platform.compliance.read"));
  if (!canOperate) return "";
  const response = state.reconciliation ?? {};
  const run = response.reconciliation ?? null;
  const differences = Array.isArray(response.differences) ? response.differences : [];
  const reversal = state.reversal?.reversal ?? null;
  return `
    <section class="platform-band platform-reconciliation">
      <div class="platform-section-heading"><div><p>Frozen watermarks</p><h2>订单、支付、Meter 与发票对账</h2></div>${run ? status(run.status) : ""}</div>
      ${run ? renderReconciliationRun(run) : `<form class="platform-reconciliation-create" data-platform-reconciliation-form>
        <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认冻结当前五类事实水位并启动对账</span></label>
        <button class="platform-primary" type="submit">创建对账</button>
      </form>`}
      ${run ? renderReconciliationDifferences(state, differences) : ""}
    </section>
    <section class="platform-band platform-invoice-reversal">
      <div class="platform-section-heading"><div><p>Dual-domain approval</p><h2>发票红字冲销</h2></div>${reversal ? status(reversal.status) : ""}</div>
      <div class="platform-reversal-forms">
        <form data-platform-reversal-form autocomplete="off">
          <label><span>发票申请 ID</span><input name="invoiceId" pattern="ivr_[0-9a-f]{32}" maxlength="36" required /></label>
          <label><span>冲红原因</span><select name="reasonCode"><option value="invoice_error">开票错误</option><option value="customer_correction">客户资料更正</option><option value="contract_canceled">合同取消</option></select></label>
          <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认提交双域审批</span></label>
          <button class="platform-danger" type="submit">创建冲红申请</button>
        </form>
        <form data-platform-reversal-review-form autocomplete="off">
          <label><span>冲红申请 ID</span><input name="reversalId" pattern="irr_[0-9a-f]{32}" maxlength="36" value="${escapeAttribute(reversal?.invoice_reversal_request_id ?? "")}" required /></label>
          <label><span>审批决定</span><select name="decision"><option value="approve">批准</option><option value="reject">拒绝</option></select></label>
          <label><span>审批依据</span><select name="reasonCode"><option value="facts_confirmed">事实已核验</option><option value="policy_confirmed">政策已核验</option><option value="evidence_missing">证据不足</option><option value="policy_blocked">政策阻断</option></select></label>
          <label class="platform-check"><input name="confirmed" type="checkbox" required /><span>确认以当前账务或合规身份审批</span></label>
          <button class="platform-primary" type="submit">提交审批</button>
        </form>
      </div>
      ${reversal ? `<div class="platform-reversal-summary"><strong>${escapeHtml(reversalStatusLabel(reversal.status))}</strong><span>${escapeHtml(reversalReasonLabel(reversal.reason_code))} · 已批准域 ${escapeHtml(reversal.approved_domains.map(approvalDomainLabel).join("、") || "无")}</span><small>状态版本 ${number(reversal.state_revision)} · ${formatTime(reversal.updated_at)}</small></div>` : ""}
    </section>
  `;
}

function renderReconciliationRun(run) {
  return `
    <div class="platform-reconciliation-summary">
      <div><span>订单</span><strong>${number(run.order_watermark)}</strong></div>
      <div><span>支付</span><strong>${number(run.payment_watermark)}</strong></div>
      <div><span>Meter</span><strong>${number(run.meter_watermark)}</strong></div>
      <div><span>结算单</span><strong>${number(run.statement_watermark)}</strong></div>
      <div><span>发票</span><strong>${number(run.invoice_watermark)}</strong></div>
      <button class="platform-icon-button" type="button" data-platform-action="reconciliation-refresh" title="刷新对账" aria-label="刷新对账">↻</button>
    </div>
  `;
}

function renderReconciliationDifferences(state, differences) {
  return `
    <div class="platform-table-wrap">
      <table class="platform-reconciliation-table">
        <thead><tr><th>差异类型</th><th>左侧事实</th><th>右侧事实</th><th>原币差额</th><th>状态</th><th>发现时间</th></tr></thead>
        <tbody>${differences.map((item) => `<tr>
          <td data-label="差异类型"><strong>${escapeHtml(differenceTypeLabel(item.difference_type))}</strong></td>
          <td data-label="左侧事实"><strong>${escapeHtml(sourceTypeLabel(item.left_source_type))}</strong><small>${escapeHtml(shortReference(item.left_source_id))}</small></td>
          <td data-label="右侧事实"><strong>${escapeHtml(sourceTypeLabel(item.right_source_type))}</strong><small>${escapeHtml(shortReference(item.right_source_id))}</small></td>
          <td data-label="原币差额">${escapeHtml(formatMinorMoney(item.amount_difference_minor, item.currency))}</td>
          <td data-label="状态">${status(item.status)}</td>
          <td data-label="发现时间">${formatTime(item.detected_at)}</td>
        </tr>`).join("") || emptyRow("当前水位未发现差异", 6)}</tbody>
      </table>
    </div>
    <div class="platform-pagination"><span>第 ${number((state.reconciliationCursorHistory?.length ?? 0) + 1)} 页</span><div><button type="button" data-platform-action="reconciliation-previous" ${state.reconciliationCursorHistory?.length ? "" : "disabled"}>上一页</button><button type="button" data-platform-action="reconciliation-next" ${state.reconciliation?.next_cursor ? "" : "disabled"}>下一页</button></div></div>
  `;
}

function platformCatalog(catalogs, versions, permissions) {
  return `
    ${permissions.has("platform.catalog.write") ? platformPlanForm() : ""}
    <section class="platform-band platform-plan-versions">
      <div class="platform-section-heading"><div><p>Immutable versions</p><h2>套餐版本记录</h2></div><span>${number(catalogs.length)} 个目录 · ${number(versions.length)} 个版本</span></div>
      <div class="platform-table-wrap"><table class="platform-plan-table"><thead><tr><th>套餐</th><th>版本</th><th>状态</th><th>价格</th><th>试用</th><th>权益</th></tr></thead><tbody>${versions.map(platformPlanRow).join("") || emptyRow("暂无套餐版本", 6)}</tbody></table></div>
    </section>
  `;
}

function platformPlanForm() {
  return `
    <section class="platform-band platform-plan-publisher">
      <div class="platform-section-heading"><div><p>Version publisher</p><h2>创建套餐版本</h2></div></div>
      <form data-platform-plan-form autocomplete="off">
        <label><span>Plan code</span><input name="planCode" pattern="[a-z0-9][a-z0-9_-]{1,39}" maxlength="40" required /></label>
        <label><span>展示名称</span><input name="displayName" maxlength="80" required /></label>
        <label><span>展示顺序</span><input name="displayOrder" type="number" min="0" max="10000" value="100" required /></label>
        <label><span>计费周期</span><select name="billingPeriod"><option value="monthly">每月</option><option value="yearly">每年</option><option value="none">免费</option></select></label>
        <label><span>币种</span><select name="currency"><option value="CNY">CNY</option><option value="USD">USD</option></select></label>
        <label><span>单价</span><input name="unitPrice" inputmode="decimal" pattern="(?:0|[1-9][0-9]{0,10})\.[0-9]{2}" value="0.00" required /></label>
        <label><span>试用天数</span><input name="trialDays" type="number" min="0" max="90" value="0" required /></label>
        <label><span>月请求数</span><input name="gatewayRequests" type="number" min="0" max="1000000000" value="10000" required /></label>
        <label><span>Workspace 席位</span><input name="workspaceSeats" type="number" min="1" max="100000" value="5" required /></label>
        <label><span>模型别名</span><input name="modelAliases" type="number" min="1" max="100000" value="10" required /></label>
        <label><span>留存天数</span><input name="retentionDays" type="number" min="1" max="3650" value="30" required /></label>
        <fieldset class="platform-plan-switches"><legend>发布选项</legend><label class="platform-check"><input name="prioritySupport" type="checkbox" /><span>优先支持</span></label><label class="platform-check"><input name="publish" type="checkbox" /><span>立即发布</span></label></fieldset>
        <button class="platform-primary" type="submit">创建版本</button>
      </form>
    </section>
  `;
}

function platformPlanRow(version) {
  const entitlements = version.entitlements ?? {};
  return `<tr>
    <td data-label="套餐"><strong>${escapeHtml(version.display_name)}</strong><small>${escapeHtml(version.plan_code)}</small></td>
    <td data-label="版本"><strong>v${number(version.version_number)}</strong><small>${escapeHtml(version.plan_version_id)}</small></td>
    <td data-label="状态">${status(version.status)}<small>${escapeHtml(version.catalog_status)}</small></td>
    <td data-label="价格"><strong>${escapeHtml(version.billing_period === "none" ? "免费" : `${version.currency} ${version.unit_price}`)}</strong><small>${escapeHtml(statusLabel(version.billing_period))}</small></td>
    <td data-label="试用">${number(version.trial_days)} 天</td>
    <td data-label="权益"><strong>${number(entitlements.workspace_seats)} 席位 · ${number(entitlements.gateway_requests_per_month)} 次/月</strong><small>${number(entitlements.model_aliases)} 别名 · ${number(entitlements.retention_days)} 天留存${entitlements.priority_support ? " · 优先支持" : ""}</small></td>
  </tr>`;
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
    : /pending|watch|stale|at_risk|not_configured/u.test(text)
      ? "is-warn"
      : /revoked|expired|disabled|critical|suspended|deleted|conflict|unavailable/u.test(text)
        ? "is-danger"
        : "";
  return `<span class="platform-status ${kind}">${escapeHtml(statusLabel(text))}</span>`;
}

function roleLabel(value) { return String(value ?? "").replace(/^platform_/u, "").replaceAll("_", " "); }
function domainLabel(value) { return value === "workspace" ? "客户" : value === "platform" ? "平台" : value; }
function statusLabel(value) { return String(value).replaceAll("_", " "); }
function riskStatusOptions(current) { return [["", "全部"], ["open", "待处理"], ["under_review", "复核中"], ["restricted", "已限制"], ["cleared", "已清除"], ["closed", "已关闭"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function billingStatusLabel(value) { return ({ open: "进行中", closed: "已关账", reconciliation_required: "需要对账" })[value] ?? statusLabel(value); }
function billingStatusOptions(current) { return [["open", "进行中"], ["closed", "已关账"], ["reconciliation_required", "需要对账"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function refundStatusLabel(value) { return ({ requested: "待审核", processing: "处理中", succeeded: "已退款", failed: "失败", rejected: "已拒绝" })[value] ?? statusLabel(value); }
function refundStatusOptions(current) { return [["requested", "待审核"], ["processing", "处理中"], ["succeeded", "已退款"], ["failed", "失败"], ["rejected", "已拒绝"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
function refundReasonLabel(value) { return ({ customer_request: "客户申请", service_failure: "服务故障", duplicate_charge: "重复扣款" })[value] ?? statusLabel(value); }
function differenceTypeLabel(value) { return ({ order_payment_amount_mismatch: "订单与支付金额不一致", paid_order_missing_capture: "已支付订单缺少扣款", closed_cycle_missing_statement: "关账周期缺少结算单", meter_summary_watermark_mismatch: "Meter 摘要水位不一致", issued_invoice_missing_artifact: "已开票但缺少产物" })[value] ?? "未知差异"; }
function sourceTypeLabel(value) { return ({ order: "订单", billing_cycle: "账单周期", meter_summary: "Meter 摘要", invoice_request: "发票申请", payment_intent: "支付意图", billing_statement: "结算单", meter_reading: "Meter 读数", invoice_artifact: "发票产物" })[value] ?? "无"; }
function reversalStatusLabel(value) { return ({ pending_approvals: "等待账务与合规审批", queued: "已进入 Provider 队列", completed: "冲红完成", rejected: "已拒绝" })[value] ?? statusLabel(value); }
function reversalReasonLabel(value) { return ({ invoice_error: "开票错误", customer_correction: "客户资料更正", contract_canceled: "合同取消" })[value] ?? statusLabel(value); }
function approvalDomainLabel(value) { return value === "billing" ? "账务" : value === "compliance" ? "合规" : "未知"; }
function formatMinorMoney(value, currency) { const amount = Number(value); return Number.isSafeInteger(amount) ? new Intl.NumberFormat("zh-CN", { style: "currency", currency: new Set(["CNY", "USD"]).has(currency) ? currency : "CNY", currencyDisplay: "code" }).format(amount / 100) : "-"; }
/** 构造审核状态筛选选项，选择值只能来自后端固定枚举。 */
function verificationStatusOptions(current) { return [["submitted", "待审核"], ["in_review", "审核中"], ["approved", "已通过"], ["rejected", "已拒绝"], ["expired", "已过期"]].map(([value, label]) => `<option value="${value}" ${value === current ? "selected" : ""}>${label}</option>`).join(""); }
/** 将主体类型映射为安全文案，不显示法定名称或登记号。 */
function entityTypeLabel(value) { return ({ company: "企业", nonprofit: "非营利组织", government: "政府机构" })[value] ?? "未知主体"; }
/** 将证据类型映射为固定标签，不显示文件名和受控对象引用。 */
function evidenceTypeLabel(value) { return ({ business_license: "营业执照", registration_certificate: "登记证书", authorization_letter: "授权文件" })[value] ?? "证据"; }
/** 汇总证据扫描结论，Platform 列表不暴露各扫描器内部错误。 */
function scanStateLabel(value) {
  if (!value || !value.evidence_count) return "无证据";
  if (value.malware_status === "passed" && value.dlp_status === "passed") return "已通过";
  if (value.malware_status === "blocked" || value.dlp_status === "blocked") return "已阻断";
  if (value.malware_status === "pending" || value.dlp_status === "pending") return "扫描中";
  return "扫描失败";
}
function meterLabel(value) { return ({ api_requests: "API 请求", input_tokens: "输入 Token", output_tokens: "输出 Token", total_tokens: "总 Token" })[value] ?? statusLabel(value); }
function actionLabel(value) { return String(value).replaceAll("_", " ").replaceAll(".", " / "); }
function kindLabel(value) { return value === "organization" ? "Organization" : value === "personal" ? "Personal" : value; }
function navSymbol(view) { return ({ overview: "⌂", infrastructure: "▤", gateway: "↗", observability: "⌁", "abuse-risk": "!", operators: "◎", workspaces: "▦", support: "◇", audit: "≡", billing: "¤" })[view] ?? "·"; }
function infrastructureKindLabel(value) { return ({ database_cluster: "DatabaseCluster", cache_cluster: "CacheCluster", object_store: "ObjectStore", key_ring: "KeyRing" })[value] ?? statusLabel(value); }
function shortHash(value) { const text = String(value ?? ""); return text.length === 64 ? `${text.slice(0, 12)}…${text.slice(-8)}` : "-"; }
function emptyRow(label, columns = 5) { return `<tr><td colspan="${number(columns)}" class="platform-empty">${escapeHtml(label)}</td></tr>`; }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function signedNumber(value) { const amount = Number(value); return `${amount > 0 ? "+" : ""}${number(amount)}`; }
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date); }
function formatTime(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "-" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date); }
function shortWorkspace(value) { const text = String(value ?? ""); return text.length > 16 ? `${text.slice(0, 12)}…${text.slice(-4)}` : text; }
function shortReference(value) { const text = String(value ?? ""); return text.length > 20 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "-"; }
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
