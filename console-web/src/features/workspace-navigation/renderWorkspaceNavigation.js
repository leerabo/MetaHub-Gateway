import { WORKSPACE_ROUTE_REGISTRY } from "../../lib/workspaceNavigation.js";

const TASK_COPY = Object.freeze({
  workspace_profile: ["组织资料", "组织名称"],
  member_responsibilities: ["后台成员与职责", "管理、开发、安全与财务"],
  subscription_ready: ["确认订阅状态", "核对当前套餐、额度与账务状态。"],
  provider_connected: ["接入模型服务", "模型厂家与服务密钥"],
  model_catalog_ready: ["配置可用模型", "模型名称与路由"],
  project_key_ready: ["成员自助创建 KEY", "统一网关与个人访问凭证"],
  first_gateway_call: ["接入并开始使用", "连接信息与首次调用状态"],
  security_policy_ready: ["配置安全策略", "确认策略在当前空间生效。"],
  dlp_ready: ["配置数据防护", "建立适合当前业务的数据防护规则。"],
  audit_coverage: ["检查审计覆盖", "确认关键操作具备可追溯证据。"],
  cost_controls: ["设置成本控制", "建立预算与成本异常边界。"],
  finops_ready: ["检查内部 FinOps", "核对成本率与内部计量事实。"],
  data_lifecycle: ["配置数据生命周期", "确认保留、导出和删除规则。"],
  service_visibility: ["查看服务状态", "确认当前空间的服务可用性。"],
});

// 管理员配置闭环在成员可自助创建 KEY 后结束。首次调用是成员使用事实，不能成为管理员待办门槛。
const START_TASKS = ["provider_connected", "model_catalog_ready", "project_key_ready"];
const START_ACTIONS = Object.freeze({
  provider_connected: "配置模型服务",
  model_catalog_ready: "配置模型",
  project_key_ready: "成员 KEY 已启用",
  first_gateway_call: "查看接入信息",
});

const STATUS_COPY = Object.freeze({
  not_started: "尚未开始",
  ready: "待完成",
  blocked: "等待前置条件",
  in_progress: "处理中",
  completed: "已完成",
  needs_attention: "需要关注",
  not_applicable: "不适用",
});

const TOOL_COPY = Object.freeze({
  provider_error_overrides: ["Provider 错误覆盖", "管理空间内的错误归一规则。"],
  response_cache: ["响应缓存", "查看和调整响应缓存策略。"],
  policy_simulator: ["策略模拟器", "在保存前验证策略结果。"],
  intent_security: ["意图安全测试", "验证输入意图的安全分类。"],
  webhook_dead_letters: ["Webhook 死信", "检查和恢复失败的 Webhook 投递。"],
  debug_console: ["调试控制台", "执行受控的 Gateway 调试。"],
  deployment_summary: ["部署摘要", "查看当前空间的部署诊断。"],
});

const ADMIN_WORK_DOMAINS = Object.freeze([
  Object.freeze({
    code: "access",
    title: "接入与成员",
    description: "成员、个人 KEY 和客户端接入",
    titleKey: "workspace.domain.access.title",
    descriptionKey: "workspace.domain.access.description",
    tasks: Object.freeze(["workspace_profile", "member_responsibilities", "project_key_ready"]),
  }),
  Object.freeze({
    code: "gateway",
    title: "模型与网关",
    description: "Provider、默认模型和路由",
    titleKey: "workspace.domain.gateway.title",
    descriptionKey: "workspace.domain.gateway.description",
    tasks: Object.freeze(["provider_connected", "model_catalog_ready"]),
  }),
  Object.freeze({
    code: "usage",
    title: "用量与风险",
    description: "订阅、额度、预算和成本异常",
    titleKey: "workspace.domain.usage.title",
    descriptionKey: "workspace.domain.usage.description",
    tasks: Object.freeze(["subscription_ready", "cost_controls", "finops_ready"]),
  }),
  Object.freeze({
    code: "governance",
    title: "治理与运维",
    description: "策略、数据防护、审计和生命周期",
    titleKey: "workspace.domain.governance.title",
    descriptionKey: "workspace.domain.governance.description",
    tasks: Object.freeze(["security_policy_ready", "dlp_ready", "audit_coverage", "data_lifecycle"]),
  }),
]);

