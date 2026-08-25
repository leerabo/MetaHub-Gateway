// 账号安全渲染层只展示后端脱敏投影，企业认证表单可见性不构成服务端授权。
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
        ${renderOnboardingSection(state)}
        ${renderSaasSubscriptionSection(state)}
        ${renderPaymentCommerceSection(state)}
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
        ${renderEnterpriseIdentitySection(state)}
        ${renderScimDirectorySection(state)}
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
      <a class="identity-wordmark" href="/identity.html" aria-label="MetaHub 账号安全首页">
        <span class="identity-brand-mark" aria-hidden="true">M</span>
        <strong>MetaHub</strong>
      </a>
      ${renderWorkspaceSelector(state)}
      <div class="identity-topbar-actions">
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

function renderSaasSubscriptionSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || !new Set(["owner", "finance_admin"]).has(context.role)) return "";
  const plans = Array.isArray(state.saasCatalog?.plans) ? state.saasCatalog.plans : [];
  const versions = plans.flatMap((plan) => (plan.versions ?? []).map((version) => ({
    ...version,
    plan_code: plan.plan_code,
    display_name: plan.display_name,
  })));
  const response = state.subscription ?? {};
  const current = response.subscription ?? null;
  const entitlement = response.entitlement ?? null;
  const trial = response.trial ?? null;
  const order = response.latest_order ?? null;
  const currentPlan = versions.find((version) => version.plan_version_id === current?.plan_version_id);
  const pendingPlan = versions.find(
    (version) => version.plan_version_id === current?.pending_plan_version_id,
  );
  const candidates = versions.filter((version) => (
    version.status === "published"
    && version.plan_version_id !== current?.plan_version_id
    && version.plan_version_id !== current?.pending_plan_version_id
  ));
  return `
    <section class="identity-subscription" aria-labelledby="identity-subscription-title">
      <div class="identity-section-heading">
        <div><p>SaaS 商业化</p><h2 id="identity-subscription-title">套餐与权益</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-subscription" aria-label="刷新套餐与权益" title="刷新套餐与权益" ${state.subscriptionBusy ? "disabled" : ""}>↻</button>
      </div>
      ${state.subscriptionError ? renderError(state.subscriptionError) : ""}
      ${current ? renderCurrentSubscription(current, currentPlan, pendingPlan, trial, order) : renderNoSubscription()}
      ${entitlement ? renderEntitlementFacts(entitlement.entitlements) : ""}
      <div class="identity-saas-plan-list" aria-label="可用套餐">
        ${plans.map((plan) => renderSaasPlan(plan, current?.plan_version_id, current?.pending_plan_version_id)).join("") || '<p class="identity-empty">当前没有已发布套餐。</p>'}
      </div>
      ${renderSubscriptionCommand(current, candidates, state.subscriptionBusy)}
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
              ? `<span class="identity-commerce-status is-${escapeAttribute(invoice.status)}">${escapeHtml(invoiceStatusLabel(invoice.status))}</span>`
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

function renderPaymentCommerceSection(state) {
  const context = state.workspaceContext ?? null;
  if (!context || !new Set(["owner", "finance_admin"]).has(context.role)) return "";
  const response = state.billingOrders ?? {};
  const orders = Array.isArray(response.orders) ? response.orders : [];
  const intents = Array.isArray(response.payment_intents) ? response.payment_intents : [];
  const refunds = Array.isArray(response.refunds) ? response.refunds : [];
  const dunning = Array.isArray(response.dunning_cases) ? response.dunning_cases : [];
  const payable = orders.filter((order) => order.status === "pending_payment" && !intents.some(
    (intent) => intent.order_id === order.order_id
      && new Set(["requires_action", "processing", "succeeded"]).has(intent.status),
  ));
  const refundable = orders.filter(
    (order) => order.captured_amount_minor > order.refunded_amount_minor,
  );
  return `
    <section class="identity-payment-commerce" aria-labelledby="identity-payment-commerce-title" aria-busy="${state.paymentBusy === true}">
      <div class="identity-section-heading">
        <div><p>商业交易</p><h2 id="identity-payment-commerce-title">订单、支付与退款</h2></div>
        <button class="identity-icon-command" type="button" data-identity-action="refresh-payment-commerce" aria-label="刷新订单与支付" title="刷新订单与支付" ${state.paymentBusy ? "disabled" : ""}>↻</button>
      </div>
      ${state.paymentError ? renderError(state.paymentError) : ""}
      ${dunning.length ? renderDunningFacts(dunning) : ""}
      ${orders.length ? renderOrderTable(orders) : '<p class="identity-empty">当前没有订阅订单。</p>'}
      ${intents.length || refunds.length ? renderPaymentHistory(intents, refunds) : ""}
      <div class="identity-payment-commands">
        ${renderPaymentIntentForm(payable, state.paymentBusy)}
        ${renderRefundForm(refundable, state.paymentBusy)}
      </div>
    </section>
  `;
}

function renderOrderTable(orders) {
  return `
    <div class="identity-payment-table-wrap">
      <table class="identity-payment-table">
        <thead><tr><th>订单</th><th>类型</th><th>状态</th><th>应付</th><th>实收</th><th>已退</th></tr></thead>
        <tbody>${orders.map((order) => `
          <tr>
            <td data-label="订单"><strong>${escapeHtml(order.order_id)}</strong><span>${escapeHtml(formatIdentityDate(order.created_at))}</span></td>
            <td data-label="类型">${escapeHtml(orderKindLabel(order.order_kind))}</td>
            <td data-label="状态"><span class="identity-commerce-status is-${escapeAttribute(order.status)}">${escapeHtml(commerceStatusLabel(order.status))}</span></td>
            <td data-label="应付">${escapeHtml(formatMinorMoney(order.amount_minor, order.currency))}</td>
            <td data-label="实收">${escapeHtml(formatMinorMoney(order.captured_amount_minor, order.currency))}</td>
            <td data-label="已退">${escapeHtml(formatMinorMoney(order.refunded_amount_minor, order.currency))}</td>
          </tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderPaymentHistory(intents, refunds) {
  return `
    <div class="identity-payment-history">
      <div><span>支付意图</span><strong>${formatIdentityNumber(intents.length)}</strong><small>${escapeHtml(latestCommerceState(intents, "暂无支付"))}</small></div>
      <div><span>退款申请</span><strong>${formatIdentityNumber(refunds.length)}</strong><small>${escapeHtml(latestCommerceState(refunds, "暂无退款"))}</small></div>
    </div>
    ${refunds.length ? `<div class="identity-payment-table-wrap"><table class="identity-payment-table identity-refund-table"><thead><tr><th>退款</th><th>订单</th><th>原因</th><th>金额</th><th>状态</th></tr></thead><tbody>${refunds.map((refund) => `<tr><td data-label="退款"><strong>${escapeHtml(refund.refund_id)}</strong><span>${escapeHtml(formatIdentityDate(refund.requested_at))}</span></td><td data-label="订单">${escapeHtml(refund.order_id)}</td><td data-label="原因">${escapeHtml(refundReasonLabel(refund.reason_code))}</td><td data-label="金额">${escapeHtml(formatMinorMoney(refund.amount_minor, refund.currency))}</td><td data-label="状态"><span class="identity-commerce-status is-${escapeAttribute(refund.status)}">${escapeHtml(commerceStatusLabel(refund.status))}</span></td></tr>`).join("")}</tbody></table></div>` : ""}
  `;
}

function renderDunningFacts(cases) {
  const active = cases.filter((item) => item.status === "open");
  return `<div class="identity-dunning-strip"><div><span>催收案件</span><strong>${formatIdentityNumber(cases.length)}</strong></div><div><span>待处理</span><strong>${formatIdentityNumber(active.length)}</strong></div><div><span>下一动作</span><strong>${escapeHtml(formatIdentityDate(active[0]?.next_action_at))}</strong></div></div>`;
}

function renderPaymentIntentForm(orders, busy) {
  if (!orders.length) return '<div class="identity-payment-command-empty"><strong>支付意图</strong><span>当前没有可发起支付的订单。</span></div>';
  return `
    <form data-payment-intent-form autocomplete="off">
      <div><strong>发起支付</strong><span>一次性渠道凭据不会保存在页面状态中。</span></div>
      <label><span>待支付订单</span><select name="orderId" required>${orders.map((order) => `<option value="${escapeAttribute(order.order_id)}">${escapeHtml(orderKindLabel(order.order_kind))} · ${escapeHtml(formatMinorMoney(order.amount_minor, order.currency))}</option>`).join("")}</select></label>
      <label><span>支付渠道</span><select name="provider"><option value="mockpay">MockPay</option><option value="cloudpay">CloudPay</option></select></label>
      <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>创建支付意图</button>
    </form>
  `;
}

function renderRefundForm(orders, busy) {
  if (!orders.length) return '<div class="identity-payment-command-empty"><strong>申请退款</strong><span>当前没有可退款的已收款订单。</span></div>';
  const maximum = Math.max(...orders.map(
    (order) => order.captured_amount_minor - order.refunded_amount_minor,
  ));
  return `
    <form data-refund-request-form autocomplete="off">
      <div><strong>申请退款</strong><span>金额使用原币最小单位，提交后由平台账务审核。</span></div>
      <label><span>已收款订单</span><select name="orderId" required>${orders.map((order) => `<option value="${escapeAttribute(order.order_id)}">可退 ${escapeHtml(formatMinorMoney(order.captured_amount_minor - order.refunded_amount_minor, order.currency))} · ${escapeHtml(order.order_id)}</option>`).join("")}</select></label>
      <label><span>退款金额（最小单位）</span><input name="amountMinor" type="number" min="1" max="${maximum}" step="1" required /></label>
      <label><span>原因</span><select name="reasonCode"><option value="customer_request">客户申请</option><option value="service_failure">服务故障</option><option value="duplicate_charge">重复扣款</option></select></label>
      <label class="identity-subscription-trial"><input name="confirmed" type="checkbox" required /><span>确认提交审核</span></label>
      <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>提交退款申请</button>
    </form>
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
        <label class="is-wide"><span>受控对象引用</span><input name="objectReference" pattern="obj_[A-Za-z0-9._:-]+" maxlength="256" autocomplete="off" required /></label>
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
  return '<div class="identity-subscription-empty"><strong>暂无计量周期</strong><span>首条可计量 Usage 到达后会生成账单周期</span></div>';
}

function renderNoSubscription() {
  return '<div class="identity-subscription-empty"><strong>尚未订阅</strong><span>未创建 Workspace 订阅记录</span></div>';
}

function renderCurrentSubscription(current, currentPlan, pendingPlan, trial, order) {
  return `
    <dl class="identity-subscription-summary">
      <div><dt>当前套餐</dt><dd>${escapeHtml(currentPlan?.display_name ?? current.plan_version_id)}</dd></div>
      <div><dt>订阅状态</dt><dd>${escapeHtml(subscriptionStatusLabel(current.status))}</dd></div>
      <div><dt>周期结束</dt><dd>${escapeHtml(formatIdentityDate(current.current_period_ends_at))}</dd></div>
      <div><dt>待生效套餐</dt><dd>${escapeHtml(pendingPlan?.display_name ?? (current.pending_plan_version_id ? "已登记" : "无"))}</dd></div>
      <div><dt>试用结束</dt><dd>${escapeHtml(formatIdentityDate(trial?.ends_at))}</dd></div>
      <div><dt>最近订单</dt><dd>${escapeHtml(order ? subscriptionStatusLabel(order.status) : "无")}</dd></div>
    </dl>
  `;
}

function renderEntitlementFacts(entitlements = {}) {
  return `
    <div class="identity-entitlement-strip" aria-label="当前权益">
      ${entitlementFact("月请求", formatIdentityNumber(entitlements.gateway_requests_per_month))}
      ${entitlementFact("席位", formatIdentityNumber(entitlements.workspace_seats))}
      ${entitlementFact("模型别名", formatIdentityNumber(entitlements.model_aliases))}
      ${entitlementFact("留存", `${formatIdentityNumber(entitlements.retention_days)} 天`)}
      ${entitlementFact("优先支持", entitlements.priority_support ? "包含" : "不包含")}
    </div>
  `;
}

function entitlementFact(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function renderSaasPlan(plan, currentPlanVersionId, pendingPlanVersionId) {
  const version = (plan.versions ?? []).find((item) => item.status === "published")
    ?? plan.versions?.[0];
  if (!version) return "";
  const marker = version.plan_version_id === currentPlanVersionId
    ? "当前"
    : version.plan_version_id === pendingPlanVersionId ? "待生效" : "";
  return `
    <article class="identity-saas-plan-row">
      <div><strong>${escapeHtml(plan.display_name)}</strong><span>${escapeHtml(plan.plan_code)} · v${formatIdentityNumber(version.version_number)}</span></div>
      <div><strong>${escapeHtml(formatPlanPrice(version))}</strong><span>${escapeHtml(billingPeriodLabel(version.billing_period))}</span></div>
      <div><strong>${formatIdentityNumber(version.entitlements?.workspace_seats)} 席位</strong><span>${formatIdentityNumber(version.entitlements?.gateway_requests_per_month)} 次/月</span></div>
      <div>${marker ? `<span class="identity-plan-marker">${escapeHtml(marker)}</span>` : `<span>${formatIdentityNumber(version.trial_days)} 天试用</span>`}</div>
    </article>
  `;
}

function renderSubscriptionCommand(current, candidates, busy) {
  if (candidates.length === 0 && !current) return "";
  const create = !current;
  return `
    <div class="identity-subscription-actions">
      ${candidates.length ? `<form class="identity-subscription-command" ${create ? "data-saas-subscription-create-form" : "data-saas-subscription-change-form"}>
        <label><span>目标套餐</span><select name="planVersionId" required>${candidates.map((version) => `<option value="${escapeAttribute(version.plan_version_id)}">${escapeHtml(version.display_name)} · ${escapeHtml(formatPlanPrice(version))}</option>`).join("")}</select></label>
        ${create ? '<label class="identity-subscription-trial"><input name="startTrial" type="checkbox" /><span>使用可用试用期</span></label>' : ""}
        <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>${create ? "创建订阅" : "登记套餐变更"}</button>
      </form>` : ""}
      ${current && !new Set(["canceled", "expired"]).has(current.status) ? `<button class="identity-danger-command" type="button" data-identity-action="cancel-subscription" ${busy ? "disabled" : ""}>取消订阅</button>` : ""}
    </div>
  `;
}

function formatPlanPrice(version) {
  if (version.billing_period === "none" || version.unit_price === "0.00") return "免费";
  return `${version.currency} ${version.unit_price}`;
}

function billingPeriodLabel(value) {
  return ({ none: "无周期", monthly: "每月", yearly: "每年" })[value] ?? String(value ?? "-");
}

function subscriptionStatusLabel(value) {
  return ({
    pending: "待支付",
    active: "生效中",
    trialing: "试用中",
    past_due: "逾期",
    canceled: "已取消",
    expired: "已到期",
    pending_payment: "待支付",
    not_required: "无需支付",
    scheduled: "已排期",
  })[value] ?? String(value ?? "-").replaceAll("_", " ");
}

function orderKindLabel(value) {
  return ({ create: "创建订阅", upgrade: "升级", downgrade: "降级", renewal: "续费" })[value]
    ?? String(value ?? "-").replaceAll("_", " ");
}

function commerceStatusLabel(value) {
  return ({
    pending_payment: "待支付", paid: "已支付", fulfilled: "已履约", past_due: "逾期",
    canceled: "已取消", refunded: "已全额退款", partially_refunded: "部分退款",
    requires_action: "等待操作", processing: "处理中", succeeded: "已成功", failed: "失败",
    requested: "待审核", rejected: "已拒绝", open: "催收中", recovered: "已恢复",
    closed: "已关闭",
  })[value] ?? String(value ?? "-").replaceAll("_", " ");
}

function refundReasonLabel(value) {
  return ({
    customer_request: "客户申请",
    service_failure: "服务故障",
    duplicate_charge: "重复扣款",
  })[value] ?? String(value ?? "-").replaceAll("_", " ");
}

function latestCommerceState(items, empty) {
  return items.length ? commerceStatusLabel(items[0].status) : empty;
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
        ${showNavigation ? `<a class="identity-quiet-command identity-onboarding-link" href="/${escapeAttribute(step.navigation_target)}">${step.status === "complete" ? "查看" : "打开配置"}</a>` : ""}
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
