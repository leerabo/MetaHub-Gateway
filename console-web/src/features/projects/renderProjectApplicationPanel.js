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

/** 将展开动作限制在当前面板，支持键盘并通知控制器保留展开状态。 */
export function bindProjectExpansion(container, onToggle = () => {}) {
  container.querySelectorAll("[data-toggle-project]").forEach((button) => {
    button.addEventListener("click", () => {
      const children = Array.from(container.querySelectorAll(".project-children"))
        .find((group) => group.id === button.getAttribute("aria-controls"));
      if (!children) return;
      const expanded = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(expanded));
      children.hidden = !expanded;
      onToggle(button.dataset.toggleProject, expanded);
    });
  });
}

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
        <p>按业务系统归类调用，方便查看用量和预算。</p>
      </div>
      <span class="projects-count">${escapeHtml(projects.length)} 个项目 · ${escapeHtml(applications.length)} 个应用</span>
    </div>
    ${renderActionError(projectState.actionError)}
    ${renderFeedback(projectState.feedback)}
    ${canWrite ? renderAdminForms(projects) : renderReadOnlyNotice()}
    ${renderProjectTree(projects, applications, canWrite, projectState.expandedProjectIds ?? [])}
    ${canWrite ? renderEditDialogs(projects, applications) : ""}
    ${renderApplicationMembersDialog(canWrite)}
  `;
}

/**
 * 渲染加载态，避免导航切换时项目面板空白。
 */
function renderLoadingPanel() {
  return `
    <div class="panel-heading">
      <h2 id="projects-title">项目与应用</h2>
      <p>正在读取当前组织的项目和应用。</p>
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
      <details class="project-create-disclosure"${projects.length ? "" : " open"}>
        <summary>新建项目</summary>
        ${renderProjectForm()}
      </details>
      ${projects.length
        ? `<details class="project-create-disclosure"><summary>新建应用</summary>${renderApplicationForm(projects)}</details>`
        : '<p class="project-next-step">创建项目后，这里会出现“新建应用”。</p>'}
    </div>
  `;
}

/**
 * 渲染项目创建表单。
 * 负责人从组织成员选择，不要求用户输入内部编号。
 */
function renderProjectForm() {
  return `
    <form class="project-form" id="create-project-form">
        <h3>新建项目</h3>
      <label>
        <span>项目名称</span>
        <input name="name" maxlength="120" required placeholder="账务核心" />
      </label>
      <label>
        <span>说明</span>
        <input name="description" maxlength="500" placeholder="业务系统、负责人或用途" />
      </label>
      ${renderOwnerPicker()}
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
        <h3>新建应用</h3>
      <label>
        <span>所属项目</span>
        <select name="projectId" required${disabled}>
          ${renderProjectOptions(activeProjects)}
        </select>
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
function renderProjectOptions(projects, selectedProjectId = "") {
  if (!projects.length) {
    return `<option value="">先创建 active 项目</option>`;
  }
  return projects
    .map((item) => `<option value="${escapeAttribute(item.project_id)}"${item.project_id === selectedProjectId ? " selected" : ""}>${escapeHtml(item.name)}</option>`)
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
function renderProjectTree(projects, applications, canWrite, expandedProjectIds) {
  return `
    <div class="projects-grid" data-has-actions="${canWrite ? "true" : "false"}" role="table" aria-label="当前租户项目">
      <div class="project-row project-head" role="row">
        <span>项目 / 应用</span>
        <span>状态</span>
        <span>负责人 / 成员</span>
        <span>应用数量</span>
        <span>KEY 数量</span>
        <span>更新时间</span>
        ${canWrite ? "<span>操作</span>" : ""}
      </div>
      ${projects.length ? projects.map((item) => {
        const children = applications.filter((application) => application.project_id === item.project_id);
        const expanded = expandedProjectIds.includes(item.project_id);
        return `<div class="project-tree-group">${renderProjectRow(item, children, canWrite, expanded)}${renderApplicationSection(children, canWrite, item.project_id, expanded)}</div>`;
      }).join("") : renderProjectEmpty()}
    </div>
  `;
}

/**
 * 子行沿用项目列定义；只展示人数，完整名单按需在弹框读取。
 */
function renderApplicationSection(applications, canWrite, projectId, expanded) {
  const rows = applications.length
    ? applications.map((application) => renderApplicationChildRow(application, canWrite)).join("")
    : `<div class="application-child-row application-child-empty" role="row">
        <span>这个项目还没有应用。</span>
      </div>`;
  return `<div class="project-children" id="project-children-${escapeAttribute(projectId)}" role="rowgroup"${expanded ? "" : " hidden"}>${rows}</div>`;
}

/**
 * 渲染单个项目行，避免把项目 ID 当成主要展示字段。
 */
function renderProjectRow(item, applications, canWrite, expanded) {
  return `
    <div class="project-row" role="row">
      <span class="project-name" data-label="项目">
        <button type="button" class="project-expand-button" data-toggle-project="${escapeAttribute(item.project_id)}" aria-expanded="${expanded}" aria-controls="project-children-${escapeAttribute(item.project_id)}">
        <svg class="project-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>
        <strong>${escapeHtml(item.name)}</strong>
        </button>
      </span>
      <span data-label="状态">${renderStatusMark(item.status)}</span>
      <span data-label="负责人" class="project-owner">${escapeHtml(item.owner_display_name || (item.owner_user_id ? "负责人姓名待读取" : "未设置负责人"))}</span>
      <span data-label="应用数量">${escapeHtml(item.application_count ?? applications.length)}</span>
      <span data-label="KEY 数量">${escapeHtml(item.virtual_key_count ?? 0)}</span>
      <time data-label="更新时间" datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
      ${canWrite ? `<span class="project-row-actions" data-label="操作">
        <button type="button" data-edit-project="${escapeAttribute(item.project_id)}">编辑</button>
        <button type="button" data-delete-project="${escapeAttribute(item.project_id)}"${item.virtual_key_count ? " disabled title=\"已有 KEY 或使用记录时请结束项目\"" : ""}>删除</button>
      </span>` : ""}
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
 * 渲染项目下的应用子行。应用永远跟随父项目展示，避免用户误以为它是独立资源。
 */
function renderApplicationChildRow(item, canWrite) {
  return `
    <div class="application-child-row" role="row">
      <span class="project-name application-child-name" data-label="应用">
        <strong>${escapeHtml(item.name)}</strong>
      </span>
      <span data-label="状态">${renderStatusMark(item.status)}</span>
      <span data-label="成员"><button type="button" class="application-member-count" data-manage-application-members="${escapeAttribute(item.application_id)}" data-application-name="${escapeAttribute(item.name)}" aria-label="查看 ${escapeAttribute(item.name)} 成员">${item.member_count == null ? "查看成员" : `${escapeHtml(item.member_count)} 位成员`}</button></span>
      <span class="application-project-count" aria-hidden="true">—</span>
      <span data-label="KEY 数量">${escapeHtml(item.virtual_key_count ?? 0)}</span>
      <time data-label="更新时间" datetime="${escapeAttribute(item.updated_at)}">${escapeHtml(formatDateTime(item.updated_at))}</time>
      ${canWrite ? `<span class="project-row-actions" data-label="操作">
        <button type="button" data-edit-application="${escapeAttribute(item.application_id)}">编辑</button>
        <button type="button" data-delete-application="${escapeAttribute(item.application_id)}"${item.virtual_key_count ? " disabled title=\"已有 KEY 或历史用量时请结束应用\"" : ""}>删除</button>
      </span>` : ""}
    </div>
  `;
}

function renderApplicationMembersDialog(canWrite) {
  return `
    <dialog class="model-alias-dialog project-members-dialog" data-application-members-dialog aria-labelledby="application-members-dialog-title">
      <form method="dialog" class="model-alias-dialog-content project-members-form" data-application-members-form data-can-write="${canWrite}">
        <div class="model-alias-dialog-heading">
          <div><span class="model-alias-dialog-kicker">应用</span><h3 id="application-members-dialog-title" data-application-members-title>应用成员</h3></div>
          <button type="button" class="icon-button" data-project-dialog-close aria-label="关闭">×</button>
        </div>
        <p class="application-members-help">${canWrite ? "勾选组织成员以分配到此应用；名单支持搜索和分页。" : "查看已分配到此应用的成员；名单支持搜索和分页。"}</p>
        <label class="application-members-search"><span class="sr-only">搜索成员</span><input type="search" data-application-members-search placeholder="搜索成员" autocomplete="off" /></label>
        <div class="application-members-list" data-application-members-list><div class="empty-state">正在读取成员</div></div>
        <div class="application-members-meta"><span data-application-members-count>0 人已选择</span><span data-application-members-page></span><span class="application-members-pagination"><button type="button" class="button-secondary" data-application-members-prev disabled>上一页</button><button type="button" class="button-secondary" data-application-members-next disabled>下一页</button></span></div>
        <div class="project-actions"><button type="button" class="button-secondary" data-project-dialog-close>${canWrite ? "取消" : "关闭"}</button>${canWrite ? '<button type="submit" data-application-members-save>保存</button>' : ""}</div>
      </form>
    </dialog>`;
}

function renderEditDialogs(projects, applications) {
  return `${projects.map((item) => `
    <dialog class="project-edit-dialog" data-project-edit-dialog="${escapeAttribute(item.project_id)}">
      <form method="dialog" class="project-form" data-project-edit-form="${escapeAttribute(item.project_id)}">
        <div class="project-dialog-heading"><h3>编辑项目</h3><button type="button" class="icon-button" data-project-dialog-close>×</button></div>
        <label><span>项目名称</span><input name="name" maxlength="120" required value="${escapeAttribute(item.name)}" /></label>
        <label><span>说明</span><input name="description" maxlength="500" value="${escapeAttribute(item.description ?? "")}" /></label>
        ${renderOwnerPicker(item)}
        <label><span>状态</span><select name="status">${renderStatusOptions(item.status)}</select></label>
        <div class="project-actions"><button type="submit">保存</button></div>
      </form>
    </dialog>`).join("")}
    ${applications.map((item) => `
    <dialog class="project-edit-dialog" data-application-edit-dialog="${escapeAttribute(item.application_id)}">
      <form method="dialog" class="project-form" data-application-edit-form="${escapeAttribute(item.application_id)}">
        <div class="project-dialog-heading"><h3>编辑应用</h3><button type="button" class="icon-button" data-project-dialog-close>×</button></div>
        <label><span>所属项目</span><select name="projectId">${renderProjectOptions(projects.filter((project) => project.status !== "archived"), item.project_id)}</select></label>
        <label><span>应用名称</span><input name="name" maxlength="120" required value="${escapeAttribute(item.name)}" /></label>
        <label><span>说明</span><input name="description" maxlength="500" value="${escapeAttribute(item.description ?? "")}" /></label>
        <label><span>状态</span><select name="status">${renderStatusOptions(item.status)}</select></label>
        <div class="project-actions"><button type="submit">保存</button></div>
      </form>
    </dialog>`).join("")}`;
}

function renderOwnerPicker(project = {}) {
  return `<div class="project-owner-picker" data-project-owner-picker>
    <label><span>负责人</span><input type="search" data-project-owner-search placeholder="搜索组织成员" autocomplete="off" /></label>
    <label><span class="sr-only">选择负责人</span><select name="ownerUserId" data-project-owner-select data-current-owner="${escapeAttribute(project.owner_user_id ?? "")}"><option value="">未设置负责人</option>${project.owner_user_id ? `<option value="${escapeAttribute(project.owner_user_id)}" selected>${escapeHtml(project.owner_display_name || "当前负责人")}</option>` : ""}</select></label>
    <span class="project-owner-hint" data-project-owner-hint>打开表单后读取成员，可按姓名搜索。</span>
  </div>`;
}

function renderStatusOptions(selected) {
  return Object.entries(STATUS_LABELS)
    .map(([value, label]) => `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`)
    .join("");
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