const FINANCE_WORK_DOMAINS = Object.freeze([
  Object.freeze({
    code: "finance",
    title: "财务与风险",
    description: "订阅、用量、预算和成本异常",
    titleKey: "workspace.domain.finance.title",
    descriptionKey: "workspace.domain.finance.description",
    tasks: Object.freeze(["subscription_ready", "cost_controls", "finops_ready"]),
  }),
]);

export function renderWorkspaceTaskHome(state, i18n, options = {}) {
  if (!state) {
    return `<section class="workspace-task-home" aria-live="polite"><p class="workspace-navigation-state">${escapeHtml(t(i18n, "workspace.loading", "正在加载接入进度..."))}</p></section>`;
  }
  if (state?.status === "error" || state?.error) {
    return `<section class="workspace-task-home" role="alert"><p class="workspace-navigation-state is-error">${escapeHtml(t(i18n, "workspace.load_failed", "接入进度加载失败。"))}</p><button type="button" data-resource-retry>${escapeHtml(t(i18n, "workspace.reload", "重新加载"))}</button></section>`;
  }
  const tasks = Array.isArray(state.tasks) ? state.tasks : [];
  if (state.role_code === "viewer") return renderEmployeeAccessHome(state, options, i18n);
  const primary = START_TASKS.map((code) => tasks.find((task) => task.task_code === code)).filter(Boolean);
  const isAdmin = state.role_code === "owner" || state.role_code === "admin";
  const isFinance = state.role_code === "finance_admin";
  if (primary.length || isAdmin) {
    const secondary = tasks.filter((task) => !START_TASKS.includes(task.task_code));
    const next = primary.find((task) => task.status !== "completed" && task.action_enabled);
    return '<section class="workspace-task-home workspace-start-flow" aria-labelledby="workspace-task-home-title">'
      + (isAdmin ? renderWorkDomains(tasks, i18n, ADMIN_WORK_DOMAINS, "workspace.admin_workbench.title", "管理员工作台", "workspace.admin_workbench.scope", "四个工作域") : "")
      + (primary.length
        ? `<div class="workspace-navigation-heading"><h3 id="workspace-task-home-title">${escapeHtml(t(i18n, "workspace.setup_progress", "组织接入进度"))}</h3>`
          + '<span>' + primary.filter((task) => task.status === "completed").length + ' / ' + primary.length + ` ${escapeHtml(t(i18n, "workspace.completed", "已完成"))}</span></div>`
          + renderSetupProgress(primary, i18n)
          + renderNextSetupStep(primary, next, i18n)
        : `<div class="workspace-navigation-heading"><h3 id="workspace-task-home-title">${escapeHtml(t(i18n, "workspace.setup_progress", "组织接入进度"))}</h3><span>${escapeHtml(t(i18n, "workspace.pending", "待确认"))}</span></div>`)
      + (secondary.length ? `<details class="workspace-other-tasks"><summary>${escapeHtml(t(i18n, "workspace.other_tasks", "其他管理事项"))}</summary><div class="workspace-task-list">`
        + secondary.map((task) => renderTask(task, false, i18n)).join("") + '</div></details>' : '') + '</section>';
  }
  const summary = `<div class="workspace-task-summary"><div><span>${escapeHtml(t(i18n, "workspace.completed", "已完成"))}</span><strong>`
    + escapeHtml(state.completed_count ?? 0)
    + `</strong></div><div><span>${escapeHtml(t(i18n, "workspace.needs_attention", "需要关注"))}</span><strong>`
    + escapeHtml(state.needs_attention_count ?? 0)
    + `</strong></div><div><span>${escapeHtml(t(i18n, "workspace.source_status", "来源状态"))}</span><strong>`
    + escapeHtml(freshnessLabel(state.source_freshness, i18n))
    + '</strong></div></div>';
  const content = tasks.length === 0
    ? `<p class="workspace-navigation-state">${escapeHtml(t(i18n, "workspace.no_tasks", "当前职责没有待处理任务。"))}</p>`
    : '<div class="workspace-task-list">' + tasks.map((task) => renderTask(task, false, i18n)).join("") + "</div>";
  const roleSummary = isFinance
    ? renderWorkDomains(tasks, i18n, FINANCE_WORK_DOMAINS, "workspace.finance_workbench.title", "财务工作台", "workspace.finance_workbench.scope", "例外优先")
      + `<details class="workspace-other-tasks"><summary>${escapeHtml(t(i18n, "workspace.finance_details", "查看财务任务明细"))}</summary>${content}</details>`
    : content;
  return '<section class="workspace-task-home" aria-labelledby="workspace-task-home-title">'
    + `<div class="workspace-navigation-heading"><div><p>${escapeHtml(t(i18n, "workspace.current_role", "当前职责"))}</p><h3 id="workspace-task-home-title">${escapeHtml(t(i18n, "workspace.get_started", "开始使用"))}</h3></div>`
    + '<span class="workspace-role-badge">' + escapeHtml(roleLabel(state.role_code, i18n)) + "</span></div>"
    + summary + roleSummary + "</section>";
}

