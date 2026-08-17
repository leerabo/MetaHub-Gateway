// PAGE-119 伙伴门户渲染：只展示后端授权后的租户数据，并保持表单与下载边界。
import { partnerPortalPageOwner } from "./partnerPortalPageState.js";

/** 渲染租户隔离的伙伴门户视图，不以隐藏按钮替代后端授权。 */
export function renderPartnerPortal(state, authContext, consoleShellState, i18n) {
  const owner = partnerPortalPageOwner(authContext, consoleShellState);
  if (!owner || !state) return "";
  const t = (key, fallback) => i18n?.t?.(key, {}, fallback) ?? fallback;
  if (state.phase === "loading") return renderLoading(t);
  if (state.phase === "not_configured") return renderNotConfigured(state, owner, t);
  if (state.phase === "error") return renderError(state.profileError, t);
  if (state.phase !== "ready" || !state.profile) return renderLoading(t);
  return renderReady(state, owner, t);
}

function renderLoading(t) {
  return `
    <div class="partner-portal-loading" role="status">
      <span class="status-dot checking" aria-hidden="true"></span>
      <span>${escapeHtml(t("partner_portal.loading", "正在读取伙伴门户"))}</span>
    </div>
  `;
}

function renderNotConfigured(state, owner, t) {
  return `
    <div class="partner-portal-empty">
      <p class="partner-portal-kicker">PAGE-119</p>
      <h2 id="partner-portal-title">${escapeHtml(t("partner_portal.not_configured_title", "伙伴门户尚未配置"))}</h2>
      <p>${escapeHtml(t("partner_portal.not_configured_body", "当前租户还没有可用的渠道伙伴资料。"))}</p>
      ${owner.canConfigure ? `
        <form class="partner-portal-setup-form" data-partner-setup-form>
          ${field("partner_code", t("partner_portal.partner_code", "伙伴代码"), { pattern: "[a-z0-9][a-z0-9._-]{0,79}", maxlength: 80, required: true })}
          ${field("display_name", t("partner_portal.display_name", "伙伴名称"), { maxlength: 120, required: true })}
          ${field("portal_name", t("partner_portal.portal_name", "门户名称"), { maxlength: 80, required: true })}
          ${colorField("brand_primary_color", t("partner_portal.brand_color", "品牌主色"), "#176B5B")}
          ${field("support_text", t("partner_portal.support_text", "支持信息"), { maxlength: 160 })}
          ${formError(state.mutationError, t)}
          <button class="primary-button" type="submit" ${state.mutationState === "submitting" ? "disabled" : ""}>
            ${escapeHtml(t("partner_portal.configure", "创建伙伴门户"))}
          </button>
        </form>
      ` : `<p class="partner-portal-readonly-note">${escapeHtml(t("partner_portal.configure_required", "请联系租户管理员完成初始化。"))}</p>`}
    </div>
  `;
}

function renderReady(state, owner, t) {
  const profile = state.profile;
  return `
    <div class="partner-portal-workbench" data-partner-portal-root>
      <header class="partner-portal-header">
        <div>
          <p class="partner-portal-kicker">${escapeHtml(profile.partner_code)} · ${escapeHtml(t("partner_portal.scope", "当前租户"))}</p>
          <h2 id="partner-portal-title">${escapeHtml(profile.portal_name)}</h2>
          ${profile.support_text ? `<p>${escapeHtml(profile.support_text)}</p>` : ""}
        </div>
        <div class="partner-portal-header-actions">
          <span class="partner-portal-status">${escapeHtml(t("partner_portal.status.active", "运行中"))}</span>
          <button class="secondary-button" type="button" data-partner-refresh>${escapeHtml(t("common.refresh", "刷新"))}</button>
        </div>
      </header>
      ${feedback(state.feedback, t)}
      <div class="partner-portal-layout">
        <main class="partner-portal-main">
          ${renderCustomers(state, owner, t)}
          ${renderCommission(state, owner, t)}
        </main>
        <aside class="partner-portal-aside">
          ${renderBranding(state, owner, t)}
          ${renderDownloads(state, owner, t)}
        </aside>
      </div>
      ${renderDialog(state, t)}
    </div>
  `;
}

