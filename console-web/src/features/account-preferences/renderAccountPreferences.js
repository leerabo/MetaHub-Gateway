export function renderAccountPreferences(state) {
  const preference = state?.preference;
  const status = String(state?.status ?? "idle");
  const busy = state?.busy === true;
  return `
    <section id="account-preferences" class="identity-account-preference" aria-labelledby="identity-account-preference-title"
      aria-busy="${busy}">
      <div class="identity-section-heading">
        <div>
          <p>全局账号偏好</p>
          <h2 id="identity-account-preference-title">语言与显示</h2>
        </div>
        ${preference ? `<span>Revision ${Number(preference.revision)}</span>` : ""}
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
      ` : status === "idle" ? '<p class="identity-empty">账号偏好尚未加载。</p>' : ""}
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