function renderSetupProgress(tasks, i18n) {
  const total = tasks.length;
  const completed = tasks.filter((task) => task.status === "completed").length;
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  return '<div class="workspace-setup-progress" aria-label="' + escapeAttribute(t(i18n, "workspace.setup_progress_label", "组织接入完成度")) + '">'
    + '<div class="workspace-setup-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + percent + '" aria-valuetext="' + escapeAttribute(completed + " / " + total) + '">'
    + '<span style="width:' + percent + '%"></span></div>'
    + '<span class="workspace-setup-progress-value">' + completed + ' / ' + total + '</span></div>';
}

function renderNextSetupStep(tasks, next, i18n) {
  const nextIndex = next ? tasks.indexOf(next) + 1 : 0;
  const nextCopy = next ? TASK_COPY[next.task_code] : null;
  const primary = next
    ? '<div class="workspace-next-step-card"><span class="workspace-task-status">' + escapeHtml(t(i18n, "workspace.next_step", "下一步")) + '</span><strong>'
      + escapeHtml(t(i18n, `workspace.task.${next.task_code}.title`, nextCopy?.[0] ?? "继续完成接入")) + '</strong><p>'
      + escapeHtml(t(i18n, `workspace.task.${next.task_code}.description`, nextCopy?.[1] ?? "打开对应页面继续处理。")) + '</p>'
      + renderTaskAction(next, true, i18n) + '</div>'
    : '<div class="workspace-next-step-card is-complete"><span class="workspace-task-status">' + escapeHtml(t(i18n, "workspace.completed", "已完成")) + '</span><strong>'
      + escapeHtml(t(i18n, "workspace.setup_complete.title", "组织接入流程已完成")) + '</strong><p>'
      + escapeHtml(t(i18n, "workspace.setup_complete.description", "现在可以复查配置；成员登录组织空间后可自行创建个人 KEY。")) + '</p></div>';
  const detailsLabel = next
    ? t(i18n, "workspace.setup_steps_all", `查看全部接入步骤（${nextIndex} / ${tasks.length}）`, { current: nextIndex, total: tasks.length })
    : t(i18n, "workspace.setup_steps_view", "查看接入步骤");
  return primary + '<details class="workspace-start-steps"><summary>' + escapeHtml(detailsLabel) + '</summary><ol>'
    + tasks.map((task, index) => '<li' + (task === next ? ' aria-current="step"' : '') + '><span class="workspace-step-number">'
      + (index + 1) + '</span>' + renderTask(task, false, i18n) + '</li>').join("") + '</ol></details>';
}

