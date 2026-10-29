import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

/**
 * 渲染当前租户信息面板。
 *
 * 所有展示数据来自登录 Session 对应 tenant_id；只读用户可以查看，写入动作仍由后端
 * 校验 admin 权限。
 */
export function renderTenantPanel(tenantState, authContext, workspaceContext) {
  if (workspaceContext) return renderWorkspaceProfile(tenantState, authContext, workspaceContext);
  // 未登录时保留登录占位；已登录但授权快照缺失或失权时必须清空 DOM，不能伪装成认证等待态。
  if (!authContext?.user) {
    return `
      <div class="panel-heading">
        <h2 id="tenant-title">租户信息</h2>
        <p>登录后查看当前租户。</p>
      </div>
      <div class="empty-state">等待登录。</div>
    `;
  }
  // 已认证但缺少后端 tenant.settings.read 时必须零 DOM，不能按 admin 角色名回补入口。
  if (!hasPermission(authContext, PERMISSIONS.TENANT_SETTINGS_READ)) return "";
  // 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
  if (!tenantState) {
    return `
      <div class="panel-heading">
        <h2 id="tenant-title">租户信息</h2>
        <p>正在读取当前租户。</p>
      </div>
      <div class="empty-state">等待 /tenant/current 响应。</div>
    `;
  }
  // 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
  if (tenantState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="tenant-title">租户信息</h2>
        <p>当前租户暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(tenantState.message)}
      </div>
    `;
  }

  const tenant = tenantState.tenant ?? {};
  // v1 租户设置仅授予管理员，页面与后端均消费同一稳定权限码，不再按角色名分叉。
  const canEdit = hasPermission(authContext, PERMISSIONS.TENANT_SETTINGS_READ);
  return `
    <div class="panel-heading tenant-heading">
      <div>
        <h2 id="tenant-title">租户信息</h2>
        <p>所有读写都基于当前登录 Session 的 tenant_id。</p>
      </div>
      <span class="tenant-status status-${escapeHtml(tenant.status ?? "unknown")}">${escapeHtml(tenant.status ?? "-")}</span>
    </div>
    <form class="tenant-form" id="tenant-form">
      <div class="tenant-meta">
        ${renderMeta("租户标识", tenant.slug)}
        ${renderMeta("租户 ID", tenant.tenant_id)}
        ${renderMeta("当前管理员", authContext.user.email)}
        ${renderMeta("更新时间", tenant.updated_at)}
      </div>
      <label>
        <span>租户名称</span>
        <input name="tenantName" value="${escapeAttribute(tenant.name ?? "")}" maxlength="160" ${canEdit ? "" : "disabled"} />
      </label>
      <label>
        <span>服务模式</span>
        <select name="serviceMode" ${canEdit ? "" : "disabled"}>
          <option value="internal_only" ${tenant.service_mode === "internal_only" ? "selected" : ""}>internal_only</option>
          <option value="public_service" ${tenant.service_mode === "public_service" ? "selected" : ""}>public_service</option>
        </select>
      </label>
      <div class="tenant-actions">
        <button type="submit" ${canEdit ? "" : "disabled"}>保存租户信息</button>
        <span class="tenant-feedback" role="status">${canEdit ? "" : "当前角色只能查看租户信息"}</span>
      </div>
    </form>
  `;
}

function renderWorkspaceProfile(state, authContext, context) {
  if (!hasPermission(authContext, PERMISSIONS.TENANT_SETTINGS_READ)) return "";
  const title = "组织资料";
  const heading = `<div class="panel-heading tenant-heading"><h2 id="tenant-title">${title}</h2></div>`;
  if (state?.state === "error") {
    return `${heading}<p class="error-state" role="alert">${escapeHtml(state.message ?? "组织资料暂时无法读取，请稍后重试")}</p>`;
  }
  if (!state?.workspace) return `${heading}<p role="status">正在读取资料。</p>`;
  const workspace = state.workspace;
  const canEdit = context.role === "owner" && workspace.status === "active";
  return `${heading}
    <form class="tenant-form" id="workspace-profile-form">
      <label><span>名称</span><input name="workspaceName" value="${escapeAttribute(workspace.name)}" maxlength="160" required ${canEdit ? "" : "disabled"} /></label>
      <div class="tenant-actions">
        ${canEdit ? '<button type="submit">保存资料</button>' : ""}
        <span class="tenant-feedback" role="status">${canEdit ? "" : "仅组织所有者可修改资料"}</span>
      </div>
    </form>
    <details class="workspace-system-details">
      <summary>系统信息（只读）</summary>
      <dl class="tenant-meta">
        ${renderMeta("部署区域", workspace.region)}
        ${renderMeta("服务范围", workspace.service_mode === "internal_only" ? "组织内部" : "对外服务")}
        ${renderMeta("默认环境", workspace.environment?.code ?? "-")}
      </dl>
    </details>`;
}

/**
 * 渲染租户元数据键值对。
 *
 * 统一函数保证所有标签和值都经过 HTML 转义。
 */
function renderMeta(label, value) {
  return `
    <div>
      <dt>${escapeHtml(label)}</dt>
      <dd title="${escapeAttribute(value ?? "-")}">${escapeHtml(value ?? "-")}</dd>
    </div>
  `;
}

/**
 * HTML 文本转义。
 *
 * 租户名称和服务模式来自数据库或表单输入，渲染时必须按文本处理。
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
 * HTML 属性转义。
 *
 * value 属性需要额外处理反引号，防止租户名称影响表单结构。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
