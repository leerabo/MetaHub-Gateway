/** Dense, role-aware PRD-140 customer-success workbench. */

export function renderPlatformCustomerSuccess(state = {}, permissions = new Set(), context = {}) {
  const items = state.directory?.items ?? [];
  const selected = items.find(
    (item) => item.customer_account_id === state.selectedAccountId,
  ) ?? null;
  const canOperate = permissions.has("platform.customer_success.read");
  const openTasks = items.reduce((total, item) => total + Number(item.open_task_count ?? 0), 0);
  const risky = items.filter((item) => ["at_risk", "critical"].includes(item.health_risk)).length;
  return `<div data-platform-customer-success-root data-phase="${escapeAttribute(state.phase ?? "idle")}">
    <header class="platform-page-heading platform-customer-success-heading">
      <div><p>客户运营</p><h1>客户成功与续费</h1></div>
      <span>客户状态、待办与续费</span>
    </header>
    ${phaseBanner(state)}
    <section class="platform-metrics" aria-label="客户成功目录摘要">
      ${metric("本页客户", items.length, state.directory?.has_more ? "还有下一页" : "当前结果")}
      ${metric("健康风险", risky, "需要关注的客户")}
      ${metric("待办任务", openTasks, "待处理 / 处理中")}
      ${metric("续费机会", state.renewals?.items?.length ?? 0, "选中客户")}
    </section>
    <section class="platform-band platform-customer-directory">
      <div class="platform-section-heading"><div><p>组织客户</p><h2>客户目录</h2></div><span>最多 50 条 / 页</span></div>
      ${directoryFilters(state)}
      <div class="platform-customer-layout">
        <div class="platform-customer-list" role="listbox" aria-label="客户目录">
          ${items.map((item) => accountRow(item, state.selectedAccountId)).join("")
            || empty("暂无符合条件的客户。")}
          ${directoryPagination(state)}
        </div>
        <div class="platform-customer-detail">
          ${selected ? customerDetail(state, selected, permissions, context)
            : empty("选择一个客户以查看安全运营投影。")}
        </div>
      </div>
    </section>
    ${canOperate ? batchApprovalPanel(state, context) : ""}
  </div>`;
}

function directoryFilters(state) {
  const filters = state.filters ?? {};
  return `<form class="platform-filter-form platform-customer-filter" data-platform-customer-filter-form autocomplete="off">
    <label><span>客户编号</span><input name="customerAccountId" value="${escapeAttribute(filters.customer_account_id ?? "")}" pattern="csa_[0-9a-f]{32}" maxlength="36" /></label>
    ${select("区域", "region", filters.region, [["", "全部区域"], ["cn-north", "华北"], ["cn-east", "华东"], ["ap-southeast", "亚太"]])}
    ${select("状态", "operationalStatus", filters.operational_status, [["", "全部状态"], ["active", "活跃"], ["suspended", "已暂停"], ["deletion_pending", "等待删除"], ["migration_conflict", "迁移异常"]])}
    ${select("启用阶段", "activation", filters.activation, [["", "全部阶段"], ["not_started", "未开始"], ["in_progress", "进行中"], ["activated", "已启用"]])}
    ${select("使用情况", "adoption", filters.adoption, [["", "全部"], ["unknown", "未知"], ["low", "较低"], ["medium", "一般"], ["high", "较高"]])}
    ${select("健康风险", "healthRisk", filters.health_risk, [["", "全部风险"], ["healthy", "健康"], ["watch", "需关注"], ["at_risk", "有风险"], ["critical", "严重"]])}
    <button class="platform-primary" type="submit" ${busy(state, "directory")}>筛选</button>
  </form>`;
}

function accountRow(item, selectedId) {
  const selected = item.customer_account_id === selectedId;
  const name = item.display_name ?? shortId(item.customer_account_id);
  const supportCount = Number(item.open_support_ticket_count ?? 0);
  return `<button class="platform-customer-row${selected ? " is-selected" : ""}" type="button"
    role="option" aria-selected="${selected}" data-platform-customer-select="${escapeAttribute(item.customer_account_id)}">
    <span class="platform-customer-identity"><strong>${escapeHtml(name)}</strong></span>
    <span><small>状态</small><strong>${escapeHtml(label(item.operational_status ?? item.subscription_status))}</strong></span>
    <span><small>健康</small>${status(item.health_risk ?? "not_available")}</span>
    <span><small>续费</small><strong>${escapeHtml(formatDate(item.renewal_at) || label(item.renewal_status))}</strong></span>
    <span><small>支持</small><strong>${number(supportCount)}</strong></span>
  </button>`;
}

