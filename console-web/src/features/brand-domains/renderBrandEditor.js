/** Shared visual editor for PRD-142 brand projections. Authentication and owner state stay outside. */

export function renderBrandEditor(state, options = {}) {
  const scope = safeScope(options.scope);
  const title = String(options.title ?? "品牌外观");
  const kicker = String(options.kicker ?? "Brand domain");
  if (!scope) return "";
  if (!state || state.phase === "idle" || state.phase === "loading") {
    return `<section class="brand-domain-editor" data-brand-domain-editor="${escapeAttribute(scope)}" aria-busy="true"><div class="brand-domain-heading"><div><p>${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2></div></div><p class="brand-domain-empty">正在读取品牌配置</p></section>`;
  }
  if (state.phase === "error" || !state.branding) {
    return `<section class="brand-domain-editor" data-brand-domain-editor="${escapeAttribute(scope)}"><div class="brand-domain-heading"><div><p>${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2></div></div><p class="brand-domain-error" role="alert">品牌服务暂不可用 · ${escapeHtml(state.error?.code ?? "branding.unavailable")} · ${escapeHtml(state.error?.requestId ?? "-")}</p></section>`;
  }
  const item = state.branding;
  const actions = new Set(item.actions ?? []);
  const disabled = state.busy ? " disabled" : "";
  const releaseAction = actions.has("publish") ? "publish" : actions.has("release") ? "release" : "";
  return `<section class="brand-domain-editor" data-brand-domain-editor="${escapeAttribute(scope)}" aria-busy="${state.busy === true}">
    <div class="brand-domain-heading">
      <div><p>${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2></div>
      <div class="brand-domain-heading-status">${status(item.status)}<span>Rev ${number(item.revision)}</span></div>
    </div>
    ${state.feedback ? `<p class="brand-domain-feedback" role="status">${escapeHtml(state.feedback.message)} · ${escapeHtml(state.feedback.requestId)}</p>` : ""}
    ${state.error ? `<p class="brand-domain-error" role="alert">${escapeHtml(state.error.code)} · ${escapeHtml(state.error.requestId)}</p>` : ""}
    ${summary(item)}
    ${actions.has("update") ? visualForm(scope, item, disabled) : readonlyNotice()}
    ${actions.has("attach_asset") || actions.has("remove_asset") ? assetControls(scope, item, actions, disabled) : ""}
    ${options.allowDomain === true && actions.has("request_domain") ? domainForm(scope, disabled) : ""}
    ${releaseAction || actions.has("suspend") || actions.has("rollback_lkg")
      ? lifecycleControls(scope, item, actions, releaseAction, disabled) : ""}
  </section>`;
}

function summary(item) {
  const desired = Object.hasOwn(item, "desired_generation")
    ? `<div><dt>Desired</dt><dd>${number(item.desired_generation)}</dd></div><div><dt>Applied</dt><dd>${number(item.applied_generation)}</dd></div>`
    : `<div><dt>Published</dt><dd>${number(item.published_generation)}</dd></div>`;
  return `<dl class="brand-domain-summary">
    ${desired}<div><dt>LKG</dt><dd>${number(item.lkg_generation)}</dd></div>
    <div><dt>Logo</dt><dd>${assetLabel(item.logo)}</dd></div>
    <div><dt>Favicon</dt><dd>${assetLabel(item.favicon)}</dd></div>
    ${Object.hasOwn(item, "ack_status") ? `<div><dt>Edge ACK</dt><dd>${escapeHtml(label(item.ack_status ?? "pending"))}</dd></div>` : ""}
    ${item.domain ? `<div><dt>Domain</dt><dd>${escapeHtml(item.domain.canonical_domain)} · ${escapeHtml(label(item.domain.status))}</dd></div>` : ""}
  </dl>
  ${item.domain?.challenge ? `<div class="brand-domain-challenge" role="status"><strong>DNS TXT challenge</strong><code>${escapeHtml(item.domain.challenge)}</code><span>该值仅显示于本次响应。</span></div>` : ""}`;
}

function visualForm(scope, item, disabled) {
  return `<form class="brand-domain-form brand-domain-visual" data-brand-command-form="${escapeAttribute(scope)}" autocomplete="off">
    <input type="hidden" name="action" value="update" />
    <div class="brand-domain-section-heading"><h3>视觉字段</h3><span>完整草稿 · CAS</span></div>
    <div class="brand-domain-field-grid">
      ${field("产品名称", "product_name", item.product_name, 80, disabled)}
      ${colorField("主色", "primary_color", item.primary_color, disabled)}
      ${colorField("辅色", "secondary_color", item.secondary_color, disabled)}
      ${field("登录标题", "login_title", item.login_title, 120, disabled)}
      <label class="brand-domain-wide"><span>登录文案</span><textarea name="login_message" maxlength="240" required${disabled}>${escapeHtml(item.login_message)}</textarea></label>
    </div>
    ${stepUp(disabled)}${confirm("保存草稿", disabled)}
  </form>`;
}

