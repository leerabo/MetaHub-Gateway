import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 渲染 PAGE-085 角色权限矩阵。
 *
 * 页面只消费后端 Permission/RolePermission DTO；角色上限、锁定状态和可编辑单元格均来自响应，
 * 本模块不按 Finance/Security/Ops 名称推导任何授权。后端仍对 GET/PATCH 执行最终权限与租户校验。
 */
export function renderRolePermissionMatrix(state, authContext) {
  if (!hasPermission(authContext, PERMISSIONS.RBAC_MATRIX_MANAGE)) return "";
  if (!state || state.state === "idle" || state.state === "loading") {
    return `
      <section class="role-permission-page" aria-labelledby="role-permission-title">
        ${renderHeading()}
        <div class="empty-state" role="status">正在读取当前租户权限矩阵。</div>
      </section>
    `;
  }
  // 完整矩阵不可用时只展示统一错误与 request_id，禁止渲染残留角色或 checkbox。
  if (state.state === "error") {
    return `
      <section class="role-permission-page" aria-labelledby="role-permission-title">
        ${renderHeading()}
        ${renderError(state.loadError, "权限矩阵暂不可用")}
      </section>
    `;
  }

  const permissions = Array.isArray(state.permissions) ? state.permissions : [];
  const roles = Array.isArray(state.roles) ? state.roles : [];
  const selectedRole = roles.find((role) => role.role_id === state.selectedRoleId) ?? roles[0] ?? null;
  const categories = [...new Set(permissions.map((permission) => permission.category))];
  const visiblePermissions = state.categoryFilter === "all"
    ? permissions
    : permissions.filter((permission) => permission.category === state.categoryFilter);
  const draftCodes = new Set(state.draftPermissionCodes ?? []);

  return `
    <section class="role-permission-page" aria-labelledby="role-permission-title">
      ${renderHeading(state)}
      <div class="role-permission-toolbar">
        <label>
          <span>权限分组</span>
          <select id="role-permission-category" name="category">
            <option value="all"${state.categoryFilter === "all" ? " selected" : ""}>全部分组</option>
            ${categories.map((category) => `
              <option value="${escapeAttribute(category)}"${state.categoryFilter === category ? " selected" : ""}>
                ${escapeHtml(category)}
              </option>
            `).join("")}
          </select>
        </label>
        <span class="role-permission-count">${visiblePermissions.length} / ${permissions.length} 项权限</span>
      </div>

      ${renderRoleTabs(roles, selectedRole?.role_id, state.dirty)}
      ${renderDesktopMatrix(visiblePermissions, roles, state, draftCodes)}
      ${renderMobilePermissions(visiblePermissions, selectedRole, state, draftCodes)}
      ${renderSaveBar(selectedRole, state)}
      ${state.loadError ? renderError(state.loadError, "权限矩阵刷新失败") : ""}
      ${state.actionError ? renderError(state.actionError, "角色权限保存失败") : ""}
      ${state.feedback ? `<p class="role-permission-feedback" role="status">${escapeHtml(state.feedback)}</p>` : ""}
    </section>
  `;
}

/** 渲染页面标题与目录 revision；不展示 authorization fingerprint 或完整 owner。 */
function renderHeading(state = {}) {
  return `
    <div class="panel-heading role-permission-heading">
      <div>
        <h2 id="role-permission-title">角色权限</h2>
        <p>当前租户固定角色目录。保存仅替换选定角色的已授权权限。</p>
      </div>
      ${state.catalogRevision
        ? `<span class="status-mark status-active">目录 revision ${escapeHtml(state.catalogRevision)}</span>`
        : ""}
    </div>
  `;
}

/** 渲染移动端角色 Tab，也作为桌面端当前编辑角色的快捷选择。 */
function renderRoleTabs(roles, selectedRoleId, dirty) {
  return `
    <div class="role-permission-tabs" role="tablist" aria-label="角色">
      ${roles.map((role) => {
        const selected = role.role_id === selectedRoleId;
        return `
          <button
            type="button"
            role="tab"
            aria-selected="${selected}"
            tabindex="${selected ? "0" : "-1"}"
            data-rbac-role-tab="${escapeAttribute(role.role_id)}"
            ${dirty && !selected ? "disabled" : ""}
          >
            ${escapeHtml(role.name)}${role.immutable ? " · 锁定" : ""}
          </button>
        `;
      }).join("")}
    </div>
  `;
}

/** 渲染桌面紧凑矩阵；每个 checkbox 的 disabled 只依据后端 immutable/grantable。 */
function renderDesktopMatrix(permissions, roles, state, draftCodes) {
  return `
    <div class="role-permission-matrix" role="table" aria-label="角色权限矩阵">
      <div class="role-permission-row role-permission-head" role="row">
        <span role="columnheader">权限</span>
        ${roles.map((role) => `
          <span role="columnheader" title="${escapeAttribute(role.name)}">
            ${escapeHtml(role.name)}
          </span>
        `).join("")}
      </div>
      ${permissions.map((permission) => `
        <div class="role-permission-row" role="row">
          ${renderPermissionSummary(permission, "rowheader")}
          ${roles.map((role) => renderPermissionCell(permission, role, state, draftCodes, "desktop")).join("")}
        </div>
      `).join("")}
    </div>
  `;
}

