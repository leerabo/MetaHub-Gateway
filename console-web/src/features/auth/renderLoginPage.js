/**
 * 渲染控制台登录页。
 *
 * 登录页只收集租户标识、邮箱和密码；真正的租户上下文和角色由后端认证接口返回，
 * 页面不会自行推断权限。
 */
export function renderLoginPage(container, options = {}) {
  if (!container) {
    return;
  }
  const branding = options.branding && typeof options.branding === "object" ? options.branding : {};
  const logoUrl = safePublicBrandAssetPath(branding.logo_path);
  const productName = boundedText(branding.product_name, 120) ? branding.product_name : "MetaHub";
  const loginTitle = boundedText(branding.login_title, 160) ? branding.login_title : "MetaHub 控制台登录";
  const loginMessage = boundedText(branding.login_message, 400) ? branding.login_message : "";
  container.innerHTML = `
    <main class="login-shell">
      <section class="login-panel" aria-labelledby="login-title">
        <div class="login-brand">
          ${logoUrl ? `<div class="brand-mark brand-mark-image"><img class="brand-logo" src="${escapeAttribute(logoUrl)}" alt="${escapeAttribute(productName)}"></div>` : `<div class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></div>`}
          <div>
            <p class="eyebrow">${escapeHtml(productName)}</p>
            <h1 id="login-title">${escapeHtml(loginTitle)}</h1>
          </div>
        </div>
        <p class="login-brand-message" data-login-brand-message${loginMessage ? "" : " hidden"}>${escapeHtml(loginMessage)}</p>
        <form class="login-form" id="login-form">
          <label>
            <span>租户标识</span>
            <input name="tenantSlug" autocomplete="organization" value="${escapeAttribute(options.tenantSlug ?? "default")}" />
          </label>
          <label>
            <span>邮箱</span>
            <input name="email" type="email" autocomplete="username" value="${escapeAttribute(options.email ?? "")}" required />
          </label>
          <label>
            <span>密码</span>
            <input name="password" type="password" autocomplete="current-password" required />
          </label>
          ${
            options.error
              ? `<div class="login-error" role="alert">${escapeHtml(options.error)}${options.requestId ? `<br /><small>request_id: ${escapeHtml(options.requestId)}</small>` : ""}</div>`
              : ""
          }
          <button type="submit">登录</button>
        </form>
      </section>
    </main>
  `;
}

/** 白标公开投影仍需在渲染边界做长度和控制字符保护。 */
function boundedText(value, max) {
  const text = String(value ?? "");
  return text.length > 0 && text.length <= max && !/[\u0000-\u001f\u007f]/u.test(text);
}

/** Public branding assets are content-addressed same-origin PNG routes only. */
function safePublicBrandAssetPath(value) {
  const path = String(value ?? "");
  return /^\/branding\/public\/assets\/bas_[0-9a-f]{32}$/u.test(path) ? path : "";
}

/**
 * HTML 文本转义。
 *
 * 登录页的默认租户、邮箱和错误信息都可能来自外部状态，渲染前统一转义。
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
 * 额外转义反引号，避免默认值写入 value 属性时破坏属性边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
