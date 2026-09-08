import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 用户角色标签用于控制台展示，真实角色变更必须走后端权限和最后管理员保护。
const ROLE_LABELS = {
  admin: "管理员",
  developer: "开发者",
  viewer: "只读用户",
  finance_admin: "财务管理员",
  security_admin: "安全管理员",
  ops_admin: "运维管理员",
};
const WORKSPACE_ROLE_LABELS = { owner: "所有者", admin: "管理员", developer: "开发者",
  security_admin: "安全管理员", finance_admin: "财务管理员", legal_admin: "法务管理员",
  auditor: "审计员", viewer: "只读成员", service_account: "机器主体" };
const MEMBERSHIP_STATUS_LABELS = { pending: "待加入", active: "启用", disabled: "停用" };

/**
 * 渲染租户内用户管理面板。
 *
 * admin 才能看到该面板；这只是前端入口控制，后端用户 API 仍会校验管理员权限。
 */
export function renderUserManagementPanel(usersState, authContext, invitationState = {}) {
  if (!hasPermission(authContext, PERMISSIONS.USER_MANAGE)) {
    return "";
  }
  if (usersState?.kind === "workspace") return renderWorkspaceMembers(usersState);
  if (!usersState) {
    return `
      <div class="panel-heading">
        <h2 id="users-title">用户管理</h2>
        <p>正在读取当前租户用户和角色目录。</p>
      </div>
      <div class="empty-state">等待 /users 响应。</div>
    `;
  }
  // 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
  if (usersState.state === "error") {
    return `
      <div class="panel-heading">
        <h2 id="users-title">用户管理</h2>
        <p>用户管理暂不可用。</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(usersState.message)} request_id: ${escapeHtml(usersState.requestId ?? "-")}
      </div>
    `;
  }

  const roles = usersState.roles ?? [];
  const users = usersState.users ?? [];
  return `
    <div class="panel-heading users-heading">
      <div>
        <h2 id="users-title">用户管理</h2>
        <p>当前租户内本地账号。角色变更由后端 RBAC 校验。</p>
      </div>
      <div class="users-heading-actions">
        <span class="users-count">${users.length} 个用户</span>
        <button type="button" class="invite-user-button" id="invite-user-button">邀请成员</button>
      </div>
    </div>
    <div class="users-grid" role="table" aria-label="当前租户用户">
      <div class="users-row users-head" role="row">
        <span>用户</span>
        <span>角色</span>
        <span>状态</span>
        <span>操作</span>
      </div>
      ${users.map((user) => renderUserRow(user, roles)).join("")}
    </div>
    <form class="create-user-form" id="create-user-form">
      <h3>新增用户</h3>
      <label>
        <span>邮箱</span>
        <input name="email" type="email" maxlength="255" required />
      </label>
      <label>
        <span>显示名</span>
        <input name="displayName" maxlength="160" required />
      </label>
      <label>
        <span>初始密码</span>
        <input name="password" type="password" minlength="8" maxlength="256" required />
      </label>
      <label>
        <span>角色</span>
        <select name="role">
          ${roles.map((role) => renderRoleOption(role, "developer")).join("")}
        </select>
      </label>
      <div class="users-actions">
        <button type="submit">创建用户</button>
        <span class="users-feedback" role="status"></span>
      </div>
    </form>
    ${renderInvitationDialog(roles, invitationState)}
  `;
}