function renderTaskAction(task, next = false, i18n) {
  const route = task.action_route_code;
  const routeAllowed = route === null || Boolean(WORKSPACE_ROUTE_REGISTRY[route]);
  const enabled = Boolean(task.action_enabled && route && routeAllowed);
  return enabled
    ? '<button type="button" class="' + (next ? 'workspace-next-action' : 'workspace-task-action') + '" data-workspace-task-route="'
      + escapeAttribute(route) + '">' + escapeHtml(task.status === "completed" ? t(i18n, "workspace.view_config", "查看配置") : t(i18n, `workspace.action.${task.task_code}`, START_ACTIONS[task.task_code] ?? "打开")) + '</button>'
    : '<span class="workspace-task-no-action">' + escapeHtml(task.status === "completed" ? t(i18n, "workspace.done", "完成") : t(i18n, "workspace.view_status", "查看状态")) + '</span>';
}

function renderWorkDomains(tasks, i18n, domains, headingKey, headingFallback, statusKey, statusFallback) {
  const byCode = new Map(tasks.map((task) => [task.task_code, task]));
  return '<section class="workspace-admin-workbench" aria-labelledby="workspace-admin-workbench-title">'
    + '<div class="workspace-navigation-heading"><div><p>' + escapeHtml(t(i18n, "workspace.workbench.eyebrow", "任务摘要")) + '</p><h3 id="workspace-admin-workbench-title">' + escapeHtml(t(i18n, headingKey, headingFallback)) + '</h3></div><span>' + escapeHtml(t(i18n, statusKey, statusFallback)) + '</span></div>'
    + '<div class="workspace-admin-domain-grid">'
    + domains.map((domain) => {
      const domainTasks = domain.tasks.map((code) => byCode.get(code)).filter(Boolean);
      const incomplete = domainTasks.find((task) => task.status !== "completed" && task.action_enabled);
      const reviewTask = domainTasks.find((task) => task.status === "completed"
        && task.action_enabled && task.action_route_code
        && WORKSPACE_ROUTE_REGISTRY[task.action_route_code]);
      const completed = domainTasks.filter((task) => task.status === "completed").length;
      const attention = domainTasks.filter((task) => task.status === "needs_attention").length;
      const blocked = domainTasks.some((task) => task.status === "blocked");
      const inProgress = domainTasks.some((task) => task.status === "in_progress");
      const unfinished = domainTasks.some((task) => task.status !== "completed");
      const status = !domainTasks.length
        ? "pending"
        : attention
          ? "needs_attention"
          : blocked
            ? "blocked"
            : inProgress
              ? "in_progress"
              : unfinished
                ? "ready"
                : "completed";
      const statusDetail = status === "pending"
        ? t(i18n, "workspace.status.waiting", "等待状态")
        : statusLabel(status, i18n);
      return '<article class="workspace-admin-domain" data-domain-status="' + escapeAttribute(status) + '">'
        + '<div><span class="workspace-task-status">' + escapeHtml(statusLabel(status, i18n)) + '</span><h4>' + escapeHtml(t(i18n, domain.titleKey, domain.title ?? "")) + '</h4><p>' + escapeHtml(t(i18n, domain.descriptionKey, domain.description ?? "")) + '</p></div>'
        + '<div class="workspace-admin-domain-meta"><span>' + escapeHtml(t(i18n, "workspace.domain.completed_count", `${completed} / ${domainTasks.length} 已完成`, { completed, total: domainTasks.length })) + '</span>'
        + (incomplete?.action_route_code
          ? '<button type="button" class="workspace-task-action" data-workspace-task-route="' + escapeAttribute(incomplete.action_route_code) + '">' + escapeHtml(t(i18n, `workspace.action.${incomplete.task_code}`, START_ACTIONS[incomplete.task_code] ?? "继续")) + '</button>'
          : reviewTask
            ? '<button type="button" class="workspace-task-action" data-workspace-task-route="' + escapeAttribute(reviewTask.action_route_code) + '">' + escapeHtml(t(i18n, "workspace.view_config", "查看配置")) + '</button>'
            : `<span>${escapeHtml(statusDetail)}</span>`)
        + '</div></article>';
    }).join("") + '</div></section>';
}

