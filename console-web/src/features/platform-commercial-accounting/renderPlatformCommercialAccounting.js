/** Platform commercial catalog, subscription projection, and dispute decision workbench. */

export function renderPlatformCommercialAccounting(state = {}, permissions = new Set()) {
  const plans = state.plans?.plans ?? [];
  const references = state.plans?.reference_rates ?? [];
  const subscriptions = state.subscriptions?.subscriptions ?? [];
  const canManagePlans = permissions.has("platform.commercial.plan.manage");
  const canDecide = permissions.has("platform.commercial.dispute.decide");
  return '<div class="platform-commercial-accounting" data-platform-commercial-root aria-busy="'
    + (state.loading?.length > 0 ? 'true' : 'false') + '">'
    + '<header class="platform-page-heading"><div><p>商业账务管理</p>'
      + '<h1>平台商业账务</h1></div><div class="platform-commercial-heading-actions">'
      + '<span>' + escapeHtml(phaseLabel(state.phase)) + '</span>'
      + '<button class="platform-icon-button" type="button"'
      + ' data-platform-action="commercial-refresh" title="刷新商业账务"'
      + ' aria-label="刷新商业账务">&#8635;</button></div></header>'
    + stateMessage(state)
    + planFilters(state)
    + '<section class="platform-band platform-commercial-band">'
      + sectionHeading("套餐版本", String(plans.length) + " 条")
      + commercialTable(
        '<tr><th>套餐</th><th>周期与价格</th><th>状态</th><th>版本事实</th><th>操作</th></tr>',
        plans.map((item) => planRow(item, canManagePlans)).join("")
          || emptyRow(state.loading?.includes("plans") ? "正在读取套餐" : "暂无套餐版本", 5),
      )
      + pagination(
        state.planHistory?.length > 0,
        Boolean(state.plans?.next_cursor),
        "plans",
      )
    + '</section>'
    + (canManagePlans ? createPlanForm(state) : "")
    + '<section class="platform-band platform-commercial-band">'
      + sectionHeading("官方参考成本", String(references.length) + " 条")
      + commercialTable(
        '<tr><th>模型服务 / 模型</th><th>币种</th><th>输入成本</th><th>缓存命中成本</th><th>输出成本</th>'
          + '<th>生效区间</th><th>状态</th><th>操作</th></tr>',
        references.map((item) => referenceRow(item, canManagePlans)).join("")
          || emptyRow(state.loading?.includes("plans") ? "正在读取参考成本" : "暂无参考成本", 8),
      )
    + '</section>'
    + (canManagePlans ? createReferenceForm(state) : "")
    + subscriptionFilters(state)
    + '<section class="platform-band platform-commercial-band">'
    + sectionHeading("组织订阅", String(subscriptions.length) + " 条")
      + subscriptionRows(subscriptions, canDecide)
      + pagination(
        state.subscriptionHistory?.length > 0,
        Boolean(state.subscriptions?.next_cursor),
        "subscriptions",
      )
    + '</section>'
    + '</div>';
}

function planFilters(state) {
  const filters = state.planFilters ?? {};
  return '<section class="platform-band platform-commercial-filter-band">'
    + '<form data-platform-commercial-plan-filter-form autocomplete="off">'
      + field("planCode", "套餐代码", filters.plan_code)
      + select("status", "状态", filters.status, [
        ["", "全部"], ["legacy_reconciled", "历史已核对"], ["candidate", "候选"],
        ["review_required", "待审核"], ["released", "已发布"], ["retired", "已退役"],
      ])
      + select("currency", "币种", filters.currency, [
        ["", "全部"], ["CNY", "CNY"], ["USD", "USD"],
      ])
      + select("billingPeriod", "周期", filters.billing_period, [
        ["", "全部"], ["none", "无周期"], ["monthly", "月付"], ["yearly", "年付"],
      ])
      + '<button type="submit">筛选套餐</button>'
    + '</form></section>';
}

