/** Renderers for PRD-144 Workspace subscription and Internal FinOps pages. */

export function renderWorkspaceSubscription(state = {}) {
  const response = state.subscription;
  const subscription = response?.subscription ?? null;
  const invoices = subscription?.invoices ?? [];
  const disputes = subscription?.disputes ?? [];
  return '<section class="workspace-commercial-accounting" data-workspace-commercial-root="workspace-subscription">'
    + pageHeading(
      "商业订阅",
      "订阅、付款、发票与争议",
      state.loading?.length ? "正在同步" : subscription?.projection_state ?? "current",
    )
    + stateMessage(state)
    + (subscription ? (
      '<section class="commercial-summary" aria-label="订阅摘要">'
        + metric("套餐", subscription.display_name, subscription.plan_code)
        + metric(
          "周期",
          formatDate(subscription.current_period_started_at),
          formatDate(subscription.current_period_ends_at),
        )
        + metric("应付", money(subscription.current_due_minor, subscription.currency), subscription.currency)
        + metric("已支付", money(subscription.paid_minor, subscription.currency), subscription.payment_status)
        + metric("已开票", money(subscription.invoiced_minor, subscription.currency), subscription.invoice_status)
        + metric("已退款", money(subscription.refunded_minor, subscription.currency), subscription.dispute_status)
      + '</section>'
      + '<section class="commercial-band">'
        + sectionHeading("账务状态", status(subscription.subscription_status))
        + '<dl class="commercial-facts">'
          + fact("订单", subscription.order_status)
          + fact("付款", subscription.payment_status)
          + fact("发票", subscription.invoice_status)
          + fact("争议", subscription.dispute_status)
          + fact("投影代次", String(subscription.projection_generation))
          + fact("数据时间", formatDate(subscription.projected_at))
        + '</dl>'
      + '</section>'
      + invoiceSection(invoices)
      + disputeSection(disputes)
      + disputeForm(state, invoices)
    ) : emptyState(state))
    + '</section>';
}

export function renderWorkspaceInternalFinOps(state = {}, navigationItems = []) {
  const rates = state.rates?.rates ?? [];
  const available = new Set(
    (Array.isArray(navigationItems) ? navigationItems : [])
      .filter((item) => item?.visible !== false && item?.disabled !== true)
      .map((item) => item.id),
  );
  return '<section class="workspace-commercial-accounting" data-workspace-commercial-root="internal-finops">'
    + pageHeading(
      "企业内部 FinOps",
      "Provider 成本率与内部成本事实",
      state.loading?.length ? "正在同步" : String(rates.length) + " 个版本",
    )
    + stateMessage(state)
    + finOpsLinks(available)
    + rateFilters(state)
    + '<section class="commercial-band">'
      + sectionHeading("Provider 成本率", status(state.phase))
      + '<div class="commercial-table-wrap"><table class="commercial-table">'
        + '<thead><tr><th>Provider / 模型</th><th>币种</th><th>输入成本</th>'
        + '<th>输出成本</th><th>生效区间</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>'
        + '<tbody>'
          + (rates.map((rate) => rateRow(rate, state.canManageRates)).join("")
            || emptyRow(state.loading?.length ? "正在读取成本率" : "暂无成本率", 8))
        + '</tbody>'
      + '</table></div>'
      + pagination(
        state.rateHistory?.length > 0,
        Boolean(state.rates?.next_cursor),
        "rate-previous",
        "rate-next",
      )
    + '</section>'
    + (state.canManageRates ? createRateForm() : "")
    + '</section>';
}

function pageHeading(eyebrow, title, meta) {
  return '<header class="commercial-page-heading"><div><p>' + escapeHtml(eyebrow)
    + '</p><h1>' + escapeHtml(title) + '</h1></div><div class="commercial-heading-actions">'
    + '<span>' + escapeHtml(meta) + '</span>'
    + '<button type="button" data-workspace-commercial-action="refresh"'
    + ' aria-label="刷新" title="刷新">&#8635;</button></div></header>';
}

