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
  const models = state.models?.models ?? [];
  const available = new Set(
    (Array.isArray(navigationItems) ? navigationItems : [])
      .filter((item) => item?.visible !== false && item?.disabled !== true)
      .map((item) => item.id),
  );
  return '<section class="workspace-commercial-accounting" data-workspace-commercial-root="internal-finops">'
    + pageHeading(
      "组织成本",
      "模型成本与用量记录",
      state.loading?.length ? "正在同步" : String(models.length) + " 个模型",
    )
     + stateMessage(state)
     + fxRateSection(state)
     + finOpsLinks(available)
    + '<section class="commercial-band">'
      + sectionHeading("模型成本", '<button type="button" data-model-cost-open-catalog>前往模型目录修改价格</button>')
      + '<div class="commercial-table-wrap"><table class="commercial-table">'
        + '<thead><tr><th>模型服务 / 模型</th><th>币种</th><th>输入 / 百万 Token</th>'
        + '<th>缓存命中 / 百万 Token</th><th>输出 / 百万 Token</th><th>状态</th></tr></thead>'
        + '<tbody>'
          + (models.map(modelCostRow).join("")
            || emptyRow(state.loading?.length ? "正在读取模型价格" : "暂无模型", 6))
        + '</tbody>'
      + '</table></div>'
    + '</section>'
    + '</section>';
}

function modelCostRow(model) {
  const priced = [model.input_price_per_1k, model.cache_price_per_1k, model.output_price_per_1k]
    .every((value) => value !== null && value !== undefined && value !== "");
  const price = (value) => priced ? escapeHtml(value) : "未配置";
  return '<tr><td><strong>' + escapeHtml(model.provider_name || "未命名服务") + '</strong><small>'
    + escapeHtml(model.display_name || model.internal_model_id || "未命名模型") + '</small></td>'
    + '<td>' + escapeHtml(model.currency || "-") + '</td>'
    + '<td>' + price(model.input_price_per_1k) + '</td>'
    + '<td>' + price(model.cache_price_per_1k) + '</td>'
    + '<td>' + price(model.output_price_per_1k) + '</td>'
    + '<td>' + status(model.status) + '</td></tr>';
}

function fxRateSection(state) {
  const rates = state.fxRates?.rates ?? [];
  const current = rates.find((rate) => rate.status === "active") ?? rates[0] ?? null;
  return '<section class="commercial-band commercial-fx-rate" aria-label="汇率设置">'
    + sectionHeading("成本比较汇率", '<span>USD → CNY</span>')
    + '<div class="commercial-fx-summary"><strong>1 USD = '
    + escapeHtml(current?.usd_to_cny ?? "未设置") + ' CNY</strong>'
    + (current ? '<small>自 ' + escapeHtml(formatDate(current.effective_from)) + ' 生效 · 第 '
      + escapeHtml(current.version_number) + ' 版</small>' : '') + '</div>'
    + (state.canManageRates ? '<form class="commercial-fx-form" data-workspace-fx-rate-command-form autocomplete="off">'
      + '<label><span>1 USD 兑换 CNY</span><input name="usdToCny" inputmode="decimal" pattern="[0-9]+(\\.[0-9]{1,8})?" value="'
      + escapeAttribute(current?.usd_to_cny ?? "7.2") + '" required /></label>'
      + '<label><span>生效时间</span><input type="datetime-local" name="effectiveFrom" required /></label>'
      + '<input type="hidden" name="expectedHeadRevision" value="' + escapeAttribute(current?.revision ?? 0) + '" />'
      + stepUpFields("workspace") + '<button type="submit">保存汇率</button></form>' : '')
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
    ["usage", "workspace.finance.usage", "用量与价格"],
    ["finops-ledger", "workspace.finance.internal-finops", "成本账本"],
    ["quota-buckets", "workspace.finance.quota", "额度"],
    ["budgets", "workspace.finance.budgets", "预算"],
  ].filter(([id]) => available.has(id));
  if (!links.length) return "";
  return '<nav class="commercial-related-links" aria-label="内部 FinOps 关联页面">'
    + links.map(([id, route, label]) => (
      '<button type="button" data-console-nav="' + escapeAttribute(id) + '" data-console-route="' + escapeAttribute(route) + '" data-console-target="' + escapeAttribute(id) + '">' + escapeHtml(label) + '</button>'
    )).join("") + '</nav>';
}

function stepUpFields(scope) {
  return '<label><span>验证方式</span><select name="method"'
    + ' aria-label="验证方式" data-workspace-commercial-step-up-method="' + escapeAttribute(scope) + '">'
    + '<option value="totp">动态验证码</option><option value="webauthn">安全密钥</option>'
    + '</select></label><label data-workspace-commercial-step-up-code><span>动态验证码</span>'
    + '<input name="verificationCode" aria-label="动态验证码" inputmode="numeric" autocomplete="one-time-code"'
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
function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString("zh-CN", { hour12: false })
    : "-";
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/gu, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}
function escapeAttribute(value) { return escapeHtml(value); }