function directoryPagination(state) {
  const previous = state.directoryHistory?.length > 0;
  const next = Boolean(state.directory?.next_cursor);
  return `<nav class="platform-pagination" aria-label="客户目录分页">
    <span>${previous ? `第 ${number(state.directoryHistory.length + 1)} 页` : "第 1 页"}</span>
    <div><button type="button" data-platform-action="customer-previous" ${previous && !state.loading?.length ? "" : "disabled"}>上一页</button>
    <button type="button" data-platform-action="customer-next" ${next && !state.loading?.length ? "" : "disabled"}>下一页</button></div>
  </nav>`;
}

function customerDetail(state, account, permissions, context) {
  const tabs = [
    ["health", "健康"],
    ["plans", "计划"],
    ["tasks", "任务"],
    ["renewals", "续费"],
    ["qbr", "季度回顾"],
  ];
  const allowOperational = permissions.has("platform.customer_success.read");
  const visibleTabs = allowOperational ? tabs : tabs.filter(([id]) => ["health", "renewals"].includes(id));
  return `<header class="platform-customer-detail-head">
      <div><p>${escapeHtml(account.region ?? "组织客户")}</p><h2>${escapeHtml(account.display_name ?? "客户")}</h2><details class="platform-audit-technical"><summary>技术信息</summary><code>${escapeHtml(account.customer_account_id)}</code></details></div>
      <div class="platform-customer-detail-actions">
        ${permissions.has("platform.customer_success.contact.read")
          ? `<button type="button" data-platform-action="customer-contacts" ${busy(state, "contacts")}>查看联系人</button>` : ""}
        <button type="button" data-platform-action="customer-refresh" title="刷新客户投影" aria-label="刷新客户投影" ${state.loading?.length ? "disabled" : ""}>↻</button>
      </div>
    </header>
    ${state.contactsVisible ? contactsPanel(state, permissions.has("platform.customer_success.contact.read")) : ""}
    <div class="platform-customer-facts">
      ${fact("所属组织", "组织空间")}
      ${fact("启用阶段", account.activation ?? "not_available")}
      ${fact("使用情况", account.adoption ?? "not_available")}
      ${fact("更新次数", account.revision ?? "-")}
    </div>
    <div class="platform-customer-tabs" role="tablist" aria-label="客户详情视图">
      ${visibleTabs.map(([id, text]) => `<button type="button" role="tab" aria-selected="${state.tab === id}" data-platform-customer-tab="${id}">${text}</button>`).join("")}
    </div>
    <div class="platform-customer-tab-panel" role="tabpanel">
      ${tabContent(state, account, permissions, context)}
    </div>`;
}

function tabContent(state, account, permissions, context) {
  if (state.tab === "health") return healthTab(state);
  if (state.tab === "plans") return plansTab(state, account, permissions);
  if (state.tab === "tasks") return tasksTab(state, account, permissions, context);
  if (state.tab === "renewals") return renewalsTab(state);
  return qbrTab(state);
}

function healthTab(state) {
  const health = state.health;
  if (!health) return loadingOrEmpty(state, "health", "健康投影尚不可用。");
  const score = Number(health.score_bps ?? 0);
  return `<section class="platform-success-health" aria-label="健康投影">
    <div class="platform-success-health-score">
      <span>${status(health.risk_level)}</span>
      ${health.score_bps === undefined ? "" : `<strong>${number(score / 100)}</strong><small>/ 100</small>`}
      <p>${escapeHtml(label(health.freshness))}</p>
    </div>
    <div class="platform-success-health-factors">
      ${health.factors.map((factor) => `<div><span>${escapeHtml(label(factor.code))}</span><strong>${factor.score_bps === null ? "-" : `${number(factor.score_bps / 100)}%`}</strong><small>贡献 ${number(factor.contribution_bps / 100)}%</small></div>`).join("")}
    </div>
    <details class="platform-audit-technical"><summary>技术信息</summary><div class="platform-customer-codes"><span>风险标记</span>${health.risk_codes.map(code).join("") || "-"}<span>建议动作</span>${health.action_codes.map(code).join("") || "-"}</div></details>
  </section>`;
}

