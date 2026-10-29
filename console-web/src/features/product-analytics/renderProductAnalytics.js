/** PAGE-117 只读激活漏斗渲染器；只消费已校验聚合，不推导权限、租户或事件事实。 */

import { productAnalyticsPageOwner } from "./productAnalyticsPageState.js";

// 固定七阶段仅映射受控 i18n 键，后端 event_type 不会作为任意 HTML 文本直接渲染。
const STAGE_MESSAGE_KEYS = Object.freeze({
  "tenant.registered": "product_analytics.stage.tenant_registered",
  "provider.created": "product_analytics.stage.provider_created",
  "virtual_key.created": "product_analytics.stage.virtual_key_created",
  "gateway.first_call_succeeded": "product_analytics.stage.gateway_first_call_succeeded",
  "budget.configured": "product_analytics.stage.budget_configured",
  "teammate.invited": "product_analytics.stage.teammate_invited",
  "request_logs.viewed": "product_analytics.stage.request_logs_viewed",
});

/** 在完整页面 owner 有效时渲染漏斗；未授权、离页或租户失配时返回空内容。 */
export function renderProductAnalytics(state, authContext, consoleShellState, i18n) {
  if (!productAnalyticsPageOwner(authContext, consoleShellState)) return "";
  const t = (key, fallback, values = {}) => i18n?.t?.(key, values, fallback) ?? fallback;
  const loading = !state || state.state === "loading";
  const header = `
    <header class="product-analytics-header">
      <div>
        <p class="product-analytics-kicker">${escapeHtml(t("product_analytics.scope", "当前租户 · 全历史 · UTC"))}</p>
        <h2 id="product-analytics-title">${escapeHtml(t("product_analytics.title", "增长分析"))}</h2>
      </div>
      <button type="button" class="icon-command product-analytics-refresh" data-product-analytics-refresh
        title="${escapeAttribute(t("common.refresh", "刷新"))}"
        aria-label="${escapeAttribute(t("product_analytics.refresh_aria", "刷新增长分析"))}"
        ${loading ? "disabled aria-disabled=\"true\"" : ""}>&#8635;</button>
    </header>
  `;

  if (loading) {
    return `${header}<div class="product-analytics-loading" role="status">${escapeHtml(t("product_analytics.loading", "正在读取激活漏斗"))}</div>`;
  }
  // 错误态只渲染状态机裁剪后的固定 code 与 request_id，不读取服务端消息正文。
  if (state.state === "error") {
    return `${header}${renderError(state.error, t)}`;
  }
  if (state.state !== "ready" || !state.data) {
    return `${header}${renderError(null, t)}`;
  }

  const data = state.data;
  const summary = data.summary;
  const initialNotice = summary.completed_stage_count === 1 && data.stages[0]?.reached
    ? `<p class="product-analytics-initial" role="status">${escapeHtml(t("product_analytics.initial", "租户已注册，继续完成接入步骤即可推进激活。"))}</p>`
    : summary.completed_stage_count === 0
      ? `<p class="product-analytics-initial" role="status">${escapeHtml(t("product_analytics.empty", "当前租户尚无激活事件。"))}</p>`
      : "";

  return `
    ${header}
    <section class="product-analytics-summary" aria-labelledby="product-analytics-summary-title">
      <div class="product-analytics-summary-heading">
        <h3 id="product-analytics-summary-title">${escapeHtml(t("product_analytics.summary", "激活概览"))}</h3>
        <span class="product-analytics-activation ${summary.activated ? "is-activated" : ""}">
          ${escapeHtml(summary.activated
            ? t("product_analytics.activated", "已激活")
            : t("product_analytics.not_activated", "未激活"))}
        </span>
      </div>
      <div class="product-analytics-metrics">
        ${metric(t("product_analytics.completed", "完成阶段"), `${summary.completed_stage_count} / ${summary.total_stage_count}`)}
        ${metric(t("product_analytics.depth", "连续深度"), `${summary.cumulative_stage_count} / ${summary.total_stage_count}`)}
        ${metric(t("product_analytics.completion", "完成率"), formatBps(summary.completion_bps))}
        ${metric(t("product_analytics.events", "事件总数"), String(data.stages.reduce((sum, stage) => sum + stage.event_count, 0)))}
      </div>
      <progress class="product-analytics-progress" max="10000" value="${summary.completion_bps}">${formatBps(summary.completion_bps)}</progress>
      ${initialNotice}
    </section>
    <section class="product-analytics-funnel" aria-labelledby="product-analytics-funnel-title">
      <h3 id="product-analytics-funnel-title">${escapeHtml(t("product_analytics.funnel", "七阶段漏斗"))}</h3>
      <ol class="product-analytics-steps">
        ${data.stages.map((stage) => renderStep(stage, t)).join("")}
      </ol>
    </section>
    <section class="product-analytics-details" aria-labelledby="product-analytics-details-title">
      <div class="product-analytics-details-heading">
        <h3 id="product-analytics-details-title">${escapeHtml(t("product_analytics.details", "阶段明细"))}</h3>
        <time datetime="${escapeAttribute(data.generated_at)}">${escapeHtml(t("product_analytics.generated", "生成于 {value}", { value: formatUtc(data.generated_at) }))}</time>
      </div>
      ${renderTable(data.stages, t)}
    </section>
  `;
}