function renderEmployeeAccessHome(state, options, i18n) {
  const navigation = options?.navigation ?? {};
  const workspace = options?.workspaceContext ?? {};
  const gateway = navigation.gateway_base_url_status === "ready"
    ? navigation.gateway_base_url
    : workspace.gateway_base_url_status === "ready"
      ? workspace.gateway_base_url
    : null;
  const gatewayText = gateway || t(i18n, "workspace.employee.gateway_pending", "等待管理员配置统一网关地址");
  const gatewayAction = gateway
    ? '<button type="button" data-copy-code-target="employee-gateway-address" aria-label="' + escapeAttribute(t(i18n, "workspace.employee.copy_gateway", "复制统一网关地址")) + '" title="' + escapeAttribute(t(i18n, "workspace.employee.copy_gateway", "复制统一网关地址")) + '">' + escapeHtml(t(i18n, "workspace.employee.copy", "复制")) + '</button>'
    : '<span class="workspace-task-no-action">' + escapeHtml(t(i18n, "workspace.employee.contact_admin", "联系管理员")) + '</span>';
  return '<section class="workspace-task-home workspace-employee-home" aria-labelledby="workspace-employee-home-title">'
    + '<div class="workspace-navigation-heading"><div><p>' + escapeHtml(t(i18n, "workspace.current_role", "当前职责")) + '</p><h3 id="workspace-employee-home-title">' + escapeHtml(t(i18n, "workspace.employee.title", "我的接入")) + '</h3><span>' + escapeHtml(t(i18n, "workspace.employee.subtitle", "使用组织已配置的 Gateway，不需要理解后台配置。")) + '</span></div><span class="workspace-role-badge">' + escapeHtml(t(i18n, "workspace.employee.role", "员工")) + '</span></div>'
    + '<div class="workspace-employee-access-grid">'
    + '<article class="workspace-employee-access-card workspace-employee-gateway"><span>' + escapeHtml(t(i18n, "workspace.employee.gateway_label", "统一 Gateway 地址")) + '</span><strong><code id="employee-gateway-address">' + escapeHtml(gatewayText) + '</code></strong><div>' + gatewayAction + '</div><small>' + escapeHtml(t(i18n, "workspace.employee.protocol_note", "协议路径（如 /v1、/responses、/messages）仍属于同一个网关。")) + '</small></article>'
    + '<article class="workspace-employee-access-card"><span>' + escapeHtml(t(i18n, "workspace.employee.key_label", "个人 MetaHub KEY")) + '</span><strong>' + escapeHtml(t(i18n, "workspace.employee.key_issued", "成员登录后自行创建")) + '</strong><p>' + escapeHtml(t(i18n, "workspace.employee.key_note", "每位成员在组织空间内创建和管理自己的个人 KEY。请勿填写 Provider KEY，也不需要申请第二把。")) + '</p><span class="workspace-employee-key-rule">' + escapeHtml(t(i18n, "workspace.employee.revocation_note", "成员被撤权后个人 KEY 会立即失效。")) + '</span></article>'
    + '<article class="workspace-employee-access-card"><span>' + escapeHtml(t(i18n, "workspace.employee.clients_label", "支持的客户端")) + '</span><strong>' + escapeHtml(t(i18n, "workspace.employee.clients_value", "可自定义接口的客户端")) + '</strong><p>' + escapeHtml(t(i18n, "workspace.employee.clients_note", "使用同一个网关地址和个人 KEY。客户端强制要求模型名称时，再填写管理员给出的名称。")) + '</p><span class="workspace-employee-key-rule">' + escapeHtml(t(i18n, "workspace.employee.clients_limit", "ChatGPT 网页版等不支持自定义端点的工具不能直接接入。")) + '</span></article>'
    + '</div>'
    + '<div class="workspace-employee-status" role="status"><strong>' + escapeHtml(t(i18n, "workspace.employee.status_label", "接入状态")) + '</strong><span>' + escapeHtml(state.source_freshness === "fresh" ? t(i18n, "workspace.employee.status_fresh", "组织接入信息已更新") : t(i18n, "workspace.employee.status_pending", "接入信息等待更新")) + '</span></div>'
    + '</section>';
}

