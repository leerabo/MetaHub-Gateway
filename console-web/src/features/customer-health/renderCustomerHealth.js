/** PAGE-118 只渲染已校验聚合结果，不在前端推导权限、租户或源业务事实。 */

import { customerHealthPageOwner } from "./customerHealthPageState.js";

// 信号标签仅映射冻结的公开代码，未知代码不会被当成导航或富文本解释。
const SIGNAL_LABELS = Object.freeze({
  usage_trend: "usage_trend",
  reliability: "reliability",
  cost_anomaly_health: "cost_anomaly_health",
  budget_readiness: "budget_readiness",
  team_activity: "team_activity",
  ticket_health: "ticket_health",
});

/** 在后端导航 owner 有效时渲染客户健康页，否则返回空内容并阻止未授权预取结果展示。 */
export function renderCustomerHealth(state, authContext, consoleShellState, i18n) {
  if (!customerHealthPageOwner(authContext, consoleShellState)) return "";
  const t = (key, fallback, values = {}) => i18n?.t?.(key, values, fallback) ?? fallback;
  const loading = !state || state.state === "loading";
  const header = `
    <header class="customer-health-header">
      <div>
        <p class="customer-health-kicker">${escapeHtml(t("customer_health.scope", "当前租户 · 7 日评分 · UTC"))}</p>
        <h2 id="customer-health-title">${escapeHtml(t("customer_health.title", "客户健康"))}</h2>
      </div>
      <button type="button" class="icon-command customer-health-refresh" data-customer-health-refresh
        title="${escapeAttribute(t("common.refresh", "刷新"))}"
        aria-label="${escapeAttribute(t("customer_health.refresh_aria", "刷新客户健康评分"))}"
        ${loading ? "disabled aria-disabled=\"true\"" : ""}>&#8635;</button>
    </header>
  `;
  if (loading) {
    return `${header}<div class="customer-health-loading" role="status">${escapeHtml(t("customer_health.loading", "正在读取客户健康评分"))}</div>`;
  }
  // 当前错误只经脱敏渲染器展示，未知状态也按固定不可用错误失败关闭。
  if (state.state === "error") return `${header}${renderError(state.error, t)}`;
  if (state.state !== "ready" || !state.data) return `${header}${renderError(null, t)}`;

  const data = state.data;
  const noSamples = data.signals.some((signal) => signal.availability === "no_samples");
  return `
    ${header}
    ${data.freshness_status === "stale" ? `<p class="customer-health-notice is-stale" role="status">${escapeHtml(t("customer_health.stale", "当前显示最后一份可信评分，后台评估已延迟。"))}</p>` : ""}
    ${noSamples ? `<p class="customer-health-notice" role="status">${escapeHtml(t("customer_health.no_samples", "部分信号暂无样本，评分仍按冻结规则计算。"))}</p>` : ""}
    <section class="customer-health-summary" aria-labelledby="customer-health-summary-title">
      <div class="customer-health-summary-heading">
        <h3 id="customer-health-summary-title">${escapeHtml(t("customer_health.summary", "健康概览"))}</h3>
        <span class="customer-health-risk is-${escapeAttribute(data.risk_level)}">${escapeHtml(riskLabel(data.risk_level, t))}</span>
      </div>
      <div class="customer-health-metrics">
        ${metric(t("customer_health.score", "健康得分"), formatBps(data.score_bps))}
        ${metric(t("customer_health.coverage", "有效权重"), formatBps(data.effective_weight_bps))}
        ${metric(t("customer_health.activation", "激活阶段"), `${data.activation.completed_stage_count} / ${data.activation.total_stage_count}`)}
        ${metric(t("customer_health.evaluated", "评估时间"), formatUtc(data.evaluated_at))}
      </div>
      <progress class="customer-health-progress is-${escapeAttribute(data.risk_level)}" max="10000" value="${data.score_bps}">${formatBps(data.score_bps)}</progress>
      ${renderTags(data.risk_tags, t)}
    </section>
    <section class="customer-health-signals" aria-labelledby="customer-health-signals-title">
      <div class="customer-health-section-heading">
        <h3 id="customer-health-signals-title">${escapeHtml(t("customer_health.signals", "信号贡献"))}</h3>
        <time datetime="${escapeAttribute(data.window.end)}">${escapeHtml(t("customer_health.window", "截至 {value}", { value: formatUtc(data.window.end) }))}</time>
      </div>
      ${renderSignals(data.signals, t)}
    </section>
    <section class="customer-health-actions" aria-labelledby="customer-health-actions-title">
      <h3 id="customer-health-actions-title">${escapeHtml(t("customer_health.actions", "建议动作"))}</h3>
      <ul>${data.recommended_actions.map((action) => renderAction(action, consoleShellState, t)).join("")}</ul>
    </section>
    <section class="customer-health-activation" aria-labelledby="customer-health-activation-title">
      <h3 id="customer-health-activation-title">${escapeHtml(t("customer_health.activation_context", "激活上下文"))}</h3>
      <p>${escapeHtml(data.activation.activated
        ? t("customer_health.activation_complete", "当前租户已完成产品激活。")
        : t("customer_health.activation_incomplete", "当前租户完成 {value}/7 个激活阶段。", { value: data.activation.completed_stage_count }))}</p>
    </section>
  `;
}