function subscriptionFilters(state) {
  const filters = state.subscriptionFilters ?? {};
  return '<section class="platform-band platform-commercial-filter-band">'
    + '<form data-platform-commercial-subscription-filter-form autocomplete="off">'
      + field("status", "订阅状态", filters.status)
      + field("planCatalogId", "套餐目录 ID", filters.plan_catalog_id)
      + select("projectionState", "同步状态", filters.projection_state, [
        ["", "全部"], ["current", "当前"], ["partial", "部分"], ["stale", "过期"],
        ["reconciliation_required", "需要对账"], ["unknown", "未知"],
      ])
      + '<label><span>开始时间</span><input type="datetime-local" name="from" value="'
        + escapeAttribute(localDateValue(filters.from)) + '" /></label>'
      + '<label><span>结束时间</span><input type="datetime-local" name="until" value="'
        + escapeAttribute(localDateValue(filters.until)) + '" /></label>'
      + '<button type="submit">筛选订阅</button>'
    + '</form></section>';
}

function planRow(item, canManage) {
  return '<tr><td><strong>' + escapeHtml(item.display_name) + '</strong><small>'
    + escapeHtml(item.plan_code) + " · " + escapeHtml(item.plan_catalog_id) + '</small></td>'
    + '<td>' + escapeHtml(periodLabel(item.billing_period)) + '<small>'
      + money(item.unit_price_minor, item.currency) + '</small></td>'
    + '<td>' + status(item.status) + '<small>' + escapeHtml(item.readiness) + '</small></td>'
    + '<td><code>' + escapeHtml(item.plan_version_id) + '</code><small>r'
      + item.revision + " · g" + item.release_generation + '</small></td>'
    + '<td>' + (canManage ? planActions(item) : '<span class="platform-restricted">只读</span>')
      + '</td></tr>';
}

function planActions(item) {
  return '<div class="platform-commercial-row-actions">'
    + item.actions.map((action) => (
      '<form data-platform-commercial-plan-command-form autocomplete="off">'
        + hidden("action", action)
        + hidden("planVersionId", item.plan_version_id)
        + hidden("resourceId", item.platform_plan_catalog_release_id)
        + hidden("expectedRevision", item.revision)
        + (action === "release"
          ? field("governanceApprovalId", "治理批准 ID", "", true) : "")
        + stepUpFields()
        + '<button type="submit">' + escapeHtml(actionLabel(action)) + '</button>'
      + '</form>'
    )).join("") + '</div>';
}

function createPlanForm(state) {
  return '<section class="platform-band platform-commercial-command">'
    + sectionHeading("新建套餐候选", "需要验证")
    + '<form data-platform-commercial-plan-create-form autocomplete="off">'
      + field("planCode", "套餐代码", "", true)
      + field("displayName", "显示名称", "", true)
      + numberField("displayOrder", "显示顺序", 0, 0)
      + select("billingPeriod", "计费周期", "monthly", [
        ["none", "无周期"], ["monthly", "月付"], ["yearly", "年付"],
      ])
      + select("currency", "币种", "CNY", [["CNY", "CNY"], ["USD", "USD"]])
      + numberField("unitPriceMinor", "单价（最小币种单位）", 0, 0)
      + numberField("trialDays", "试用天数", 0, 0, 90)
      + numberField("gatewayRequests", "月请求额度", 0, 0)
      + numberField("workspaceSeats", "Workspace 席位", 1, 1)
      + numberField("modelAliases", "模型别名数", 1, 1)
      + numberField("retentionDays", "保留天数", 30, 1)
      + '<label class="platform-check"><input type="checkbox" name="prioritySupport" />'
        + '<span>优先支持</span></label>'
      + stepUpFields()
      + '<button class="platform-primary" type="submit"'
        + (state.intents?.plan ? " disabled" : "") + '>创建候选</button>'
    + '</form></section>';
}