export function renderWorkspaceAdvancedTools(state, i18n) {
  const body = state?.status === "loading"
    ? `<p class="workspace-navigation-state" aria-live="polite">${escapeHtml(t(i18n, "workspace.advanced_loading", "正在核对高级工具权限..."))}</p>`
    : state?.status === "error"
      ? `<p class="workspace-navigation-state is-error" role="alert">${escapeHtml(t(i18n, "workspace.advanced_unavailable", "高级工具暂时不可用，请稍后重试。"))}</p>`
      : renderToolList(state?.tools ?? [], i18n);
  return '<dialog class="workspace-advanced-dialog" data-workspace-advanced-dialog '
    + 'aria-labelledby="workspace-advanced-title"><div class="workspace-advanced-heading">'
    + `<div><p>${escapeHtml(t(i18n, "workspace.on_demand", "按需打开"))}</p><h2 id="workspace-advanced-title">${escapeHtml(t(i18n, "workspace.advanced_tools", "高级工具"))}</h2></div>`
    + `<button type="button" class="workspace-dialog-close" data-workspace-advanced-close aria-label="${escapeAttribute(t(i18n, "workspace.close_advanced", "关闭高级工具"))}" title="${escapeAttribute(t(i18n, "workspace.close", "关闭"))}">&times;</button>`
    + "</div>" + body + "</dialog>";
}

export function renderWorkspaceConnectionStatus(state, currentRoute, i18n) {
  if (!state) return "";
  const sdk = currentRoute === "workspace.developer.sdk";
  const retry = `<button type="button" data-resource-retry>${escapeHtml(t(i18n, "workspace.refresh_status", "刷新接入状态"))}</button>`;
  if (state.status === "error" || state.error || state.source_freshness !== "fresh") {
    return `<aside class="workspace-connection-status" role="status"><div><strong>${escapeHtml(t(i18n, "workspace.connection_unavailable", "接入状态暂不可用"))}</strong></div>` + retry + '</aside>';
  }
  const tasks = START_TASKS.map((code) => state.tasks?.find((task) => task.task_code === code));
  // A role without all setup facts cannot infer readiness from the visible subset.
  if (tasks.some((task) => !task)) return "";
  const missing = tasks.find((task) => task.status !== "completed");
  if (!missing && !sdk) return "";
  const firstCall = state.tasks?.find((task) => task.task_code === "first_gateway_call");
  const title = missing
    ? t(i18n, "workspace.setup_incomplete", "组织接入尚未完成")
    : firstCall?.status === "completed"
      ? t(i18n, "workspace.first_call_recorded", "已记录成功调用")
      : t(i18n, "workspace.connection_ready", "接入配置已完成，可开始使用");
  const detail = missing ? ({ provider_connected: t(i18n, "workspace.provider_pending", "模型服务待接通"), model_catalog_ready: t(i18n, "workspace.model_pending", "模型与路由待就绪"), project_key_ready: t(i18n, "workspace.key_pending", "成员可自助创建 KEY") })[missing.task_code] : "";
  const route = missing?.action_route_code;
  const action = missing?.action_enabled && route !== currentRoute && WORKSPACE_ROUTE_REGISTRY[route]
    ? '<a class="workspace-recovery-action" href="#' + escapeAttribute(route) + '">' + escapeHtml(t(i18n, `workspace.action.${missing.task_code}`, START_ACTIONS[missing.task_code])) + '</a>' : "";
  const usageDetail = !missing && firstCall?.status !== "completed"
    ? '<span>' + escapeHtml(t(i18n, "workspace.first_call_optional", "首次调用会在员工实际使用后记录，不影响管理员交付。")) + '</span>' : '';
  return '<aside class="workspace-connection-status" role="status"><div><strong>' + title + '</strong>'
    + (detail ? '<span>' + detail + '</span>' : '') + usageDetail + '</div>' + action + (sdk ? retry : '') + '</aside>';
}

export function workspaceSetupStepComplete(state, taskCode) {
  return state?.source_freshness === "fresh" && !state.error && state.status !== "error"
    && state.tasks?.some((task) => task.task_code === taskCode && task.status === "completed");
}

