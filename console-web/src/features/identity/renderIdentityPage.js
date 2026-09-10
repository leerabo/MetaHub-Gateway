// 账号安全渲染层只展示后端脱敏投影，企业认证表单可见性不构成服务端授权。
import { renderPrivacyCenter } from "./renderPrivacyCenter.js";
import { renderComplianceCenter } from "./renderComplianceCenter.js";
import { renderAccountPreferences } from "../account-preferences/renderAccountPreferences.js";
import { icon } from "../public-home/icons.js";
import { workspaceOnboardingHref } from "../../lib/workspaces.js";

const DEFAULT_PUBLIC_BRAND = Object.freeze({
  product_name: "MetaHub",
  primary_color: "#08786F",
  secondary_color: "#075F58",
  login_title: "登录账号",
  login_message: "",
  logo_path: null,
  favicon_path: null,
});
let publicBrand = DEFAULT_PUBLIC_BRAND;

/** Apply the strict Host-scoped public projection without persisting brand or host state. */
export function setIdentityPublicBranding(value, documentObject = globalThis.document) {
  publicBrand = normalizedPublicBrand(value);
  const root = documentObject?.documentElement;
  root?.style?.setProperty?.("--identity-accent", publicBrand.primary_color);
  root?.style?.setProperty?.("--identity-accent-dark", publicBrand.secondary_color);
  if (documentObject) documentObject.title = `${publicBrand.product_name} 账号安全`;
  const app = documentObject?.querySelector?.("#identity-app");
  app?.setAttribute?.("aria-label", `${publicBrand.product_name} 账号安全`);
  const favicon = documentObject?.querySelector?.("link[rel='icon']");
  favicon?.setAttribute?.("href", publicBrand.favicon_path ?? "data:,");
  const product = documentObject?.querySelector?.("[data-access-product]");
  if (product) product.textContent = publicBrand.product_name;
  const visual = documentObject?.querySelector?.(".identity-access-visual");
  visual?.setAttribute?.("aria-label", publicBrand.product_name);
  return publicBrand;
}

export function renderIdentityLoginPage(container, options = {}) {
  if (!container) return;
  const busy = options.busy === true;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-login-title">
        <header class="identity-brand">
          ${renderIdentityBrandMark()}
          <div>
            <p>${escapeHtml(publicBrand.product_name)}</p>
            <h1 id="identity-login-title">${escapeHtml(publicBrand.login_title)}</h1>
          </div>
        </header>
        ${publicBrand.login_message ? `<p class="identity-brand-message">${escapeHtml(publicBrand.login_message)}</p>` : ""}
        <p class="identity-login-welcome">欢迎回来</p>
        <form class="identity-login-form" data-identity-login-form aria-busy="${busy}">
          <label>
            <span>邮箱</span>
            <input name="email" type="email" autocomplete="username" inputmode="email" placeholder="name@company.com" value="${escapeAttribute(options.email ?? "")}" required autofocus ${busy ? "disabled" : ""} />
          </label>
          <label>
            <span>密码</span>
            <span class="identity-password-control"><input name="password" type="password" aria-label="密码" autocomplete="current-password" placeholder="输入你的密码" required ${busy ? "disabled" : ""} /><button class="identity-password-toggle" type="button" data-password-toggle aria-label="显示密码" title="显示密码" aria-pressed="false" ${busy ? "disabled" : ""}>${icon("eye")}</button></span>
          </label>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}><span>${busy ? "正在登录" : "登录"}</span>${icon("arrow-right")}</button>
          <div class="identity-access-actions">
            <button type="button" data-identity-action="show-register" ${busy ? "disabled" : ""}>创建账号</button>
            <button type="button" data-identity-action="show-reset" ${busy ? "disabled" : ""}>忘记密码</button>
          </div>
        </form>
        <p class="identity-login-assurance">${icon("shield-check")}安全连接，从这里开始。</p>
      </section>
    </div>
  `;
}

export function renderIdentityRiskChallengePage(container, options = {}) {
  if (!container) return;
  const busy = options.busy === true;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-risk-challenge-title">
        ${renderAccessBrand("identity-risk-challenge-title", "附加验证")}
        <form class="identity-login-form" data-identity-risk-challenge-form autocomplete="off" aria-busy="${busy}">
          <div class="identity-risk-reference"><span>Challenge</span><strong>${escapeHtml(shortRiskReference(options.challengeId))}</strong></div>
          <label>
            <span>验证答案</span>
            <input name="answer" type="password" autocomplete="one-time-code" minlength="6" maxlength="128" required autofocus ${busy ? "disabled" : ""} />
          </label>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>完成验证</button>
          <div class="identity-access-actions is-single">
            <button type="button" data-identity-action="cancel-risk-challenge" ${busy ? "disabled" : ""}>返回账号安全</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

export function renderIdentityRegistrationPage(container, options = {}) {
  if (!container) return;
  const busy = options.busy === true;
  container.innerHTML = `
    <div class="identity-login-shell">
      <section class="identity-login-panel" aria-labelledby="identity-registration-title">
        ${renderAccessBrand("identity-registration-title", "创建账号")}
        <form class="identity-login-form" data-identity-registration-form aria-busy="${busy}">
          <fieldset class="identity-registration-fields" ${busy ? "disabled" : ""}>
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
            ${registrationPassword("password", "密码")}
          </label>
          <label>
            <span>确认密码</span>
            ${registrationPassword("confirmPassword", "确认密码")}
          </label>
          <label class="identity-consent">
            <input name="consent" type="checkbox" required />
            <span>我已阅读并同意<a href="/privacy.html" target="_blank" rel="noopener">隐私条款</a></span>
          </label>
          ${options.policy?.human_verification ? `<label><span>图形验证码</span><span class="identity-captcha-control">
            <input name="captchaAnswer" aria-label="图形验证码" type="text" inputmode="numeric" autocomplete="off" pattern="[0-9]{5}" maxlength="5" required />
            <img ${options.captcha ? `src="${escapeAttribute(options.captcha.image)}"` : "hidden"} alt="五位数字验证码" width="180" height="64" />
            <button type="button" data-identity-action="refresh-captcha" aria-label="刷新验证码" title="刷新验证码">${icon("rotate-cw")}</button>
          </span></label>` : ""}
          </fieldset>
          ${renderError(options.error)}
          <button class="identity-primary" type="submit" ${busy || options.unavailable ? "disabled" : ""}><span>${busy ? (options.loading ? "正在加载" : "正在注册") : "注册"}</span>${icon("arrow-right")}</button>
          <div class="identity-access-actions is-single">
            <button type="button" data-identity-action="show-login">返回登录</button>
          </div>
        </form>
      </section>
    </div>
  `;
}

function registrationPassword(name, label) {
  return `<span class="identity-password-control"><input name="${name}" aria-label="${label}" type="password" autocomplete="new-password" minlength="8" required /><button class="identity-password-toggle" type="button" data-password-toggle aria-label="显示${label}" title="显示${label}" aria-pressed="false">${icon("eye")}</button></span>`;
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
  if (isNewOrganizationAccount(state) && !state.accountSecurityView
      && (!state.error || state.workspaceView === "create")) {
    renderOrganizationStart(container, state);
    return;
  }
  const account = state.account;
  const sessions = Array.isArray(state.sessions) ? state.sessions : [];
  const currentIndex = sessions.findIndex((session) => session.is_current);
  const currentSession = currentIndex >= 0 ? sessions[currentIndex] : state.currentSession;
  const otherSessions = sessions
    .map((session, index) => ({ session, index }))
    .filter(({ session }) => !session.is_current);
  const displayName = account.display_name || account.email;

  if (state.notificationCenterOpen) {
    container.innerHTML = `
      <div class="identity-security-shell">
        ${renderIdentityTopbar(state)}
        <div class="identity-security-main">
          ${renderNotificationCenter(state)}
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="identity-security-shell">
      ${renderIdentityTopbar(state)}
      <div class="identity-security-main">
        <section class="identity-heading" aria-labelledby="identity-security-title">
          <p>账号安全</p>
          <h1 id="identity-security-title">${escapeHtml(displayName)}</h1>
          <span>${escapeHtml(account.email)}</span>
        </section>
        ${renderFeedback(state.feedback)}
        ${renderError(state.error)}
        ${renderAccountPreferences(state.accountPreferenceState)}
        ${renderOnboardingSection(state)}
        ${renderWorkspaceBillingSection(state)}
        ${renderInvoiceSection(state)}
        ${renderBusinessVerificationSection(state)}
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
        ${renderConditionalAccessSection(state)}
        ${renderDataGovernanceSection(state)}
        ${renderPrivacyCenter(state)}
        ${renderComplianceCenter(state)}
        ${renderEnterpriseIdentitySection(state)}
        ${renderScimDirectorySection(state)}
        ${renderMachineIdentitySection(state)}
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

function renderIdentityTopbar(state) {
  const unread = Math.max(0, Number(state.notificationInbox?.unread_count ?? 0));
  const badge = unread > 99 ? "99+" : String(unread);
  const available = Boolean(state.workspaceContext);
  return `
    <header class="identity-topbar">
      <a class="identity-wordmark" href="/identity.html" aria-label="${escapeAttribute(publicBrand.product_name)} 账号安全首页">
        ${renderIdentityBrandMark()}
        <strong>${escapeHtml(publicBrand.product_name)}</strong>
      </a>
      ${renderWorkspaceSelector(state)}
      <div class="identity-topbar-actions">
        ${isNewOrganizationAccount(state) ? '<button class="identity-quiet-command" type="button" data-identity-action="show-organization-start">返回组织开通</button>' : ""}
        ${available ? `<a class="identity-quiet-command" href="/console.html">进入控制台 ${icon("arrow-right")}</a>` : ""}
        <button
          class="identity-notification-button"
          type="button"
          data-identity-action="open-notifications"
          title="通知中心"
          aria-label="通知中心，${unread} 条未读"
          ${available ? "" : "disabled"}
        >
          <span aria-hidden="true">&#128276;</span>
          <b class="identity-notification-badge ${unread > 0 ? "is-active" : ""}">${escapeHtml(badge)}</b>
        </button>
        <button class="identity-quiet-command" type="button" data-identity-action="logout">退出登录</button>
      </div>
    </header>
  `;
}

function renderNotificationCenter(state) {
  const inbox = state.notificationInbox ?? { notifications: [], unread_count: 0, next_cursor: null };
  const notifications = Array.isArray(inbox.notifications) ? inbox.notifications : [];
  const page = Math.max(1, Number(state.notificationPage ?? 1));
  return `
    <section class="identity-notification-center" aria-labelledby="identity-notification-title" aria-busy="${state.notificationBusy === true}">
      <header class="identity-notification-heading">
        <button class="identity-icon-command" type="button" data-identity-action="close-notifications" title="返回账号安全" aria-label="返回账号安全">&#8592;</button>
        <div>
          <p>Workspace inbox</p>
          <h1 id="identity-notification-title">通知中心</h1>
          <span>${Number(inbox.unread_count ?? 0)} 条未读</span>
        </div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-notifications" title="刷新通知" aria-label="刷新通知" ${state.notificationBusy ? "disabled" : ""}>&#8635;</button>
      </header>
      ${renderError(state.notificationError)}
      <div class="identity-notification-toolbar">
        <div class="identity-segmented" role="group" aria-label="通知筛选">
          <button type="button" data-identity-action="notification-filter-all" aria-pressed="${!state.notificationUnreadOnly}">全部</button>
          <button type="button" data-identity-action="notification-filter-unread" aria-pressed="${state.notificationUnreadOnly === true}">未读</button>
        </div>
        <span>第 ${page} 页</span>
      </div>
      <div class="identity-notification-list">
        ${notifications.length > 0
          ? notifications.map(renderNotificationItem).join("")
          : '<p class="identity-empty">当前筛选下没有通知。</p>'}
      </div>
      <div class="identity-notification-pagination">
        <button type="button" data-identity-action="notification-previous" ${page > 1 && !state.notificationBusy ? "" : "disabled"}>上一页</button>
        <button type="button" data-identity-action="notification-next" ${inbox.next_cursor && !state.notificationBusy ? "" : "disabled"}>下一页</button>
      </div>
      ${renderNotificationPreferences(state)}
    </section>
  `;
}

function renderNotificationItem(notification, index) {
  return `
    <article class="identity-notification-item ${notification.read ? "" : "is-unread"}">
      <span class="identity-notification-state" aria-hidden="true"></span>
      <div>
        <div class="identity-notification-meta">
          <span>${escapeHtml(notificationCategoryLabel(notification.category))}</span>
          <time datetime="${escapeAttribute(notification.created_at)}">${escapeHtml(formatDateTime(notification.created_at))}</time>
        </div>
        <h2>${escapeHtml(notification.subject)}</h2>
        <p>${escapeHtml(notification.body)}</p>
      </div>
      <button
        class="identity-icon-command"
        type="button"
        data-identity-action="mark-notification-read"
        data-notification-index="${index}"
        title="标记为已读"
        aria-label="标记 ${escapeAttribute(notification.subject)} 为已读"
        ${notification.read ? "disabled" : ""}
      >&#10003;</button>
    </article>
  `;
}