function stateMessage(state) {
  const feedback = state.feedback
    ? '<p class="commercial-feedback" role="status">' + escapeHtml(state.feedback) + '</p>'
    : "";
  if (!state.error) return feedback;
  return feedback + '<div class="commercial-state is-error" role="alert"><strong>'
    + escapeHtml(phaseLabel(state.phase)) + '</strong><span>'
    + escapeHtml(state.error.message) + '</span><code>'
    + escapeHtml(state.error.requestId ?? "-") + '</code></div>';
}

function emptyState(state) {
  if (state.loading?.length) {
    return '<div class="commercial-state"><strong>正在读取订阅</strong></div>';
  }
  if (state.phase === "empty") {
    return '<div class="commercial-state commercial-empty-subscription" role="status">'
      + '<strong>当前 Workspace 暂无订阅</strong>'
      + '<p>当前没有可用套餐、付款或发票记录，账单争议也无法提交。请联系平台运营开通套餐；开通后刷新此页即可查看商业账务。</p>'
      + '</div>';
  }
  return '<div class="commercial-state"><strong>'
    + escapeHtml(phaseLabel(state.phase))
    + '</strong></div>';
}

function invoiceSection(invoices) {
  return '<section class="commercial-band">'
    + sectionHeading("发票", '<span>' + invoices.length + " 条</span>")
    + '<div class="commercial-table-wrap"><table class="commercial-table">'
      + '<thead><tr><th>发票</th><th>金额</th><th>税额</th><th>状态</th><th>更新时间</th></tr></thead>'
      + '<tbody>' + (invoices.map((invoice) => (
        '<tr><td><strong>' + escapeHtml(invoice.invoice_number_masked ?? "未编号")
          + '</strong><small>' + escapeHtml(invoice.platform_invoice_id) + '</small></td>'
          + '<td>' + money(invoice.total_minor, invoice.currency) + '</td>'
          + '<td>' + money(invoice.tax_minor, invoice.currency) + '</td>'
          + '<td>' + status(invoice.status) + '</td><td>' + formatDate(invoice.updated_at) + '</td></tr>'
      )).join("") || emptyRow("暂无发票", 5)) + '</tbody>'
    + '</table></div></section>';
}

function disputeSection(disputes) {
  return '<section class="commercial-band">'
    + sectionHeading("商业争议", '<span>' + disputes.length + " 条</span>")
    + '<div class="commercial-table-wrap"><table class="commercial-table">'
      + '<thead><tr><th>争议</th><th>来源</th><th>金额</th><th>原因</th><th>状态</th><th>结果</th></tr></thead>'
      + '<tbody>' + (disputes.map((item) => (
        '<tr><td><code>' + escapeHtml(item.billing_dispute_case_id) + '</code></td>'
          + '<td>' + escapeHtml(item.source_kind) + '<small>' + escapeHtml(item.source_ref) + '</small></td>'
          + '<td>' + money(item.disputed_amount_minor, item.currency) + '</td>'
          + '<td>' + escapeHtml(reasonLabel(item.reason_code)) + '</td>'
          + '<td>' + status(item.status) + '</td>'
          + '<td>' + escapeHtml(item.decision_code ? decisionLabel(item.decision_code) : "-")
          + (item.credited_amount_minor
            ? '<small>' + money(item.credited_amount_minor, item.currency) + '</small>' : "")
          + '</td></tr>'
      )).join("") || emptyRow("暂无商业争议", 6)) + '</tbody>'
    + '</table></div></section>';
}