/** 渲染单个固定阶段的完成状态和事件数，不展示 actor、来源或请求正文。 */
function renderStep(stage, t) {
  const state = stageState(stage);
  const label = stageLabel(stage.event_type, t);
  return `
    <li class="product-analytics-step ${state.className}" data-stage-order="${stage.order}">
      <span class="product-analytics-step-number" aria-hidden="true">${stage.order}</span>
      <strong>${escapeHtml(label)}</strong>
      <span>${escapeHtml(state.label(t))}</span>
      <small>${escapeHtml(t("product_analytics.event_count", "{value} 次事件", { value: stage.event_count }))}</small>
    </li>
  `;
}

/** 渲染七阶段明细表；所有标签来自固定映射，计数与时间均来自权威聚合。 */
function renderTable(stages, t) {
  return `
    <div class="product-analytics-table" role="table" aria-label="${escapeAttribute(t("product_analytics.details", "阶段明细"))}">
      <div class="product-analytics-row product-analytics-table-head" role="row">
        <span role="columnheader">${escapeHtml(t("product_analytics.column.stage", "阶段"))}</span>
        <span role="columnheader">${escapeHtml(t("product_analytics.column.status", "状态"))}</span>
        <span role="columnheader">${escapeHtml(t("product_analytics.column.events", "事件数"))}</span>
        <span role="columnheader">${escapeHtml(t("product_analytics.column.first", "首次发生（UTC）"))}</span>
        <span role="columnheader">${escapeHtml(t("product_analytics.column.last", "最近发生（UTC）"))}</span>
        <span role="columnheader">${escapeHtml(t("product_analytics.column.conversion", "相邻转化率"))}</span>
      </div>
      ${stages.map((stage) => {
        const state = stageState(stage);
        return `
          <div class="product-analytics-row" role="row">
            <span role="cell" data-label="${escapeAttribute(t("product_analytics.column.stage", "阶段"))}"><strong>${stage.order}. ${escapeHtml(stageLabel(stage.event_type, t))}</strong></span>
            <span role="cell" data-label="${escapeAttribute(t("product_analytics.column.status", "状态"))}"><span class="product-analytics-status ${state.className}">${escapeHtml(state.label(t))}</span></span>
            <span role="cell" data-label="${escapeAttribute(t("product_analytics.column.events", "事件数"))}">${stage.event_count}</span>
            <time role="cell" data-label="${escapeAttribute(t("product_analytics.column.first", "首次发生（UTC）"))}" datetime="${escapeAttribute(stage.first_occurred_at ?? "")}">${escapeHtml(formatUtc(stage.first_occurred_at))}</time>
            <time role="cell" data-label="${escapeAttribute(t("product_analytics.column.last", "最近发生（UTC）"))}" datetime="${escapeAttribute(stage.last_occurred_at ?? "")}">${escapeHtml(formatUtc(stage.last_occurred_at))}</time>
            <span role="cell" data-label="${escapeAttribute(t("product_analytics.column.conversion", "相邻转化率"))}">${escapeHtml(formatBps(stage.conversion_from_previous_bps))}</span>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

/** 渲染脱敏错误坐标，错误消息、diagnosis 和响应载荷均不会进入页面。 */
function renderError(error, t) {
  const code = escapeHtml(error?.code ?? "product_analytics.unavailable");
  const requestId = escapeHtml(error?.requestId ?? "-");
  return `
    <div class="product-analytics-error" role="alert">
      <strong>${escapeHtml(t("product_analytics.error", "增长分析暂不可用"))}</strong>
      <span>${escapeHtml(t("product_analytics.error_code", "错误码"))}: <code>${code}</code></span>
      <span>request_id: <code>${requestId}</code></span>
    </div>
  `;
}

/** 根据 reached 与 cumulative_reached 计算展示状态，不在前端重算后端事件事实。 */
function stageState(stage) {
  if (stage.cumulative_reached) {
    return { className: "is-complete", label: (t) => t("product_analytics.status.complete", "已完成") };
  }
  if (stage.reached) {
    return { className: "is-out-of-order", label: (t) => t("product_analytics.status.out_of_order", "已发生，前序有缺口") };
  }
  if (stage.order === 1 || stage.conversion_from_previous_bps !== null) {
    return { className: "is-gap", label: (t) => t("product_analytics.status.gap", "待完成") };
  }
  return { className: "is-blocked", label: (t) => t("product_analytics.status.blocked", "累计已中断") };
}

/** 将受支持的事件类型映射到固定多语言标签，未知值仅作为已转义兜底。 */
function stageLabel(eventType, t) {
  return t(STAGE_MESSAGE_KEYS[eventType], eventType);
}

/** 输出一个已转义概览指标，避免聚合值或标签注入 HTML。 */
function metric(label, value) {
  return `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

/** 将后端基点值格式化为百分比，不改变或补算权威完成率。 */
function formatBps(value) {
  if (value === null || value === undefined) return "-";
  const percent = value / 100;
  return `${Number.isInteger(percent) ? percent : percent.toFixed(2).replace(/0+$/u, "").replace(/\.$/u, "")}%`;
}

/** 将已校验 UTC 时间转成紧凑展示文本，空阶段保持短横线占位。 */
function formatUtc(value) {
  if (!value) return "-";
  return `${String(value).replace("T", " ").replace(/Z$/u, "")} UTC`;
}

/** 转义文本节点中的 HTML 特殊字符，阻止任意聚合值形成可执行标记。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 复用 HTML 转义处理属性值，确保 aria 与 datetime 不突破属性边界。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