function renderWorkspaceMembers(state) {
  const members = state.memberships ?? [];
  const error = state.actionError ?? (state.state === "error" ? state : null);
  const offset = state.offset ?? 0, limit = state.limit ?? 20, total = state.total ?? 0;
  return `<div class="panel-heading users-heading">
    <h2 id="users-title">空间成员</h2>
    <div class="users-heading-actions"><span class="users-count">${total} 位成员</span>
      <a href="identity.html#identity-memberships-title">邀请成员${state.role === "owner" ? "与所有权管理" : ""}</a>
    </div></div>
    ${error ? `<p class="error-state" role="alert">${escapeHtml(error.message ?? "成员管理暂不可用")}
      request_id: ${escapeHtml(error.requestId ?? "-")}</p>` : ""}
    ${state.feedback ? `<p role="status">${escapeHtml(state.feedback)}</p>` : ""}
    <div class="users-grid workspace-members-grid" role="table" aria-label="当前空间成员">
      <div class="users-row users-head" role="row"><span>成员</span><span>职责</span><span>状态</span><span>操作</span></div>
      ${members.map(renderWorkspaceMemberRow).join("")}
    </div>
    ${members.length === 0 && !error ? '<p class="empty-state">暂无成员记录</p>' : ""}
    <div class="users-heading-actions workspace-members-pagination">
      <button type="button" data-workspace-members-refresh>刷新</button>
      <button type="button" data-workspace-members-offset="${Math.max(0, offset - limit)}"
        aria-label="上一页" title="上一页"${offset === 0 ? " disabled" : ""}>&#8592;</button>
      <span>${members.length ? offset + 1 : 0}-${members.length ? Math.min(offset + members.length, total) : 0} / ${total}</span>
      <button type="button" data-workspace-members-offset="${offset + limit}"
        aria-label="下一页" title="下一页"${offset + limit >= total ? " disabled" : ""}>&#8594;</button>
    </div>`;
}

function renderWorkspaceMemberRow(member) {
  const editable = member.can_manage && !["owner", "service_account"].includes(member.role)
    && ["active", "disabled"].includes(member.status);
  const identity = `<span class="user-identity"><strong>${escapeHtml(member.display_name)}</strong>
    ${member.is_current ? "<small>当前账号</small>" : ""}</span>`;
  if (!editable) return `<div class="users-row" role="row">${identity}
    <span>${escapeHtml(WORKSPACE_ROLE_LABELS[member.role] ?? member.role)}</span>
    <span>${escapeHtml(MEMBERSHIP_STATUS_LABELS[member.status] ?? member.status)}</span><span>-</span></div>`;
  const roles = Object.entries(WORKSPACE_ROLE_LABELS).filter(([code]) => !["owner", "service_account"].includes(code));
  return `<form class="users-row" role="row" data-workspace-member-form
    data-membership-id="${escapeAttribute(member.membership_id)}" data-membership-revision="${member.revision}"
    data-membership-role="${escapeAttribute(member.role)}" data-membership-status="${escapeAttribute(member.status)}">
    ${identity}<span><select name="role" aria-label="${escapeAttribute(member.display_name)} 职责">
      ${roles.map(([code, name]) => renderRoleOption({ code, name }, member.role)).join("")}</select></span>
    <span><select name="status" aria-label="${escapeAttribute(member.display_name)} 状态">
      ${["active", "disabled"].map((code) => renderRoleOption({ code, name: MEMBERSHIP_STATUS_LABELS[code] }, member.status)).join("")}
    </select></span><span class="users-row-actions"><button type="submit" disabled>保存</button></span></form>`;
}

/**
 * 渲染 PAGE-087 原生 dialog。
 *
 * 安全快照只控制反馈和按钮可见性；原始 Token、接受链接及邮件正文只由事件处理器闭包提供。
 */