function disputeForm(state, invoices) {
  if (!state.canSubmitDispute || !invoices.length) return "";
  return '<section class="commercial-band commercial-command">'
    + sectionHeading("提交账单争议", '<span>step-up</span>')
    + '<form data-workspace-commercial-dispute-form autocomplete="off">'
      + '<label><span>发票</span><select name="sourceId" required>'
        + invoices.map((invoice) => (
          '<option value="' + escapeAttribute(invoice.platform_invoice_id)
            + '" data-source-revision="' + escapeAttribute(invoice.source_revision) + '">'
            + escapeHtml(invoice.invoice_number_masked ?? invoice.platform_invoice_id)
            + " · " + money(invoice.total_minor, invoice.currency) + '</option>'
        )).join("")
      + '</select></label>'
      + '<label><span>原因</span><select name="reasonCode" required>'
        + reasonOptions()
      + '</select></label>'
      + stepUpFields("workspace")
      + '<button type="submit"' + (state.intents?.dispute ? " disabled" : "") + '>提交争议</button>'
    + '</form></section>';
}

function finOpsLinks(available) {
  const links = [
    ["usage", "Usage / PriceSnapshot"],
    ["finops-ledger", "内部账本"],
    ["quota-buckets", "额度"],
    ["budgets", "预算"],
  ].filter(([id]) => available.has(id));
  if (!links.length) return "";
  return '<nav class="commercial-related-links" aria-label="内部 FinOps 关联页面">'
    + links.map(([id, label]) => (
      '<a href="#' + escapeAttribute(id) + '">' + escapeHtml(label) + '</a>'
    )).join("") + '</nav>';
}

function rateFilters(state) {
  const filters = state.rateFilters ?? {};
  return '<section class="commercial-band"><form class="commercial-filter"'
    + ' data-workspace-provider-cost-filter-form autocomplete="off">'
    + input("providerId", "Provider ID", filters.provider_id)
    + input("modelId", "模型 ID", filters.model_id)
    + '<label><span>币种</span><select name="currency">' + options(filters.currency, [
      ["", "全部"], ["CNY", "CNY"], ["USD", "USD"],
    ]) + '</select></label>'
    + '<label><span>状态</span><select name="status">' + options(filters.status, [
      ["", "全部"], ["active", "生效"], ["superseded", "已替代"], ["disabled", "已停用"],
    ]) + '</select></label>'
    + '<label><span>生效时点</span><input type="datetime-local" name="effectiveAt" value="'
      + escapeAttribute(localDateValue(filters.effective_at)) + '" /></label>'
    + '<button type="submit">筛选</button></form></section>';
}

function rateRow(rate, canManage) {
  return '<tr><td><strong>' + escapeHtml(rate.provider_name) + '</strong><small>'
    + escapeHtml(rate.provider_id) + " / " + escapeHtml(rate.model_alias)
    + " / " + escapeHtml(rate.model_id) + '</small></td>'
    + '<td>' + escapeHtml(rate.currency) + '</td>'
    + '<td>' + micros(rate.input_rate_micros_per_1k, rate.currency) + '</td>'
    + '<td>' + micros(rate.output_rate_micros_per_1k, rate.currency) + '</td>'
    + '<td>' + formatDate(rate.effective_from) + '<small>'
      + formatDate(rate.effective_until) + '</small></td>'
    + '<td>' + escapeHtml(sourceLabel(rate.source_type)) + '<small>'
      + escapeHtml(rate.source_ref) + '</small></td>'
    + '<td>' + status(rate.status) + '<small>v' + rate.version_number
      + " · r" + rate.head_revision + '</small></td>'
    + '<td>' + (canManage ? rateActions(rate) : '<span class="commercial-muted">只读</span>')
    + '</td></tr>';
}