function plansTab(state, account, permissions) {
  const plans = state.detail?.plans ?? [];
  const canManage = permissions.has("platform.customer_success.plan.manage");
  return `<section class="platform-customer-records">
    <div class="platform-customer-record-list">
      ${plans.map((plan) => `<article><header><div><strong>${escapeHtml(label(plan.objective_code))}</strong><small>${formatDate(plan.starts_on)} - ${formatDate(plan.ends_on)}</small></div>${status(plan.status)}</header>
        <dl>${fact("健康目标", `${number(plan.target_health_bps / 100)}%`)}${fact("启用步骤", `${number(plan.target_activation_steps)} / 8`)}${fact("使用目标", `${number(plan.target_adoption_bps / 100)}%`)}${fact("更新次数", plan.revision)}</dl>
        ${canManage && !["completed", "canceled"].includes(plan.status) ? planActions(plan) : ""}</article>`).join("") || empty("暂无成功计划。")}
    </div>
    ${canManage ? planCreateForm(state, account) : ""}
  </section>`;
}

function planCreateForm(state, account) {
  const sequence = state.health?.source_sequence;
  if (!sequence) return '<p class="platform-empty">健康水位可用后才能创建计划。</p>';
  return `<form class="platform-customer-command" data-platform-customer-plan-create-form autocomplete="off">
    <h3>创建成功计划</h3>
    ${select("目标", "objectiveCode", "adoption", [["activation", "启用"], ["adoption", "使用"], ["reliability", "可靠性"], ["renewal", "续费"], ["security_readiness", "安全准备"]])}
    <label><span>开始日期</span><input type="date" name="startsOn" required /></label>
    <label><span>结束日期</span><input type="date" name="endsOn" required /></label>
    <label><span>健康目标</span><input type="number" name="targetHealthBps" min="0" max="10000" value="8000" required /></label>
    <label><span>激活步骤</span><input type="number" name="targetActivationSteps" min="0" max="8" value="8" required /></label>
    <label><span>采用目标</span><input type="number" name="targetAdoptionBps" min="0" max="10000" value="7500" required /></label>
    <input type="hidden" name="customerAccountId" value="${escapeAttribute(account.customer_account_id)}" />
    <input type="hidden" name="sourceHealthSequence" value="${number(sequence)}" />
    ${confirmation("创建计划", busy(state, "write-plan"))}
  </form>`;
}

function planActions(plan) {
  const actions = plan.status === "draft" ? [["activate", "启用"], ["cancel", "取消"]]
    : [["complete", "完成"], ["cancel", "取消"]];
  return `<div class="platform-customer-inline-actions">${actions.map(([operation, text]) => `<form data-platform-customer-plan-command-form><input type="hidden" name="operation" value="${operation}" /><input type="hidden" name="customerAccountId" value="${escapeAttribute(plan.customer_account_id)}" /><input type="hidden" name="planId" value="${escapeAttribute(plan.customer_success_plan_id)}" /><input type="hidden" name="expectedRevision" value="${number(plan.revision)}" /><input type="hidden" name="confirmed" value="on" /><button type="submit">${text}</button></form>`).join("")}</div>`;
}

function tasksTab(state, account, permissions, context) {
  const tasks = state.detail?.tasks ?? [];
  const canManage = permissions.has("platform.customer_success.task.manage");
  return `<section class="platform-customer-records">
    <div class="platform-customer-record-list">
      ${tasks.map((task) => `<article><header><div><strong>${escapeHtml(label(task.task_type))}</strong><small>${formatTime(task.due_at)} · ${escapeHtml(label(task.priority))}</small></div>${status(task.status)}</header>
        <dl>${fact("处理原因", task.reason_code)}${fact("负责人", "平台运营")}${fact("必办", task.required ? "是" : "否")}${fact("更新次数", task.revision)}</dl>
        ${canManage && !["completed", "canceled"].includes(task.status) ? taskActions(task) : ""}</article>`).join("") || empty("暂无客户任务。")}
    </div>
    ${canManage ? taskForms(state, account, context) : ""}
  </section>`;
}

