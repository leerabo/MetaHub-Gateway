import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

const ROLE_LABELS = Object.freeze({
  admin: "管理员", developer: "开发者", viewer: "只读成员",
  finance_admin: "财务管理员", security_admin: "安全管理员", ops_admin: "运维管理员",
});
const ROLE_I18N_KEYS = Object.freeze({
  admin: "rbac.role.admin", developer: "rbac.role.developer", viewer: "rbac.role.viewer",
  finance_admin: "rbac.role.finance_admin", security_admin: "rbac.role.security_admin",
  ops_admin: "rbac.role.ops_admin",
});
const CATEGORY_LABELS = Object.freeze({
  admin_assistant: "管理助手", approval: "审批", audit: "审计", batch: "批量任务",
  billing: "财务", chat: "企业对话", console: "控制台", developer: "开发者工具",
  environment: "部署环境", experiment: "模型评测", file: "文件", identity: "企业身份",
  integration: "集成", key: "员工 KEY", mcp: "MCP 工具", migration: "配置迁移",
  model: "模型", observability: "可观测性", partner: "伙伴", project: "项目",
  prompt_template: "提示词模板", provider: "模型服务", response_cache: "响应缓存",
  routing: "模型路由", security: "安全治理", system: "系统", tenant: "组织",
});
const CATEGORY_I18N_KEYS = Object.freeze(Object.fromEntries(
  Object.keys(CATEGORY_LABELS).map((category) => [category, `rbac.category.${category}`]),
));
const EN_PERMISSION_LABELS = Object.freeze({
  "console.basic.read": "Basic console access",
  "rbac.matrix.manage": "Manage roles and permissions",
  "audit.read": "Read audit logs",
  "audit.export": "Export audit logs",
});

function roleLabel(role, i18n) {
  if (i18n?.locale === "en-US" && ROLE_I18N_KEYS[role.code]) {
    return translate(i18n, ROLE_I18N_KEYS[role.code], role.code);
  }
  if (role.name !== role.code) return role.name;
  const fallback = ROLE_LABELS[role.code] ?? role.name;
  return translate(i18n, ROLE_I18N_KEYS[role.code], fallback);
}

/**
 * 渲染 PAGE-085 角色权限矩阵。
 *
 * 页面只消费后端 Permission/RolePermission DTO；角色上限、锁定状态和可编辑单元格均来自响应，
 * 本模块不按 Finance/Security/Ops 名称推导任何授权。后端仍对 GET/PATCH 执行最终权限与租户校验。
 */