/** 将六个冻结信号渲染为可在桌面与移动端重排的只读安全证据表。 */
function renderSignals(signals, t) {
  return `
    <div class="customer-health-table" role="table" aria-label="${escapeAttribute(t("customer_health.signals", "信号贡献"))}">
      <div class="customer-health-row customer-health-table-head" role="row">
        <span role="columnheader">${escapeHtml(t("customer_health.column.signal", "信号"))}</span>
        <span role="columnheader">${escapeHtml(t("customer_health.column.status", "状态"))}</span>
        <span role="columnheader">${escapeHtml(t("customer_health.column.score", "得分"))}</span>
        <span role="columnheader">${escapeHtml(t("customer_health.column.weight", "权重"))}</span>
        <span role="columnheader">${escapeHtml(t("customer_health.column.contribution", "贡献"))}</span>
        <span role="columnheader">${escapeHtml(t("customer_health.column.evidence", "安全证据"))}</span>
      </div>
      ${signals.map((signal) => `
        <div class="customer-health-row" role="row" data-customer-health-signal="${escapeAttribute(signal.code)}">
          <strong role="cell" data-label="${escapeAttribute(t("customer_health.column.signal", "信号"))}">${escapeHtml(signalLabel(signal.code, t))}</strong>
          <span role="cell" data-label="${escapeAttribute(t("customer_health.column.status", "状态"))}"><span class="customer-health-status is-${escapeAttribute(signal.health_status)}">${escapeHtml(statusLabel(signal, t))}</span></span>
          <span role="cell" data-label="${escapeAttribute(t("customer_health.column.score", "得分"))}">${escapeHtml(formatBps(signal.score_bps))}</span>
          <span role="cell" data-label="${escapeAttribute(t("customer_health.column.weight", "权重"))}">${escapeHtml(formatBps(signal.weight_bps))}</span>
          <span role="cell" data-label="${escapeAttribute(t("customer_health.column.contribution", "贡献"))}">${escapeHtml(formatBps(signal.contribution_bps))}</span>
          <span role="cell" data-label="${escapeAttribute(t("customer_health.column.evidence", "安全证据"))}">${escapeHtml(evidenceLabel(signal, t))}</span>
        </div>
      `).join("")}
    </div>
  `;
}

/** 渲染服务端已排序的风险标签，不根据前端临时状态增删风险结论。 */
function renderTags(tags, t) {
  if (!tags.length) {
    return `<p class="customer-health-tags is-empty">${escapeHtml(t("customer_health.tags_empty", "当前没有风险标签"))}</p>`;
  }
  return `<ul class="customer-health-tags" aria-label="${escapeAttribute(t("customer_health.tags", "风险标签"))}">${tags.map((tag) => (
    `<li>${escapeHtml(t(`customer_health.tag.${tag}`, tag))}</li>`
  )).join("")}</ul>`;
}

/** 仅为后端导航中仍可见可用的目标生成链接，其余建议保持不可跳转文本。 */
function renderAction(action, consoleShellState, t) {
  const label = t(`customer_health.action.${action.code}`, action.code);
  const target = authorizedActionTarget(action.target_navigation_item_id, consoleShellState);
  return target
    ? `<li><a href="#${encodeURIComponent(target)}">${escapeHtml(label)} <span aria-hidden="true">&#8594;</span></a></li>`
    : `<li><span>${escapeHtml(label)}</span></li>`;
}

