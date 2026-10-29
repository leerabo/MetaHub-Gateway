export function renderAccountPreferences(state) {
  const preference = state?.preference;
  const status = String(state?.status ?? "idle");
  const busy = state?.busy === true;
  return `
    <section id="account-preferences" class="identity-account-preference" aria-labelledby="identity-account-preference-title"
      aria-busy="${busy}">
      <div class="identity-section-heading">
        <div>
          <h2 id="identity-account-preference-title">语言与显示</h2>
        </div>
        <button class="identity-quiet-command" type="button" data-identity-action="reload-account-section" ${busy ? "disabled" : ""}>重新加载</button>
      </div>
      ${stateBlock(status, state?.error)}
      ${preference ? `
        <form class="identity-account-preference-form" data-account-preference-form>
          <label>
            <span>界面语言</span>
            <select name="locale" ${busy ? "disabled" : ""}>
              ${preference.available_locales.map((locale) => `
                <option value="${escapeAttribute(locale)}" ${locale === preference.locale ? "selected" : ""}>
                  ${locale === "zh-CN" ? "简体中文" : "English"}
                </option>
              `).join("")}
            </select>
          </label>
          <button class="identity-primary" type="submit" ${busy ? "disabled" : ""}>保存语言</button>
        </form>
        <p class="identity-account-preference-note">
          ${preference.confirmation_state === "confirmed" ? "偏好已确认。" : "请确认当前语言偏好。"}
        </p>
      ` : status === "idle" ? '<p class="identity-empty" role="status">正在读取账号偏好...</p>' : ""}
      <section class="identity-account-lifecycle" aria-labelledby="identity-account-lifecycle-title">
        <h3 id="identity-account-lifecycle-title">账号生命周期</h3>
        <p>退出登录会结束当前设备会话；永久注销账号会同时处理组织、账单和审计数据，当前试用环境暂不提供自助注销入口。</p>
        <small>如需永久注销，请联系平台管理员或客服。</small>
      </section>
    </section>
  `;
}

function stateBlock(status, error) {
  if (status === "loading") return '<p class="identity-empty" role="status">正在读取账号偏好...</p>';
  if (status === "conflict") return '<p class="identity-form-error" role="alert">偏好已在其他设备更新，请刷新后重试。</p>';
  if (status === "permission-revoked") return '<p class="identity-form-error" role="alert">账号会话已失效，请重新登录。</p>';
  if (status === "dependency-unavailable") return '<p class="identity-form-error" role="alert">偏好服务暂不可用，请稍后重试。</p>';
  if (status === "error") {
    return `<p class="identity-form-error" role="alert">${escapeHtml(error?.message ?? "账号偏好请求未完成。")}</p>`;
  }
  return "";
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}
function escapeAttribute(value) {
  return escapeHtml(value).replace(/\u0060/gu, "&#96;");
}