function renderNotificationPreferences(state) {
  const values = Array.isArray(state.notificationPreferences?.preferences)
    ? state.notificationPreferences.preferences
    : [];
  const categories = ["security", "billing", "verification", "payments", "invoices", "operations"];
  const channels = ["in_app", "email", "webhook", "sms", "web_push"];
  return `
    <section class="identity-notification-preferences" aria-labelledby="identity-notification-preferences-title">
      <div class="identity-section-heading">
        <div><p>Delivery policy</p><h2 id="identity-notification-preferences-title">通知偏好</h2></div>
        <span>Revision ${Number(state.notificationPreferences?.revision ?? 0)}</span>
      </div>
      <div class="identity-notification-preference-wrap">
        <table>
          <thead><tr><th>类别</th>${channels.map((channel) => `<th>${escapeHtml(notificationChannelLabel(channel))}</th>`).join("")}</tr></thead>
          <tbody>
            ${categories.map((category) => `
              <tr>
                <th scope="row">${escapeHtml(notificationCategoryLabel(category))}</th>
                ${channels.map((channel) => renderNotificationPreferenceCell(
                  values.find((item) => item.category === category && item.channel === channel),
                  state.notificationBusy,
                )).join("")}
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function renderNotificationPreferenceCell(preference, busy) {
  if (!preference) return '<td><span class="identity-policy-state">不可用</span></td>';
  const disabled = busy || preference.mandatory || !preference.available;
  const status = preference.mandatory
    ? "系统强制"
    : preference.available
      ? ""
      : "未配置";
  const label = `${notificationCategoryLabel(preference.category)} ${notificationChannelLabel(preference.channel)}`;
  return `
    <td>
      <label class="identity-preference-toggle">
        <input
          type="checkbox"
          data-notification-preference
          data-notification-category="${escapeAttribute(preference.category)}"
          data-notification-channel="${escapeAttribute(preference.channel)}"
          aria-label="${escapeAttribute(label)}"
          ${preference.enabled ? "checked" : ""}
          ${disabled ? "disabled" : ""}
        />
        <span aria-hidden="true"></span>
      </label>
      ${status ? `<small class="identity-policy-state">${escapeHtml(status)}</small>` : ""}
    </td>
  `;
}

function notificationCategoryLabel(value) {
  return ({
    security: "安全",
    billing: "订阅",
    verification: "认证",
    payments: "支付",
    invoices: "发票",
    operations: "运维",
  })[value] ?? "通知";
}

function notificationChannelLabel(value) {
  return ({ in_app: "站内", email: "邮件", webhook: "Webhook", sms: "短信", web_push: "WebPush" })[value]
    ?? "渠道";
}

function renderOnboardingSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context) return "";
  const onboarding = state.onboarding ?? null;
  if (!onboarding) {
    return `
      <section class="identity-onboarding" aria-labelledby="identity-onboarding-title">
        <div class="identity-section-heading">
          <div><p>空间初始化</p><h2 id="identity-onboarding-title">初始化中心</h2></div>
          <button class="identity-quiet-command" type="button" data-identity-action="refresh-onboarding">重试</button>
        </div>
        ${
          state.onboardingError
            ? renderError(state.onboardingError)
            : '<p class="identity-empty">初始化状态暂时不可用。</p>'
        }
      </section>
    `;
  }
  const selectedIndex = Math.max(
    0,
    onboarding.steps.findIndex((step) => step.id === state.onboardingSelectedStepId),
  );
  const selectedStep = onboarding.steps[selectedIndex] ?? onboarding.steps[0];
  const busy = state.onboardingBusy === true;
  const canOperate = new Set(["owner", "admin", "developer"]).has(context.role);
  return `
    <section
      class="identity-onboarding"
      aria-labelledby="identity-onboarding-title"
      aria-busy="${busy}"
    >
      <div class="identity-onboarding-heading">
        <div>
          <p>空间初始化</p>
          <h2 id="identity-onboarding-title">初始化中心</h2>
          <span>${onboarding.workspace_kind === "personal" ? "个人空间" : "企业空间"} · ${onboarding.completed_steps}/${onboarding.total_steps}</span>
        </div>
        <button
          class="identity-quiet-command"
          type="button"
          data-identity-action="recheck-onboarding"
          ${busy || !canOperate ? "disabled" : ""}
        >重新检查</button>
      </div>
      <progress
        class="identity-onboarding-progress"
        max="${onboarding.total_steps}"
        value="${onboarding.completed_steps}"
        aria-label="初始化进度"
      ></progress>
      <div class="identity-onboarding-layout">
        <ol class="identity-onboarding-steps" aria-label="初始化步骤">
          ${onboarding.steps.map((step, index) => renderOnboardingStepButton(step, index, selectedIndex, busy)).join("")}
        </ol>
        ${renderOnboardingStepPanel(selectedStep, context, busy, state.onboardingError)}
      </div>
    </section>
  `;
}

function renderWorkspaceBillingSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || !new Set(["owner", "finance_admin"]).has(context.role)) return "";
  const response = state.billingSummary ?? {};
  const cycle = response.cycle ?? null;
  const available = Array.isArray(response.available_cycles) ? response.available_cycles : [];
  return `
    <section class="identity-billing" aria-labelledby="identity-billing-title" aria-busy="${state.billingBusy === true}">
      <div class="identity-section-heading">
        <div><p>计量与结算</p><h2 id="identity-billing-title">账单摘要</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-billing" aria-label="刷新账单摘要" title="刷新账单摘要" ${state.billingBusy ? "disabled" : ""}>↻</button>
      </div>
      ${state.billingError ? renderError(state.billingError) : ""}
      ${cycle ? renderBillingCycle(cycle, available, state.billingBusy) : renderNoBillingCycle()}
    </section>
  `;
}

function renderInvoiceSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || !new Set(["owner", "finance_admin"]).has(context.role)) return "";
  const statements = Array.isArray(state.billingStatements?.statements)
    ? state.billingStatements.statements
    : [];
  const invoice = state.currentInvoice?.invoice ?? null;
  return `
    <section class="identity-invoices" aria-labelledby="identity-invoices-title" aria-busy="${state.invoiceBusy === true}">
      <div class="identity-section-heading">
        <div><p>税务产物</p><h2 id="identity-invoices-title">结算单与发票</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-invoices" aria-label="刷新结算单与发票" title="刷新结算单与发票" ${state.invoiceBusy ? "disabled" : ""}>↻</button>
      </div>
      ${state.invoiceError ? renderError(state.invoiceError) : ""}
      ${statements.length ? renderStatementTable(statements, invoice, state.invoiceBusy) : '<p class="identity-empty">当前没有已发布结算单。</p>'}
      ${invoice ? renderCurrentInvoice(invoice, state.invoiceBusy) : ""}
    </section>
  `;
}

function renderStatementTable(statements, invoice, busy) {
  return `
    <div class="identity-invoice-table-wrap">
      <table class="identity-invoice-table">
        <thead><tr><th>结算单</th><th>周期</th><th>税率</th><th>原币金额</th><th>发票申请</th></tr></thead>
        <tbody>${statements.map((statement) => {
          const current = invoice?.billing_statement_id === statement.billing_statement_id;
          return `<tr>
            <td data-label="结算单"><strong>${escapeHtml(statementKindLabel(statement.statement_kind))}</strong><span>v${formatIdentityNumber(statement.immutable_version)} · ${escapeHtml(formatIdentityDate(statement.issued_at))}</span></td>
            <td data-label="周期">${escapeHtml(statement.billing_cycle_id)}</td>
            <td data-label="税率">${formatTaxRate(statement.tax_rate_basis_points)}</td>
            <td data-label="原币金额"><strong>${escapeHtml(formatMinorMoney(statement.total_minor, statement.currency))}</strong><span>未税 ${escapeHtml(formatMinorMoney(statement.subtotal_minor, statement.currency))} · 税 ${escapeHtml(formatMinorMoney(statement.tax_minor, statement.currency))}</span></td>
            <td data-label="发票申请">${current
              ? `<span class="identity-invoice-status is-${escapeAttribute(invoice.status)}">${escapeHtml(invoiceStatusLabel(invoice.status))}</span>`
              : `<form class="identity-invoice-request" data-invoice-request-form>
                  <input name="billingStatementId" type="hidden" value="${escapeAttribute(statement.billing_statement_id)}" />
                  <label><span class="identity-visually-hidden">发票类型</span><select name="invoiceType" aria-label="发票类型"><option value="electronic_general">电子普票</option><option value="electronic_special">电子专票</option></select></label>
                  <button type="submit" ${busy ? "disabled" : ""}>申请</button>
                </form>`}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderCurrentInvoice(invoice, busy) {
  const artifacts = Array.isArray(invoice.artifacts) ? invoice.artifacts : [];
  return `
    <div class="identity-current-invoice">
      <div class="identity-invoice-heading">
        <div><span>当前发票</span><strong>${escapeHtml(invoiceTypeLabel(invoice.invoice_type))} · ${escapeHtml(formatMinorMoney(invoice.total_minor, invoice.currency))}</strong><small>${escapeHtml(invoiceStatusLabel(invoice.status))} · 资料版本 ${formatIdentityNumber(invoice.billing_profile_revision)}</small></div>
        <button type="button" data-identity-action="refresh-invoice" ${busy ? "disabled" : ""}>刷新状态</button>
      </div>
      <div class="identity-invoice-artifacts">
        ${artifacts.map((artifact) => `<div>
          <div><strong>${escapeHtml(artifactKindLabel(artifact.artifact_kind))}</strong><span>${escapeHtml(artifact.invoice_number_masked)} · ${formatIdentityNumber(artifact.size_bytes)} B · ${escapeHtml(formatIdentityDate(artifact.issued_at))}</span></div>
          <button class="identity-icon-command" type="button" data-identity-action="download-invoice-artifact" data-artifact-id="${escapeAttribute(artifact.invoice_artifact_id)}" aria-label="下载${escapeAttribute(artifactKindLabel(artifact.artifact_kind))}" title="下载${escapeAttribute(artifactKindLabel(artifact.artifact_kind))}" ${busy ? "disabled" : ""}>↓</button>
        </div>`).join("") || '<p class="identity-empty">Provider 正在生成发票产物。</p>'}
      </div>
    </div>
  `;
}

/** 渲染组织 Workspace 企业认证与开票面板，响应摘要不得包含主体或账号明文。 */
function renderBusinessVerificationSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || context.kind !== "organization"
    || !new Set(["owner", "finance_admin"]).has(context.role)) return "";
  const response = state.businessVerification ?? {};
  const legal = response.legal_entity ?? null;
  const verification = response.verification ?? null;
  const contract = response.contract ?? null;
  const billing = response.billing_profile ?? null;
  const busy = state.businessVerificationBusy === true;
  return `
    <section class="identity-business-verification" aria-labelledby="identity-business-verification-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>企业主体</p><h2 id="identity-business-verification-title">企业认证</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-business-verification" aria-label="刷新企业认证" title="刷新企业认证" ${busy ? "disabled" : ""}>↻</button>
      </div>
      ${state.businessVerificationError ? renderError(state.businessVerificationError) : ""}
      <dl class="identity-business-facts">
        <div><dt>认证状态</dt><dd>${escapeHtml(businessVerificationStatusLabel(verification?.status ?? "not_submitted"))}</dd></div>
        <div><dt>国家/主体</dt><dd>${legal ? `${escapeHtml(legal.country_code)} · ${escapeHtml(entityTypeLabel(legal.entity_type))}` : "未配置"}</dd></div>
        <div><dt>登记号</dt><dd>${escapeHtml(legal?.registration_number_masked ?? "未配置")}</dd></div>
        <div><dt>资料版本</dt><dd>${legal ? `Rev ${formatIdentityNumber(legal.revision)}` : "-"}</dd></div>
        <div><dt>证据扫描</dt><dd>${escapeHtml(scanSummaryLabel(verification?.evidence))}</dd></div>
        <div><dt>合同主体</dt><dd>${escapeHtml(contract?.signing_status ? contractStatusLabel(contract.signing_status) : "未生成")}</dd></div>
      </dl>
      <form class="identity-business-form" data-business-verification-form autocomplete="off">
        <label><span>国家/地区</span><select name="countryCode"><option value="CN">CN</option></select></label>
        <label><span>主体类型</span><select name="entityType"><option value="company">企业</option><option value="nonprofit">非营利组织</option><option value="government">政府机构</option></select></label>
        <label class="is-wide"><span>法定名称</span><input name="legalName" maxlength="240" autocomplete="off" required /></label>
        <label class="is-wide"><span>登记号</span><input name="registrationNumber" maxlength="128" autocomplete="off" required /></label>
        <label><span>证据类型</span><select name="evidenceType"><option value="business_license">营业执照</option><option value="registration_certificate">登记证书</option><option value="authorization_letter">授权文件</option></select></label>
        <label class="is-wide"><span>受控对象引用</span><input name="objectReference" pattern="obj_[A-Za-z0-9._:\\-]+" maxlength="256" autocomplete="off" required /></label>
        <label class="is-wide"><span>内容 SHA-256</span><input name="contentSha256" pattern="[0-9a-f]{64}" maxlength="64" autocomplete="off" required /></label>
        <label><span>MIME</span><select name="mimeType"><option value="application/pdf">PDF</option><option value="image/png">PNG</option><option value="image/jpeg">JPEG</option></select></label>
        <label><span>大小（bytes）</span><input name="sizeBytes" type="number" min="1" max="10485760" required /></label>
        <label><span>KMS 版本</span><input name="kmsKeyVersion" type="number" min="1" max="1000000" required /></label>
        <label class="is-wide"><span>扫描证明</span><input name="scanAttestation" pattern="[0-9a-f]{64}" maxlength="64" autocomplete="off" required /></label>
        <label class="identity-confirm is-wide"><input name="confirmed" type="checkbox" required /><span>确认提交</span></label>
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>${verification ? "重新提交认证" : "提交认证"}</button>
      </form>
    </section>
    <section class="identity-business-verification" aria-labelledby="identity-business-billing-title" aria-busy="${busy}">
      <div class="identity-section-heading"><div><p>合同与开票</p><h2 id="identity-business-billing-title">开票资料</h2></div></div>
      <dl class="identity-business-facts">
        <div><dt>验证状态</dt><dd>${escapeHtml(billingVerificationLabel(billing?.verification_status ?? "unverified"))}</dd></div>
        <div><dt>税号</dt><dd>${escapeHtml(billing?.tax_number_masked ?? "未配置")}</dd></div>
        <div><dt>地址/电话</dt><dd>${configuredPair(billing?.address_configured, billing?.phone_configured)}</dd></div>
        <div><dt>银行账号</dt><dd>${escapeHtml(billing?.bank_account_masked ?? "未配置")}</dd></div>
      </dl>
      <form class="identity-business-form" data-business-billing-form autocomplete="off">
        <label class="is-wide"><span>发票抬头</span><input name="invoiceTitle" maxlength="240" autocomplete="off" required /></label>
        <label class="is-wide"><span>税号</span><input name="taxNumber" maxlength="128" autocomplete="off" required /></label>
        <label class="is-wide"><span>地址</span><input name="address" maxlength="500" autocomplete="off" /></label>
        <label><span>电话</span><input name="phone" maxlength="80" autocomplete="off" /></label>
        <label class="is-wide"><span>开户行</span><input name="bankName" maxlength="240" autocomplete="off" /></label>
        <label class="is-wide"><span>银行账号</span><input name="bankAccount" maxlength="128" autocomplete="off" /></label>
        <label class="identity-confirm is-wide"><input name="confirmed" type="checkbox" required /><span>确认更新</span></label>
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>更新开票资料</button>
      </form>
    </section>
  `;
}