function referenceRow(item, canManage) {
  return '<tr><td><strong>' + escapeHtml(item.provider_id) + '</strong><small>'
    + escapeHtml(item.model_id) + '</small></td><td>' + escapeHtml(item.currency) + '</td>'
    + '<td>' + micros(item.input_rate_micros_per_1k, item.currency) + '</td>'
    + '<td>' + micros(item.cache_rate_micros_per_1k, item.currency) + '</td>'
    + '<td>' + micros(item.output_rate_micros_per_1k, item.currency) + '</td>'
    + '<td>' + formatDate(item.effective_from) + '<small>'
      + formatDate(item.effective_until) + '</small></td>'
    + '<td>' + status(item.status) + '<small>r' + item.revision
      + " · g" + item.release_generation + '</small></td>'
    + '<td>' + (canManage ? referenceActions(item) : '<span class="platform-restricted">只读</span>')
      + '</td></tr>';
}

function referenceActions(item) {
  return '<div class="platform-commercial-row-actions">'
    + item.actions.map((action) => (
      '<form data-platform-commercial-reference-command-form autocomplete="off">'
        + hidden("action", action)
        + hidden("providerId", item.provider_id)
        + hidden("referenceRateId", item.provider_reference_rate_release_id)
        + hidden("resourceId", item.provider_reference_rate_release_id)
        + hidden("expectedRevision", item.revision)
        + (action === "release"
          ? field("governanceApprovalId", "治理批准 ID", "", true) : "")
        + stepUpFields()
        + '<button type="submit">' + escapeHtml(actionLabel(action)) + '</button>'
      + '</form>'
    )).join("") + '</div>';
}

function createReferenceForm(state) {
  return '<section class="platform-band platform-commercial-command">'
    + sectionHeading("新建官方参考成本候选", "平台管理员")
    + '<form data-platform-commercial-reference-create-form autocomplete="off">'
      + field("providerId", "服务商", "", true)
      + field("modelId", "模型 ID", "", true)
      + select("currency", "币种", "CNY", [["CNY", "CNY"], ["USD", "USD"]])
      + numberField("inputRate", "输入微单位 / 百万 Token", 0, 0)
      + numberField("cacheRate", "缓存命中微单位 / 百万 Token", 0, 0)
      + numberField("outputRate", "输出微单位 / 百万 Token", 0, 0)
      + '<label><span>生效时间</span><input type="datetime-local"'
        + ' name="effectiveFrom" required /></label>'
      + '<label><span>结束时间</span><input type="datetime-local"'
        + ' name="effectiveUntil" /></label>'
      + field("sourceVersion", "来源版本", "", true)
      + numberField("expectedRevision", "当前 head revision", 0, 0)
      + stepUpFields()
      + '<button class="platform-primary" type="submit"'
        + (state.intents?.reference ? " disabled" : "") + '>创建候选</button>'
    + '</form></section>';
}

function subscriptionRows(subscriptions, canDecide) {
  if (!subscriptions.length) {
    return '<div class="platform-commercial-empty">当前筛选下没有订阅投影</div>';
  }
  return '<div class="platform-commercial-subscriptions">'
    + subscriptions.map((subscription) => (
      '<article><header><div><strong>' + escapeHtml(subscription.display_name) + '</strong>'
        + '<small>' + escapeHtml(subscription.workspace_customer_ref) + " · "
          + escapeHtml(subscription.subscription_id) + '</small></div>'
        + status(subscription.projection_state) + '</header>'
        + '<dl>'
          + fact("订阅", subscription.subscription_status)
          + fact("周期结束", formatDate(subscription.current_period_ends_at))
          + fact("应付", money(subscription.current_due_minor, subscription.currency))
          + fact("已支付", money(subscription.paid_minor, subscription.currency))
          + fact("已退款", money(subscription.refunded_minor, subscription.currency))
          + fact("已开票", money(subscription.invoiced_minor, subscription.currency))
        + '</dl>'
        + '<div class="platform-commercial-projection-status">'
          + status(subscription.order_status) + status(subscription.payment_status)
          + status(subscription.invoice_status) + status(subscription.dispute_status)
        + '</div>'
        + invoiceSummary(subscription.invoices)
        + platformDisputes(subscription.disputes, canDecide)
      + '</article>'
    )).join("") + '</div>';
}

