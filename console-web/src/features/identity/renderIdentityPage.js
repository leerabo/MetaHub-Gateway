export function renderIdentityLoginPage(container, options = {}) {
  if (!container) return;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-login-title">
        <header class="identity-brand">
          <span class="identity-brand-mark" aria-hidden="true">M</span>
          <div>
            <p>MetaHub</p>
            <h1 id="identity-login-title">登录账号</h1>
          </div>
        </header>
        <form class="identity-login-form" data-identity-login-form>
          <label>
            <span>邮箱</span>
            <input name="email" type="email" autocomplete="username" inputmode="email" value="${escapeAttribute(options.email ?? "")}" required autofocus />
          </label>
          <label>
            <span>密码</span>
            <input name="password" type="password" autocomplete="current-password" required />
          </label>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit">登录</button>
          <div class="identity-access-actions">
            <button type="button" data-identity-action="show-register">创建账号</button>
            <button type="button" data-identity-action="show-reset">忘记密码</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentityRegistrationPage(container, options = {}) {
  if (!container) return;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-registration-title">
        ${renderAccessBrand("identity-registration-title", "创建账号")}
        <form class="identity-login-form" data-identity-registration-form>
          <label>
            <span>邮箱</span>
            <input name="email" type="email" autocomplete="username" inputmode="email" value="${escapeAttribute(options.email ?? "")}" required autofocus />
          </label>
          <label>
            <span>显示名称</span>
            <input name="displayName" type="text" autocomplete="name" maxlength="120" value="${escapeAttribute(options.displayName ?? "")}" required />
          </label>
          <label>
            <span>密码</span>
            <input name="password" type="password" autocomplete="new-password" minlength="8" required />
          </label>
          <label>
            <span>确认密码</span>
            <input name="confirmPassword" type="password" autocomplete="new-password" minlength="8" required />
          </label>
          <label class="identity-consent">
            <input name="consent" type="checkbox" required />
            <span>同意隐私条款</span>
          </label>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit">继续验证</button>
          <div class="identity-access-actions is-single">
            <button type="button" data-identity-action="show-login">返回登录</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentityVerificationPage(container, options = {}) {
  if (!container) return;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-verification-title">
        ${renderAccessBrand("identity-verification-title", "验证邮箱")}
        <form class="identity-login-form" data-identity-verification-form>
          <label>
            <span>验证码</span>
            <input name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" minlength="6" maxlength="6" required autofocus />
          </label>
          ${renderFeedback(options.feedback)}
          ${renderError(options.error)}
          <button class="identity-primary" type="submit">完成验证</button>
          <div class="identity-access-actions">
            <button type="button" data-identity-action="resend-verification">重新发送</button>
            <button type="button" data-identity-action="show-login">返回登录</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentityResetRequestPage(container, options = {}) {
  if (!container) return;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-reset-request-title">
        ${renderAccessBrand("identity-reset-request-title", "找回密码")}
        <form class="identity-login-form" data-identity-reset-request-form>
          <label>
            <span>邮箱</span>
            <input name="email" type="email" autocomplete="username" inputmode="email" value="${escapeAttribute(options.email ?? "")}" required autofocus />
          </label>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit">发送验证码</button>
          <div class="identity-access-actions is-single">
            <button type="button" data-identity-action="show-login">返回登录</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentityResetConfirmPage(container, options = {}) {
  if (!container) return;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-reset-confirm-title">
        ${renderAccessBrand("identity-reset-confirm-title", "设置新密码")}
        <form class="identity-login-form" data-identity-reset-confirm-form>
          <label>
            <span>验证码</span>
            <input name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" minlength="6" maxlength="6" required autofocus />
          </label>
          <label>
            <span>新密码</span>
            <input name="newPassword" type="password" autocomplete="new-password" minlength="8" required />
          </label>
          <label>
            <span>确认新密码</span>
            <input name="confirmPassword" type="password" autocomplete="new-password" minlength="8" required />
          </label>
          ${renderFeedback(options.feedback)}
          ${renderError(options.error)}
          <button class="identity-primary" type="submit">更新密码</button>
          <div class="identity-access-actions is-single">
            <button type="button" data-identity-action="show-login">返回登录</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentitySecurityPage(container, state) {
  if (!container) return;
  const account = state.account;
  const sessions = Array.isArray(state.sessions) ? state.sessions : [];
  const currentIndex = sessions.findIndex((session) => session.is_current);
  const currentSession = currentIndex >= 0 ? sessions[currentIndex] : state.currentSession;
  const otherSessions = sessions
    .map((session, index) => ({ session, index }))
    .filter(({ session }) => !session.is_current);
  const displayName = account.display_name || account.email;

  container.innerHTML = `
    <div class="identity-security-shell">
      <header class="identity-topbar">
        <a class="identity-wordmark" href="/identity.html" aria-label="MetaHub 账号安全首页">
          <span class="identity-brand-mark" aria-hidden="true">M</span>
          <strong>MetaHub</strong>
        </a>
        ${renderWorkspaceSelector(state)}
        <button class="identity-quiet-command" type="button" data-identity-action="logout">退出登录</button>
      </header>
      <div class="identity-security-main">
        <section class="identity-heading" aria-labelledby="identity-security-title">
          <p>账号安全</p>
          <h1 id="identity-security-title">${escapeHtml(displayName)}</h1>
          <span>${escapeHtml(account.email)}</span>
        </section>
        ${renderFeedback(state.feedback)}
        ${renderError(state.error)}
        <section class="identity-summary" aria-labelledby="identity-summary-title">
          <div class="identity-section-heading">
            <div>
              <p>安全摘要</p>
              <h2 id="identity-summary-title">账号保护状态</h2>
            </div>
          </div>
          <dl class="identity-summary-list">
            <div>
              <dt>账号状态</dt>
              <dd><span class="identity-status-dot is-ok" aria-hidden="true"></span>${accountStatusLabel(account.status)}</dd>
            </div>
            <div>
              <dt>多重验证</dt>
              <dd>${account.mfa_configured ? "已启用" : "尚未启用"}</dd>
            </div>
          </dl>
        </section>
        ${renderMfaSection(state)}
        ${renderWorkspaceSection(state)}
        ${renderMembershipSection(state)}
        <section class="identity-sessions" aria-labelledby="identity-current-session-title">
          <div class="identity-section-heading">
            <div>
              <p>登录设备</p>
              <h2 id="identity-current-session-title">当前设备</h2>
            </div>
          </div>
          ${renderCurrentSession(currentSession, currentIndex)}
          <div class="identity-section-heading identity-other-heading">
            <div>
              <p>其他登录</p>
              <h2>其他设备</h2>
            </div>
            <span>${otherSessions.length > 0 ? `${otherSessions.length} 台` : "无"}</span>
          </div>
          <div class="identity-session-list">
            ${otherSessions.length > 0 ? otherSessions.map(({ session, index }) => renderOtherSession(session, index)).join("") : '<p class="identity-empty">当前没有其他设备登录。</p>'}
          </div>
        </section>
        <section class="identity-password" aria-labelledby="identity-password-title">
          <div class="identity-section-heading">
            <div>
              <p>登录密码</p>
              <h2 id="identity-password-title">修改密码</h2>
            </div>
          </div>
          <form class="identity-password-form" data-identity-password-form>
            <label>
              <span>当前密码</span>
              <input name="currentPassword" type="password" autocomplete="current-password" required />
            </label>
            <label>
              <span>新密码</span>
              <input name="newPassword" type="password" autocomplete="new-password" minlength="8" required />
            </label>
            <label>
              <span>确认新密码</span>
              <input name="confirmPassword" type="password" autocomplete="new-password" minlength="8" required />
            </label>
            <div class="identity-password-actions">
              <button class="identity-primary" type="submit">更新密码</button>
            </div>
          </form>
        </section>
      </div>
    </div>
  `;
}

function renderMfaSection(state) {
  if (state.account.mfa_configured) {
    return `
      <section class="identity-mfa" aria-labelledby="identity-mfa-title">
        <div class="identity-section-heading">
          <div>
            <p>多重验证</p>
            <h2 id="identity-mfa-title">动态验证已启用</h2>
          </div>
          ${state.platformAccess ? '<a class="identity-primary identity-platform-link" href="/platform.html">进入平台工作台</a>' : ""}
        </div>
      </section>`;
  }
  const enrollment = state.mfaEnrollment;
  if (enrollment) {
    return `
      <section class="identity-mfa" aria-labelledby="identity-mfa-title">
        <div class="identity-section-heading">
          <div>
            <p>多重验证</p>
            <h2 id="identity-mfa-title">激活动态验证</h2>
          </div>
        </div>
        <div class="identity-mfa-enrollment">
          <div>
            <span>认证器密钥</span>
            <code>${escapeHtml(enrollment.secret)}</code>
            <a href="${escapeAttribute(enrollment.provisioning_uri)}">在认证器中打开</a>
          </div>
          <form data-identity-mfa-verification-form autocomplete="off">
            <label>
              <span>动态验证码</span>
              <input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required />
            </label>
            <button class="identity-primary" type="submit">验证并启用</button>
          </form>
        </div>
      </section>`;
  }
  return `
    <section class="identity-mfa" aria-labelledby="identity-mfa-title">
      <div class="identity-section-heading">
        <div>
          <p>多重验证</p>
          <h2 id="identity-mfa-title">启用动态验证</h2>
        </div>
      </div>
      <form class="identity-mfa-start" data-identity-mfa-enrollment-form autocomplete="off">
        <label>
          <span>当前密码</span>
          <input name="currentPassword" type="password" autocomplete="current-password" required />
        </label>
        <button class="identity-primary" type="submit">创建认证器密钥</button>
      </form>
    </section>`;
}

function renderWorkspaceSelector(state) {
  const choices = Array.isArray(state.workspaceChoices) ? state.workspaceChoices : [];
  const context = state.workspaceContext ?? null;
  if (choices.length === 0) {
    return '<div class="identity-workspace-selector is-empty"><span>空间</span><strong>尚未进入</strong></div>';
  }
  const current = context
    ? choices.find((choice) => choice.workspace_id === context.workspace_id) ?? context
    : null;
  if (choices.length === 1) {
    const choice = choices[0];
    return `
      <div class="identity-workspace-selector">
        <span>空间</span>
        ${
          current
            ? `<strong>${escapeHtml(current.name)}</strong><small>${escapeHtml(roleLabel(context.role))}</small>`
            : `<button type="button" data-identity-action="switch-workspace" data-workspace-index="0">进入 ${escapeHtml(choice.name)}</button>`
        }
      </div>
    `;
  }
  return `
    <details class="identity-workspace-selector">
      <summary>
        <span>空间</span>
        <strong>${escapeHtml(current?.name ?? "选择空间")}</strong>
        ${current ? `<small>${escapeHtml(roleLabel(context.role))}</small>` : ""}
      </summary>
      <div class="identity-workspace-popover">
        <label>
          <span class="identity-visually-hidden">搜索空间</span>
          <input type="search" autocomplete="off" placeholder="搜索空间" data-workspace-search />
        </label>
        <div class="identity-workspace-options">
          ${choices.map((choice, index) => renderWorkspaceOption(choice, index, context)).join("")}
        </div>
      </div>
    </details>
  `;
}

function renderWorkspaceOption(choice, index, context) {
  const active = context?.workspace_id === choice.workspace_id;
  const machine = choice.role === "service_account";
  const searchText = `${choice.name} ${choice.slug ?? ""}`;
  return `
    <button
      type="button"
      data-workspace-option
      data-workspace-search-text="${escapeAttribute(searchText)}"
      data-identity-action="switch-workspace"
      data-workspace-index="${index}"
      ${active || machine ? "disabled" : ""}
    >
      <span>${escapeHtml(choice.name)}</span>
      <small>${machine ? "机器主体" : escapeHtml(roleLabel(choice.role))}${active ? " · 当前" : ""}</small>
    </button>
  `;
}

function renderMembershipSection(state) {
  const context = state.workspaceContext ?? null;
  const members = Array.isArray(state.memberships) ? state.memberships : [];
  const navigationCount = Array.isArray(state.navigationItems)
    ? state.navigationItems.filter((item) => item.visible !== false && item.disabled !== true).length
    : 0;
  return `
    <section class="identity-memberships" aria-labelledby="identity-memberships-title">
      <div class="identity-section-heading">
        <div>
          <p>成员与权限</p>
          <h2 id="identity-memberships-title">当前空间成员</h2>
        </div>
        <button class="identity-quiet-command" type="button" data-identity-action="refresh-workspace-context">刷新</button>
      </div>
      ${
        context
          ? `
            <div class="identity-context-summary">
              <div><span>当前职责</span><strong>${escapeHtml(roleLabel(context.role))}</strong></div>
              <div><span>可访问功能</span><strong>${navigationCount} 项</strong></div>
              <div><span>会话有效至</span><strong>${escapeHtml(formatDateTime(context.expires_at))}</strong></div>
            </div>
            ${renderMembershipList(members)}
            ${renderInvitationForm(state)}
            ${renderOwnerTransferForm(context, members)}
          `
          : '<p class="identity-empty">选择一个空间后可查看成员与职责。</p>'
      }
    </section>
  `;
}

function renderMembershipList(members) {
  if (members.length === 0) {
    return '<p class="identity-empty">当前职责没有可查看的成员记录。</p>';
  }
  return `<div class="identity-membership-list">${members
    .map((member, index) => renderMembershipRow(member, index))
    .join("")}</div>`;
}

function renderMembershipRow(member, index) {
  const machine = member.role === "service_account";
  const identityLabel = machine ? "机器主体" : member.is_current ? "当前账号" : "成员";
  if (!member.can_manage || machine || member.role === "owner") {
    return `
      <article class="identity-membership-row">
        <div class="identity-member-mark" aria-hidden="true">${machine ? "S" : "U"}</div>
        <div class="identity-member-name">
          <strong>${escapeHtml(member.display_name)}</strong>
          <span>${identityLabel}</span>
        </div>
        <div class="identity-member-facts">
          <span>${escapeHtml(roleLabel(member.role))}</span>
          <span>${escapeHtml(statusLabel(member.status))}</span>
        </div>
      </article>
    `;
  }
  return `
    <form class="identity-membership-row is-editable" data-membership-update-form data-membership-index="${index}">
      <div class="identity-member-mark" aria-hidden="true">U</div>
      <div class="identity-member-name">
        <strong>${escapeHtml(member.display_name)}</strong>
        <span>${identityLabel}</span>
      </div>
      <label>
        <span>职责</span>
        <select name="role" aria-label="${escapeAttribute(`${member.display_name} 职责`)}">
          ${renderRoleOptions(member.role)}
        </select>
      </label>
      <label>
        <span>状态</span>
        <select name="status" aria-label="${escapeAttribute(`${member.display_name} 状态`)}">
          ${renderOption("active", "启用", member.status)}
          ${renderOption("disabled", "停用", member.status)}
        </select>
      </label>
      <button class="identity-quiet-command" type="submit">保存</button>
    </form>
  `;
}

function renderInvitationForm(state) {
  const context = state.workspaceContext;
  if (!new Set(["owner", "admin"]).has(context?.role)) return "";
  const result = state.invitationResult;
  return `
    <div class="identity-membership-action">
      <div class="identity-section-heading">
        <div><p>邀请</p><h3>添加成员</h3></div>
      </div>
      <form class="identity-membership-form" data-membership-invitation-form>
        <label>
          <span>邮箱</span>
          <input name="email" type="email" autocomplete="email" maxlength="320" required />
        </label>
        <label>
          <span>职责</span>
          <select name="role" required>${renderRoleOptions("developer")}</select>
        </label>
        <label>
          <span>有效期</span>
          <select name="expiresInHours" required>
            ${renderOption("24", "24 小时", "72")}
            ${renderOption("72", "72 小时", "72")}
            ${renderOption("168", "7 天", "72")}
          </select>
        </label>
        <button class="identity-primary" type="submit">生成邀请</button>
      </form>
      ${
        result && state.hasInvitationSecret
          ? `
            <div class="identity-invitation-result" role="status">
              <div><strong>${escapeHtml(result.email)}</strong><span>${escapeHtml(roleLabel(result.role))} · ${escapeHtml(formatDateTime(result.expiresAt))}</span></div>
              <button type="button" class="identity-quiet-command" data-identity-action="copy-invitation-link">复制链接</button>
              <button type="button" class="identity-quiet-command" data-identity-action="copy-invitation-mail">复制文案</button>
            </div>
          `
          : ""
      }
    </div>
  `;
}

function renderOwnerTransferForm(context, members) {
  if (context?.role !== "owner") return "";
  const eligible = members
    .map((member, index) => ({ member, index }))
    .filter(({ member }) =>
      member.status === "active" && !new Set(["owner", "service_account"]).has(member.role),
    );
  if (eligible.length === 0) return "";
  return `
    <div class="identity-membership-action is-owner-transfer">
      <div class="identity-section-heading">
        <div><p>所有权</p><h3>转移空间所有权</h3></div>
      </div>
      <form class="identity-membership-form" data-owner-transfer-form>
        <label>
          <span>新 Owner</span>
          <select name="targetIndex" required>
            ${eligible
              .map(({ member, index }) => `<option value="${index}">${escapeHtml(member.display_name)} · ${escapeHtml(roleLabel(member.role))}</option>`)
              .join("")}
          </select>
        </label>
        <label>
          <span>当前密码</span>
          <input name="currentPassword" type="password" autocomplete="current-password" required />
        </label>
        <label class="identity-owner-transfer-confirmation">
          <input name="confirmTransfer" type="checkbox" required />
          <span>我确认将空间所有权转移给所选成员；转移后当前账号将失去 Owner 权限。</span>
        </label>
        <button class="identity-danger-command" type="submit">确认转移</button>
      </form>
    </div>
  `;
}

function renderRoleOptions(selectedRole) {
  return [
    ["admin", "管理员"],
    ["developer", "开发者"],
    ["security_admin", "安全管理员"],
    ["finance_admin", "财务管理员"],
    ["legal_admin", "法务管理员"],
    ["auditor", "审计员"],
    ["viewer", "只读成员"],
  ]
    .map(([value, label]) => renderOption(value, label, selectedRole))
    .join("");
}

function roleLabel(role) {
  const labels = {
    owner: "Owner",
    admin: "管理员",
    developer: "开发者",
    security_admin: "安全管理员",
    finance_admin: "财务管理员",
    legal_admin: "法务管理员",
    auditor: "审计员",
    viewer: "只读成员",
    service_account: "服务账号",
  };
  return labels[role] ?? "成员";
}

function statusLabel(status) {
  return { active: "启用", disabled: "停用", pending: "待激活" }[status] ?? "未知";
}

function renderWorkspaceSection(state) {
  const workspaces = Array.isArray(state.workspaces) ? state.workspaces : [];
  const creating = state.workspaceView === "create";
  return `
    <section class="identity-workspaces" aria-labelledby="identity-workspaces-title">
      <div class="identity-section-heading">
        <div>
          <p>空间</p>
          <h2 id="identity-workspaces-title">我的空间</h2>
        </div>
        ${
          creating
            ? ""
            : '<button class="identity-quiet-command" type="button" data-identity-action="show-workspace-create"><span aria-hidden="true">+</span> 创建空间</button>'
        }
      </div>
      ${creating ? renderWorkspaceCreateForm(state.workspaceDraft) : renderWorkspaceList(workspaces)}
    </section>
  `;
}

function renderWorkspaceList(workspaces) {
  if (workspaces.length === 0) {
    return '<p class="identity-empty">尚未创建空间。</p>';
  }
  return `<div class="identity-workspace-list">${workspaces
    .map(
      (workspace) => `
        <article class="identity-workspace-row">
          <div class="identity-workspace-mark" aria-hidden="true">${workspace.kind === "personal" ? "P" : "O"}</div>
          <div>
            <strong>${escapeHtml(workspace.name)}</strong>
            <span>${workspace.kind === "personal" ? "个人空间" : "企业空间"}${workspace.slug ? ` · ${escapeHtml(workspace.slug)}` : ""}</span>
          </div>
          <dl>
            <div><dt>Region</dt><dd>${escapeHtml(workspace.region)}</dd></div>
            <div><dt>环境</dt><dd>${workspace.environment?.code === "dev" ? "dev" : "不可用"}</dd></div>
            <div><dt>状态</dt><dd>${workspaceStatusLabel(workspace)}</dd></div>
          </dl>
        </article>
      `,
    )
    .join("")}</div>`;
}

function renderWorkspaceCreateForm(draft = {}) {
  const kind = draft?.kind === "personal" ? "personal" : "organization";
  return `
    <form class="identity-workspace-form" data-workspace-create-form>
      <input type="hidden" name="kind" value="${kind}" />
      <div class="identity-workspace-kind" role="group" aria-label="空间类型">
        <button type="button" data-identity-action="set-workspace-kind" data-workspace-kind="personal" aria-pressed="${kind === "personal"}">个人</button>
        <button type="button" data-identity-action="set-workspace-kind" data-workspace-kind="organization" aria-pressed="${kind === "organization"}">企业</button>
      </div>
      ${
        kind === "organization"
          ? `
            <label>
              <span>企业名称</span>
              <input name="name" type="text" maxlength="160" autocomplete="organization" value="${escapeAttribute(draft?.name ?? "")}" required autofocus />
            </label>
            <label>
              <span>空间标识</span>
              <input name="slug" type="text" minlength="3" maxlength="48" pattern="[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])" value="${escapeAttribute(draft?.slug ?? "")}" required />
            </label>
          `
          : ""
      }
      <label>
        <span>Region</span>
        <select name="region" required>
          ${renderOption("cn-north-1", "华北", draft?.region)}
          ${renderOption("cn-east-1", "华东", draft?.region)}
        </select>
      </label>
      <label>
        <span>服务模式</span>
        <select name="serviceMode" required>
          ${renderOption("internal_only", "内部服务", draft?.serviceMode)}
          ${renderOption("public_service", "公共服务", draft?.serviceMode)}
        </select>
      </label>
      <div class="identity-workspace-actions">
        <button class="identity-quiet-command" type="button" data-identity-action="cancel-workspace-create">取消</button>
        <button class="identity-primary" type="submit">创建</button>
      </div>
    </form>
  `;
}

function renderOption(value, label, selectedValue) {
  return `<option value="${value}"${selectedValue === value ? " selected" : ""}>${label}</option>`;
}

function workspaceStatusLabel(workspace) {
  if (workspace.status === "migration_conflict" || workspace.provisioning?.status === "failed") {
    return "需要处理";
  }
  if (workspace.profile_state === "draft") return "资料待确认";
  return "可用";
}

function renderCurrentSession(session, index) {
  if (!session) {
    return '<p class="identity-empty">当前设备信息暂时不可用。</p>';
  }
  return `
    <article class="identity-current-session">
      <div class="identity-device-mark" aria-hidden="true"></div>
      <div>
        <strong>${escapeHtml(deviceLabel(session.device_family))}</strong>
        <span>本机 · 最近使用 ${escapeHtml(formatDateTime(session.last_seen_at))}</span>
        <small>登录于 ${escapeHtml(formatDateTime(session.created_at))}，有效至 ${escapeHtml(formatDateTime(session.expires_at))}</small>
      </div>
      <button class="identity-danger-command" type="button" data-identity-action="revoke-session" data-session-index="${index}">退出此设备</button>
    </article>
  `;
}

function renderOtherSession(session, index) {
  return `
    <article class="identity-session-row">
      <div class="identity-device-mark" aria-hidden="true"></div>
      <div>
        <strong>${escapeHtml(deviceLabel(session.device_family))}</strong>
        <span>最近使用 ${escapeHtml(formatDateTime(session.last_seen_at))}</span>
        <small>有效至 ${escapeHtml(formatDateTime(session.expires_at))}</small>
      </div>
      <button class="identity-danger-command" type="button" data-identity-action="revoke-session" data-session-index="${index}">撤销</button>
    </article>
  `;
}

function renderFeedback(feedback) {
  return feedback ? `<p class="identity-feedback" role="status">${escapeHtml(feedback)}</p>` : "";
}

function renderAccessBrand(titleId, title) {
  return `
    <header class="identity-brand">
      <span class="identity-brand-mark" aria-hidden="true">M</span>
      <div>
        <p>MetaHub</p>
        <h1 id="${escapeAttribute(titleId)}">${escapeHtml(title)}</h1>
      </div>
    </header>
  `;
}

function renderError(error) {
  if (!error) return "";
  return `<div class="identity-error" role="alert"><span>${escapeHtml(error.message ?? error)}</span>${error.requestId && error.requestId !== "-" ? `<small>request_id: ${escapeHtml(error.requestId)}</small>` : ""}</div>`;
}

function accountStatusLabel(status) {
  return status === "active" ? "正常" : "需要重新登录";
}

function deviceLabel(value) {
  const labels = {
    chrome: "Chrome 浏览器",
    edge: "Edge 浏览器",
    firefox: "Firefox 浏览器",
    safari: "Safari 浏览器",
    mobile: "移动设备",
    unknown: "浏览器设备",
  };
  const normalized = String(value ?? "unknown").toLowerCase();
  return labels[normalized] ?? "浏览器设备";
}

function formatDateTime(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
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
  return escapeHtml(value).replaceAll("`", "&#096;");
}