function rateActions(rate) {
  const forms = [];
  if (rate.actions.includes("create_version")) {
    forms.push('<details><summary>新版本</summary><form data-workspace-provider-cost-command-form'
      + ' autocomplete="off"><input type="hidden" name="action" value="create_version" />'
      + rateHidden(rate)
      + numberInput("inputRate", "输入微单位 / 1K", rate.input_rate_micros_per_1k)
      + numberInput("outputRate", "输出微单位 / 1K", rate.output_rate_micros_per_1k)
      + '<label><span>生效时间</span><input type="datetime-local" name="effectiveFrom" required /></label>'
      + '<label><span>结束时间</span><input type="datetime-local" name="effectiveUntil" /></label>'
      + stepUpFields("workspace") + '<button type="submit">创建版本</button></form></details>');
  }
  if (rate.actions.includes("adopt_reference")) {
    forms.push('<details><summary>采用参考</summary><form data-workspace-provider-cost-command-form'
      + ' autocomplete="off"><input type="hidden" name="action" value="adopt_reference" />'
      + rateHidden(rate)
      + input("referenceRateId", "参考 release ID", "", true)
      + stepUpFields("workspace") + '<button type="submit">采用</button></form></details>');
  }
  if (rate.actions.includes("disable")) {
    forms.push('<form data-workspace-provider-cost-command-form autocomplete="off">'
      + '<input type="hidden" name="action" value="disable" />' + rateHidden(rate)
      + stepUpFields("workspace") + '<button class="is-danger" type="submit">停用</button></form>');
  }
  return '<div class="commercial-row-actions">' + forms.join("") + '</div>';
}

function createRateForm() {
  return '<section class="commercial-band commercial-command">'
    + sectionHeading("新建 Provider 成本率", '<span>Workspace owner</span>')
    + '<form data-workspace-provider-cost-command-form autocomplete="off">'
      + '<input type="hidden" name="action" value="create_version" />'
      + '<input type="hidden" name="expectedHeadRevision" value="0" />'
      + input("providerId", "Provider ID", "", true)
      + input("modelId", "模型 ID", "", true)
      + '<label><span>币种</span><select name="currency" required>'
        + '<option value="CNY">CNY</option><option value="USD">USD</option></select></label>'
      + numberInput("inputRate", "输入微单位 / 1K", 0)
      + numberInput("outputRate", "输出微单位 / 1K", 0)
      + '<label><span>生效时间</span><input type="datetime-local" name="effectiveFrom" required /></label>'
      + '<label><span>结束时间</span><input type="datetime-local" name="effectiveUntil" /></label>'
      + stepUpFields("workspace")
      + '<button type="submit">创建成本率</button></form></section>';
}

function rateHidden(rate) {
  return '<input type="hidden" name="resourceId" value="'
    + escapeAttribute(rate.workspace_provider_cost_rate_id) + '" />'
    + '<input type="hidden" name="expectedHeadRevision" value="'
    + escapeAttribute(rate.head_revision) + '" />'
    + '<input type="hidden" name="rateId" value="'
    + escapeAttribute(rate.workspace_provider_cost_rate_id) + '" />'
    + '<input type="hidden" name="providerId" value="' + escapeAttribute(rate.provider_id) + '" />'
    + '<input type="hidden" name="modelId" value="' + escapeAttribute(rate.model_id) + '" />'
    + '<input type="hidden" name="currency" value="' + escapeAttribute(rate.currency) + '" />';
}

function stepUpFields(scope) {
  return '<label><span>验证方式</span><select name="method"'
    + ' data-workspace-commercial-step-up-method="' + escapeAttribute(scope) + '">'
    + '<option value="totp">动态验证码</option><option value="webauthn">安全密钥</option>'
    + '</select></label><label data-workspace-commercial-step-up-code><span>动态验证码</span>'
    + '<input name="verificationCode" inputmode="numeric" autocomplete="one-time-code"'
    + ' pattern="[0-9]{6}" maxlength="6" data-workspace-commercial-sensitive required /></label>';
}