function invoiceSummary(invoices) {
  return '<div class="platform-commercial-nested"><h3>发票</h3>'
    + commercialTable(
      '<tr><th>发票</th><th>金额</th><th>税额</th><th>状态</th></tr>',
      (invoices ?? []).map((invoice) => (
        '<tr><td><strong>' + escapeHtml(invoice.invoice_number_masked ?? "未编号")
          + '</strong><small>' + escapeHtml(invoice.platform_invoice_id) + '</small></td>'
          + '<td>' + money(invoice.total_minor, invoice.currency) + '</td>'
          + '<td>' + money(invoice.tax_minor, invoice.currency) + '</td>'
          + '<td>' + status(invoice.status) + '</td></tr>'
      )).join("") || emptyRow("暂无发票", 4),
    ) + '</div>';
}

function platformDisputes(disputes, canDecide) {
  return '<div class="platform-commercial-nested"><h3>商业争议</h3>'
    + commercialTable(
      '<tr><th>争议</th><th>金额</th><th>原因</th><th>状态</th><th>裁决</th></tr>',
      (disputes ?? []).map((item) => (
        '<tr><td><code>' + escapeHtml(item.billing_dispute_case_id)
          + '</code><small>' + escapeHtml(item.source_ref) + '</small></td>'
          + '<td>' + money(item.disputed_amount_minor, item.currency) + '</td>'
          + '<td>' + escapeHtml(reasonLabel(item.reason_code)) + '</td>'
          + '<td>' + status(item.status) + '</td>'
          + '<td>' + (canDecide ? disputeActions(item)
            : '<span class="platform-restricted">只读</span>') + '</td></tr>'
      )).join("") || emptyRow("暂无商业争议", 5),
    ) + '</div>';
}

function disputeActions(item) {
  return '<div class="platform-commercial-row-actions">'
    + item.actions.map((action) => (
      '<form data-platform-commercial-dispute-command-form autocomplete="off">'
        + hidden("action", action)
        + hidden("disputeId", item.billing_dispute_case_id)
        + hidden("expectedRevision", item.state_revision)
        + hidden("expectedSourceRevision", item.source_revision)
        + (action === "approve_credit"
          ? select("decisionCode", "裁决", "facts_confirmed", [
            ["facts_confirmed", "事实确认"],
            ["duplicate_confirmed", "重复确认"],
            ["service_credit", "服务抵扣"],
          ])
          : action === "reject"
            ? hidden("decisionCode", "claim_not_supported") : "")
        + stepUpFields()
        + '<button type="submit">' + escapeHtml(actionLabel(action)) + '</button>'
      + '</form>'
    )).join("") + '</div>';
}

function stepUpFields() {
  return '<label><span>验证方式</span><select name="method"'
    + ' data-platform-commercial-step-up-method>'
      + '<option value="totp">动态验证码</option><option value="webauthn">安全密钥</option>'
    + '</select></label><label data-platform-commercial-step-up-code><span>动态验证码</span>'
      + '<input name="verificationCode" inputmode="numeric" autocomplete="one-time-code"'
      + ' pattern="[0-9]{6}" maxlength="6" data-platform-sensitive required /></label>';
}

