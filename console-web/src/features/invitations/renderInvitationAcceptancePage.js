/** 渲染认证 bootstrap 之前的公开邀请接受页，所有输入和响应都不含原始 Token。 */

const ROLE_LABELS = {
  admin: "管理员",
  developer: "开发者",
  viewer: "只读用户",
  finance_admin: "财务管理员",
  security_admin: "安全管理员",
  ops_admin: "运维管理员",
};

/** 根据公开接受状态渲染表单、错误或成功结果。 */
export function renderInvitationAcceptancePage(state = {}) {
  const status = state.status ?? "form";
  const feedback = state.message
    ? `<p class="invitation-public-feedback ${status === "error" ? "error-state" : ""}" role="status">${escapeHtml(state.message)}${state.requestId ? ` · request_id: ${escapeHtml(state.requestId)}` : ""}</p>`
    : '<p class="invitation-public-feedback" role="status"></p>';
  if (status === "success") {
    return pageShell(`
      <div class="invitation-public-result" role="status">
        <span class="invitation-result-kicker">邀请已接受</span>
        <h1>已加入组织</h1>
        ${feedback}
        <dl>
          <div><dt>账号</dt><dd>${escapeHtml(state.user?.email ?? "-")}</dd></div>
          <div><dt>角色</dt><dd>${escapeHtml(ROLE_LABELS[state.user?.role] ?? state.user?.role ?? "-")}</dd></div>
        </dl>
        <button type="button" class="invitation-login-button" data-invitation-login>前往登录</button>
      </div>
    `);
  }
  if (status === "invalid") {
    return pageShell(`
      <div class="invitation-public-result error-state" role="alert">
        <span class="invitation-result-kicker">邀请不可用</span>
        <h1>链接已失效</h1>
        ${feedback}
        <button type="button" class="invitation-login-button" data-invitation-login>前往登录</button>
      </div>
    `);
  }
  if (status === "loading") return pageShell('<h1>接受邀请</h1><p role="status">正在读取邀请...</p>');
  if (status === "preview-error") return pageShell(`<h1>邀请暂不可用</h1>${feedback}
    <button type="button" data-invitation-preview-retry>重试</button><a href="./">返回首页</a>`);
  const submitting = status === "submitting";
  return pageShell(`
    <div class="invitation-public-heading">
      <span class="invitation-result-kicker">后台成员邀请</span>
      <h1>接受邀请</h1>
      ${state.invitation ? `<dl class="invitation-recipient">
        <div><dt>受邀邮箱</dt><dd>${escapeHtml(state.invitation.email)}</dd></div>
        <div><dt>组织</dt><dd>${escapeHtml(state.invitation.workspace_name)}</dd></div>
        <div><dt>职责</dt><dd>${escapeHtml(ROLE_LABELS[state.invitation.role] ?? state.invitation.role)}</dd></div>
      </dl>` : ""}
    </div>
    <form class="invitation-accept-form" id="invitation-accept-form">
      <label>
        <span>显示名</span>
        <input name="displayName" maxlength="160" autocomplete="name" required ${submitting ? "disabled" : ""} />
      </label>
      <label>
        <span>密码</span>
        <input name="password" type="password" minlength="8" maxlength="256" autocomplete="new-password" required ${submitting ? "disabled" : ""} />
      </label>
      <label>
        <span>确认密码</span>
        <input name="confirmPassword" type="password" minlength="8" maxlength="256" autocomplete="new-password" required ${submitting ? "disabled" : ""} />
      </label>
      <button type="submit" ${submitting ? "disabled aria-busy=\"true\"" : ""}>${submitting ? "正在加入" : "加入并登录"}</button>
      ${feedback}
    </form>
  `);
}

/** 公开页使用独立、无控制台导航的布局，避免已有 Session 组件读取或发送请求。 */
function pageShell(content) {
  return `
    <section class="invitation-public-shell" aria-labelledby="invitation-public-title">
      <header class="invitation-public-brand">
        <span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span>
        <strong>MetaHub AI Gateway</strong>
      </header>
      <div class="invitation-public-content" id="invitation-public-title">${content}</div>
    </section>
  `;
}

/** 转义 API 返回的用户摘要和固定反馈，禁止进入 HTML 边界。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
