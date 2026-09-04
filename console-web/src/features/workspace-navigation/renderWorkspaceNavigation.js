import { WORKSPACE_ROUTE_REGISTRY } from "../../lib/workspaceNavigation.js";

const TASK_COPY = Object.freeze({
  workspace_profile: ["确认空间资料", "核对空间名称、区域与服务方式。"],
  member_responsibilities: ["分配成员职责", "确认管理、开发、安全与财务职责覆盖。"],
  subscription_ready: ["确认订阅状态", "核对当前套餐、额度与账务状态。"],
  provider_connected: ["连接 Provider", "建立至少一个可用的模型服务连接。"],
  model_catalog_ready: ["准备模型目录", "确认模型与别名能够供应用选择。"],
  project_key_ready: ["准备项目与调用凭据", "建立项目、应用和受控调用凭据。"],
  first_gateway_call: ["完成首次真实调用", "用实际调用验证 Gateway 链路。"],
  security_policy_ready: ["配置安全策略", "确认策略在当前空间生效。"],
  dlp_ready: ["配置数据防护", "建立适合当前业务的数据防护规则。"],
  audit_coverage: ["检查审计覆盖", "确认关键操作具备可追溯证据。"],
  cost_controls: ["设置成本控制", "建立预算与成本异常边界。"],
  finops_ready: ["检查内部 FinOps", "核对成本率与内部计量事实。"],
  data_lifecycle: ["配置数据生命周期", "确认保留、导出和删除规则。"],
  service_visibility: ["查看服务状态", "确认当前空间的服务可用性。"],
});

const STATUS_COPY = Object.freeze({
  not_started: "尚未开始",
  ready: "可以处理",
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

export function renderWorkspaceTaskHome(state) {
  if (!state) {
    return '<section class="workspace-task-home" aria-live="polite"><p class="workspace-navigation-state">正在读取当前职责的下一步任务...</p></section>';
  }
  if (state?.status === "error" || state?.error) {
    return '<section class="workspace-task-home" role="alert"><p class="workspace-navigation-state is-error">任务事实暂时不可用，请稍后刷新。</p></section>';
  }
  const tasks = Array.isArray(state.tasks) ? state.tasks : [];
  const summary = '<div class="workspace-task-summary"><div><span>已完成</span><strong>'
    + escapeHtml(state.completed_count ?? 0)
    + '</strong></div><div><span>需要关注</span><strong>'
    + escapeHtml(state.needs_attention_count ?? 0)
    + '</strong></div><div><span>来源状态</span><strong>'
    + escapeHtml(freshnessLabel(state.source_freshness))
    + '</strong></div></div>';
  const content = tasks.length === 0
    ? '<p class="workspace-navigation-state">当前职责没有待处理任务。</p>'
    : '<div class="workspace-task-list">' + tasks.map(renderTask).join("") + "</div>";
  return '<section class="workspace-task-home" aria-labelledby="workspace-task-home-title">'
    + '<div class="workspace-navigation-heading"><div><p>当前职责</p><h3 id="workspace-task-home-title">开始使用</h3></div>'
    + '<span class="workspace-role-badge">' + escapeHtml(roleLabel(state.role_code)) + "</span></div>"
    + summary + content + "</section>";
}

export function renderWorkspaceAdvancedTools(state) {
  const body = state?.status === "loading"
    ? '<p class="workspace-navigation-state" aria-live="polite">正在核对高级工具权限...</p>'
    : state?.status === "error"
      ? '<p class="workspace-navigation-state is-error" role="alert">高级工具暂时不可用，请稍后重试。</p>'
      : renderToolList(state?.tools ?? []);
  return '<dialog class="workspace-advanced-dialog" data-workspace-advanced-dialog '
    + 'aria-labelledby="workspace-advanced-title"><div class="workspace-advanced-heading">'
    + '<div><p>按需打开</p><h2 id="workspace-advanced-title">高级工具</h2></div>'
    + '<button type="button" class="workspace-dialog-close" data-workspace-advanced-close aria-label="关闭高级工具" title="关闭">&times;</button>'
    + "</div>" + body + "</dialog>";
}

function renderTask(task) {
  const copy = TASK_COPY[task.task_code] ?? ["空间任务", "打开对应页面继续处理。"];
  const route = task.action_route_code;
  const routeAllowed = route === null || Boolean(WORKSPACE_ROUTE_REGISTRY[route]);
  const enabled = Boolean(task.action_enabled && route && routeAllowed);
  return '<article class="workspace-task-row" data-task-status="' + escapeAttribute(task.status) + '">'
    + '<div><span class="workspace-task-status">' + escapeHtml(STATUS_COPY[task.status] ?? "状态待确认") + "</span>"
    + "<h4>" + escapeHtml(copy[0]) + "</h4><p>" + escapeHtml(copy[1]) + "</p></div>"
    + (enabled
      ? '<button type="button" data-workspace-task-route="' + escapeAttribute(route) + '">打开</button>'
      : '<span class="workspace-task-no-action">' + (task.status === "completed" ? "完成" : "查看状态") + "</span>")
    + "</article>";
}

function renderToolList(tools) {
  if (!Array.isArray(tools) || tools.length === 0) {
    return '<p class="workspace-navigation-state">当前职责没有可用的高级工具。</p>';
  }
  return '<div class="workspace-advanced-list">' + tools.map((tool) => {
    const copy = TOOL_COPY[tool.tool_code] ?? ["高级工具", "打开受控工具。"];
    const enabled = tool.availability === "available" && tool.action_enabled;
    return '<article class="workspace-advanced-row"><div><h3>' + escapeHtml(copy[0])
      + "</h3><p>" + escapeHtml(enabled ? copy[1] : reasonLabel(tool.reason_code)) + "</p></div>"
      + '<button type="button" data-workspace-advanced-tool="' + escapeAttribute(tool.tool_code)
      + '" data-workspace-advanced-route="' + escapeAttribute(tool.route_code) + '" '
      + (enabled ? "" : "disabled") + ">" + (enabled ? "打开" : "不可用") + "</button></article>";
  }).join("") + "</div>";
}

function freshnessLabel(value) {
  return ({ fresh: "已更新", stale: "最近快照", unavailable: "来源不可用" })[value] ?? "待确认";
}

function roleLabel(value) {
  return ({
    owner: "空间所有者",
    admin: "管理员",
    developer: "开发者",
    security_admin: "安全管理员",
    finance_admin: "财务管理员",
    legal_admin: "法务管理员",
    auditor: "审计员",
    viewer: "查看者",
  })[value] ?? "当前成员";
}

function reasonLabel(value) {
  return value ? "当前空间尚未满足打开条件。" : "此工具当前不可用。";
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