/** 核验建议目标与后端导航镜像完全一致，防止服务端字符串形成越权链接。 */
function authorizedActionTarget(target, consoleShellState) {
  const normalized = String(target ?? "");
  if (!/^[A-Za-z0-9_.:-]{1,80}$/.test(normalized)) return "";
  const items = consoleShellState?.navigation?.navigation_items;
  const owned = Array.isArray(items) && items.some((item) => (
    item?.id === normalized
    && item?.target_section === normalized
    && item?.visible === true
    && item?.disabled === false
  ));
  return owned ? normalized : "";
}

/** 将计数和比率转换为固定摘要，不展示用户、请求正文或上游异常明文。 */
function evidenceLabel(signal, t) {
  const current = formatInteger(signal.current_value);
  const comparison = formatInteger(signal.comparison_value);
  const rate = formatBps(signal.rate_bps);
  // 每类证据仅显示安全聚合计数和比率，绝不拼接请求、用户或异常正文。
  if (signal.code === "usage_trend") return t("customer_health.evidence.usage", "当前 {current} · 前期 {comparison} · 趋势 {rate}", { current, comparison, rate });
  if (signal.code === "reliability") return t("customer_health.evidence.reliability", "成功 {current} / 请求 {comparison} · 成功率 {rate}", { current, comparison, rate });
  if (signal.code === "cost_anomaly_health") return t("customer_health.evidence.cost", "未解决 {current} · 严重 {comparison}", { current, comparison });
  if (signal.code === "budget_readiness") return t("customer_health.evidence.budget", "有效预算规则 {current}", { current });
  if (signal.code === "team_activity") return t("customer_health.evidence.team", "近期活跃 {current} / 可用用户 {comparison} · 活跃率 {rate}", { current, comparison, rate });
  return t("customer_health.evidence.ticket", "工单数据源未接入");
}

/** 将冻结信号代码映射为本地化名称，未知值仍按转义后的普通文本显示。 */
function signalLabel(code, t) {
  return t(`customer_health.signal.${SIGNAL_LABELS[code]}`, code);
}

/** 按可用性优先选择状态文案，明确区分未接入、无样本和健康等级。 */
function statusLabel(signal, t) {
  if (signal.availability === "unavailable") return t("customer_health.status.unavailable", "未接入");
  if (signal.availability === "no_samples") return t("customer_health.status.no_samples", "暂无样本");
  return t(`customer_health.status.${signal.health_status}`, signal.health_status);
}

/** 把服务端风险等级转换为本地化标签，不在渲染层重新计算风险。 */
function riskLabel(risk, t) {
  return t(`customer_health.risk.${risk}`, risk);
}

/** 仅展示固定错误码和 request_id，屏蔽异常正文、Token 与上游响应。 */
function renderError(error, t) {
  const code = escapeHtml(error?.code ?? "customer_health.unavailable");
  const requestId = escapeHtml(error?.requestId ?? "-");
  return `
    <div class="customer-health-error" role="alert">
      <strong>${escapeHtml(t("customer_health.error", "客户健康评分暂不可用"))}</strong>
      <span>${escapeHtml(t("customer_health.error_code", "错误码"))}: <code>${code}</code></span>
      <span>request_id: <code>${requestId}</code></span>
    </div>
  `;
}

/** 生成经过转义的摘要指标单元，标签和值均不得作为 HTML 注入。 */
function metric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/** 将整数基点格式化为百分比，缺失值统一显示短横线而不猜测数据。 */
function formatBps(value) {
  if (value === null || value === undefined) return "-";
  const percent = value / 100;
  return `${percent.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}%`;
}

/** 以中文数字分组格式展示安全计数，空值保持明确缺失状态。 */
function formatInteger(value) {
  return value === null || value === undefined ? "-" : value.toLocaleString("zh-CN");
}

/** 将已校验 UTC 整秒文本转换为可读形式，不执行本地时区推断。 */
function formatUtc(value) {
  return value ? String(value).replace("T", " ").replace("Z", " UTC") : "-";
}

/** 转义所有 HTML 文本元字符，阻止评分或诊断字段进入可执行标记。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;")
    .replaceAll("`", "&#096;");
}

/** 复用 HTML 转义处理属性值，避免导航目标或文案突破属性边界。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