function renderBillingCycle(cycle, available, busy) {
  return `
    <div class="identity-billing-toolbar">
      <div>
        <strong>${escapeHtml(formatIdentityDate(cycle.period_started_at))} 至 ${escapeHtml(formatIdentityDate(cycle.period_ends_at))}</strong>
        <span>${escapeHtml(billingCycleStatusLabel(cycle.status))} · ${escapeHtml(cycle.currency)} · 水位 ${formatIdentityNumber(cycle.source_watermark)}</span>
      </div>
      <div class="identity-billing-actions">
        ${available.length > 1 ? `
          <form data-workspace-billing-cycle-form>
            <label><span class="identity-visually-hidden">账单周期</span><select name="cycleId" aria-label="账单周期">${available.map((cycleId, index) => `<option value="${escapeAttribute(cycleId)}" ${cycleId === cycle.billing_cycle_id ? "selected" : ""}>${cycleId === cycle.billing_cycle_id ? "当前周期" : `历史周期 ${index + 1}`}</option>`).join("")}</select></label>
            <button type="submit" ${busy ? "disabled" : ""}>查看</button>
          </form>` : ""}
        <button type="button" data-identity-action="download-billing-export" ${busy ? "disabled" : ""}>导出 CSV</button>
      </div>
    </div>
    <div class="identity-billing-table-wrap">
      <table class="identity-billing-table">
        <thead><tr><th>Meter</th><th>用量</th><th>配额</th><th>超额</th><th>调整</th><th>有效金额</th></tr></thead>
        <tbody>${cycle.meters.map(renderWorkspaceMeterRow).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderWorkspaceMeterRow(meter) {
  const amount = meter.billing_mode === "informational"
    ? "未计价"
    : `${meter.currency} ${meter.effective_amount}`;
  return `<tr>
    <td data-label="Meter"><strong>${escapeHtml(meterLabel(meter.meter_code))}</strong><span>${escapeHtml(meter.unit)}</span></td>
    <td data-label="用量">${formatIdentityNumber(meter.usage_quantity)}</td>
    <td data-label="配额">${meter.billing_mode === "informational" ? "信息项" : formatIdentityNumber(meter.allocated_quantity)}</td>
    <td data-label="超额"><strong class="${meter.overage_quantity > 0 ? "is-overage" : ""}">${formatIdentityNumber(meter.overage_quantity)}</strong></td>
    <td data-label="调整">${formatSignedNumber(meter.adjustment_quantity)}</td>
    <td data-label="有效金额"><strong>${escapeHtml(amount)}</strong></td>
  </tr>`;
}

function renderNoBillingCycle() {
  return '<div class="identity-empty-strip"><strong>暂无计量周期</strong><span>首条可计量 Usage 到达后会生成账单周期</span></div>';
}

function formatMinorMoney(value, currency) {
  const amount = Number(value);
  if (!Number.isSafeInteger(amount)) return "-";
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: new Set(["CNY", "USD"]).has(currency) ? currency : "CNY",
    currencyDisplay: "code",
  }).format(amount / 100);
}

function formatIdentityDate(value) {
  const date = new Date(value);
  return value && !Number.isNaN(date.valueOf())
    ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date)
    : "-";
}

function formatIdentityNumber(value) {
  return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0";
}

function formatSignedNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0";
  return `${number > 0 ? "+" : ""}${formatIdentityNumber(number)}`;
}

function billingCycleStatusLabel(value) {
  return ({
    open: "进行中",
    closed: "已关账",
    reconciliation_required: "需要对账",
  })[value] ?? String(value ?? "-").replaceAll("_", " ");
}

function statementKindLabel(value) {
  return ({ standard: "标准结算单", credit: "贷项结算单", replacement: "更正结算单" })[value]
    ?? "结算单";
}

function invoiceTypeLabel(value) {
  return ({ electronic_general: "电子普通发票", electronic_special: "电子专用发票" })[value]
    ?? "电子发票";
}

function invoiceStatusLabel(value) {
  return ({
    requested: "已申请",
    processing: "开票中",
    issued: "已开具",
    failed: "开票失败",
    reversal_pending: "冲红审批中",
    reversed: "已冲红",
  })[value] ?? "状态不可用";
}

function artifactKindLabel(value) {
  return ({ invoice_pdf: "发票 PDF", invoice_csv: "发票 CSV", red_letter_pdf: "红字 PDF" })[value]
    ?? "发票产物";
}

function formatTaxRate(basisPoints) {
  const value = Number(basisPoints);
  return Number.isSafeInteger(value) ? `${(value / 100).toFixed(2)}%` : "-";
}

function meterLabel(value) {
  return ({
    api_requests: "API 请求",
    input_tokens: "输入 Token",
    output_tokens: "输出 Token",
    total_tokens: "总 Token",
  })[value] ?? String(value ?? "-").replaceAll("_", " ");
}

function renderOnboardingStepButton(step, index, selectedIndex, busy) {
  const selected = index === selectedIndex;
  return `
    <li>
      <button
        type="button"
        data-identity-action="select-onboarding-step"
        data-onboarding-step-index="${index}"
        aria-current="${selected ? "step" : "false"}"
        ${busy ? "disabled" : ""}
      >
        <span class="identity-onboarding-step-number" aria-hidden="true">${step.order}</span>
        <span class="identity-onboarding-step-copy">
          <strong>${escapeHtml(step.title)}</strong>
          <small>${escapeHtml(onboardingStatusLabel(step.status))}</small>
        </span>
        <span class="identity-onboarding-step-mark" aria-hidden="true">${step.status === "complete" ? "✓" : "›"}</span>
      </button>
    </li>
  `;
}

function renderOnboardingStepPanel(step, context, busy, error) {
  const canOperate = new Set(["owner", "admin", "developer"]).has(context.role);
  const canManageProfile = context.role === "owner" || (
    context.kind === "organization" && context.role === "admin"
  );
  const canMutate = step.id === "workspace_profile" ? canManageProfile : canOperate;
  const canConfirm = canMutate
    && step.action === "confirm"
    && new Set(["current", "deferred", "needs_attention"]).has(step.status);
  const canDefer = canMutate && new Set(["current", "needs_attention"]).has(step.status);
  const canReopen = canMutate
    && step.status === "complete"
    && step.source === "user_confirmed"
    && new Set(["workspace_profile", "environment"]).has(step.id);
  const showNavigation = new Set(["configure", "view", "recheck"]).has(step.action) || !canMutate;
  const navigationHref = workspaceOnboardingHref(step);
  return `
    <div class="identity-onboarding-panel" tabindex="-1" data-onboarding-step-panel>
      <div class="identity-onboarding-panel-heading">
        <div>
          <span>步骤 ${step.order}</span>
          <h3>${escapeHtml(step.title)}</h3>
        </div>
        <span class="identity-onboarding-state is-${escapeAttribute(step.status)}">${escapeHtml(onboardingStatusLabel(step.status))}</span>
      </div>
      <dl class="identity-onboarding-facts">
        <div><dt>来源</dt><dd>${escapeHtml(onboardingSourceLabel(step.source))}</dd></div>
        <div><dt>检查时间</dt><dd>${escapeHtml(formatDateTime(step.last_checked_at))}</dd></div>
        <div><dt>诊断</dt><dd>${escapeHtml(onboardingBlockingLabel(step.blocking_code))}</dd></div>
      </dl>
      ${error ? renderError(error) : ""}
      <div class="identity-onboarding-actions">
        ${canConfirm ? `<button class="identity-primary" type="button" data-identity-action="confirm-onboarding-step" ${busy ? "disabled" : ""}>确认完成</button>` : ""}
        ${canDefer ? `<button class="identity-quiet-command" type="button" data-identity-action="defer-onboarding-step" ${busy ? "disabled" : ""}>稍后处理</button>` : ""}
        ${canReopen ? `<button class="identity-quiet-command" type="button" data-identity-action="reopen-onboarding-step" ${busy ? "disabled" : ""}>重新配置</button>` : ""}
        ${showNavigation && navigationHref ? `<a class="identity-quiet-command identity-onboarding-link" href="${escapeAttribute(navigationHref)}">${step.status === "complete" ? "查看" : "打开配置"}</a>` : ""}
      </div>
    </div>
  `;
}

function onboardingStatusLabel(status) {
  return {
    current: "当前步骤",
    blocked: "等待上一步",
    complete: "已完成",
    deferred: "稍后处理",
    needs_attention: "需要处理",
  }[status] ?? "未知";
}

function onboardingSourceLabel(source) {
  return {
    system_created: "系统准备",
    user_confirmed: "你已确认",
    verified: "已验证",
    legacy_seed: "历史配置",
    none: "未配置",
  }[source] ?? "未配置";
}

function onboardingBlockingLabel(code) {
  if (String(code ?? "").startsWith("requires.")) return "请先完成上一步";
  return {
    evidence_changed: "已验证配置发生变化",
    legacy_configuration: "历史配置需要重新验证",
  }[code] ?? "无阻塞项";
}

function renderConditionalAccessSection(state) {
  if (!state.workspaceContext || state.conditionalAccessVisible !== true) return "";
  const policy = state.conditionalAccess?.policy ?? null;
  const simulation = state.conditionalAccess?.latest_simulation ?? null;
  const canManage = state.conditionalAccessCanManage === true;
  const busy = state.conditionalAccessBusy === true;
  const selectedExceptions = new Set(policy?.exception_membership_ids ?? []);
  return `
    <section class="identity-conditional-access" aria-labelledby="identity-conditional-access-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>Workspace Security</p><h2 id="identity-conditional-access-title">条件访问</h2></div>
        <button class="identity-quiet-command" type="button" data-identity-action="refresh-conditional-access" ${busy ? "disabled" : ""}>刷新</button>
      </div>
      ${renderError(state.conditionalAccessError)}
      <div class="identity-security-summary">
        <div><span>执行模式</span><strong>${escapeHtml(conditionalModeLabel(policy?.mode))}</strong></div>
        <div><span>策略版本</span><strong>${policy?.revision ?? 0}</strong></div>
        <div><span>最近评估</span><strong>${simulation ? escapeHtml(conditionalSimulationLabel(simulation)) : "尚未评估"}</strong></div>
      </div>
      ${simulation ? `
        <div class="identity-impact-summary">
          <div><span>受影响会话</span><strong>${simulation.affected_session_count}</strong></div>
          <div><span>Owner 恢复</span><strong>${simulation.owner_ready ? "已就绪" : "未就绪"}</strong></div>
          <div><span>评估时间</span><strong>${escapeHtml(formatDateTime(simulation.simulated_at))}</strong></div>
        </div>
      ` : ""}
      ${canManage ? `
        <div class="identity-conditional-toolbar">
          <button class="identity-quiet-command" type="button" data-identity-action="simulate-conditional-access" ${busy ? "disabled" : ""}>运行影响评估</button>
        </div>
        <form class="identity-conditional-form" data-conditional-access-form autocomplete="off">
          <div class="identity-conditional-grid">
            <label><span>执行模式</span><select name="mode">${renderOption("audit", "审计", policy?.mode ?? "audit")}${renderOption("enforced", "强制", policy?.mode ?? "audit")}</select></label>
            <label><span>区域规则</span><select name="regionRule">${renderOption("any", "不限区域", policy?.region_rule ?? "any")}${renderOption("domestic_only", "仅境内", policy?.region_rule ?? "any")}</select></label>
            <label><span>网络规则</span><select name="networkRule">${renderOption("any", "不限网络", policy?.network_rule ?? "any")}${renderOption("trusted_only", "仅受信网络", policy?.network_rule ?? "any")}</select></label>
            <label class="is-wide"><span>受信 CIDR</span><textarea name="allowedCidrs" rows="3" spellcheck="false">${escapeHtml((policy?.allowed_cidrs ?? []).join("\n"))}</textarea></label>
          </div>
          <div class="identity-conditional-toggles">
            ${renderPolicyToggle("requireMfa", "要求 MFA", policy?.require_mfa ?? true)}
            ${renderPolicyToggle("requireStepUpForHighRisk", "高风险操作要求 step-up", policy?.require_step_up_for_high_risk ?? true)}
            ${renderPolicyToggle("allowTrustedDevices", "允许受信设备满足 MFA", policy?.allow_trusted_devices ?? false)}
          </div>
          ${renderConditionalExceptions(state.memberships, selectedExceptions)}
          <div class="identity-step-up-fields">
            <label><span>验证方式</span><select name="stepUpMethod">${renderOption("totp", "动态验证码", "totp")}${renderOption("webauthn", "安全密钥", "totp")}${renderOption("recovery_code", "恢复码", "totp")}</select></label>
            <label><span>验证码或恢复码</span><input name="stepUpCode" type="password" autocomplete="one-time-code" maxlength="20" /></label>
          </div>
          <label class="identity-confirm"><input name="confirmed" type="checkbox" /><span>确认强制模式影响与 Owner 恢复条件</span></label>
          <div class="identity-conditional-actions"><button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>保存策略</button></div>
        </form>
      ` : '<p class="identity-empty">当前职责仅可查看条件访问策略。</p>'}
    </section>
  `;
}

function renderPolicyToggle(name, label, checked) {
  return `<label><input name="${name}" type="checkbox" ${checked ? "checked" : ""} /><span>${escapeHtml(label)}</span></label>`;
}

function renderConditionalExceptions(memberships, selected) {
  const members = Array.isArray(memberships)
    ? memberships.filter((member) => member.status === "active" && member.role !== "service_account")
    : [];
  if (members.length === 0) return "";
  return `
    <fieldset class="identity-conditional-exceptions">
      <legend>例外成员</legend>
      ${members.map((member) => `
        <label><input name="exceptionMembershipIds" type="checkbox" value="${escapeAttribute(member.membership_id)}" ${selected.has(member.membership_id) ? "checked" : ""} /><span>${escapeHtml(member.display_name)} · ${escapeHtml(roleLabel(member.role))}</span></label>
      `).join("")}
    </fieldset>
  `;
}

function conditionalModeLabel(value) {
  return ({ audit: "审计", enforced: "强制" })[value] ?? "未配置";
}

function conditionalSimulationLabel(value) {
  return value.outcome_code === "simulation.ready" ? "可启用" : "Owner 未就绪";
}

function renderDataGovernanceSection(state) {
  if (!state.workspaceContext || state.dataGovernanceVisible !== true) return "";
  const governance = state.dataGovernance;
  const busy = state.dataGovernanceBusy === true;
  if (!governance) {
    return `
      <section class="identity-data-governance" aria-labelledby="identity-data-governance-title" aria-busy="${busy}">
        <div class="identity-section-heading">
          <div><p>Data governance</p><h2 id="identity-data-governance-title">驻留与保留</h2></div>
          <button class="identity-quiet-command" type="button" data-identity-action="refresh-data-governance" ${busy ? "disabled" : ""}>重试</button>
        </div>
        ${renderError(state.dataGovernanceError)}
        <p class="identity-empty">数据治理状态暂时不可用。</p>
      </section>
    `;
  }
  const policy = governance.policy;
  const fence = governance.fence;
  const draft = state.dataGovernanceDraft ?? governanceDraftFromProjection(governance);
  const classifications = new Map(
    (governance.classifications ?? []).map((item) => [item.code, item]),
  );
  const activeHolds = (governance.legal_holds ?? []).filter((hold) => hold.status === "active");
  const terminalSchedules = (governance.deletion_schedules ?? [])
    .filter((item) => new Set(["blocked", "completed", "failed"]).has(item.status))
    .slice(0, 12);
  return `
    <section class="identity-data-governance" aria-labelledby="identity-data-governance-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>Data governance</p><h2 id="identity-data-governance-title">驻留与保留</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-data-governance" title="刷新数据治理" aria-label="刷新数据治理" ${busy ? "disabled" : ""}>&#8635;</button>
      </div>
      ${renderError(state.dataGovernanceError)}
      <div class="identity-governance-summary">
        <div><span>Primary group</span><strong>${escapeHtml(policy.primary_residency_group)}</strong><small>${escapeHtml(policy.cross_region_mode === "deny" ? "禁止跨组" : "仅允许同组")}</small></div>
        <div><span>Policy generation</span><strong>${policy.generation}</strong><small>Revision ${policy.revision}</small></div>
        <div><span>Hold fence</span><strong>${fence.hold_generation}</strong><small>${activeHolds.length} 个 active hold</small></div>
        <div><span>Deletion fence</span><strong>${fence.deletion_generation}</strong><small>Fence rev ${fence.revision}</small></div>
      </div>
      <div class="identity-governance-meta">
        <span>允许组：${policy.allowed_residency_groups.map((item) => escapeHtml(item)).join(" · ")}</span>
        <span>有效至 ${escapeHtml(formatDateTime(policy.valid_until))}</span>
        <span>Policy ${escapeHtml(shortDigest(policy.policy_sha256))}</span>
      </div>
      ${renderGovernancePolicyForm(state, draft, classifications)}
      ${state.legalHoldVisible === true ? renderLegalHoldPanel(state, activeHolds) : ""}
      ${renderDeletionSchedulePanel(terminalSchedules)}
    </section>
  `;
}

function renderGovernancePolicyForm(state, draft, classifications) {
  const governance = state.dataGovernance;
  const canManage = state.dataGovernanceCanManage === true;
  const busy = state.dataGovernanceBusy === true;
  const simulation = governance.simulation;
  const policies = new Map(
    draft.retentionPolicies.map((item) => [item.classificationCode, item]),
  );
  const rows = ["audit", "billing", "file", "identity", "prompt", "response"]
    .map((code) => {
      const item = policies.get(code);
      const classification = classifications.get(code);
      return `
        <tr>
          <th scope="row"><strong>${escapeHtml(classificationLabel(code))}</strong><small>${escapeHtml(sensitivityLabel(classification?.sensitivity_level))}</small></th>
          <td data-label="保留天数"><input name="retentionDays:${code}" type="number" min="1" max="3650" value="${item.retentionDays}" aria-label="${escapeAttribute(`${classificationLabel(code)}保留天数`)}" ${canManage ? "" : "disabled"} required /></td>
          <td data-label="到期动作"><select name="deletionMode:${code}" aria-label="${escapeAttribute(`${classificationLabel(code)}删除方式`)}" ${canManage ? "" : "disabled"}>${deletionModeOptions(item.deletionMode)}</select></td>
          <td data-label="备份宽限"><input name="backupGraceDays:${code}" type="number" min="0" max="365" value="${item.backupGraceDays}" aria-label="${escapeAttribute(`${classificationLabel(code)}备份宽限天数`)}" ${canManage ? "" : "disabled"} required /></td>
        </tr>`;
    }).join("");
  return `
    <div class="identity-governance-block">
      <div class="identity-enterprise-subheading">
        <div><span>Policy projection</span><h3>分类保留策略</h3></div>
        <span>6 类完整策略</span>
      </div>
      ${simulation ? `
        <div class="identity-governance-simulation ${simulation.placement_compliant ? "is-ready" : "is-blocked"}">
          <div><span>候选 generation</span><strong>${simulation.next_generation}</strong></div>
          <div><span>Placement</span><strong>${simulation.placement_compliant ? "合规" : "需要迁移"}</strong></div>
          <div><span>候选摘要</span><strong>${escapeHtml(shortDigest(simulation.candidate_sha256))}</strong></div>
          <div><span>迁移组件</span><strong>${simulation.migration_required_components.length ? simulation.migration_required_components.map(componentLabel).map(escapeHtml).join(" · ") : "无"}</strong></div>
        </div>
      ` : ""}
      <form data-data-governance-policy-form autocomplete="off">
        <div class="identity-governance-policy-grid">
          <label class="is-wide"><span>允许驻留组</span><textarea name="allowedResidencyGroups" rows="2" spellcheck="false" ${canManage ? "" : "disabled"} required>${escapeHtml(draft.allowedResidencyGroups.join("\n"))}</textarea></label>
          <label><span>Primary group</span><input name="primaryResidencyGroup" value="${escapeAttribute(draft.primaryResidencyGroup)}" pattern="[a-z0-9][a-z0-9\\-]{2,39}" maxlength="40" ${canManage ? "" : "disabled"} required /></label>
          <label><span>跨区模式</span><select name="crossRegionMode" ${canManage ? "" : "disabled"}>${renderOption("deny", "禁止跨组", draft.crossRegionMode)}${renderOption("same_group", "仅允许同组", draft.crossRegionMode)}</select></label>
          <label><span>变更原因</span><select name="reasonCode" ${canManage ? "" : "disabled"}>${governanceReasonOptions(draft.reasonCode)}</select></label>
        </div>
        <div class="identity-governance-table-wrap">
          <table><thead><tr><th>数据分类</th><th>保留天数</th><th>到期动作</th><th>备份宽限</th></tr></thead><tbody>${rows}</tbody></table>
        </div>
        ${canManage ? `
          <div class="identity-governance-actions">
            <button class="identity-quiet-command" name="governanceAction" value="simulate" type="submit" ${busy ? "disabled" : ""}>模拟策略</button>
            <label class="identity-confirm"><input name="confirmed" type="checkbox" required ${simulation ? "" : "disabled"} /><span>确认应用已模拟的候选策略</span></label>
            <button class="identity-primary" name="governanceAction" value="apply" type="submit" ${busy || !simulation ? "disabled" : ""}>应用并发布</button>
          </div>
        ` : '<p class="identity-empty">当前职责仅可查看驻留与保留策略。</p>'}
      </form>
    </div>
  `;
}

function renderLegalHoldPanel(state, activeHolds) {
  const holds = state.dataGovernance.legal_holds ?? [];
  const canManage = state.legalHoldCanManage === true;
  const busy = state.dataGovernanceBusy === true;
  return `
    <div class="identity-governance-block">
      <div class="identity-enterprise-subheading">
        <div><span>Deletion preservation</span><h3>法律保全</h3></div>
        <span>${activeHolds.length} active · ${holds.filter((item) => item.status !== "active").length} 历史</span>
      </div>
      ${canManage ? `
        <form class="identity-legal-hold-form" data-legal-hold-create-form autocomplete="off">
          <label><span>数据分类</span><select name="classificationCode">${classificationOptions("prompt")}</select></label>
          <label><span>保全范围</span><select name="scopeKind"><option value="classification">完整分类</option><option value="object_digest">对象摘要</option></select></label>
          <label class="is-wide"><span>对象 SHA-256（仅对象范围）</span><input name="selectorSha256" type="password" autocomplete="off" pattern="[0-9a-f]{64}" maxlength="64" /></label>
          <label><span>保全原因</span><select name="reasonCode">${holdReasonOptions("litigation")}</select></label>
          <label><span>可选到期时间</span><input name="expiresAt" type="datetime-local" /></label>
          <label class="identity-confirm is-wide"><input name="confirmed" type="checkbox" required /><span>确认冻结该范围的删除，不扩大读取权限</span></label>
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>创建保全</button>
        </form>
      ` : ""}
      <div class="identity-legal-hold-list">
        ${holds.length ? holds.map((hold, index) => renderLegalHoldRow(hold, index, canManage, busy)).join("") : '<p class="identity-empty">尚无法律保全记录。</p>'}
      </div>
    </div>
  `;
}

function renderLegalHoldRow(hold, index, canManage, busy) {
  return `
    <article class="identity-legal-hold-row">
      <div><strong>${escapeHtml(classificationLabel(hold.classification_code))}</strong><span>${escapeHtml(hold.scope_kind === "classification" ? "完整分类" : "对象摘要已配置")}</span></div>
      <div><strong>${escapeHtml(holdStatusLabel(hold.status))}</strong><span>${escapeHtml(holdReasonLabel(hold.reason_code))}</span></div>
      <div><strong>Generation ${hold.created_hold_generation}</strong><span>${hold.expires_at ? `到期 ${escapeHtml(formatDateTime(hold.expires_at))}` : "长期有效"}</span></div>
      ${canManage && hold.status === "active" ? `
        <form data-legal-hold-release-form data-legal-hold-index="${index}">
          <select name="reasonCode" aria-label="释放原因"><option value="matter_closed">事项已结束</option><option value="legal_instruction">法务指令</option></select>
          <label class="identity-confirm"><input name="confirmed" type="checkbox" required /><span>确认释放</span></label>
          <button class="identity-danger-command" type="submit" ${busy ? "disabled" : ""}>释放</button>
        </form>
      ` : '<span class="identity-policy-state">只读</span>'}
    </article>
  `;
}

function renderDeletionSchedulePanel(schedules) {
  return `
    <div class="identity-governance-block">
      <div class="identity-enterprise-subheading">
        <div><span>Terminal evidence</span><h3>最近删除终态</h3></div>
        <span>${schedules.length} 条</span>
      </div>
      <div class="identity-governance-table-wrap">
        <table>
          <thead><tr><th>数据分类</th><th>终态</th><th>处理计数</th><th>Checkpoint</th><th>完成时间</th></tr></thead>
          <tbody>${schedules.length ? schedules.map((item) => `
            <tr>
              <th scope="row">${escapeHtml(classificationLabel(item.classification_code))}</th>
              <td>${escapeHtml(deletionStatusLabel(item.status))}</td>
              <td>删除 ${item.deleted_count} · 保留 ${item.retained_count}</td>
              <td>${escapeHtml(item.safe_outcome_code ?? item.checkpoint_code)}</td>
              <td>${escapeHtml(formatDateTime(item.completed_at ?? item.updated_at))}</td>
            </tr>`).join("") : '<tr><td colspan="5" class="identity-empty">尚无删除终态。</td></tr>'}</tbody>
        </table>
      </div>
    </div>
  `;
}

function governanceDraftFromProjection(governance) {
  return {
    allowedResidencyGroups: [...governance.policy.allowed_residency_groups],
    primaryResidencyGroup: governance.policy.primary_residency_group,
    crossRegionMode: governance.policy.cross_region_mode,
    reasonCode: "compliance_update",
    retentionPolicies: governance.retention_policies.map((item) => ({
      classificationCode: item.classification_code,
      retentionDays: item.retention_days,
      deletionMode: item.deletion_mode,
      backupGraceDays: item.backup_grace_days,
    })),
  };
}

function classificationLabel(value) {
  return ({ prompt: "Prompt", response: "Response", file: "文件", audit: "审计", identity: "身份", billing: "账务" })[value] ?? "未知";
}

function sensitivityLabel(value) {
  return ({ restricted: "受限", confidential: "机密", internal: "内部" })[value] ?? "未知";
}

function componentLabel(value) {
  return ({ gateway: "Gateway", backup: "Backup", disaster_recovery: "DR" })[value] ?? value;
}

function deletionModeOptions(selected) {
  return [
    ["hard_delete", "硬删除"],
    ["archive_then_delete", "归档后删除"],
    ["anonymize", "匿名化"],
  ].map(([value, label]) => renderOption(value, label, selected)).join("");
}

function governanceReasonOptions(selected) {
  return [
    ["compliance_update", "合规更新"],
    ["regional_expansion", "区域扩展"],
    ["regional_restriction", "区域收紧"],
    ["retention_update", "保留期更新"],
  ].map(([value, label]) => renderOption(value, label, selected)).join("");
}

function classificationOptions(selected) {
  return ["audit", "billing", "file", "identity", "prompt", "response"]
    .map((value) => renderOption(value, classificationLabel(value), selected)).join("");
}

function holdReasonOptions(selected) {
  return [
    ["litigation", "诉讼"],
    ["regulatory_inquiry", "监管问询"],
    ["internal_investigation", "内部调查"],
    ["preservation_notice", "保全通知"],
  ].map(([value, label]) => renderOption(value, label, selected)).join("");
}

function holdReasonLabel(value) {
  return ({ litigation: "诉讼", regulatory_inquiry: "监管问询", internal_investigation: "内部调查", preservation_notice: "保全通知" })[value] ?? "未知";
}

function holdStatusLabel(value) {
  return ({ active: "生效中", released: "已释放", expired: "已到期" })[value] ?? "未知";
}

function deletionStatusLabel(value) {
  return ({ blocked: "已阻断", completed: "已完成", failed: "失败" })[value] ?? value;
}

function shortDigest(value) {
  const text = String(value ?? "");
  return text.length === 64 ? `${text.slice(0, 10)}…${text.slice(-6)}` : "-";
}

function renderEnterpriseIdentitySection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || !new Set(["owner", "security_admin", "auditor"]).has(context.role)) return "";
  const summary = state.enterpriseIdentity ?? null;
  const domains = Array.isArray(summary?.domains) ? summary.domains : [];
  const connections = Array.isArray(summary?.connections) ? summary.connections : [];
  const candidates = Array.isArray(summary?.break_glass_candidates)
    ? summary.break_glass_candidates
    : [];
  const policy = summary?.policy ?? null;
  const canManage = new Set(["owner", "security_admin"]).has(context.role);
  const busy = state.enterpriseIdentityBusy === true;
  const verifiedDomains = domains.filter((domain) => domain.status === "verified");
  const activeConnections = connections.filter((connection) => connection.status === "active");
  const readyCandidates = candidates.filter(
    (candidate) => candidate.password_configured && candidate.mfa_configured,
  );
  return `
    <section
      class="identity-enterprise"
      aria-labelledby="identity-enterprise-title"
      aria-busy="${busy}"
    >
      <div class="identity-section-heading identity-enterprise-heading">
        <div>
          <p>Enterprise SSO</p>
          <h2 id="identity-enterprise-title">企业身份</h2>
        </div>
        <button
          class="identity-icon-command"
          type="button"
          data-identity-action="refresh-enterprise-identity"
          title="刷新企业身份状态"
          aria-label="刷新企业身份状态"
          ${busy ? "disabled" : ""}
        >&#8635;</button>
      </div>
      ${state.enterpriseIdentityError ? renderError(state.enterpriseIdentityError) : ""}
      ${summary ? `
        <dl class="identity-enterprise-summary">
          <div><dt>已验证域名</dt><dd>${verifiedDomains.length}</dd></div>
          <div><dt>活动连接</dt><dd>${activeConnections.length}</dd></div>
          <div><dt>访问策略</dt><dd>${policy?.mode === "enforced" ? "强制 SSO" : "可选"}</dd></div>
          <div><dt>恢复入口</dt><dd>${policy?.break_glass_configured ? "已配置" : "未配置"}</dd></div>
        </dl>
        ${renderEnterpriseDomains(domains, state, canManage, busy)}
        ${renderEnterpriseConnections(connections, canManage, busy)}
        ${canManage ? renderEnterpriseManagement(
          state,
          verifiedDomains,
          activeConnections,
          candidates,
          readyCandidates,
          busy,
        ) : ""}
      ` : '<p class="identity-empty">企业身份状态暂时不可用。</p>'}
    </section>
  `;
}

function renderEnterpriseDomains(domains, state, canManage, busy) {
  const verification = state.enterpriseVerificationRecord ?? null;
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Domain control</span><h3>企业域名</h3></div>
        <span>${domains.length} 个</span>
      </div>
      ${verification ? `
        <div class="identity-dns-record" role="status">
          <div><span>DNS TXT</span><strong>${escapeHtml(verification.domain)}</strong></div>
          <code>${escapeHtml(verification.value)}</code>
        </div>
      ` : ""}
      <div class="identity-enterprise-list">
        ${domains.length > 0 ? domains.map((domain, index) => `
          <div class="identity-enterprise-row">
            <div class="identity-enterprise-primary">
              <strong>${escapeHtml(domain.domain)}</strong>
              <span>${domain.status === "verified"
                ? `验证于 ${escapeHtml(formatDateTime(domain.verified_at))}`
                : `验证记录有效至 ${escapeHtml(formatDateTime(domain.challenge_expires_at))}`}</span>
            </div>
            <span class="identity-enterprise-state is-${escapeAttribute(domain.status)}">
              ${escapeHtml(enterpriseDomainStatusLabel(domain.status))}
            </span>
            ${canManage && domain.status === "pending" ? `
              <button
                class="identity-quiet-command"
                type="button"
                data-identity-action="verify-enterprise-domain"
                data-enterprise-domain-index="${index}"
                ${busy ? "disabled" : ""}
              >检查 DNS</button>
            ` : ""}
          </div>
        `).join("") : '<p class="identity-empty">尚未添加企业域名。</p>'}
      </div>
    </div>
  `;
}