function taskForms(state, account, context) {
  const operatorId = context?.operator?.operator_id ?? "";
  const planOptions = (state.detail?.plans ?? []).filter((item) => !["completed", "canceled"].includes(item.status));
  return `<div class="platform-customer-command-grid">
    <form class="platform-customer-command" data-platform-customer-task-create-form autocomplete="off"><h3>创建任务</h3>
      ${select("类型", "taskType", "adoption_follow_up", taskTypeOptions())}
      ${select("优先级", "priority", "normal", priorityOptions())}
      <label><span>计划</span><select name="planId"><option value="">不关联计划</option>${planOptions.map((plan) => `<option value="${escapeAttribute(plan.customer_success_plan_id)}">${escapeHtml(label(plan.objective_code))}</option>`).join("")}</select></label>
      <label><span>到期时间</span><input type="datetime-local" name="dueAt" required /></label>
      <label><span>处理原因</span><input name="reasonCode" value="adoption.follow_up" pattern="[a-z][a-z0-9._-]{2,79}" required /></label>
      <input type="hidden" name="customerAccountId" value="${escapeAttribute(account.customer_account_id)}" /><input type="hidden" name="assigneeOperatorId" value="${escapeAttribute(operatorId)}" />
      <label class="platform-check"><input type="checkbox" name="required" checked /><span>标记为必办</span></label>${confirmation("创建任务", busy(state, "write-task"))}</form>
    <form class="platform-customer-command" data-platform-customer-batch-prepare-form autocomplete="off"><h3>准备批量任务</h3>
      <label class="platform-customer-command-wide"><span>客户编号</span><textarea name="accountIds" rows="3" maxlength="3699" required>${escapeHtml(account.customer_account_id)}</textarea></label>
      ${select("类型", "taskType", "health_review", taskTypeOptions())}${select("优先级", "priority", "normal", priorityOptions())}
      <label><span>到期时间</span><input type="datetime-local" name="dueAt" required /></label><label><span>处理原因</span><input name="reasonCode" value="health.batch_review" pattern="[a-z][a-z0-9._-]{2,79}" required /></label>
      <input type="hidden" name="assigneeOperatorId" value="${escapeAttribute(operatorId)}" />${confirmation("提交审批", busy(state, "write-task"))}</form>
  </div>`;
}

function taskActions(task) {
  const options = task.status === "open"
    ? [["in_progress", "开始"], ["completed", "完成"], ["canceled", "取消"]]
    : [["completed", "完成"], ["canceled", "取消"]];
  return `<div class="platform-customer-inline-actions">${options.map(([target, text]) => `<form data-platform-customer-task-command-form><input type="hidden" name="taskId" value="${escapeAttribute(task.customer_task_id)}" /><input type="hidden" name="targetStatus" value="${target}" /><input type="hidden" name="expectedRevision" value="${number(task.revision)}" /><input type="hidden" name="confirmed" value="on" /><button type="submit">${text}</button></form>`).join("")}</div>`;
}

