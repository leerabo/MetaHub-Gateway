import { createI18n } from "../../i18n/messages.js";

/**
 * 渲染语言设置页。
 * 所有可见文案都通过 i18n key 获取，便于后续新增语言和缺失 key 测试。
 */
export function renderLanguageSettingsPanel(userPreferencesState, i18n = createI18n()) {
  const state = userPreferencesState ?? { state: "loading" };
  // 错误态是用户可见反馈入口，需要保留后端 request_id 便于排查。
  if (state.state === "error") {
    return renderErrorState(state, i18n);
  }
  if (!state.preference) {
    return renderLoadingState(i18n);
  }
  const preference = state.preference;
  const currentLocale = preference.locale ?? i18n.locale;
  const locales = preference.available_locales ?? [];
  return `
    <section class="language-settings-panel" aria-labelledby="language-settings-title">
      <div class="panel-heading">
        <div>
          <h2 id="language-settings-title">${escapeHtml(i18n.t("language.title"))}</h2>
          <p>${escapeHtml(i18n.t("language.description"))}</p>
        </div>
        <span>${escapeHtml(i18n.t("language.current"))}: ${escapeHtml(currentLocale)}</span>
      </div>
      ${renderFeedback(state, i18n)}
      <form id="language-settings-form" class="language-settings-form">
        <fieldset>
          <legend>${escapeHtml(i18n.t("language.available"))}</legend>
          <div class="language-option-grid">
            ${locales.map((locale) => renderLocaleOption(locale, currentLocale)).join("")}
          </div>
        </fieldset>
        <p class="language-fallback-note">${escapeHtml(i18n.t("language.fallback_note"))}</p>
        <button type="submit">${escapeHtml(i18n.t("language.save"))}</button>
      </form>
    </section>
  `;
}

/**
 * 渲染语言偏好加载态。
 */
// 加载态仍然必须通过 i18n 取文案，避免首屏刷新时出现硬编码语言。
function renderLoadingState(i18n) {
  return `
    <section class="language-settings-panel" aria-labelledby="language-settings-title">
      <div class="panel-heading">
        <h2 id="language-settings-title">${escapeHtml(i18n.t("language.title"))}</h2>
        <p>${escapeHtml(i18n.t("language.loading"))}</p>
      </div>
    </section>
  `;
}

/**
 * 渲染语言偏好错误态，并展示 request_id 方便排查。
 */
function renderErrorState(state, i18n) {
  const label = state.code ? i18n.errorLabel(state.code) : i18n.t("language.error");
  return `
    <section class="language-settings-panel" aria-labelledby="language-settings-title">
      <div class="panel-heading">
        <h2 id="language-settings-title">${escapeHtml(i18n.t("language.title"))}</h2>
        <p>${escapeHtml(label)}</p>
      </div>
      <div class="empty-state error-state">
        ${escapeHtml(state.message ?? i18n.t("language.error"))}
        ${escapeHtml(i18n.t("language.request_id", { value: state.requestId ?? "-" }))}
      </div>
    </section>
  `;
}

/**
 * 渲染保存反馈或后端统一错误。
 */
function renderFeedback(state, i18n) {
  if (state.actionError) {
    const label = state.actionError.code ? i18n.errorLabel(state.actionError.code) : i18n.t("language.error");
    return `
      <div class="empty-state error-state">
        ${escapeHtml(label)} ${escapeHtml(i18n.t("language.request_id", { value: state.actionError.requestId ?? "-" }))}
      </div>
    `;
  }
  if (state.feedback) {
    return `<div class="empty-state">${escapeHtml(state.feedback)}</div>`;
  }
  return "";
}

/**
 * 渲染单个语言选项，选项 label 来自后端白名单而不是用户输入。
 */
function renderLocaleOption(locale, currentLocale) {
  const value = locale.locale ?? "";
  const checked = value === currentLocale ? " checked" : "";
  return `
    <label class="language-option">
      <input type="radio" name="locale" value="${escapeAttribute(value)}"${checked}>
      <strong>${escapeHtml(locale.native_label ?? value)}</strong>
      <span>${escapeHtml(locale.label ?? value)}</span>
    </label>
  `;
}

/**
 * HTML 文本转义，所有来自 API 或字典的展示文本都统一经过这里。
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
 * HTML 属性转义，避免 locale 值破坏属性边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