function renderTask(task, next = false, i18n) {
  const copy = TASK_COPY[task.task_code] ?? ["空间任务", "打开对应页面继续处理。"];
  return '<article class="workspace-task-row" data-task-status="' + escapeAttribute(task.status) + '">'
    + '<div><span class="workspace-task-status">' + escapeHtml(next ? t(i18n, "workspace.next_step", "下一步") : statusLabel(task.status, i18n)) + "</span>"
    + "<h4>" + escapeHtml(t(i18n, `workspace.task.${task.task_code}.title`, copy[0])) + "</h4><p>" + escapeHtml(t(i18n, `workspace.task.${task.task_code}.description`, copy[1])) + "</p></div>"
    + renderTaskAction(task, next, i18n)
    + "</article>";
}

function renderToolList(tools, i18n) {
  if (!Array.isArray(tools) || tools.length === 0) {
    return `<p class="workspace-navigation-state">${escapeHtml(t(i18n, "workspace.no_advanced_tools", "当前职责没有可用的高级工具。"))}</p>`;
  }
  return '<div class="workspace-advanced-list">' + tools.map((tool) => {
    const copy = TOOL_COPY[tool.tool_code] ?? ["高级工具", "打开受控工具。"];
    const enabled = tool.availability === "available" && tool.action_enabled;
    return '<article class="workspace-advanced-row"><div><h3>' + escapeHtml(t(i18n, `workspace.tool.${tool.tool_code}.title`, copy[0]))
      + "</h3><p>" + escapeHtml(enabled ? t(i18n, `workspace.tool.${tool.tool_code}.description`, copy[1]) : t(i18n, "workspace.tool_unavailable_reason", reasonLabel(tool.reason_code))) + "</p></div>"
      + '<button type="button" data-workspace-advanced-tool="' + escapeAttribute(tool.tool_code)
      + '" data-workspace-advanced-route="' + escapeAttribute(tool.route_code) + '" '
      + (enabled ? "" : "disabled") + ">" + (enabled ? escapeHtml(t(i18n, "workspace.open", "打开")) : escapeHtml(t(i18n, "workspace.unavailable", "不可用"))) + "</button></article>";
  }).join("") + "</div>";
}

function freshnessLabel(value, i18n) {
  return ({ fresh: t(i18n, "workspace.fresh", "已更新"), stale: t(i18n, "workspace.stale", "最近快照"), unavailable: t(i18n, "workspace.unavailable_source", "来源不可用") })[value] ?? t(i18n, "workspace.pending", "待确认");
}

function roleLabel(value, i18n) {
  return ({
    owner: t(i18n, "workspace.role.owner", "空间所有者"),
    admin: t(i18n, "workspace.role.admin", "管理员"),
    developer: t(i18n, "workspace.role.developer", "开发者"),
    security_admin: t(i18n, "workspace.role.security_admin", "安全管理员"),
    finance_admin: t(i18n, "workspace.role.finance_admin", "财务管理员"),
    legal_admin: t(i18n, "workspace.role.legal_admin", "法务管理员"),
    auditor: t(i18n, "workspace.role.auditor", "审计员"),
    viewer: t(i18n, "workspace.role.viewer", "查看者"),
  })[value] ?? t(i18n, "workspace.role.member", "当前成员");
}

function reasonLabel(value) {
  return value ? "当前空间尚未满足打开条件。" : "此工具当前不可用。";
}

function statusLabel(value, i18n) {
  return ({
    not_started: t(i18n, "workspace.status.not_started", "尚未开始"),
    ready: t(i18n, "workspace.status.ready", "待完成"),
    blocked: t(i18n, "workspace.status.blocked", "等待前置条件"),
    in_progress: t(i18n, "workspace.status.in_progress", "处理中"),
    completed: t(i18n, "workspace.status.completed", "已完成"),
    needs_attention: t(i18n, "workspace.status.needs_attention", "需要关注"),
    not_applicable: t(i18n, "workspace.status.not_applicable", "不适用"),
    pending: t(i18n, "workspace.status.pending", "待确认"),
  })[value] ?? t(i18n, "workspace.status.unknown", "状态待确认");
}

function t(i18n, key, fallback, params = {}) {
  const text = i18n?.t?.(key, params, fallback);
  return text == null || text === key ? fallback : text;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll(String.fromCharCode(96), "&#096;");
}
