/**
 * 模块职责：渲染租户级 dev/test/prod Environment 设置页。
 * 维护边界：页面隐藏按钮只改善体验，Environment 写权限和 tenant_id 隔离必须由后端强制执行。
 * 来源文件：console-web/src/features/environments/renderEnvironmentPanel.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 标准代码只控制前端展示体验，最终读写权限与代码合法性由后端校验。
const STANDARD_ENVIRONMENTS = [
  { code: "dev", label: "开发", defaultName: "开发环境" },
  { code: "test", label: "测试", defaultName: "测试环境" },
  { code: "prod", label: "生产", defaultName: "生产环境" },
];

/**
 * 渲染 Environment 设置页，admin 可创建和启停，developer 只读。
 */
export function renderEnvironmentPanel(state, authContext) {
  // Environment is platform deployment administration, not a Workspace setup task.
  if (!isPlatformOperator(authContext) || !hasPermission(authContext, PERMISSIONS.ENVIRONMENT_READ)) {
    return "";
  }
  if (!state) {
    return renderShell("正在读取当前租户环境。", `<div class="empty-state">等待 /environments 响应。</div>`);
  }
  if (state.state === "error") {
    return renderShell(
      "环境设置暂不可用。",
      `<div class="empty-state error-state">${escapeHtml(state.message ?? "请稍后重试")}</div>`,
    );
  }
  const environments = state.environments ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.ENVIRONMENT_WRITE);
  return `
    <div class="panel-heading environment-heading">
      <div>
        <h2 id="environments-title">环境设置</h2>
        <p>按开发、测试、生产隔离 Virtual Key、模型别名、预算、日志和用量。</p>
      </div>
      <span class="environment-count">${environments.length} / 3</span>
    </div>
    <section class="environment-guide" aria-labelledby="environment-guide-title">
      <div>
        <h3 id="environment-guide-title">环境隔离</h3>
        <p>只有需要分开测试、生产流量或预算时才创建多个环境。</p>
      </div>
      <ol>
        <li><strong>开发</strong></li>
        <li><strong>测试</strong><span>上线前验证模型、路由和额度。</span></li>
        <li><strong>生产</strong><span>正式员工和客户使用的独立边界。</span></li>
      </ol>
    </section>
    ${renderActionError(state.actionError)}
    ${canWrite ? renderCreateForm(environments, state.feedback) : ""}
    <div class="environment-segments" role="list" aria-label="当前租户环境">
      ${STANDARD_ENVIRONMENTS.map((definition) => renderEnvironmentSegment(definition, environments, canWrite)).join("")}
    </div>
  `;
}

function isPlatformOperator(authContext) {
  const roles = authContext?.platform_context?.operator?.roles
    ?? authContext?.platformSession?.context?.operator?.roles
    ?? [];
  return Array.isArray(roles) && roles.some((role) => (
    typeof role === "string" && (role.startsWith("platform_") || role === "release_manager")
  ));
}

/**
 * 渲染页面通用标题和状态内容。
 */
function renderShell(description, body) {
  return `
    <div class="panel-heading">
      <div><h2 id="environments-title">环境设置</h2><p>${escapeHtml(description)}</p></div>
    </div>
    ${body}
  `;
}

/**
 * 渲染尚未创建的标准环境表单；已存在代码不会再次出现在下拉中。
 */
function renderCreateForm(environments, feedback) {
  const existingCodes = new Set(environments.map((item) => item.code));
  const missing = STANDARD_ENVIRONMENTS.filter((item) => !existingCodes.has(item.code));
  if (!missing.length) {
    return `<p class="environment-feedback" role="status">${escapeHtml(feedback ?? "三个标准环境已齐备")}</p>`;
  }
  return `
    <form id="create-environment-form" class="environment-create-form">
      <label>
        <span>环境</span>
        <select name="code">${missing.map((item) => `<option value="${item.code}" data-default-name="${item.defaultName}">${item.label}</option>`).join("")}</select>
      </label>
      <label>
        <span>名称</span>
        <input name="name" maxlength="80" required value="${escapeAttribute(missing[0].defaultName)}" />
      </label>
      <label class="environment-description-field">
        <span>说明</span>
        <input name="description" maxlength="240" placeholder="用途、验证范围或上线边界" />
      </label>
      <button type="submit">创建环境</button>
      <span class="environment-feedback" role="status">${escapeHtml(feedback ?? "")}</span>
    </form>
  `;
}

/**
 * 渲染单个标准环境；缺失环境显示待创建状态，存在环境展示关联数量和启停操作。
 */
function renderEnvironmentSegment(definition, environments, canWrite) {
  const environment = environments.find((item) => item.code === definition.code);
  if (!environment) {
    return `
      <section class="environment-segment environment-missing" role="listitem">
        <header><strong>${definition.label}</strong></header>
        <p>尚未创建</p>
      </section>
    `;
  }
  const nextStatus = environment.status === "active" ? "disabled" : "active";
  const actionLabel = environment.status === "active" ? "禁用" : "启用";
  return `
    <section class="environment-segment environment-${escapeAttribute(environment.status)}" role="listitem">
      <header>
        <div><strong>${escapeHtml(environment.name)}</strong></div>
        <mark class="status-mark status-${escapeAttribute(environment.status)}">${escapeHtml(environmentStatusLabel(environment.status))}</mark>
      </header>
      <p>${escapeHtml(environment.description ?? "无说明")}</p>
      <dl>
        ${renderMetric("KEY 数量", environment.virtual_key_count)}
        ${renderMetric("模型路由", environment.model_alias_count)}
        ${renderMetric("预算规则", environment.budget_rule_count)}
      </dl>
      ${
        canWrite
          ? `<form data-environment-status-form data-environment-id="${escapeAttribute(environment.environment_id)}">
              <input type="hidden" name="status" value="${nextStatus}" />
              <button type="submit">${actionLabel}</button>
            </form>`
          : ""
      }
    </section>
  `;
}

/**
 * 渲染 Environment 关联数量，不展示任何 Key 明文或请求内容。
 */
function renderMetric(label, value) {
  return `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? 0)}</dd></div>`;
}

function renderActionError(error) {
  if (!error) return "";
  return `<div class="environment-action-error" role="alert">${escapeHtml(error.message ?? "环境操作失败")}</div>`;
}

function environmentStatusLabel(value) {
  return ({ active: "启用", disabled: "停用", pending: "准备中" })[value] ?? "状态未知";
}

/**
 * 转义 HTML 文本，保护名称、说明和错误文案的展示边界。
 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/**
 * 转义 HTML 属性并额外处理反引号。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