/** 移动端只渲染当前角色的纵向权限列表，避免六角色列产生横向滚动。 */
function renderMobilePermissions(permissions, selectedRole, state, draftCodes) {
  if (!selectedRole) return '<div class="empty-state">当前没有可用角色。</div>';
  return `
    <div class="role-permission-mobile" role="list" aria-label="${escapeAttribute(selectedRole.name)} 权限">
      ${permissions.map((permission) => `
        <div class="role-permission-mobile-item" role="listitem">
          ${renderPermissionSummary(permission)}
          ${renderPermissionCell(permission, selectedRole, state, draftCodes, "mobile")}
        </div>
      `).join("")}
    </div>
  `;
}

/** 渲染权限名称、稳定 code、描述和风险等级。 */
function renderPermissionSummary(permission, role = "") {
  return `
    <span class="role-permission-summary"${role ? ` role="${role}"` : ""}>
      <strong>${escapeHtml(permission.name)}</strong>
      <code>${escapeHtml(permission.code)}</code>
      <small>${escapeHtml(permission.description)}</small>
      <mark class="permission-risk permission-risk-${escapeAttribute(permission.risk_level)}">
        ${escapeHtml(riskLabel(permission.risk_level))}
      </mark>
    </span>
  `;
}

/** 渲染一个角色权限单元格；未知或不可授予权限显示锁定且不产生交互节点。 */
function renderPermissionCell(permission, role, state, draftCodes, layout) {
  const isDraftRole = role.role_id === state.selectedRoleId && Array.isArray(state.draftPermissionCodes);
  const granted = isDraftRole
    ? draftCodes.has(permission.code)
    : role.permission_codes.includes(permission.code);
  const grantable = !role.immutable && role.grantable_permission_codes.includes(permission.code);
  const disabled = !grantable || state.busy || state.serverChanged;
  const inputId = `rbac-${layout}-${safeDomId(role.role_id)}-${safeDomId(permission.code)}`;
  return `
    <span class="role-permission-cell" role="cell">
      <input
        id="${escapeAttribute(inputId)}"
        type="checkbox"
        data-rbac-permission-toggle
        data-role-id="${escapeAttribute(role.role_id)}"
        data-permission-code="${escapeAttribute(permission.code)}"
        ${granted ? "checked" : ""}
        ${disabled ? "disabled" : ""}
        aria-label="${escapeAttribute(`${role.name} ${permission.name}`)}"
      />
      <label for="${escapeAttribute(inputId)}" title="${escapeAttribute(disabled ? "该权限已锁定或不在角色授权上限内" : "切换权限")}">
        ${granted ? "已授权" : "未授权"}
      </label>
    </span>
  `;
}

/** 渲染 CAS、dirty、冲突和保存命令；最长 hash 使用可换行 code。 */
function renderSaveBar(selectedRole, state) {
  if (!selectedRole) return "";
  const saveDisabled = !state.dirty || state.busy || state.serverChanged;
  return `
    <div class="role-permission-savebar">
      <div>
        <strong>${escapeHtml(selectedRole.name)}</strong>
        <span>revision ${escapeHtml(selectedRole.permission_revision)}</span>
        <code>${escapeHtml(selectedRole.matrix_sha256)}</code>
      </div>
      <div class="role-permission-save-actions">
        ${state.serverChanged
          ? '<span class="role-permission-conflict" role="alert">服务器矩阵已更新，请重新加载后再保存。</span>'
          : ""}
        <button id="role-permission-discard" type="button" ${!state.dirty && !state.serverChanged ? "disabled" : ""}>放弃草稿</button>
        <button id="role-permission-save" type="button" ${saveDisabled ? "disabled" : ""}>
          ${state.busy ? "保存中" : "保存权限"}
        </button>
      </div>
    </div>
  `;
}

/** 渲染统一错误码和 request_id，不回显 SQL、响应正文或矩阵内容。 */
function renderError(error, fallback) {
  return `
    <div class="empty-state error-state role-permission-error" role="alert">
      <strong>${escapeHtml(error?.code ?? "rbac.unavailable")}</strong>
      <span>${escapeHtml(error?.message ?? fallback)}</span>
      <code>request_id: ${escapeHtml(error?.requestId ?? "-")}</code>
    </div>
  `;
}

/** 把风险枚举转换为简短展示文案，不参与授权判断。 */
function riskLabel(value) {
  return { read: "读取", write: "写入", critical: "关键" }[value] ?? value;
}

/** 把稳定 ID 转为仅供 input/label 关联的 DOM 片段。 */
function safeDomId(value) {
  return String(value ?? "").replaceAll(/[^A-Za-z0-9_-]/g, "-");
}

/** 转义后端展示文本，防止权限名称或描述突破 HTML 文本边界。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 属性值在 HTML 转义基础上额外处理反引号。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