function renderEnterpriseConnections(connections, canManage, busy) {
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Federation</span><h3>SSO 连接与证书窗口</h3></div>
        <span>${connections.length} 个</span>
      </div>
      <div class="identity-enterprise-list">
        ${connections.length > 0 ? connections.map((connection, index) => `
          <div class="identity-enterprise-connection">
            <div class="identity-enterprise-connection-heading">
              <div class="identity-enterprise-primary">
                <strong>${escapeHtml(connection.display_name)}</strong>
                <span>${escapeHtml(connection.connection_code)} · ${connection.protocol.toUpperCase()}</span>
              </div>
              <span class="identity-enterprise-state is-${escapeAttribute(connection.status)}">
                ${escapeHtml(enterpriseConnectionStatusLabel(connection.status))}
              </span>
              ${canManage && connection.status !== "active" ? `
                <button
                  class="identity-quiet-command"
                  type="button"
                  data-identity-action="test-enterprise-connection"
                  data-enterprise-connection-index="${index}"
                  ${busy ? "disabled" : ""}
                >运行测试</button>
              ` : ""}
            </div>
            <div class="identity-certificate-list">
              ${connection.certificates.map((certificate) => `
                <div>
                  <span>${certificate.purpose === "oidc_jwk" ? "OIDC signing key" : "SAML certificate"}</span>
                  <strong>${escapeHtml(enterpriseCertificateStatusLabel(certificate.status))}</strong>
                  <time datetime="${escapeAttribute(certificate.not_after)}">
                    至 ${escapeHtml(formatDateTime(certificate.not_after))}
                  </time>
                </div>
              `).join("")}
            </div>
          </div>
        `).join("") : '<p class="identity-empty">尚未创建 SSO 连接。</p>'}
      </div>
    </div>
  `;
}

function renderEnterpriseManagement(
  state,
  verifiedDomains,
  activeConnections,
  candidates,
  readyCandidates,
  busy,
) {
  const protocol = state.enterpriseConnectionProtocol === "saml" ? "saml" : "oidc";
  const policy = state.enterpriseIdentity?.policy ?? null;
  return `
    <div class="identity-enterprise-management">
      <form class="identity-enterprise-domain-form" data-enterprise-domain-form>
        <div><span>Domain setup</span><h3>添加域名</h3></div>
        <label><span>域名</span><input name="domain" type="text" maxlength="253" autocomplete="off" required /></label>
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>生成验证记录</button>
      </form>
      <div class="identity-enterprise-command">
        <div class="identity-enterprise-command-heading">
          <div><span>Connection setup</span><h3>新建 SSO 连接</h3></div>
          <div class="identity-segmented" role="group" aria-label="SSO 协议">
            <button type="button" data-identity-action="set-enterprise-protocol" data-enterprise-protocol="oidc" aria-pressed="${protocol === "oidc"}">OIDC</button>
            <button type="button" data-identity-action="set-enterprise-protocol" data-enterprise-protocol="saml" aria-pressed="${protocol === "saml"}">SAML</button>
          </div>
        </div>
        ${verifiedDomains.length > 0 ? `
          <form class="identity-enterprise-connection-form" data-enterprise-connection-form>
            <label><span>已验证域名</span><select name="domainIndex" required>${verifiedDomains.map((domain) => {
              const index = state.enterpriseIdentity.domains.indexOf(domain);
              return `<option value="${index}">${escapeHtml(domain.domain)}</option>`;
            }).join("")}</select></label>
            <label><span>连接代码</span><input name="connectionCode" type="text" maxlength="64" autocomplete="off" required /></label>
            <label><span>显示名称</span><input name="displayName" type="text" maxlength="120" autocomplete="off" required /></label>
            <label class="is-wide"><span>Issuer / Entity ID</span><input name="issuer" type="url" maxlength="512" autocomplete="off" required /></label>
            ${protocol === "oidc" ? `
              <label class="is-wide"><span>Authorization endpoint</span><input name="authorizationEndpoint" type="url" maxlength="1024" autocomplete="off" required /></label>
              <label class="is-wide"><span>Token endpoint</span><input name="tokenEndpoint" type="url" maxlength="1024" autocomplete="off" required /></label>
              <label><span>Client ID</span><input name="clientId" type="text" maxlength="256" autocomplete="off" required /></label>
              <label><span>Client secret</span><input name="clientSecret" type="password" minlength="16" maxlength="2048" autocomplete="new-password" required /></label>
              <label class="is-wide"><span>静态 JWKS</span><textarea name="jwks" maxlength="65536" rows="5" spellcheck="false" required></textarea></label>
            ` : `
              <label class="is-wide"><span>SSO URL</span><input name="ssoUrl" type="url" maxlength="1024" autocomplete="off" required /></label>
              <label class="is-wide"><span>签名证书 PEM</span><textarea name="certificatePem" maxlength="32768" rows="7" spellcheck="false" required></textarea></label>
            `}
            <div class="identity-enterprise-form-actions">
              <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>创建连接</button>
            </div>
          </form>
        ` : '<p class="identity-empty">验证企业域名后可创建连接。</p>'}
      </div>
      <form class="identity-enterprise-policy-form" data-enterprise-policy-form>
        <div><span>Access policy</span><h3>强制策略与恢复</h3></div>
        <label><span>模式</span><select name="mode"><option value="optional" ${policy?.mode !== "enforced" ? "selected" : ""}>可选</option><option value="enforced" ${policy?.mode === "enforced" ? "selected" : ""}>强制 SSO</option></select></label>
        <label><span>活动连接</span><select name="connectionIndex"><option value="">未选择</option>${activeConnections.map((connection) => {
          const index = state.enterpriseIdentity.connections.indexOf(connection);
          return `<option value="${index}" ${policy?.active_connection_id === connection.sso_connection_id ? "selected" : ""}>${escapeHtml(connection.display_name)}</option>`;
        }).join("")}</select></label>
        <label><span>Break-glass Owner</span><select name="candidateIndex"><option value="">未选择</option>${candidates.map((candidate, index) => `<option value="${index}" ${candidate.password_configured && candidate.mfa_configured ? "" : "disabled"}>${escapeHtml(candidate.display_name)} · ${candidate.password_configured && candidate.mfa_configured ? "就绪" : "需密码与 MFA"}</option>`).join("")}</select></label>
        <button class="identity-primary" type="submit" ${busy || (policy?.mode === "enforced" && readyCandidates.length === 0) ? "disabled" : ""}>保存策略</button>
      </form>
    </div>
  `;
}

function enterpriseDomainStatusLabel(value) {
  return ({ pending: "待验证", verified: "已验证", suspended: "已暂停" })[value] ?? "未知";
}

function enterpriseConnectionStatusLabel(value) {
  return ({ draft: "草稿", testing: "测试中", active: "活动", disabled: "已停用" })[value]
    ?? "未知";
}

function enterpriseCertificateStatusLabel(value) {
  return ({ staged: "待启用", active: "活动", retired: "宽限期", expired: "已过期" })[value]
    ?? "未知";
}

function renderScimDirectorySection(state) {
  const context = state.workspaceContext ?? null;
  if (!context
    || !new Set(["owner", "admin", "security_admin", "auditor", "viewer"])
      .has(context.role)) return "";
  const summary = state.scimDirectory ?? null;
  const tokens = Array.isArray(summary?.tokens) ? summary.tokens : [];
  const groups = Array.isArray(summary?.groups) ? summary.groups : [];
  const mappings = Array.isArray(summary?.role_mappings) ? summary.role_mappings : [];
  const syncs = Array.isArray(summary?.sync_runs) ? summary.sync_runs : [];
  const conflicts = Array.isArray(summary?.conflicts) ? summary.conflicts : [];
  const canManage = new Set(["owner", "admin", "security_admin"]).has(context.role);
  const busy = state.scimDirectoryBusy === true;
  const openConflicts = conflicts.filter((item) => item.status === "open");
  const activeToken = tokens.find((item) => item.status === "active") ?? null;
  return `
    <section class="identity-enterprise identity-scim" aria-labelledby="identity-scim-title"
      aria-busy="${busy}">
      <div class="identity-section-heading identity-enterprise-heading">
        <div><p>Directory provisioning</p><h2 id="identity-scim-title">SCIM 目录</h2></div>
        <button class="identity-icon-command" type="button"
          data-identity-action="refresh-scim-directory" title="刷新 SCIM 目录状态"
          aria-label="刷新 SCIM 目录状态" ${busy ? "disabled" : ""}>&#8635;</button>
      </div>
      ${state.scimDirectoryError ? renderError(state.scimDirectoryError) : ""}
      ${summary ? `
        <dl class="identity-enterprise-summary">
          <div><dt>已链接成员</dt><dd>${summary.linked_user_count}</dd></div>
          <div><dt>活动 Group</dt><dd>${summary.linked_group_count}</dd></div>
          <div><dt>待处理邀请</dt><dd>${summary.pending_invitation_count}</dd></div>
          <div><dt>开放冲突</dt><dd>${openConflicts.length}</dd></div>
        </dl>
        ${renderScimTokens(tokens, canManage, busy)}
        ${renderScimMappings(groups, mappings, canManage, busy)}
        ${renderScimSyncs(syncs)}
        ${renderScimConflicts(conflicts, canManage, busy)}
        ${canManage ? renderScimManagement(groups, activeToken, busy) : ""}
      ` : '<p class="identity-empty">SCIM 目录状态暂时不可用。</p>'}
      ${state.scimBearerToken ? renderScimTokenDialog(state.scimBearerToken) : ""}
    </section>
  `;
}

function renderScimTokens(tokens, canManage, busy) {
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Bearer lifecycle</span><h3>访问 token</h3></div><span>${tokens.length} 个</span>
      </div>
      <div class="identity-enterprise-list">
        ${tokens.length > 0 ? tokens.map((token, index) => `
          <div class="identity-enterprise-row">
            <div class="identity-enterprise-primary">
              <strong>${escapeHtml(token.token_prefix)}...</strong>
              <span>到期 ${escapeHtml(formatDateTime(token.expires_at))}${token.last_used_at
                ? ` · 最近使用 ${escapeHtml(formatDateTime(token.last_used_at))}` : ""}</span>
            </div>
            <span class="identity-enterprise-state is-${escapeAttribute(token.status)}">
              ${escapeHtml(scimTokenStatusLabel(token.status))}
            </span>
            ${canManage && token.status === "active" ? `
              <button class="identity-quiet-command" type="button"
                data-identity-action="rotate-scim-token" data-scim-token-index="${index}"
                ${busy ? "disabled" : ""}>轮换</button>
            ` : ""}
          </div>
        `).join("") : '<p class="identity-empty">尚未创建 SCIM token。</p>'}
      </div>
    </div>
  `;
}