function renderInvitationDialog(roles, invitationState) {
  const feedback = invitationState.feedback;
  const result = invitationState.invitation;
  const resultHidden = invitationState.hasSecret ? "" : " hidden";
  return `
    <dialog class="invitation-dialog" id="invitation-dialog" aria-labelledby="invitation-dialog-title">
      <div class="invitation-dialog-heading">
        <div>
          <span class="invitation-dialog-kicker">团队邀请</span>
          <h3 id="invitation-dialog-title">邀请用户</h3>
        </div>
        <button type="button" class="invitation-dialog-close" data-invitation-close aria-label="关闭邀请弹窗"${invitationState.busy ? " disabled aria-disabled=\"true\"" : ""}>×</button>
      </div>
      <form class="invitation-form" id="invitation-form">
        <label>
          <span>邮箱</span>
          <input name="email" type="email" maxlength="255" autocomplete="email" required />
        </label>
        <label>
          <span>角色</span>
          <select name="role">
            ${roles.map((role) => renderRoleOption(role, "developer")).join("")}
          </select>
        </label>
        <label>
          <span>有效期（小时）</span>
          <select name="expiresInHours" required>
            <option value="24">24 小时</option>
            <option value="72" selected>72 小时</option>
            <option value="168">168 小时</option>
          </select>
        </label>
        <button type="submit" class="invitation-create-button"${invitationState.busy ? " disabled aria-busy=\"true\"" : ""}>
          ${invitationState.busy ? "正在生成" : "生成邀请"}
        </button>
      </form>
      <p class="invitation-feedback${feedback?.state === "error" ? " error-state" : ""}" role="status">
        ${feedback ? `${escapeHtml(feedback.message)} · request_id: ${escapeHtml(feedback.requestId)}` : ""}
      </p>
      <div class="invitation-created-result"${resultHidden}>
        <dl>
          <div><dt>邮箱</dt><dd data-invitation-result-email>${escapeHtml(result?.email ?? "-")}</dd></div>
          <div><dt>角色</dt><dd data-invitation-result-role>${escapeHtml(ROLE_LABELS[result?.role] ?? result?.role ?? "-")}</dd></div>
          <div><dt>过期时间</dt><dd data-invitation-result-expires>${escapeHtml(result?.expiresAt ?? "-")}</dd></div>
        </dl>
        <div class="invitation-copy-actions">
          <button type="button" data-copy-invitation-link>复制邀请链接</button>
          <button type="button" data-copy-invitation-mail>复制邮件文案</button>
          <button type="button" data-manual-invitation-link hidden>手动复制链接</button>
          <button type="button" data-manual-invitation-mail hidden>手动复制邮件文案</button>
        </div>
        <div class="invitation-manual-copy" data-invitation-manual-copy hidden></div>
      </div>
    </dialog>
  `;
}

/**
 * 渲染单个用户行。
 *
 * 角色下拉只提交 role 字段，避免页面夹带 tenant_id/status 等敏感边界字段。
 */
function renderUserRow(user, roles) {
  return `
    <form class="users-row user-role-form" data-user-role-form data-user-id="${escapeAttribute(user.user_id)}" role="row">
      <span class="user-identity">
        <strong>${escapeHtml(user.display_name)}</strong>
        <em>${escapeHtml(user.email)}</em>
      </span>
      <span>
        <select name="role" aria-label="${escapeAttribute(`${user.email} 角色`)}">
          ${roles.map((role) => renderRoleOption(role, user.role)).join("")}
        </select>
      </span>
      <span><mark class="status-mark status-${escapeHtml(user.status)}">${escapeHtml(user.status)}</mark></span>
      <span class="users-row-actions">
        <button type="submit">保存角色</button>
        <small>${escapeHtml(ROLE_LABELS[user.role] ?? user.role)}</small>
      </span>
    </form>
  `;
}

/**
 * 渲染角色 option。
 *
 * label 使用本地映射，value 保持后端角色枚举，避免提交中文文案。
 */
function renderRoleOption(role, selectedRole) {
  const selected = role.code === selectedRole ? " selected" : "";
  return `<option value="${escapeAttribute(role.code)}"${selected}>${escapeHtml(role.name || role.code)}</option>`;
}

/**
 * HTML 文本转义。
 *
 * 用户邮箱、显示名和角色都来自数据库或表单，渲染前必须转义。
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
 * data-user-id 和表单 value 额外处理反引号，保护属性边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