function renderCustomers(state, owner, t) {
  const values = state.customers?.items ?? [];
  const filter = state.customerFilters;
  return `
    <section class="partner-portal-section" aria-labelledby="partner-customers-title">
      <div class="partner-portal-section-heading">
        <div>
          <h3 id="partner-customers-title">${escapeHtml(t("partner_portal.customers", "客户与历史用量"))}</h3>
          <p>${escapeHtml(t("partner_portal.usage_history_notice", "用量包含 Project 建立映射前的历史；分润仅从映射生效后开始。"))}</p>
        </div>
        ${owner.canManageCustomers ? `<button class="primary-button" type="button" data-partner-customer-create>${escapeHtml(t("partner_portal.customer_create", "映射客户"))}</button>` : ""}
      </div>
      <form class="partner-portal-filters" data-partner-customer-filters>
        ${compactDate("start_date", t("partner_portal.start_date", "开始日期"), filter.start_date)}
        ${compactDate("end_date", t("partner_portal.end_date", "结束日期"), filter.end_date)}
        <label><span>${escapeHtml(t("partner_portal.customer_status", "状态"))}</span><select name="status">
          <option value="">${escapeHtml(t("partner_portal.all", "全部"))}</option>
          <option value="active" ${filter.status === "active" ? "selected" : ""}>${escapeHtml(t("partner_portal.active", "启用"))}</option>
          <option value="disabled" ${filter.status === "disabled" ? "selected" : ""}>${escapeHtml(t("partner_portal.disabled", "已终止"))}</option>
        </select></label>
        <label class="partner-portal-filter-search"><span>${escapeHtml(t("partner_portal.search", "搜索"))}</span><input name="q" maxlength="120" value="${escapeAttribute(filter.q)}"></label>
        <button class="secondary-button" type="submit">${escapeHtml(t("partner_portal.apply", "应用"))}</button>
      </form>
      ${sectionState(state.customersState, state.customersError, values.length, t, "customers")}
      ${state.customersState === "ready" && values.length ? `
        <div class="partner-portal-table" role="table" aria-label="${escapeAttribute(t("partner_portal.customers", "客户与历史用量"))}">
          <div class="partner-portal-row partner-portal-table-head" role="row">
            <span>${escapeHtml(t("partner_portal.customer", "客户"))}</span><span>${escapeHtml(t("partner_portal.requests", "请求"))}</span>
            <span>${escapeHtml(t("partner_portal.failures", "失败"))}</span><span>${escapeHtml(t("partner_portal.tokens", "Token"))}</span>
            <span>${escapeHtml(t("partner_portal.cost", "历史成本"))}</span><span>${escapeHtml(t("partner_portal.cursor", "分润游标"))}</span><span>${escapeHtml(t("partner_portal.actions", "操作"))}</span>
          </div>
          ${values.map((item) => customerRow(item, owner, t)).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function customerRow(item, owner, t) {
  const usage = item.usage;
  const current = item.commission_cursor.freshness_status === "current";
  return `
    <div class="partner-portal-row" role="row">
      <div data-label="${escapeAttribute(t("partner_portal.customer", "客户"))}"><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.customer_code)} · ${escapeHtml(item.status)}</small></div>
      <span data-label="${escapeAttribute(t("partner_portal.requests", "请求"))}">${formatInteger(usage.request_count)}</span>
      <span data-label="${escapeAttribute(t("partner_portal.failures", "失败"))}">${formatInteger(usage.failed_count)}</span>
      <span data-label="${escapeAttribute(t("partner_portal.tokens", "Token"))}">${formatInteger(usage.total_tokens)}</span>
      <span data-label="${escapeAttribute(t("partner_portal.cost", "历史成本"))}">CNY ${formatMicros(usage.cost_cny_micros)}<br>USD ${formatMicros(usage.cost_usd_micros)}</span>
      <span data-label="${escapeAttribute(t("partner_portal.cursor", "分润游标"))}" class="partner-portal-freshness ${current ? "is-current" : "is-lagging"}">${escapeHtml(t(`partner_portal.freshness.${item.commission_cursor.freshness_status}`, current ? "当前" : "追赶中"))}</span>
      <div data-label="${escapeAttribute(t("partner_portal.actions", "操作"))}" class="partner-portal-row-actions">
        ${owner.canManageCustomers && item.status === "active" ? `
          <button class="text-button" type="button" data-partner-customer-rename="${escapeAttribute(item.partner_customer_id)}">${escapeHtml(t("partner_portal.rename", "重命名"))}</button>
          <button class="text-button danger" type="button" data-partner-customer-disable="${escapeAttribute(item.partner_customer_id)}">${escapeHtml(t("partner_portal.disable", "终止"))}</button>
        ` : `<span>${escapeHtml(t("partner_portal.readonly", "只读"))}</span>`}
      </div>
    </div>
  `;
}

function renderCommission(state, owner, t) {
  const data = state.commission;
  const values = data?.items ?? [];
  const filter = state.commissionFilters;
  return `
    <section class="partner-portal-section" aria-labelledby="partner-commission-title">
      <div class="partner-portal-section-heading">
        <div><h3 id="partner-commission-title">${escapeHtml(t("partner_portal.commission", "生效后分润"))}</h3>
          <p>${escapeHtml(t("partner_portal.commission_notice", "仅统计客户映射生效后的正式账本记录；accrued 不表示已经支付。"))}</p></div>
      </div>
      <form class="partner-portal-filters" data-partner-commission-filters>
        ${compactDate("start_date", t("partner_portal.start_date", "开始日期"), filter.start_date)}
        ${compactDate("end_date", t("partner_portal.end_date", "结束日期"), filter.end_date)}
        <label><span>${escapeHtml(t("partner_portal.customer", "客户"))}</span><select name="customer_id">
          <option value="">${escapeHtml(t("partner_portal.all", "全部"))}</option>
          ${(state.customers?.items ?? []).map((item) => `<option value="${escapeAttribute(item.partner_customer_id)}" ${filter.customer_id === item.partner_customer_id ? "selected" : ""}>${escapeHtml(item.display_name)}</option>`).join("")}
        </select></label>
        <button class="secondary-button" type="submit">${escapeHtml(t("partner_portal.apply", "应用"))}</button>
      </form>
      ${data ? `<div class="partner-portal-currency-summaries">${data.totals.map((item) => `
        <div><span>${escapeHtml(item.currency)}</span><strong>${formatMicros(item.commission_amount_micros)}</strong><small>${escapeHtml(t("partner_portal.commission_amount", "分润微元换算"))} · ${formatInteger(item.request_count)} ${escapeHtml(t("partner_portal.requests", "请求"))}</small></div>
      `).join("") || `<p>${escapeHtml(t("partner_portal.no_commission", "当前筛选范围暂无分润"))}</p>`}</div>` : ""}
      ${data?.cursor?.freshness_status === "lagging" ? `<p class="partner-portal-warning">${escapeHtml(t("partner_portal.lagging", "分润物化仍在追赶，当前报告不是最终结算结果。"))}</p>` : ""}
      ${sectionState(state.commissionState, state.commissionError, values.length, t, "commission")}
      ${state.commissionState === "ready" && values.length ? `
        <div class="partner-portal-table partner-portal-commission-table" role="table">
          <div class="partner-portal-row partner-portal-table-head" role="row"><span>${escapeHtml(t("partner_portal.customer", "客户"))}</span><span>${escapeHtml(t("partner_portal.occurred", "发生时间"))}</span><span>${escapeHtml(t("partner_portal.currency", "币种"))}</span><span>${escapeHtml(t("partner_portal.amount", "账本金额"))}</span><span>${escapeHtml(t("partner_portal.commission_amount", "分润"))}</span></div>
          ${values.map((item) => `<div class="partner-portal-row" role="row">
            <div data-label="${escapeAttribute(t("partner_portal.customer", "客户"))}"><strong>${escapeHtml(item.customer_name)}</strong><small>${escapeHtml(item.customer_code)}</small></div>
            <time data-label="${escapeAttribute(t("partner_portal.occurred", "发生时间"))}">${escapeHtml(item.source_created_at)}</time>
            <span data-label="${escapeAttribute(t("partner_portal.currency", "币种"))}">${escapeHtml(item.currency)}</span>
            <span data-label="${escapeAttribute(t("partner_portal.amount", "账本金额"))}">${formatMicros(item.amount_micros)}</span>
            <span data-label="${escapeAttribute(t("partner_portal.commission_amount", "分润"))}">${formatMicros(item.commission_amount_micros)}</span>
          </div>`).join("")}
        </div>
      ` : ""}
    </section>
  `;
}

function renderBranding(state, owner, t) {
  const item = state.profile;
  return `
    <section class="partner-portal-side-section" aria-labelledby="partner-branding-title">
      <h3 id="partner-branding-title">${escapeHtml(t("partner_portal.branding", "白标配置"))}</h3>
      <form data-partner-branding-form>
        ${field("portal_name", t("partner_portal.portal_name", "门户名称"), { value: item.portal_name, maxlength: 80, required: true, disabled: !owner.canManageBranding })}
        ${colorField("brand_primary_color", t("partner_portal.brand_color", "品牌主色"), item.brand_primary_color, !owner.canManageBranding)}
        ${field("support_text", t("partner_portal.support_text", "支持信息"), { value: item.support_text ?? "", maxlength: 160, disabled: !owner.canManageBranding })}
        <input type="hidden" name="revision" value="${item.revision}">
        ${state.mutationError && state.mutationKind === "branding" ? formError(state.mutationError, t) : ""}
        ${owner.canManageBranding ? `<button class="primary-button" type="submit" ${state.mutationState === "submitting" ? "disabled" : ""}>${escapeHtml(t("partner_portal.save_branding", "保存白标"))}</button>` : `<p class="partner-portal-readonly-note">${escapeHtml(t("partner_portal.readonly", "只读"))}</p>`}
      </form>
    </section>
  `;
}

function renderDownloads(state, owner, t) {
  return `
    <section class="partner-portal-side-section" aria-labelledby="partner-downloads-title">
      <h3 id="partner-downloads-title">${escapeHtml(t("partner_portal.secure_downloads", "安全交付"))}</h3>
      <p>${escapeHtml(t("partner_portal.download_notice", "附件仅保留在当前点击调用栈，完成后立即回收浏览器对象 URL。"))}</p>
      <div class="partner-portal-download-actions">
        ${owner.canExportCommission ? `<button class="secondary-button" type="button" data-partner-commission-export ${state.download.state === "loading" ? "disabled" : ""}>${escapeHtml(t("partner_portal.export_csv", "导出分润 CSV"))}</button>` : ""}
        ${owner.canDownloadDelivery ? `<button class="secondary-button" type="button" data-partner-delivery-download ${state.download.state === "loading" ? "disabled" : ""}>${escapeHtml(t("partner_portal.delivery_package", "下载交付包"))}</button>` : ""}
      </div>
      ${state.download.error ? formError(state.download.error, t) : ""}
      ${state.download.state === "saved" ? `<p class="partner-portal-feedback">${escapeHtml(t("partner_portal.download_started", "安全下载已开始"))} · ${escapeHtml(state.download.requestId || "-")}</p>` : ""}
    </section>
  `;
}

function renderDialog(state, t) {
  const dialog = state.dialog;
  if (!dialog.open) return "";
  if (dialog.kind === "create") return `
    <dialog class="partner-portal-dialog" aria-labelledby="partner-dialog-title">
      <form data-partner-customer-create-form>
        <h3 id="partner-dialog-title">${escapeHtml(t("partner_portal.customer_create", "映射客户"))}</h3>
        <label><span>${escapeHtml(t("partner_portal.project", "Project"))}</span><select name="project_id" required>
          <option value="">${escapeHtml(t("partner_portal.select_project", "选择可映射 Project"))}</option>
          ${state.projects.map((item) => `<option value="${escapeAttribute(item.project_id)}">${escapeHtml(item.name)} · ${escapeHtml(item.project_code)}</option>`).join("")}
        </select></label>
        ${field("customer_code", t("partner_portal.customer_code", "客户代码"), { pattern: "[a-z0-9][a-z0-9._-]{0,79}", maxlength: 80, required: true })}
        ${field("display_name", t("partner_portal.customer_name", "客户名称"), { maxlength: 120, required: true })}
        ${formError(state.mutationError, t)}
        <div class="partner-portal-dialog-actions"><button class="secondary-button" type="button" data-partner-dialog-close>${escapeHtml(t("common.cancel", "取消"))}</button><button class="primary-button" type="submit">${escapeHtml(t("common.confirm", "确认"))}</button></div>
      </form>
    </dialog>
  `;
  const disable = dialog.kind === "disable";
  return `
    <dialog class="partner-portal-dialog" aria-labelledby="partner-dialog-title">
      <form data-partner-customer-update-form data-kind="${escapeAttribute(dialog.kind)}" data-customer-id="${escapeAttribute(dialog.customerId)}" data-revision="${dialog.revision}">
        <h3 id="partner-dialog-title">${escapeHtml(t(disable ? "partner_portal.disable_title" : "partner_portal.rename_title", disable ? "终止客户映射" : "重命名客户"))}</h3>
        ${disable ? `<p>${escapeHtml(t("partner_portal.disable_confirm", "终止后历史用量和既有分润仍可读取，但不会再产生 cutoff 之后的分润。"))}</p>` : field("display_name", t("partner_portal.customer_name", "客户名称"), { value: dialog.displayName, maxlength: 120, required: true })}
        ${formError(state.mutationError, t)}
        <div class="partner-portal-dialog-actions"><button class="secondary-button" type="button" data-partner-dialog-close>${escapeHtml(t("common.cancel", "取消"))}</button><button class="primary-button ${disable ? "danger-button" : ""}" type="submit">${escapeHtml(t("common.confirm", "确认"))}</button></div>
      </form>
    </dialog>
  `;
}

function sectionState(status, error, count, t, kind) {
  if (status === "loading" || status === "idle") return `<div class="partner-portal-section-state" role="status">${escapeHtml(t("partner_portal.section_loading", "正在读取"))}</div>`;
  if (status === "error") return `<div class="partner-portal-section-state is-error" role="alert">${escapeHtml(error?.code ?? "partner.unavailable")} · ${escapeHtml(error?.requestId ?? "-")}</div>`;
  if (!count) return `<div class="partner-portal-section-state">${escapeHtml(t(`partner_portal.${kind}_empty`, kind === "customers" ? "暂无客户映射" : "暂无分润记录"))}</div>`;
  return "";
}

function renderError(error, t) {
  return `<div class="partner-portal-error" role="alert"><h2 id="partner-portal-title">${escapeHtml(t("partner_portal.error", "伙伴门户暂不可用"))}</h2><code>${escapeHtml(error?.code ?? "partner.unavailable")}</code><span>${escapeHtml(error?.requestId ?? "-")}</span></div>`;
}

function feedback(value, t) {
  return value ? `<p class="partner-portal-feedback" role="status">${escapeHtml(value.message || t("partner_portal.saved", "已保存"))} · ${escapeHtml(value.requestId)}</p>` : "";
}

function formError(error, t) {
  return error ? `<p class="partner-portal-form-error" role="alert">${escapeHtml(t("partner_portal.operation_failed", "操作失败"))} · ${escapeHtml(error.code)} · ${escapeHtml(error.requestId)}</p>` : "";
}

function field(name, label, options = {}) {
  return `<label><span>${escapeHtml(label)}</span><input name="${name}" type="text" value="${escapeAttribute(options.value ?? "")}" ${options.maxlength ? `maxlength="${options.maxlength}"` : ""} ${options.pattern ? `pattern="${escapeAttribute(options.pattern)}"` : ""} ${options.required ? "required" : ""} ${options.disabled ? "disabled" : ""}></label>`;
}

function colorField(name, label, value, disabled = false) {
  return `<label class="partner-portal-color-field"><span>${escapeHtml(label)}</span><input name="${name}" type="color" value="${escapeAttribute(value)}" ${disabled ? "disabled" : ""}></label>`;
}

function compactDate(name, label, value) {
  return `<label><span>${escapeHtml(label)}</span><input name="${name}" type="date" value="${escapeAttribute(value ?? "")}"></label>`;
}

function formatInteger(value) { return new Intl.NumberFormat("zh-CN").format(Number(value ?? 0)); }
function formatMicros(value) { return new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 }).format(Number(value ?? 0) / 1_000_000); }
function escapeAttribute(value) { return escapeHtml(value); }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[character]));
}
