/**
 * 模块职责：渲染 Project/Application 管理面板，让企业用户按业务系统维护调用归属。
 * 维护边界：前端只做展示和表单收口；读写权限、tenant_id 隔离和归属状态必须由后端接口强制校验。
 * 来源文件：console-web/src/features/projects/renderProjectApplicationPanel.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 状态标签只用于控制台展示，不能替代后端状态机和绑定校验。
const STATUS_LABELS = {
  active: "启用",
  disabled: "停用",
  archived: "归档",
};

/**
 * 渲染项目和应用管理面板。
 * 项目/应用是日志、用量、预算和 Virtual Key 绑定的业务归属根，页面必须避免显示或提交 tenant_id。
 */
export function renderProjectApplicationPanel(projectState, authContext) {
  // 控制台只消费后端 project.read，真实 tenant 边界仍由两个业务接口独立校验。
  if (!hasPermission(authContext, PERMISSIONS.PROJECT_READ)) {
    return "";
  }
  if (!projectState) {
    return renderLoadingPanel();
  }
  // 错误态必须展示 request_id，便于把控制台问题定位到控制面调用日志。
  if (projectState.state === "error") {
    return renderErrorPanel(projectState);
  }

  const projects = projectState.projects ?? [];
  const applications = projectState.applications ?? [];
  const canWrite = hasPermission(authContext, PERMISSIONS.PROJECT_WRITE);
  return `
    <div class="panel-heading projects-heading">
      <div>
        <h2 id="projects-title">项目与应用</h2>
        <p>按业务系统维护 Project/Application，供 Virtual Key、日志、用量和预算归因使用。</p>
      </div>
      <span class="projects-count">${escapeHtml(projects.length)} 个项目 · ${escapeHtml(applications.length)} 个应用</span>
    </div>
    ${renderActionError(projectState.actionError)}
    ${renderFeedback(projectState.feedback)}
    ${canWrite ? renderAdminForms(projects) : renderReadOnlyNotice()}
    ${renderProjectTable(projects)}
    ${renderApplicationTable(applications)}
  `;
}

/**
 * 渲染加载态，避免导航切换时项目面板空白。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="projects-title">项目与应用</h2>
      <p>正在读取当前租户的 Project/Application。</p>
    </div>
    <div class="empty-state">等待 /projects 和 /applications 响应。</div>
  `;
}

/**
 * 渲染接口错误态，并保留后端 request_id。
 */