function commercialTable(head, body) {
  return '<div class="platform-table-wrap"><table class="platform-commercial-table"><thead>'
    + head + '</thead><tbody>' + body + '</tbody></table></div>';
}
function pagination(previous, next, kind) {
  return '<div class="platform-commercial-pagination">'
    + '<button type="button" data-platform-commercial-page="' + escapeAttribute(kind)
      + '" data-direction="previous"' + (previous ? "" : " disabled")
      + ' aria-label="上一页">&#8592;</button>'
    + '<button type="button" data-platform-commercial-page="' + escapeAttribute(kind)
      + '" data-direction="next"' + (next ? "" : " disabled")
      + ' aria-label="下一页">&#8594;</button></div>';
}
function stateMessage(state) {
  const feedback = state.feedback
    ? '<p class="platform-feedback" role="status">' + escapeHtml(state.feedback) + '</p>' : "";
  if (!state.error) return feedback;
  return feedback + '<div class="platform-error" role="alert"><strong>'
    + escapeHtml(phaseLabel(state.phase)) + '</strong><span>'
    + escapeHtml(state.error.message) + '</span><code>'
    + escapeHtml(state.error.requestId ?? "-") + '</code></div>';
}
function sectionHeading(title, meta) {
  return '<div class="platform-section-heading"><div><p>账务记录</p><h2>'
    + escapeHtml(title) + '</h2></div><span>' + escapeHtml(meta) + '</span></div>';
}
function field(name, label, value, required = false) {
  return '<label><span>' + escapeHtml(label) + '</span><input name="'
    + escapeAttribute(name) + '" value="' + escapeAttribute(value ?? "") + '" maxlength="160"'
    + (required ? " required" : "") + ' /></label>';
}
function numberField(name, label, value, minimum, maximum = null) {
  return '<label><span>' + escapeHtml(label) + '</span><input type="number" name="'
    + escapeAttribute(name) + '" value="' + escapeAttribute(value) + '" min="'
    + escapeAttribute(minimum) + '" step="1"'
    + (maximum === null ? "" : ' max="' + escapeAttribute(maximum) + '"')
    + ' required /></label>';
}
function select(name, label, selected, entries) {
  return '<label><span>' + escapeHtml(label) + '</span><select name="'
    + escapeAttribute(name) + '">'
    + entries.map(([value, optionLabel]) => (
      '<option value="' + escapeAttribute(value) + '"'
        + (value === selected ? " selected" : "") + '>'
        + escapeHtml(optionLabel) + '</option>'
    )).join("") + '</select></label>';
}
function hidden(name, value) {
  return '<input type="hidden" name="' + escapeAttribute(name)
    + '" value="' + escapeAttribute(value) + '" />';
}
function fact(label, value) {
  return '<div><dt>' + escapeHtml(label) + '</dt><dd>' + escapeHtml(value ?? "-") + '</dd></div>';
}
function emptyRow(message, columns) {
  return '<tr><td colspan="' + columns + '" class="platform-empty">'
    + '<span class="platform-empty-label">' + escapeHtml(message) + '</span></td></tr>';
}
function status(value) {
  return '<span class="platform-status" data-status="' + escapeAttribute(value ?? "unknown")
    + '">' + escapeHtml(phaseLabel(value)) + '</span>';
}
function phaseLabel(value) {
  return ({
    ready: "正常", loading: "加载中", idle: "待加载", empty: "暂无数据",
    partial: "部分数据", stale: "数据过期", reconciliation_required: "需要对账",
    permission_revoked: "权限已撤销", revision_conflict: "版本冲突",
    dependency_unavailable: "依赖不可用", candidate: "候选", review_required: "待审核",
    released: "已发布", retired: "已退役", legacy_reconciled: "历史已核对",
    current: "当前", active: "生效", superseded: "已替代", disabled: "已停用",
    submitted: "已提交", under_review: "审核中", approved: "已批准",
    rejected: "已拒绝", withdrawn: "已撤回", issued: "已开具", failed: "失败",
  })[value] ?? String(value ?? "未知");
}
function actionLabel(value) {
  return ({
    submit_review: "提交审核", release: "发布", retire: "退役",
    start_review: "开始审核", approve_credit: "批准抵扣", reject: "拒绝",
  })[value] ?? value;
}
function periodLabel(value) {
  return ({ none: "无周期", monthly: "月付", yearly: "年付" })[value] ?? value;
}
function reasonLabel(value) {
  return ({
    amount_mismatch: "金额不符", duplicate_charge: "重复收费",
    service_not_delivered: "服务未交付", tax_mismatch: "税额不符",
  })[value] ?? value;
}
function money(value, currency) {
  if (value === null || value === undefined) return "-";
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency", currency: String(currency), currencyDisplay: "code",
    }).format(Number(value) / 100);
  } catch {
    return String(value) + " " + String(currency ?? "");
  }
}
function micros(value, currency) {
  return escapeHtml(String(Number(value || 0))) + '<small>micro ' + escapeHtml(currency) + " / 百万 Token</small>";
}
function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false }) : "-";
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