function renderScimMappings(groups, mappings, canManage, busy) {
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Least privilege</span><h3>Group 角色映射</h3></div>
        <span>${mappings.length} / ${groups.length}</span>
      </div>
      <div class="identity-enterprise-list">
        ${mappings.length > 0 ? mappings.map((mapping) => `
          <div class="identity-enterprise-row">
            <div class="identity-enterprise-primary">
              <strong>${escapeHtml(mapping.group_display_name)}</strong>
              <span>更新于 ${escapeHtml(formatDateTime(mapping.updated_at))}</span>
            </div>
            <span class="identity-enterprise-state is-${escapeAttribute(mapping.status)}">
              ${escapeHtml(scimRoleLabel(mapping.role_code))}
            </span>
          </div>
        `).join("") : '<p class="identity-empty">尚未配置 Group 角色映射。</p>'}
      </div>
      ${canManage && groups.length > 0 ? `
        <form class="identity-scim-mapping-form" data-scim-mapping-form>
          <label><span>Directory Group</span><select name="groupIndex" required>
            ${groups.map((group, index) => `<option value="${index}">${escapeHtml(group.display_name)}</option>`).join("")}
          </select></label>
          <label><span>Workspace 角色</span><select name="roleCode" required>
            ${["viewer", "auditor", "developer", "finance_admin", "legal_admin", "security_admin", "admin"]
              .map((role) => `<option value="${role}">${escapeHtml(scimRoleLabel(role))}</option>`)
              .join("")}
          </select></label>
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>保存映射</button>
        </form>
      ` : ""}
    </div>
  `;
}

function renderScimSyncs(syncs) {
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Sync activity</span><h3>最近同步</h3></div><span>${syncs.length} 次</span>
      </div>
      <div class="identity-scim-sync-list">
        ${syncs.length > 0 ? syncs.slice(0, 10).map((sync) => `
          <div class="identity-scim-sync-row">
            <time datetime="${escapeAttribute(sync.started_at)}">${escapeHtml(formatDateTime(sync.started_at))}</time>
            <strong>${escapeHtml(scimSyncStatusLabel(sync.status))}</strong>
            <span>新增 ${sync.created_count}</span><span>更新 ${sync.updated_count}</span>
            <span>停用 ${sync.disabled_count}</span><span>冲突 ${sync.conflict_count}</span>
          </div>
        `).join("") : '<p class="identity-empty">尚无同步记录。</p>'}
      </div>
    </div>
  `;
}

