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
        ${renderWorkspaceSection(state)}
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
