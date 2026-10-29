// PAGE-120 设置页：展示当前租户 BrandingConfig，Logo 只接受本地 PNG 选择器，不渲染远程地址。
import { safeBrandingLogoUrl } from "../../lib/branding.js";

/** 渲染白标设置面板；owner 由后端导航/RBAC 快照构造，不能从按钮状态推断权限。 */
export function renderBrandingSettings(state, owner, i18n = null) {
  if (!owner || !state) return "";
  const t = (key, fallback) => i18n?.t?.(key, {}, fallback) ?? fallback;
  if (state.phase === "loading" || state.phase === "idle") return loading(t);
  if (state.phase === "error") return errorState(state.error, t);
  if (!state.branding) return loading(t);
  const item = state.branding;
  const canManage = owner.canManage === true;
  const logoUrl = safeBrandingLogoUrl(state.logoUrl);
  return `
    <section class="branding-settings-panel" data-branding-page aria-labelledby="branding-settings-title">
      <div class="panel-heading branding-settings-heading">
        <div>
          <p class="eyebrow">${escapeHtml(t("branding.kicker", "客户 / 渠道白标"))}</p>
          <h2 id="branding-settings-title">${escapeHtml(t("branding.title", "白标设置"))}</h2>
          <p>${escapeHtml(t("branding.description", "配置登录页与控制台的品牌呈现。"))}</p>
        </div>
      </div>
      ${state.feedback ? `<p class="branding-feedback" role="status">${escapeHtml(t("branding.saved", "白标已保存"))}</p>` : ""}
      ${state.mutationError ? `<p class="branding-form-error" role="alert">${escapeHtml(state.mutationError.message ?? t("branding.operation_failed", "操作失败"))}</p>` : ""}
      <form class="branding-settings-form" data-branding-form>
        ${field("product_name", t("branding.product_name", "产品名称"), item.product_name, 120, canManage)}
        ${field("primary_color", t("branding.primary_color", "主色"), item.primary_color, 7, canManage, "color")}
        ${field("secondary_color", t("branding.secondary_color", "辅色"), item.secondary_color, 7, canManage, "color")}
        ${field("login_title", t("branding.login_title", "登录页标题"), item.login_title, 160, canManage)}
        ${textarea("login_message", t("branding.login_message", "登录页文案"), item.login_message, 400, canManage)}
        <label><span>${escapeHtml(t("branding.custom_domain", "自定义域名预留"))}</span><input name="custom_domain" value="${escapeAttribute(item.custom_domain ?? "")}" maxlength="253" ${canManage ? "" : "disabled"} placeholder="brand.example.com"></label>
        <p class="branding-domain-status">${escapeHtml(t("branding.domain_status", "域名状态"))}: ${escapeHtml(item.custom_domain_status)}</p>
        <label class="branding-logo-field"><span>${escapeHtml(t("branding.logo", "Logo（PNG）"))}</span><input name="logo_png" type="file" accept="image/png" ${canManage ? "" : "disabled"}></label>
        ${item.logo_present && logoUrl ? `<img class="branding-logo-preview" src="${escapeAttribute(logoUrl)}" alt="${escapeAttribute(item.product_name)}">` : `<p class="branding-logo-empty">${escapeHtml(t("branding.logo_empty", "未配置 Logo"))}</p>`}
        ${item.logo_present && canManage ? `<label class="branding-logo-clear"><input name="logo_clear" type="checkbox"> <span>${escapeHtml(t("branding.logo_clear", "清除当前 Logo"))}</span></label>` : ""}
        ${canManage ? `<button class="primary-button" type="submit" ${state.mutationState === "submitting" ? "disabled" : ""}>${escapeHtml(t("branding.save", "保存白标"))}</button>` : `<p class="branding-readonly-note">${escapeHtml(t("branding.readonly", "当前账号只读"))}</p>`}
      </form>
    </section>
  `;
}

function loading(t) { return `<section class="branding-settings-panel" data-branding-page><h2>${escapeHtml(t("branding.title", "白标设置"))}</h2><p>${escapeHtml(t("branding.loading", "正在读取白标配置"))}</p></section>`; }
function errorState(error, t) { return `<section class="branding-settings-panel" data-branding-page><h2>${escapeHtml(t("branding.title", "白标设置"))}</h2><div class="empty-state error-state" role="alert">${escapeHtml(error?.message ?? t("branding.error", "白标服务暂不可用"))}</div></section>`; }
function field(name, label, value, maxlength, enabled, type = "text") { return `<label><span>${escapeHtml(label)}</span><input name="${escapeAttribute(name)}" type="${escapeAttribute(type)}" value="${escapeAttribute(value)}" maxlength="${escapeAttribute(maxlength)}" ${enabled ? "" : "disabled"}></label>`; }
function textarea(name, label, value, maxlength, enabled) { return `<label><span>${escapeHtml(label)}</span><textarea name="${escapeAttribute(name)}" maxlength="${escapeAttribute(maxlength)}" ${enabled ? "" : "disabled"}>${escapeHtml(value)}</textarea></label>`; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[character])); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