function renderScimConflicts(conflicts, canManage, busy) {
  return `
    <div class="identity-enterprise-block">
      <div class="identity-enterprise-subheading">
        <div><span>Manual recovery</span><h3>目录冲突</h3></div>
        <span>${conflicts.filter((item) => item.status === "open").length} 个开放</span>
      </div>
      <div class="identity-enterprise-list">
        ${conflicts.length > 0 ? conflicts.map((conflict, index) => `
          <div class="identity-scim-conflict-row">
            <div class="identity-enterprise-primary">
              <strong>${escapeHtml(scimConflictReasonLabel(conflict.reason_code))}</strong>
              <span>${escapeHtml(scimSubjectLabel(conflict.subject_type))} · ${escapeHtml(formatDateTime(conflict.created_at))}</span>
            </div>
            <span class="identity-enterprise-state is-${escapeAttribute(conflict.status)}">
              ${escapeHtml(scimConflictStatusLabel(conflict.status))}
            </span>
            ${canManage && conflict.status === "open" ? `
              <div class="identity-scim-conflict-actions">
                <button type="button" data-identity-action="resolve-scim-conflict"
                  data-scim-conflict-index="${index}" data-scim-resolution="keep_local"
                  ${busy ? "disabled" : ""}>保留本地</button>
                <button type="button" data-identity-action="resolve-scim-conflict"
                  data-scim-conflict-index="${index}" data-scim-resolution="accept_directory"
                  ${busy ? "disabled" : ""}>接受目录</button>
                <button type="button" data-identity-action="resolve-scim-conflict"
                  data-scim-conflict-index="${index}" data-scim-resolution="ignore_directory"
                  ${busy ? "disabled" : ""}>忽略目录</button>
              </div>
            ` : ""}
          </div>
        `).join("") : '<p class="identity-empty">当前没有目录冲突。</p>'}
      </div>
    </div>
  `;
}

function renderScimManagement(groups, activeToken, busy) {
  return `
    <div class="identity-enterprise-management identity-scim-management">
      ${activeToken ? "" : `
        <form class="identity-scim-token-form" data-scim-token-form>
          <div><span>Provisioning credential</span><h3>创建 SCIM token</h3></div>
          <label><span>有效期</span><select name="expiresInDays">
            <option value="30">30 天</option><option value="90" selected>90 天</option>
            <option value="180">180 天</option><option value="365">365 天</option>
          </select></label>
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>创建 token</button>
        </form>
      `}
      ${groups.length === 0 ? '<p class="identity-empty">Directory Group 同步后可配置角色映射。</p>' : ""}
    </div>
  `;
}

function renderScimTokenDialog(token) {
  return `
    <dialog class="identity-scim-token-dialog" data-scim-token-dialog open
      aria-labelledby="identity-scim-token-dialog-title">
      <div class="identity-scim-token-dialog-heading">
        <div><span>仅本次显示</span><h3 id="identity-scim-token-dialog-title">SCIM Bearer token</h3></div>
        <button class="identity-icon-command" type="button" data-identity-action="dismiss-scim-token"
          title="关闭" aria-label="关闭">&#215;</button>
      </div>
      <code>${escapeHtml(token)}</code>
      <div class="identity-scim-token-dialog-actions">
        <button class="identity-primary" type="button" data-identity-action="copy-scim-token">复制</button>
        <button type="button" data-identity-action="dismiss-scim-token">关闭</button>
      </div>
    </dialog>
  `;
}

function scimTokenStatusLabel(value) {
  return ({ active: "活动", rotating: "轮换宽限", revoked: "已撤销" })[value] ?? "未知";
}

function scimRoleLabel(value) {
  return ({
    admin: "管理员",
    developer: "开发者",
    security_admin: "安全管理员",
    finance_admin: "财务管理员",
    legal_admin: "法务管理员",
    auditor: "审计员",
    viewer: "只读成员",
  })[value] ?? "未知";
}

function scimSyncStatusLabel(value) {
  return ({ running: "同步中", completed: "已完成", failed: "失败", interrupted: "已中断" })[value]
    ?? "未知";
}

function scimConflictReasonLabel(value) {
  return ({
    owner_protected: "Owner 保护",
    human_membership: "人工成员关系",
    revision_stale: "目录版本过旧",
    duplicate_external_id: "外部标识重复",
    account_unverified: "账号未验证",
    role_conflict: "角色冲突",
  })[value] ?? "未知冲突";
}

function scimConflictStatusLabel(value) {
  return ({ open: "待处理", resolved: "已解决", ignored: "已忽略" })[value] ?? "未知";
}

function scimSubjectLabel(value) {
  return ({ user: "用户", group: "Group", membership: "成员关系" })[value] ?? "目录对象";
}

function renderMachineIdentitySection(state) {
  if (!state.machineIdentityVisible) return "";
  const accounts = Array.isArray(state.machineIdentity?.service_accounts)
    ? state.machineIdentity.service_accounts : [];
  const busy = state.machineIdentityBusy === true;
  const canManage = state.machineIdentityCanManage === true;
  return `
    <section class="identity-machine" aria-labelledby="identity-machine-title" aria-busy="${busy}">
      <div class="identity-section-heading">
        <div><p>Developer access</p><h2 id="identity-machine-title">机器身份</h2></div>
        <button class="identity-icon-command" type="button"
          data-identity-action="refresh-machine-identity" aria-label="刷新机器身份"
          title="刷新机器身份" ${busy ? "disabled" : ""}>↻</button>
      </div>
      ${state.machineIdentityError ? renderError(state.machineIdentityError) : ""}
      ${renderMachineAccountTable(accounts, canManage, busy)}
      ${canManage && state.machineIdentity ? renderMachineAccountForm(busy) : !canManage ? `
        <p class="identity-empty">当前职责仅可查看机器身份。</p>
      ` : ""}
      ${state.machineCredential ? renderMachineCredentialDialog(state.machineCredential) : ""}
    </section>
  `;
}