function metric(label, value, detail) {
  return '<div><p>' + escapeHtml(label) + '</p><strong>' + escapeHtml(value ?? "-")
    + '</strong><small>' + escapeHtml(detail ?? "") + '</small></div>';
}
function fact(label, value) {
  return '<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(value ?? "-") + '</dd></div>';
}
function sectionHeading(title, meta) {
  return '<div class="commercial-section-heading"><h2>' + escapeHtml(title)
    + '</h2>' + meta + '</div>';
}
function input(name, label, value, required = false) {
  return '<label><span>' + escapeHtml(label) + '</span><input name="' + escapeAttribute(name)
    + '" value="' + escapeAttribute(value ?? "") + '" maxlength="128"'
    + (required ? " required" : "") + ' /></label>';
}
function numberInput(name, label, value) {
  return '<label><span>' + escapeHtml(label) + '</span><input type="number" name="'
    + escapeAttribute(name) + '" min="0" step="1" value="' + escapeAttribute(value)
    + '" required /></label>';
}
function options(selected, entries) {
  return entries.map(([value, label]) => (
    '<option value="' + escapeAttribute(value) + '"'
      + (value === selected ? " selected" : "") + '>' + escapeHtml(label) + '</option>'
  )).join("");
}
function pagination(previous, next, previousAction, nextAction) {
  return '<div class="commercial-pagination">'
    + '<button type="button" data-workspace-commercial-action="' + previousAction + '"'
      + (previous ? "" : " disabled") + ' aria-label="上一页">&#8592;</button>'
    + '<button type="button" data-workspace-commercial-action="' + nextAction + '"'
      + (next ? "" : " disabled") + ' aria-label="下一页">&#8594;</button></div>';
}
function emptyRow(message, columns) {
  return '<tr><td colspan="' + columns + '" class="commercial-empty">' + escapeHtml(message) + '</td></tr>';
}
function status(value) {
  return '<span class="commercial-status" data-status="' + escapeAttribute(value ?? "unknown")
    + '">' + escapeHtml(phaseLabel(value)) + '</span>';
}
function phaseLabel(value) {
  return ({
    ready: "正常",
    current: "当前",
    loading: "加载中",
    idle: "待加载",
    empty: "暂无数据",
    partial: "部分数据",
    stale: "数据过期",
    reconciliation_required: "需要对账",
    permission_revoked: "权限已撤销",
    session_invalid: "会话已失效",
    revision_conflict: "版本冲突",
    dependency_unavailable: "依赖不可用",
    payment_pending: "付款处理中",
    invoice_failed: "开票失败",
    recovery_in_progress: "恢复中",
    active: "生效",
    superseded: "已替代",
    disabled: "已停用",
    issued: "已开具",
    failed: "失败",
    submitted: "已提交",
    under_review: "审核中",
    approved: "已批准",
    rejected: "已拒绝",
    withdrawn: "已撤回",
  })[value] ?? String(value ?? "未知");
}
function reasonOptions() {
  return [
    ["amount_mismatch", "金额不符"],
    ["duplicate_charge", "重复收费"],
    ["service_not_delivered", "服务未交付"],
    ["tax_mismatch", "税额不符"],
  ].map(([value, label]) => (
    '<option value="' + value + '">' + label + '</option>'
  )).join("");
}
function reasonLabel(value) {
  return ({
    amount_mismatch: "金额不符",
    duplicate_charge: "重复收费",
    service_not_delivered: "服务未交付",
    tax_mismatch: "税额不符",
  })[value] ?? value;
}
function decisionLabel(value) {
  return ({
    facts_confirmed: "事实确认",
    duplicate_confirmed: "重复确认",
    service_credit: "服务抵扣",
    claim_not_supported: "证据不支持",
  })[value] ?? value;
}
function sourceLabel(value) {
  return ({
    manual: "手工版本",
    platform_reference: "官方参考",
    legacy_model_price: "历史模型价格",
    legacy_model: "历史模型基线",
  })[value] ?? value;
}
function money(value, currency) {
  if (value === null || value === undefined) return "-";
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: String(currency),
      currencyDisplay: "code",
    }).format(Number(value) / 100);
  } catch {
    return String(value) + " " + String(currency ?? "");
  }
}
function micros(value, currency) {
  return escapeHtml(String(value)) + '<small>micro ' + escapeHtml(currency) + " / 1K</small>";
}
function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false })
    : "-";
}
function localDateValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}
function escapeAttribute(value) { return escapeHtml(value); }