export function renderRolePermissionMatrix(state, authContext, i18n) {
  if (!hasPermission(authContext, PERMISSIONS.RBAC_MATRIX_MANAGE)) return "";
  if (!state || state.state === "idle" || state.state === "loading") {
    return `
      <section class="role-permission-page" aria-labelledby="role-permission-title">
        ${renderHeading({}, i18n)}
        <div class="empty-state" role="status">${translate(i18n, "rbac.loading", "正在读取当前租户权限矩阵。")}</div>
      </section>
    `;
  }
  // 完整矩阵不可用时只展示统一错误与 request_id，禁止渲染残留角色或 checkbox。
  if (state.state === "error") {
    return `
      <section class="role-permission-page" aria-labelledby="role-permission-title">
        ${renderHeading(i18n)}
        ${renderError(state.loadError, translate(i18n, "rbac.unavailable", "权限矩阵暂不可用"), i18n)}
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
      ${renderHeading(state, i18n)}
      <div class="role-permission-toolbar">
        <label>
          <span>${translate(i18n, "rbac.category_filter", "按权限分组查看")}</span>
          <select id="role-permission-category" name="category" aria-describedby="role-permission-category-help">
            <option value="all"${state.categoryFilter === "all" ? " selected" : ""}>${translate(i18n, "rbac.all_categories", "全部分组")}</option>
            ${categories.map((category) => `
              <option value="${escapeAttribute(category)}"${state.categoryFilter === category ? " selected" : ""}>
                ${escapeHtml(translate(i18n, CATEGORY_I18N_KEYS[category], CATEGORY_LABELS[category] ?? category))}
              </option>
            `).join("")}
          </select>
        </label>
        <button
          type="button"
          class="role-permission-view-toggle"
          id="role-permission-view-mode"
          data-rbac-view-mode="${state.viewMode === "all" ? "all" : "role"}"
          aria-pressed="${state.viewMode === "all"}"
        >${state.viewMode === "all" ? translate(i18n, "rbac.focus_view", "仅看当前角色") : translate(i18n, "rbac.compare_view", "对比全部角色")}</button>
        <span class="role-permission-count">${visiblePermissions.length} / ${permissions.length} ${translate(i18n, "rbac.permission_count", "项权限")}</span>
        <small id="role-permission-category-help" class="role-permission-help">${translate(i18n, "rbac.category_help", "默认只显示当前角色；先选择职责分组，再按需打开完整六角色对比。")}</small>
      </div>

      ${renderRoleTabs(roles, selectedRole?.role_id, state.dirty, i18n)}
      ${state.viewMode === "all"
        ? renderDesktopMatrix(visiblePermissions, roles, state, draftCodes, i18n)
        : `<div class="role-permission-focus" aria-label="${escapeAttribute(translate(i18n, "rbac.current_role_permissions", "{role} 当前权限", { role: roleLabel(selectedRole, i18n) }))}">
            ${renderFocusedRolePermissions(visiblePermissions, selectedRole, state, draftCodes, i18n)}
          </div>
          <details class="role-permission-compare">
            <summary>${translate(i18n, "rbac.compare_summary", `对比全部角色（${permissions.length} 项权限）`, { count: permissions.length })}</summary>
            ${renderDesktopMatrix(visiblePermissions, roles, state, draftCodes, i18n)}
          </details>`}
      ${renderMobilePermissions(visiblePermissions, selectedRole, state, draftCodes, i18n)}
      ${renderSaveBar(selectedRole, state, i18n)}
      ${state.loadError ? renderError(state.loadError, translate(i18n, "rbac.refresh_failed", "权限矩阵刷新失败"), i18n) : ""}
      ${state.actionError ? renderError(state.actionError, translate(i18n, "rbac.save_failed", "角色权限保存失败"), i18n) : ""}
      ${state.feedback ? `<p class="role-permission-feedback" role="status">${escapeHtml(state.feedback)}</p>` : ""}
    </section>
  `;
}

/** 渲染页面标题与目录 revision；不展示 authorization fingerprint 或完整 owner。 */
function renderHeading(state = {}, i18n) {
  return `
    <div class="panel-heading role-permission-heading">
      <div>
        <h2 id="role-permission-title">${translate(i18n, "rbac.title", "角色权限")}</h2>
        <p class="role-permission-scope">${translate(i18n, "rbac.scope", "这里管理六个基础角色。法务管理员和审计员属于组织成员职责，不在此矩阵编辑。")}</p>
      </div>
      ${state.catalogRevision
        ? `<span class="status-mark status-active">${translate(i18n, "rbac.catalog_revision", "权限目录")}</span>`
        : ""}
    </div>
  `;
}

/** 渲染移动端角色 Tab，也作为桌面端当前编辑角色的快捷选择。 */
function renderRoleTabs(roles, selectedRoleId, dirty, i18n) {
  return `
    <div class="role-permission-tabs" role="tablist" aria-label="${escapeAttribute(translate(i18n, "rbac.roles", "角色"))}">
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
            ${escapeHtml(roleLabel(role, i18n))}${role.immutable ? ` · ${translate(i18n, "rbac.locked", "锁定")}` : ""}
          </button>
        `;
      }).join("")}
    </div>
  `;
}

/** 渲染桌面紧凑矩阵；每个 checkbox 的 disabled 只依据后端 immutable/grantable。 */
function renderDesktopMatrix(permissions, roles, state, draftCodes, i18n) {
  return `
    <div class="role-permission-matrix" role="table" aria-label="${escapeAttribute(translate(i18n, "rbac.matrix", "角色权限矩阵"))}">
      <div class="role-permission-row role-permission-head" role="row">
        <span role="columnheader">${translate(i18n, "rbac.permission", "权限")}</span>
        ${roles.map((role) => `
          <span role="columnheader" title="${escapeAttribute(roleLabel(role, i18n))}">
            ${escapeHtml(roleLabel(role, i18n))}
          </span>
        `).join("")}
      </div>
      ${permissions.map((permission) => `
        <div class="role-permission-row" role="row">
          ${renderPermissionSummary(permission, "rowheader", i18n)}
          ${roles.map((role) => renderPermissionCell(permission, role, state, draftCodes, "desktop", i18n)).join("")}
        </div>
      `).join("")}
    </div>
  `;
}

/** 移动端只渲染当前角色的纵向权限列表，避免六角色列产生横向滚动。 */
function renderMobilePermissions(permissions, selectedRole, state, draftCodes, i18n) {
  if (!selectedRole) return `<div class="empty-state">${translate(i18n, "rbac.no_role", "当前没有可用角色。")}</div>`;
  const grouped = groupPermissions(permissions);
  return `
    <div class="role-permission-mobile" role="list" aria-label="${escapeAttribute(translate(i18n, "rbac.role_permissions", "{role} 权限", { role: roleLabel(selectedRole, i18n) }))}">
      ${grouped.map(([category, items], index) => renderMobilePermissionGroup(category, items, selectedRole, state, draftCodes, i18n, index === 0 || state.categoryFilter !== "all")).join("")}
    </div>
  `;
}

/** 桌面默认只展示当前角色，避免首屏同时渲染六列可编辑控件。 */
function renderFocusedRolePermissions(permissions, selectedRole, state, draftCodes, i18n) {
  if (!selectedRole) return `<div class="empty-state">${translate(i18n, "rbac.no_role", "当前没有可用角色。")}</div>`;
  const grouped = groupPermissions(permissions);
  return grouped.map(([category, items], index) => renderFocusedPermissionGroup(
    category,
    items,
    selectedRole,
    state,
    draftCodes,
    i18n,
    index === 0 || state.categoryFilter !== "all",
  )).join("") || `<div class="empty-state">${translate(i18n, "rbac.no_permissions", "当前分组没有权限项。")}</div>`;
}

/** 将权限按后端 category 分组，首屏只展开第一组，其余组通过标题按需打开。 */
function groupPermissions(permissions) {
  const groups = new Map();
  for (const permission of permissions) {
    const category = permission.category ?? "system";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(permission);
  }
  return [...groups.entries()];
}

function categoryLabel(category, i18n) {
  return translate(i18n, CATEGORY_I18N_KEYS[category], CATEGORY_LABELS[category] ?? category);
}

function renderPermissionGroupHeading(category, items, i18n) {
  return `<summary><span>${escapeHtml(categoryLabel(category, i18n))}</span><small>${items.length} 项权限</small></summary>`;
}

function renderFocusedPermissionGroup(category, items, selectedRole, state, draftCodes, i18n, open) {
  return `
    <details class="role-permission-group"${open ? " open" : ""}>
      ${renderPermissionGroupHeading(category, items, i18n)}
      <div class="role-permission-group-items">
        ${items.map((permission) => `
          <div class="role-permission-focus-item">
            ${renderPermissionSummary(permission, "", i18n)}
            ${renderPermissionCell(permission, selectedRole, state, draftCodes, "focus", i18n)}
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

function renderMobilePermissionGroup(category, items, selectedRole, state, draftCodes, i18n, open) {
  return `
    <details class="role-permission-group"${open ? " open" : ""}>
      ${renderPermissionGroupHeading(category, items, i18n)}
      <div class="role-permission-group-items">
        ${items.map((permission) => `
          <div class="role-permission-mobile-item" role="listitem">
            ${renderPermissionSummary(permission, "", i18n)}
            ${renderPermissionCell(permission, selectedRole, state, draftCodes, "mobile", i18n)}
          </div>
        `).join("")}
      </div>
    </details>
  `;
}

/** 渲染权限名称、描述和风险等级；稳定编码只留在数据属性中供交互和测试使用。 */
function renderPermissionSummary(permission, role = "", i18n) {
  return `
    <span class="role-permission-summary"${role ? ` role="${role}"` : ""}>
      <strong>${escapeHtml(permissionLabel(permission, i18n))}</strong>
      <small>${escapeHtml(permissionDescription(permission, i18n))}</small>
      <mark class="permission-risk permission-risk-${escapeAttribute(permission.risk_level)}">
        ${escapeHtml(translate(i18n, `rbac.risk.${permission.risk_level}`, riskLabel(permission.risk_level, i18n)))}
      </mark>
    </span>
  `;
}

/** 渲染一个角色权限单元格；未知或不可授予权限显示锁定且不产生交互节点。 */
function renderPermissionCell(permission, role, state, draftCodes, layout, i18n) {
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
        aria-label="${escapeAttribute(`${roleLabel(role, i18n)} ${permissionLabel(permission, i18n)}`)}"
      />
      <label for="${escapeAttribute(inputId)}" title="${escapeAttribute(disabled ? translate(i18n, "rbac.locked_help", "该权限已锁定或不在角色授权上限内") : translate(i18n, "rbac.toggle_help", "切换权限"))}">
        ${granted ? translate(i18n, "rbac.granted", "已授权") : translate(i18n, "rbac.not_granted", "未授权")}
      </label>
    </span>
  `;
}

/** 渲染 CAS、dirty、冲突和保存命令；最长 hash 使用可换行 code。 */
function renderSaveBar(selectedRole, state, i18n) {
  if (!selectedRole) return "";
  const saveDisabled = !state.dirty || state.busy || state.serverChanged;
  return `
    <div class="role-permission-savebar">
      <div>
        <strong>${escapeHtml(roleLabel(selectedRole, i18n))}</strong>
        <span>${translate(i18n, "rbac.version", "权限目录版本")}${state.dirty ? ` · ${translate(i18n, "rbac.unsaved", "有未保存的修改")}` : ""}</span>
      </div>
      <div class="role-permission-save-actions">
        ${state.serverChanged
          ? `<span class="role-permission-conflict" role="alert">${translate(i18n, "rbac.conflict", "服务器矩阵已更新，请重新加载后再保存。")}</span>`
          : ""}
        <button id="role-permission-discard" type="button" ${!state.dirty && !state.serverChanged ? "disabled" : ""}>${translate(i18n, "rbac.discard", "放弃草稿")}</button>
        <button id="role-permission-save" type="button" ${saveDisabled ? "disabled" : ""}>
          ${state.busy ? translate(i18n, "rbac.saving", "保存中") : translate(i18n, "rbac.save", "保存权限")}
        </button>
      </div>
    </div>
  `;
}

/** Render the actionable failure without leaking transport and correlation metadata. */
function renderError(error, fallback, i18n) {
  return `
    <div class="empty-state error-state role-permission-error" role="alert">
      <strong>${escapeHtml(fallback)}</strong>
      <span>${escapeHtml(error?.message ?? translate(i18n, "common.retry_later", "请稍后重试"))}</span>
    </div>
  `;
}

function translate(i18n, key, fallback, params = {}) {
  const value = i18n?.t?.(key, params, fallback);
  return value == null || value === key ? fallback.replace(/\{([A-Za-z0-9_]+)\}/g, (_, name) => params[name] ?? `{${name}}`) : value;
}

/** 把风险枚举转换为简短展示文案，不参与授权判断。 */
function riskLabel(value, i18n) {
  if (i18n?.locale === "en-US") return { read: "Read", write: "Write", critical: "Critical" }[value] ?? value;
  return { read: "读取", write: "写入", critical: "关键" }[value] ?? value;
}

function permissionLabel(permission, i18n) {
  if (i18n?.locale === "en-US") return EN_PERMISSION_LABELS[permission.code] ?? permission.code;
  return permission.name ?? permission.code;
}

function permissionDescription(permission, i18n) {
  if (i18n?.locale === "en-US") return EN_PERMISSION_LABELS[permission.code] ? `${EN_PERMISSION_LABELS[permission.code]} security scope.` : "Permission scope and security impact.";
  return permission.description ?? permission.name ?? permission.code;
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