function renderMachineAccountTable(accounts, canManage, busy) {
  if (accounts.length === 0) {
    return '<p class="identity-empty">当前空间尚未创建 Service Account。</p>';
  }
  return `
    <div class="identity-machine-table-wrap">
      <table class="identity-machine-table">
        <thead>
          <tr><th>Service Account</th><th>Scope</th><th>约束</th><th>OAuth client</th><th>状态</th><th><span class="identity-sr-only">操作</span></th></tr>
        </thead>
        <tbody>
          ${accounts.map((account, index) => renderMachineAccountRow(
            account,
            index,
            canManage,
            busy,
          )).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderMachineAccountRow(account, index, canManage, busy) {
  const client = account.oauth_client;
  const network = account.ip_allowlist.length > 0
    ? account.ip_allowlist.join(", ") : "任意可信来源";
  const lastUsed = account.last_used_at || client?.last_used_at;
  return `
    <tr>
      <td data-label="Service Account">
        <strong>${escapeHtml(account.display_name)}</strong>
        <code>${escapeHtml(account.name)}</code>
      </td>
      <td data-label="Scope">
        <strong>${escapeHtml(machineScopeTemplateLabel(account.scope_template))}</strong>
        <span>${account.scopes.map((scope) => escapeHtml(scope)).join(" · ")}</span>
      </td>
      <td data-label="约束">
        <strong>${escapeHtml(network)}</strong>
        <span>${account.mtls_required ? "mTLS 必需" : "未要求 mTLS"} · ${account.token_ttl_seconds}s TTL</span>
      </td>
      <td data-label="OAuth client">
        ${client ? `
          <code>${escapeHtml(client.client_id)}</code>
          <span>${client.credential_prefix ? `${escapeHtml(client.credential_prefix)}…` : "无活动 secret"} · Generation ${client.credential_generation}</span>
        ` : '<span>尚未创建</span>'}
      </td>
      <td data-label="状态">
        <span class="identity-machine-state is-${escapeAttribute(account.status)}">
          ${escapeHtml(machineAccountStatusLabel(account.status))}
        </span>
        <time datetime="${escapeAttribute(lastUsed || account.updated_at)}">
          ${lastUsed ? `最近使用 ${escapeHtml(formatDateTime(lastUsed))}` : "尚未使用"}
        </time>
      </td>
      <td data-label="操作">
        <div class="identity-machine-actions">
          ${canManage && account.status === "active" && !client ? `
            <button type="button" data-identity-action="create-oauth-client"
              data-machine-account-index="${index}" ${busy ? "disabled" : ""}>创建 client</button>
          ` : ""}
          ${canManage && account.status === "active" && client && client.status !== "revoked" ? `
            <button type="button" data-identity-action="rotate-machine-credential"
              data-machine-account-index="${index}" ${busy ? "disabled" : ""}>轮换</button>
          ` : ""}
          ${canManage && account.status !== "revoked" ? `
            <button class="identity-danger-command" type="button"
              data-identity-action="revoke-machine-account"
              data-machine-account-index="${index}" ${busy ? "disabled" : ""}>撤销</button>
          ` : ""}
        </div>
      </td>
    </tr>
  `;
}

function renderMachineAccountForm(busy) {
  return `
    <form class="identity-machine-form" data-machine-account-form>
      <div class="identity-machine-form-heading">
        <span>Workspace automation</span>
        <h3>创建 Service Account</h3>
      </div>
      <label><span>名称</span><input name="name" type="text" minlength="2" maxlength="80"
        pattern="[a-z0-9][a-z0-9_\\-]+" autocomplete="off" required /></label>
      <label><span>显示名称</span><input name="displayName" type="text" minlength="2"
        maxlength="120" autocomplete="off" required /></label>
      <label><span>Scope 模板</span><select name="scopeTemplate" required>
        <option value="provider_reader">Provider 只读</option>
        <option value="observability_reader">请求日志只读</option>
        <option value="compliance_collector">合规证据采集</option>
      </select></label>
      <label><span>Token TTL</span><select name="tokenTtlSeconds" required>
        <option value="300">5 分钟</option><option value="900" selected>15 分钟</option>
        <option value="1800">30 分钟</option><option value="3600">60 分钟</option>
      </select></label>
      <label class="is-wide"><span>IP allowlist</span>
        <textarea name="ipAllowlist" rows="2" maxlength="1040" autocomplete="off"></textarea>
      </label>
      <label class="is-wide"><span>mTLS SHA-256 fingerprint</span>
        <input name="mtlsFingerprint" type="text" minlength="64" maxlength="64"
          pattern="[0-9a-fA-F]{64}" autocomplete="off" spellcheck="false" />
      </label>
      <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>创建</button>
    </form>
  `;
}

function renderMachineCredentialDialog(credential) {
  return `
    <dialog class="identity-machine-dialog" data-machine-credential-dialog tabindex="-1"
      aria-labelledby="identity-machine-credential-title">
      <div class="identity-machine-dialog-heading">
        <div><span>仅本次显示</span><h3 id="identity-machine-credential-title">OAuth client credential</h3></div>
        <button class="identity-icon-command" type="button"
          data-identity-action="dismiss-machine-credential" title="关闭" aria-label="关闭">&#215;</button>
      </div>
      <dl>
        <div><dt>Service Account</dt><dd>${escapeHtml(credential.displayName)}</dd></div>
        <div><dt>Generation</dt><dd>${Number(credential.generation)}</dd></div>
        <div class="is-wide"><dt>Client ID</dt>
          <dd><code data-machine-credential-value>${escapeHtml(credential.clientId)}</code></dd></div>
        <div class="is-wide"><dt>Client secret</dt>
          <dd><code data-machine-credential-value>${escapeHtml(credential.clientSecret)}</code></dd></div>
        <div class="is-wide"><dt>到期时间</dt>
          <dd>${credential.expiresAt ? escapeHtml(formatDateTime(credential.expiresAt)) : "-"}</dd></div>
      </dl>
      <div class="identity-machine-dialog-actions">
        <button type="button" data-identity-action="copy-machine-client-id">复制 Client ID</button>
        <button class="identity-primary" type="button"
          data-identity-action="copy-machine-client-secret">复制 secret</button>
        <button type="button" data-identity-action="dismiss-machine-credential">关闭</button>
      </div>
    </dialog>
  `;
}

function machineScopeTemplateLabel(value) {
  return ({
    provider_reader: "Provider 读取",
    observability_reader: "可观测性读取",
    compliance_collector: "合规证据采集",
  })[value]
    ?? "机器只读";
}

function machineAccountStatusLabel(value) {
  return ({ active: "活动", disabled: "已停用", revoked: "已撤销" })[value] ?? "未知";
}

function renderMfaSection(state) {
  const overview = state.identitySecurity;
  const factors = Array.isArray(overview?.factors) ? overview.factors : [];
  const activeFactors = factors.filter((factor) => factor.status === "active");
  const devices = Array.isArray(overview?.trusted_devices) ? overview.trusted_devices : [];
  const method = new Set(["totp", "webauthn", "recovery_codes"])
    .has(state.mfaEnrollmentMethod)
    ? state.mfaEnrollmentMethod
    : "totp";
  const enrollment = state.mfaEnrollment;
  return `
    <section class="identity-mfa identity-security-controls" aria-labelledby="identity-mfa-title" aria-busy="${state.identitySecurityBusy === true}">
      <div class="identity-section-heading">
        <div>
          <p>身份保护</p>
          <h2 id="identity-mfa-title">验证因子与恢复</h2>
        </div>
        ${state.platformAccess ? '<a class="identity-primary identity-platform-link" href="/platform.html">进入平台工作台</a>' : ""}
      </div>
      ${renderError(state.identitySecurityError)}
      ${overview ? `
        <div class="identity-security-summary">
          <div><span>活跃因子</span><strong>${activeFactors.length}</strong></div>
          <div><span>恢复码</span><strong>${escapeHtml(recoveryCodeStateLabel(overview.recovery_code_set))}</strong></div>
          <div><span>受信设备</span><strong>${devices.filter((device) => device.status === "active").length}</strong></div>
        </div>
        <div class="identity-factor-list">
          ${factors.length > 0
            ? factors.map((factor, index) => renderMfaFactor(
              factor,
              index,
              activeFactors.length,
              state,
            )).join("")
            : '<p class="identity-empty">尚未配置验证因子。</p>'}
        </div>
      ` : ""}
      <div class="identity-security-subheading">
        <div><span>添加保护方式</span><h3>新验证因子</h3></div>
        <div class="identity-method-segmented" role="group" aria-label="验证方式">
          ${renderMfaMethodButton("totp", "认证器", method, state.identitySecurityBusy)}
          ${renderMfaMethodButton("webauthn", "安全密钥", method, state.identitySecurityBusy)}
          ${renderMfaMethodButton("recovery_codes", "恢复码", method, state.identitySecurityBusy)}
        </div>
      </div>
      ${enrollment ? renderTotpEnrollment(enrollment, state.identitySecurityBusy) : `
        <form class="identity-mfa-start" data-identity-mfa-enrollment-form autocomplete="off">
          <input type="hidden" name="method" value="${escapeAttribute(method)}" />
          <label>
            <span>当前密码</span>
            <input name="currentPassword" type="password" autocomplete="current-password" required />
          </label>
          <button class="identity-primary" type="submit" ${state.identitySecurityBusy ? "disabled" : ""}>${escapeHtml(mfaActionLabel(method, overview?.recovery_code_set))}</button>
        </form>
      `}
      ${renderControlledRecovery(
        overview?.recovery_challenge ?? null,
        activeFactors.length > 0
          && overview?.recovery_code_set?.status === "active"
          && overview.recovery_code_set.remaining_count > 0,
        state.identitySecurityBusy,
      )}
      ${renderTrustedDevices(devices, activeFactors, state.identitySecurityBusy)}
      ${renderRecoveryCodesDialog(state.recoveryCodes)}
    </section>`;
}

function renderControlledRecovery(challenge, available, busy) {
  if (challenge) {
    return `
      <div class="identity-controlled-recovery" aria-labelledby="identity-controlled-recovery-title">
        <div class="identity-security-subheading">
          <div><span>24 小时安全延迟</span><h3 id="identity-controlled-recovery-title">受控账号恢复</h3></div>
        </div>
        <div class="identity-recovery-window">
          <div><span>可完成时间</span><strong>${escapeHtml(formatDateTime(challenge.ready_at))}</strong></div>
          <div><span>失效时间</span><strong>${escapeHtml(formatDateTime(challenge.expires_at))}</strong></div>
        </div>
        <form class="identity-controlled-recovery-form" data-identity-recovery-complete-form autocomplete="off">
          <label><span>账号通知码</span><input name="notificationCode" type="password" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>
          <label><span>未使用恢复码</span><input name="recoveryCode" type="password" autocomplete="one-time-code" pattern="MHRC-[2-9A-HJ-NP-Z]{5}-[2-9A-HJ-NP-Z]{5}" maxlength="16" required /></label>
          <label class="identity-confirm"><input name="confirmed" type="checkbox" required /><span>撤销现有验证状态</span></label>
          <button class="identity-danger-command" type="submit" ${busy ? "disabled" : ""}>完成恢复</button>
        </form>
      </div>`;
  }
  return `
    <div class="identity-controlled-recovery" aria-labelledby="identity-controlled-recovery-title">
      <div class="identity-security-subheading">
        <div><span>24 小时安全延迟</span><h3 id="identity-controlled-recovery-title">受控账号恢复</h3></div>
      </div>
      <form class="identity-controlled-recovery-form is-begin" data-identity-recovery-begin-form autocomplete="off">
        <label><span>当前密码</span><input name="currentPassword" type="password" autocomplete="current-password" required /></label>
        <label class="identity-confirm"><input name="confirmed" type="checkbox" required /><span>撤销现有会话与验证因子</span></label>
        <button class="identity-danger-command" type="submit" ${available && !busy ? "" : "disabled"}>开始恢复</button>
      </form>
    </div>`;
}

function renderMfaFactor(factor, index, activeCount, state) {
  const canDisable = factor.status === "active" && activeCount > 1;
  const selected = state.factorDisableIndex !== null
    && state.factorDisableIndex !== undefined
    && Number(state.factorDisableIndex) === index;
  return `
    <div class="identity-factor-row">
      <div>
        <strong>${escapeHtml(mfaFactorLabel(factor.factor_type))}</strong>
        <span>${escapeHtml(mfaFactorStatusLabel(factor.status))} · ${escapeHtml(formatDateTime(factor.verified_at ?? factor.created_at))}</span>
      </div>
      <button class="identity-danger-command" type="button" data-identity-action="select-factor-disable" data-factor-index="${index}" ${canDisable && !state.identitySecurityBusy ? "" : "disabled"}>停用</button>
      ${selected ? `
        <form class="identity-factor-disable-form" data-identity-factor-disable-form autocomplete="off">
          <label><span>当前密码</span><input name="currentPassword" type="password" autocomplete="current-password" required /></label>
          <div>
            <button class="identity-quiet-command" type="button" data-identity-action="cancel-factor-disable">取消</button>
            <button class="identity-danger-command" type="submit">确认停用</button>
          </div>
        </form>
      ` : ""}
    </div>
  `;
}

function renderMfaMethodButton(value, label, selected, busy) {
  return `<button type="button" data-identity-action="set-mfa-method" data-mfa-method="${value}" aria-pressed="${selected === value}" ${busy ? "disabled" : ""}>${escapeHtml(label)}</button>`;
}

function renderTotpEnrollment(enrollment, busy) {
  return `
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
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>验证并启用</button>
      </form>
    </div>
  `;
}

function renderTrustedDevices(devices, activeFactors, busy) {
  const activeTotp = activeFactors.some((factor) => factor.factor_type === "totp");
  return `
    <div class="identity-security-subheading"><div><span>设备信任</span><h3>受信设备</h3></div></div>
    <div class="identity-trusted-device-list">
      ${devices.length > 0 ? devices.map((device, index) => `
        <div class="identity-trusted-device-row">
          <div><strong>${escapeHtml(device.label)}</strong><span>${escapeHtml(deviceLabel(device.device_family))} · ${escapeHtml(trustedDeviceStatusLabel(device.status))}</span><small>有效至 ${escapeHtml(formatDateTime(device.expires_at))}</small></div>
          <button class="identity-danger-command" type="button" data-identity-action="revoke-trusted-device" data-trusted-device-index="${index}" ${device.status === "active" && !busy ? "" : "disabled"}>撤销</button>
        </div>
      `).join("") : '<p class="identity-empty">尚无受信设备。</p>'}
    </div>
    <form class="identity-trusted-device-form" data-identity-trusted-device-form autocomplete="off">
      <label><span>设备名称</span><input name="label" maxlength="80" autocomplete="off" required /></label>
      <label><span>动态验证码</span><input name="totpCode" type="password" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></label>
      <button class="identity-primary" type="submit" ${activeTotp && !busy ? "" : "disabled"}>信任当前设备</button>
    </form>
  `;
}

function renderRecoveryCodesDialog(codes) {
  if (!Array.isArray(codes) || codes.length === 0) return "";
  return `
    <dialog class="identity-recovery-dialog" data-identity-recovery-dialog open aria-labelledby="identity-recovery-dialog-title">
      <div class="identity-recovery-heading"><span>一次性凭据</span><h3 id="identity-recovery-dialog-title">恢复码</h3></div>
      <ol>${codes.map((code) => `<li><code>${escapeHtml(code)}</code></li>`).join("")}</ol>
      <button class="identity-primary" type="button" data-identity-action="close-recovery-codes">完成</button>
    </dialog>
  `;
}

function mfaActionLabel(method, recoverySet) {
  if (method === "webauthn") return "注册安全密钥";
  if (method === "recovery_codes") return recoverySet ? "重新生成恢复码" : "生成恢复码";
  return "创建认证器密钥";
}

function mfaFactorLabel(value) {
  return value === "webauthn" ? "安全密钥" : "认证器应用";
}

function mfaFactorStatusLabel(value) {
  return ({ active: "已启用", pending: "待验证", disabled: "已停用" })[value] ?? "不可用";
}

function recoveryCodeStateLabel(value) {
  if (!value) return "未生成";
  if (value.status === "active") return `${value.remaining_count} 个可用`;
  return ({ exhausted: "已用尽", superseded: "已更新", revoked: "已撤销" })[value.status] ?? "不可用";
}

function trustedDeviceStatusLabel(value) {
  return ({ active: "受信", revoked: "已撤销", expired: "已过期" })[value] ?? "不可用";
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

function isNewOrganizationAccount(state) {
  return state.workspaceDirectoryReady === true && !state.workspaceContext
    && !state.platformAccess && !state.hasPendingInvitation
    && (state.workspaces?.length ?? 0) === 0 && (state.workspaceChoices?.length ?? 0) === 0;
}

function renderOrganizationStart(container, state) {
  const creating = state.workspaceView === "create";
  container.innerHTML = `
    <div class="identity-organization-shell">
      <header class="identity-organization-topbar">
        <a class="identity-wordmark" href="/">${renderIdentityBrandMark()}<strong>${escapeHtml(publicBrand.product_name)}</strong></a>
        <nav aria-label="账号操作">
          <button class="identity-quiet-command" type="button" data-identity-action="show-account-security" ${state.workspaceCreating ? "disabled" : ""}>账号安全</button>
          <button class="identity-quiet-command" type="button" data-identity-action="logout">退出登录</button>
        </nav>
      </header>
      <main class="identity-organization-main" aria-labelledby="organization-start-title">
        <p class="identity-organization-account">${escapeHtml(state.account.display_name || state.account.email)}</p>
        <h1 id="organization-start-title">${creating ? "创建组织" : "你的组织，从这里开始"}</h1>
        ${renderFeedback(state.feedback)}${renderError(state.error)}
        ${creating ? renderWorkspaceCreateForm(state.workspaceDraft, { organizationOnly: true, busy: state.workspaceCreating })
    : `<button class="identity-primary identity-organization-create" type="button" data-identity-action="show-workspace-create"><span>创建组织</span>${icon("arrow-right")}</button>`}
      </main>
    </div>`;
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
      ${creating ? renderWorkspaceCreateForm(state.workspaceDraft, { busy: state.workspaceCreating }) : renderWorkspaceList(workspaces)}
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

function renderWorkspaceCreateForm(draft = {}, { organizationOnly = false, busy = false } = {}) {
  const kind = draft?.kind === "personal" ? "personal" : "organization";
  const disabled = busy ? "disabled" : "";
  return `
    <form class="identity-workspace-form" data-workspace-create-form aria-busy="${busy}">
      <input type="hidden" name="kind" value="${kind}" />
      ${organizationOnly ? "" : `<div class="identity-workspace-kind" role="group" aria-label="空间类型">
        <button type="button" data-identity-action="set-workspace-kind" data-workspace-kind="personal" aria-pressed="${kind === "personal"}" ${disabled}>个人</button>
        <button type="button" data-identity-action="set-workspace-kind" data-workspace-kind="organization" aria-pressed="${kind === "organization"}" ${disabled}>企业</button>
      </div>`}
      ${
        kind === "organization"
          ? `
            <label>
              <span>组织名称</span>
              <input name="name" type="text" maxlength="160" autocomplete="organization" value="${escapeAttribute(draft?.name ?? "")}" required autofocus ${disabled} />
            </label>
            <label>
              <span>组织标识</span>
              <input name="slug" type="text" minlength="3" maxlength="48" pattern="[a-z0-9](?:[a-z0-9\\-]{1,46}[a-z0-9])" value="${escapeAttribute(draft?.slug ?? "")}" required ${disabled} />
            </label>
          `
          : ""
      }
      <label>
        <span>服务区域</span>
        <select name="region" required ${disabled}>
          ${renderOption("cn-north-1", "华北", draft?.region)}
          ${renderOption("cn-east-1", "华东", draft?.region)}
        </select>
      </label>
      <label>
        <span>服务模式</span>
        <select name="serviceMode" required ${disabled}>
          ${renderOption("internal_only", "内部服务", draft?.serviceMode)}
          ${renderOption("public_service", "公共服务", draft?.serviceMode)}
        </select>
      </label>
      <div class="identity-workspace-actions">
        <button class="identity-quiet-command" type="button" data-identity-action="cancel-workspace-create" ${disabled}>取消</button>
        <button class="identity-primary" type="submit" ${disabled}>${busy ? "正在创建" : kind === "organization" ? "创建组织" : "创建空间"}</button>
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
        <span>本机 · ${escapeHtml(sessionAssuranceLabel(session))} · 最近使用 ${escapeHtml(formatDateTime(session.last_seen_at))}</span>
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
        <span>${escapeHtml(sessionAssuranceLabel(session))} · 最近使用 ${escapeHtml(formatDateTime(session.last_seen_at))}</span>
        <small>有效至 ${escapeHtml(formatDateTime(session.expires_at))}</small>
      </div>
      <button class="identity-danger-command" type="button" data-identity-action="revoke-session" data-session-index="${index}">撤销</button>
    </article>
  `;
}

function sessionAssuranceLabel(session) {
  const assurance = ({ password: "密码保证", mfa: "MFA 保证", recovery: "恢复保证" })[
    session.assurance_level
  ] ?? "密码保证";
  return session.trusted_device_id ? `${assurance} · 受信设备` : assurance;
}

function renderFeedback(feedback) {
  return feedback ? `<p class="identity-feedback" role="status">${escapeHtml(feedback)}</p>` : "";
}

function renderAccessBrand(titleId, title) {
  return `
    <header class="identity-brand">
      ${renderIdentityBrandMark()}
      <div>
        <p>${escapeHtml(publicBrand.product_name)}</p>
        <h1 id="${escapeAttribute(titleId)}">${escapeHtml(title)}</h1>
      </div>
    </header>
  `;
}

function renderIdentityBrandMark() {
  if (publicBrand.logo_path) {
    return `<span class="identity-brand-mark has-image"><img src="${escapeAttribute(publicBrand.logo_path)}" alt="${escapeAttribute(publicBrand.product_name)} Logo" /></span>`;
  }
  const initial = Array.from(publicBrand.product_name)[0]?.toUpperCase() ?? "M";
  return `<span class="identity-brand-mark" aria-hidden="true">${escapeHtml(initial)}</span>`;
}

function normalizedPublicBrand(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return DEFAULT_PUBLIC_BRAND;
  const productName = safeBrandText(value.product_name, 80) || DEFAULT_PUBLIC_BRAND.product_name;
  return Object.freeze({
    product_name: productName,
    primary_color: safeBrandColor(value.primary_color) || DEFAULT_PUBLIC_BRAND.primary_color,
    secondary_color: safeBrandColor(value.secondary_color) || DEFAULT_PUBLIC_BRAND.secondary_color,
    login_title: safeBrandText(value.login_title, 120) || DEFAULT_PUBLIC_BRAND.login_title,
    login_message: safeBrandText(value.login_message, 240),
    logo_path: safeBrandAssetPath(value.logo_path),
    favicon_path: safeBrandAssetPath(value.favicon_path),
  });
}

function safeBrandText(value, maximum) {
  const text = String(value ?? "");
  return text && new TextEncoder().encode(text).length <= maximum
    && !/[\u0000-\u001f\u007f-\u009f]/u.test(text) ? text : "";
}

function safeBrandColor(value) {
  const color = String(value ?? "");
  return /^#[0-9A-F]{6}$/u.test(color) ? color : "";
}

function safeBrandAssetPath(value) {
  const path = String(value ?? "");
  return /^\/branding\/public\/assets\/bas_[0-9a-f]{32}$/u.test(path) ? path : null;
}

function renderError(error) {
  if (!error) return "";
  return `<div class="identity-error" role="alert"><span>${escapeHtml(error.message ?? error)}</span>${error.requestId && error.requestId !== "-" ? `<small>request_id: ${escapeHtml(error.requestId)}</small>` : ""}</div>`;
}

/** 将企业认证固定状态映射为页面文案，未知值保持不可用而不显示原文。 */
function businessVerificationStatusLabel(value) {
  return ({
    not_submitted: "未提交",
    submitted: "待审核",
    in_review: "审核中",
    approved: "已通过",
    rejected: "已拒绝",
    expired: "已过期",
  })[value] ?? "状态不可用";
}

/** 将法定主体枚举映射为显示文本，未知主体不透传后端自由字段。 */
function entityTypeLabel(value) {
  return ({ company: "企业", nonprofit: "非营利组织", government: "政府机构" })[value]
    ?? "未知主体";
}

/** 汇总证据扫描状态，只显示数量和聚合结论，不显示对象引用或文件地址。 */
function scanSummaryLabel(value) {
  if (!value || !value.evidence_count) return "无证据";
  if (value.malware_status === "passed" && value.dlp_status === "passed") {
    return `${formatIdentityNumber(value.evidence_count)} 份 · 已通过`;
  }
  if (value.malware_status === "blocked" || value.dlp_status === "blocked") return "已阻断";
  if (value.malware_status === "pending" || value.dlp_status === "pending") return "扫描中";
  return "扫描失败";
}

/** 将不可变合同状态映射为最小用户文案，不渲染合同正文或内部标识。 */
function contractStatusLabel(value) {
  return ({ verified: "主体已验证", signed: "已签约", expired: "已过期" })[value] ?? "未生成";
}

/** 将开票资料验证状态映射为固定文案，资料变化后明确提示重新验证。 */
function billingVerificationLabel(value) {
  return ({ unverified: "未验证", verified: "已验证", stale: "需重新验证" })[value]
    ?? "未验证";
}

/** 汇总成对字段的配置状态，页面只显示是否配置而不回填任何明文。 */
function configuredPair(first, second) {
  if (first && second) return "均已配置";
  if (first || second) return "部分配置";
  return "未配置";
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

function shortRiskReference(value) {
  const text = String(value ?? "");
  return /^rch_[0-9a-f]{32}$/u.test(text) ? `${text.slice(0, 12)}...${text.slice(-6)}` : "-";
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