function assetControls(scope, item, actions, disabled) {
  return `<div class="brand-domain-assets">
    <div class="brand-domain-section-heading"><h3>PNG 资产</h3><span>16-1024 px · 最大 512 KiB</span></div>
    ${actions.has("attach_asset") ? `<form data-brand-command-form="${escapeAttribute(scope)}" autocomplete="off">
      <input type="hidden" name="action" value="attach_asset" />
      <label><span>资产类型</span><select name="asset_kind"${disabled}><option value="logo">Logo</option><option value="favicon">Favicon</option></select></label>
      <label><span>PNG 文件</span><input name="png_file" type="file" accept="image/png" required${disabled} /></label>
      ${stepUp(disabled)}${confirm("上传并扫描", disabled)}
    </form>` : ""}
    ${actions.has("remove_asset") && (item.logo || item.favicon) ? `<div class="brand-domain-remove-list">
      ${item.logo ? removeAssetForm(scope, "logo", disabled) : ""}
      ${item.favicon ? removeAssetForm(scope, "favicon", disabled) : ""}
    </div>` : ""}
  </div>`;
}

function removeAssetForm(scope, kind, disabled) {
  return `<form data-brand-command-form="${escapeAttribute(scope)}" autocomplete="off"><input type="hidden" name="action" value="remove_asset" /><input type="hidden" name="asset_kind" value="${kind}" />${stepUp(disabled, true)}${confirm(`移除 ${kind === "logo" ? "Logo" : "Favicon"}`, disabled, true)}</form>`;
}

function domainForm(scope, disabled) {
  return `<form class="brand-domain-form brand-domain-domain" data-brand-command-form="${escapeAttribute(scope)}" autocomplete="off">
    <input type="hidden" name="action" value="request_domain" />
    <div class="brand-domain-section-heading"><h3>验证域名</h3><span>DNS TXT · 双解析器</span></div>
    <label><span>规范域名</span><input name="domain" type="text" maxlength="253" placeholder="brand.example.com" required${disabled} /></label>
    ${stepUp(disabled)}${confirm("申请验证", disabled)}
  </form>`;
}

function lifecycleControls(scope, item, actions, releaseAction, disabled) {
  const controls = [];
  if (releaseAction) controls.push(lifecycleForm(scope, releaseAction, releaseAction === "publish" ? "发布到 Gateway" : "发布公开品牌", disabled, "primary"));
  if (actions.has("rollback_lkg") && item.lkg_generation > 0) controls.push(lifecycleForm(scope, "rollback_lkg", "回滚至 LKG", disabled));
  if (actions.has("suspend")) controls.push(lifecycleForm(scope, "suspend", "暂停品牌", disabled, "danger"));
  return `<div class="brand-domain-lifecycle"><div class="brand-domain-section-heading"><h3>发布控制</h3><span>一次性 step-up</span></div>${controls.join("")}</div>`;
}

function lifecycleForm(scope, action, text, disabled, tone = "") {
  return `<form data-brand-command-form="${escapeAttribute(scope)}" autocomplete="off"><input type="hidden" name="action" value="${escapeAttribute(action)}" />${stepUp(disabled, true)}${confirm(text, disabled, true, tone)}</form>`;
}

function stepUp(disabled, compact = false) {
  return `<div class="brand-domain-step-up${compact ? " is-compact" : ""}"><label><span>Step-up</span><select name="method" data-brand-step-up-method${disabled}><option value="totp">TOTP</option><option value="webauthn">安全密钥</option></select></label><label data-brand-step-up-code><span>TOTP 验证码</span><input name="verificationCode" type="password" inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" data-brand-sensitive required${disabled} /></label></div>`;
}

function confirm(text, disabled, compact = false, tone = "") {
  return `<div class="brand-domain-confirm${compact ? " is-compact" : ""}"><label><input name="confirmed" type="checkbox" required${disabled} /><span>确认执行</span></label><button class="brand-domain-command${tone ? ` is-${tone}` : ""}" type="submit"${disabled}>${escapeHtml(text)}</button></div>`;
}

function field(labelText, name, value, maxlength, disabled) {
  return `<label><span>${escapeHtml(labelText)}</span><input name="${escapeAttribute(name)}" value="${escapeAttribute(value)}" maxlength="${maxlength}" required${disabled} /></label>`;
}

function colorField(labelText, name, value, disabled) {
  return `<label><span>${escapeHtml(labelText)}</span><span class="brand-domain-color"><input name="${escapeAttribute(name)}" type="color" value="${escapeAttribute(value)}" required${disabled} /><code>${escapeHtml(value)}</code></span></label>`;
}

function readonlyNotice() {
  return '<p class="brand-domain-empty">当前平台身份只有发布或只读权限，视觉草稿不可编辑。</p>';
}

function assetLabel(value) {
  return value ? `${escapeHtml(label(value.status))} · ${number(value.width)}x${number(value.height)}` : "未配置";
}

function status(value) {
  const text = String(value ?? "unknown");
  const tone = new Set(["ready", "released", "published", "active", "applied"]).has(text)
    ? "ok" : new Set(["draft", "pending_domain", "pending", "scanning", "stale"]).has(text)
      ? "warn" : "danger";
  return `<span class="brand-domain-status is-${tone}">${escapeHtml(label(text))}</span>`;
}

function safeScope(value) {
  const text = String(value ?? "");
  return /^[a-z][a-z0-9-]{1,39}$/u.test(text) ? text : "";
}

function label(value) { return String(value ?? "-").replaceAll("_", " "); }
function number(value) { return Number.isFinite(Number(value)) ? new Intl.NumberFormat("zh-CN").format(Number(value)) : "0"; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/gu, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
function escapeAttribute(value) { return escapeHtml(value).replaceAll("`", "&#096;"); }