function renewalsTab(state) {
  const items = state.renewals?.items ?? [];
  return `<div class="platform-table-wrap"><table class="platform-customer-renewal-table"><thead><tr><th>续费日期</th><th>状态</th><th>健康</th><th>采用</th><th>支付</th><th>发票</th><th>金额</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td data-label="续费日期">${formatTime(item.renewal_at)}</td><td data-label="状态">${status(item.status)}</td><td data-label="健康">${escapeHtml(label(item.health_risk_code ?? "restricted"))}</td><td data-label="采用">${escapeHtml(label(item.adoption_risk_code ?? "restricted"))}</td><td data-label="支付">${escapeHtml(label(item.payment_risk_code))}</td><td data-label="发票">${escapeHtml(label(item.invoice_status))}</td><td data-label="金额">${amount(item)}</td></tr>`).join("") || '<tr><td colspan="7" class="platform-empty">暂无续费机会。</td></tr>'}
  </tbody></table></div>`;
}

function qbrTab(state) {
  const items = state.detail?.qbr_summaries ?? [];
  return `<div class="platform-table-wrap"><table class="platform-customer-qbr-table"><thead><tr><th>季度</th><th>健康水位</th><th>采用</th><th>支持</th><th>续费风险</th><th>结论</th><th>生成时间</th></tr></thead><tbody>
    ${items.map((item) => `<tr><td data-label="季度"><strong>${escapeHtml(item.quarter)}</strong></td><td data-label="健康水位">${number(item.health_sequence)}</td><td data-label="采用">${item.adoption_bps === null ? "-" : `${number(item.adoption_bps / 100)}%`}</td><td data-label="支持">${number(item.support_ticket_count)}</td><td data-label="续费风险">${number(item.renewal_risk_count)}</td><td data-label="结论">${escapeHtml(label(item.outcome_code))}<small>${escapeHtml(label(item.action_code))}</small></td><td data-label="生成时间">${formatTime(item.generated_at)}</td></tr>`).join("") || '<tr><td colspan="7" class="platform-empty">暂无 QBR 摘要。</td></tr>'}
  </tbody></table></div>`;
}

function contactsPanel(state, allowed) {
  if (!allowed) return "";
  const contacts = state.contacts ?? [];
  return `<section class="platform-band platform-customer-contacts" data-platform-customer-contacts tabindex="-1" aria-labelledby="platform-customer-contacts-title">
    <div class="platform-section-heading"><div><p>单个客户信息</p><h2 id="platform-customer-contacts-title">联系人</h2></div><button type="button" data-platform-action="customer-contacts-close" title="关闭联系人" aria-label="关闭联系人">×</button></div>
    <div class="platform-customer-contact-list">${contacts.map((contact) => `<div><header><strong>${escapeHtml(contact.display_name)}</strong>${status(contact.status)}</header><dl>${fact("角色", contact.contact_role)}${fact("邮箱", contact.email)}${fact("电话", contact.phone ?? "-")}${fact("更新次数", contact.membership_revision)}</dl></div>`).join("") || empty("当前联系人不可用。")}</div>
  </section>`;
}

function batchApprovalPanel(state, context) {
  const batch = state.batch;
  if (!batch || batch.status !== "awaiting_approval") return "";
  const isOwner = context?.operator?.roles?.includes("platform_owner");
  if (!isOwner) return `<section class="platform-band platform-customer-batch"><div class="platform-section-heading"><div><p>Independent approval</p><h2>批量任务等待 Owner 审批</h2></div>${status(batch.status)}</div><p>${number(batch.account_count)} 个客户 · ${escapeHtml(shortId(batch.manifest_sha256))}</p></section>`;
  return `<section class="platform-band platform-customer-batch"><div class="platform-section-heading"><div><p>Independent approval</p><h2>批量任务审批</h2></div>${status(batch.status)}</div>
    <form data-platform-customer-batch-approve-form autocomplete="off"><input type="hidden" name="batchId" value="${escapeAttribute(batch.customer_task_batch_id)}" /><input type="hidden" name="expectedRevision" value="${number(batch.revision)}" />
      ${select("审批决定", "decision", "approved", [["approved", "通过"], ["rejected", "拒绝"]])}<label><span>处理原因</span><input name="reasonCode" value="owner.approved" pattern="[a-z][a-z0-9._-]{2,79}" required /></label>
      ${select("验证方式", "method", "totp", [["totp", "验证码"], ["webauthn", "安全密钥"]])}<label data-platform-customer-step-up-code><span>验证码</span><input type="password" name="verificationCode" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" data-platform-sensitive required /></label>
      ${confirmation("提交决定", busy(state, "write-task"))}</form>
  </section>`;
}

function phaseBanner(state) {
  if (state.feedback) return `<p class="platform-feedback" role="status">${escapeHtml(state.feedback)}</p>`;
  if (state.phase === "loading") return '<p class="platform-feedback" role="status">正在读取客户目录…</p>';
  if (state.phase === "stale") return '<p class="platform-warning" role="status">当前显示最后一份可信健康投影，来源正在追平。</p>';
  if (state.phase === "partial") return '<p class="platform-warning" role="status">部分历史已达显示上限，当前操作对象仍完整可用。</p>';
  if (state.phase === "permission_revoked") return '<p class="platform-error" role="alert">权限已撤销，客户数据已清空并正在刷新导航。</p>';
  if (state.phase === "revision_conflict") return '<p class="platform-error" role="alert">对象已被其他操作更新，请刷新后重试。</p>';
  if (state.phase === "dependency_unavailable") return '<p class="platform-error" role="alert">客户成功依赖暂不可用，未提交部分结果。</p>';
  if (state.error) return `<p class="platform-error" role="alert">${escapeHtml(state.error.message)} <code>${escapeHtml(state.error.code)}</code> <small>${escapeHtml(state.error.requestId)}</small></p>`;
  return "";
}

function loadingOrEmpty(state, lane, text) {
  return state.loading?.includes(lane)
    ? '<p class="platform-empty" role="status">正在读取可信投影…</p>'
    : empty(text);
}

function metric(name, value, detail) {
  return `<div><span>${escapeHtml(name)}</span><strong>${number(value)}</strong><small>${escapeHtml(detail)}</small></div>`;
}

function fact(name, value) {
  return `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(label(value))}</dd></div>`;
}

function code(value) {
  return `<code>${escapeHtml(value)}</code>`;
}

function status(value) {
  const text = String(value ?? "unknown");
  const kind = new Set(["active", "healthy", "fresh", "completed", "renewed", "applied"]).has(text)
    ? "is-ok" : new Set(["watch", "at_risk", "stale", "draft", "open", "in_progress", "observing", "qualified", "awaiting_approval"]).has(text)
      ? "is-warn" : "is-danger";
  return `<span class="platform-status ${kind}">${escapeHtml(label(text))}</span>`;
}

function select(text, name, current, values) {
  return `<label><span>${escapeHtml(text)}</span><select name="${escapeAttribute(name)}">${values.map(([value, labelText]) => `<option value="${escapeAttribute(value)}" ${value === (current ?? "") ? "selected" : ""}>${escapeHtml(labelText)}</option>`).join("")}</select></label>`;
}

function confirmation(text, disabled) {
  return `<label class="platform-check"><input type="checkbox" name="confirmed" required ${disabled ? "disabled" : ""}/><span>确认命令</span></label><button class="platform-primary" type="submit" ${disabled}>${escapeHtml(text)}</button>`;
}

function taskTypeOptions() {
  return [
    ["activation_review", "启用复核"], ["adoption_follow_up", "使用跟进"],
    ["health_review", "健康复核"], ["renewal_review", "续费复核"],
    ["security_review", "安全复核"], ["qbr_prepare", "季度回顾准备"],
  ];
}

function priorityOptions() {
  return [["low", "低"], ["normal", "普通"], ["high", "高"], ["critical", "紧急"]];
}

function amount(item) {
  if (item.amount_status !== "available" || item.amount_minor === null) return "-";
  return `${escapeHtml(item.currency)} ${new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(item.amount_minor / 100)}`;
}

function busy(state, lane) {
  return state.loading?.includes(lane) ? "disabled" : "";
}

function empty(text) {
  return `<p class="platform-empty">${escapeHtml(text)}</p>`;
}

function label(value) {
  const key = String(value ?? "");
  const labels = {
    active: "活跃", suspended: "已暂停", deletion_pending: "等待删除", migration_conflict: "迁移异常",
    not_started: "未开始", in_progress: "进行中", activated: "已启用", unknown: "未知",
    low: "较低", medium: "一般", high: "较高", healthy: "健康", watch: "需关注", at_risk: "有风险", critical: "严重",
    stale: "数据滞后", fresh: "正常", not_available: "暂无数据", restricted: "受限", open: "待处理", completed: "已完成",
    canceled: "已取消", draft: "草稿", qualified: "已确认", awaiting_approval: "待审批", normal: "普通",
    usage_trend: "使用趋势", reliability: "可靠性", cost_anomaly_health: "成本健康", budget_readiness: "预算准备度",
    team_activity: "团队活跃度", ticket_health: "工单健康度", usage_declining: "使用下降", budget_missing: "预算缺失",
    adoption_attention: "使用情况待改善", activate_plan: "制定启用计划", renewal_attention: "续费待跟进",
  };
  return labels[key] ?? (key.replaceAll("_", " ") || "-");
}

function shortId(value) {
  const text = String(value ?? "");
  return text.length > 20 ? `${text.slice(0, 12)}...${text.slice(-4)}` : text;
}

function number(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function formatDate(value) {
  if (!value) return "";
  const parsed = new Date(String(value).length === 10 ? `${value}T00:00:00Z` : value);
  return Number.isFinite(parsed.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(parsed)
    : "";
}

function formatTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isFinite(parsed.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(parsed)
    : "-";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function escapeAttribute(value) {
  return escapeHtml(value);
}