function renderErrorPanel(state) {
  return `
    <div class="panel-heading">
      <h2 id="projects-title">项目与应用</h2>
      <p>项目与应用暂不可用。</p>
    </div>
    <div class="empty-state error-state">
      ${escapeHtml(state.message ?? "项目接口读取失败")} request_id: ${escapeHtml(state.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染管理员创建表单。
 * 创建项目和应用分成两个表单，避免应用在没有项目的情况下被提交。
 */
function renderAdminForms(projects) {
  return `
    <div class="project-admin-forms">
      ${renderProjectForm()}
      ${renderApplicationForm(projects)}
    </div>
  `;
}

/**
 * 渲染项目创建表单。
 * 表单字段与后端 CreateProjectRequest 保持一致，不包含 tenant_id 或 owner_user_id。
 */
function renderProjectForm() {
  return `
    <form class="project-form" id="create-project-form">
      <h3>创建 Project</h3>
      <label>
        <span>项目编码</span>
        <input name="projectCode" maxlength="64" required placeholder="billing-core" />
      </label>
      <label>
        <span>项目名称</span>
        <input name="name" maxlength="120" required placeholder="账务核心" />
      </label>
      <label>
        <span>说明</span>
        <input name="description" maxlength="500" placeholder="业务系统、负责人或用途" />
      </label>
      <div class="project-actions">
        <button type="submit">创建项目</button>
      </div>
    </form>
  `;
}

/**
 * 渲染应用创建表单。
 * 应用必须选择 active 项目，归档项目不能继续挂新应用。
 */
function renderApplicationForm(projects) {
  const activeProjects = projects.filter((item) => item.status === "active");
  const disabled = activeProjects.length ? "" : " disabled";
  return `
    <form class="project-form" id="create-application-form">
      <h3>创建 Application</h3>
      <label>
        <span>所属项目</span>
        <select name="projectId" required${disabled}>
          ${renderProjectOptions(activeProjects)}
        </select>
      </label>
      <label>
        <span>应用编码</span>
        <input name="applicationCode" maxlength="64" required placeholder="web-api" />
      </label>
      <label>
        <span>应用名称</span>
        <input name="name" maxlength="120" required placeholder="账务 API" />
      </label>
      <label>
        <span>说明</span>
        <input name="description" maxlength="500" placeholder="服务、环境或调用方" />
      </label>
      <div class="project-actions">
        <button type="submit"${disabled}>创建应用</button>
      </div>
    </form>
  `;
}

/**
 * 渲染项目选择器选项，展示名称和稳定编码而不是 tenant_id。
 */
function renderProjectOptions(projects) {
  if (!projects.length) {
    return `<option value="">先创建 active 项目</option>`;
  }
  return projects
    .map((item) => `<option value="${escapeAttribute(item.project_id)}">${escapeHtml(item.name)} / ${escapeHtml(item.project_code)}</option>`)
    .join("");
}

/**
 * 渲染开发者只读提示，避免把前端按钮隐藏误解成权限实现。
 */
function renderReadOnlyNotice() {
  return `
    <div class="project-readonly">
      当前角色可查看项目与应用归属；创建和变更治理边界只能由管理员完成。
    </div>
  `;
}

/**
 * 渲染项目表格。
 * 计数来自后端聚合，前端不重新计算，避免和租户过滤口径不一致。
 */
function renderProjectTable(projects) {
  return `
    <div class="projects-grid" role="table" aria-label="当前租户项目">
      <div class="project-row project-head" role="row">
        <span>项目</span>
        <span>状态</span>
        <span>负责人</span>
        <span>应用</span>
        <span>Key</span>
        <span>更新时间</span>
      </div>
      ${projects.length ? projects.map(renderProjectRow).join("") : renderProjectEmpty()}
    </div>
  `;
}

/**
 * 渲染单个项目行，避免把项目 ID 当成主要展示字段。
 */
function renderProjectRow(item) {
  return `
    <div class="project-row" role="row">
      <span class="project-name">
        <strong>${escapeHtml(item.name)}</strong>
        <em>${escapeHtml(item.project_code)} · ${escapeHtml(item.project_id)}</em>
      </span>
      <span>${renderStatusMark(item.status)}</span>
      <span>${escapeHtml(item.owner_display_name ?? item.owner_user_id ?? "-")}</span>
      <span>${escapeHtml(item.application_count ?? 0)}</span>
      <span>${escapeHtml(item.virtual_key_count ?? 0)}</span>
      <time datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
    </div>
  `;
}

/**
 * 渲染项目空状态行，保持表格结构稳定。
 */
function renderProjectEmpty() {
  return `<div class="project-row project-empty" role="row"><span>当前租户尚未创建项目。</span></div>`;
}

/**
 * 渲染应用表格。
 * 应用作为二级归属，显示父项目名称帮助排查 Key 绑定错位。
 */
function renderApplicationTable(applications) {
  return `
    <div class="applications-grid" role="table" aria-label="当前租户应用">
      <div class="application-row application-head" role="row">
        <span>应用</span>
        <span>所属项目</span>
        <span>状态</span>
        <span>Key</span>
        <span>更新时间</span>
      </div>
      ${applications.length ? applications.map(renderApplicationRow).join("") : renderApplicationEmpty()}
    </div>
  `;
}

/**
 * 渲染单个应用行，只展示脱敏业务元数据。
 */
function renderApplicationRow(item) {
  return `
    <div class="application-row" role="row">
      <span class="project-name">
        <strong>${escapeHtml(item.name)}</strong>
        <em>${escapeHtml(item.application_code)} · ${escapeHtml(item.application_id)}</em>
      </span>
      <span>${escapeHtml(item.project_name ?? item.project_code ?? item.project_id)}</span>
      <span>${renderStatusMark(item.status)}</span>
      <span>${escapeHtml(item.virtual_key_count ?? 0)}</span>
      <time datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
    </div>
  `;
}

/**
 * 渲染应用空状态行，提示当前租户还没有二级业务归属。
 */
function renderApplicationEmpty() {
  return `<div class="application-row application-empty" role="row"><span>当前租户尚未创建应用。</span></div>`;
}

/**
 * 渲染状态标签，状态值保持后端枚举，中文只作为展示文案。
 */
function renderStatusMark(status) {
  return `<mark class="status-mark status-${escapeAttribute(status)}">${escapeHtml(STATUS_LABELS[status] ?? status ?? "-")}</mark>`;
}

/**
 * 渲染项目操作错误。
 */
function renderActionError(error) {
  // 没有操作错误时不渲染提示，避免干扰项目表格的正常扫描。
  if (!error) {
    return "";
  }
  return `
    <div class="project-action-error" role="alert">
      ${escapeHtml(error.message ?? "项目操作失败")} request_id: ${escapeHtml(error.requestId ?? "-")}
    </div>
  `;
}

/**
 * 渲染项目操作反馈。
 */
function renderFeedback(feedback) {
  return feedback ? `<div class="project-feedback" role="status">${escapeHtml(feedback)}</div>` : "";
}

/**
 * 格式化 ISO 时间，避免表格中出现冗长的小数秒。
 */
function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  return String(value).replace("T", " ").replace(/\.\d+Z?$/, "Z");
}

/**
 * HTML 文本转义，防止项目名称或说明破坏页面结构。
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
 * HTML 属性转义，额外处理反引号以保护 value 和 datetime 属性。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
