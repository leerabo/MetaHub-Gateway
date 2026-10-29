/**
 * 模块职责：渲染模型价格时间线、状态筛选和管理员新增价格表单。
 * 维护边界：页面不重算历史 Usage；价格权限、租户隔离和 Decimal 校验必须由后端执行。
 * 来源文件：console-web/src/features/model-prices/renderModelPricePanel.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 价格状态标签只展示后端时间线结论，前端不依据本地时钟重判生效区间。
const STATUS_LABELS = {
  current: "当前生效",
  historical: "历史",
  scheduled: "待生效",
};

/**
 * 渲染价格管理主面板，模型列表由模型目录接口提供并按当前租户过滤。
 */
export function renderModelPricePanel(state, modelsState, authContext) {
  // 财务和安全角色可按矩阵读取价格；缺少 model_price.read 时必须零 DOM。
  if (!hasPermission(authContext, PERMISSIONS.MODEL_PRICE_READ)) {
    return "";
  }
  if (!state) {
    return renderShell(`<div class="empty-state">正在读取模型价格时间线。</div>`);
  }
  if (state.state === "error") {
    return renderShell(
      `<div class="empty-state error-state">${escapeHtml(state.message ?? "模型价格暂不可用，请稍后重试")}</div>`,
    );
  }
  const plans = state.price_plans ?? [];
  const models = (modelsState?.models ?? []).filter((model) => model.status === "active");
  return `
    <div class="panel-heading model-price-heading">
      <div>
        <h2 id="model-prices-title">模型价格</h2>
        <p>价格按 UTC 生效时间选择，已入账用量和价格快照不会重算。</p>
      </div>
      <span class="model-price-as-of">截至 ${escapeHtml(formatDateTime(state.as_of))}</span>
    </div>
    ${renderActionError(state.actionError)}
    ${hasPermission(authContext, PERMISSIONS.MODEL_PRICE_WRITE) ? renderCreateForm(models, state.feedback) : ""}
    ${renderFilterForm(state.filters, models)}
    <div class="model-price-table" role="table" aria-label="模型价格时间线">
      <div class="model-price-row model-price-head" role="row">
        <span>模型</span><span>状态</span><span>输入 / 百万 Token</span><span>输出 / 百万 Token</span><span>缓存命中 / 百万 Token</span>
        <span>币种</span><span>生效时间</span><span>结束时间</span>
      </div>
      ${plans.length ? plans.map(renderPlanRow).join("") : `<div class="model-price-empty">暂无价格计划</div>`}
    </div>
  `;
}

/**
 * 渲染统一页面标题壳，供加载和错误状态保持稳定布局。
 */
function renderShell(body) {
  return `
    <div class="panel-heading"><div><h2 id="model-prices-title">模型价格</h2><p>有效期价格与 Usage 快照。</p></div></div>
    ${body}
  `;
}

/**
 * 渲染管理员新增价格表单，生效时间使用本地输入并由提交层转换为 UTC。
 */
function renderCreateForm(models, feedback) {
  return `
    <form id="create-model-price-form" class="model-price-create-form">
      <label><span>模型</span><select name="modelId" aria-label="模型" required>${renderModelOptions(models)}</select></label>
      <label><span>输入 / 百万 Token</span><input name="inputPricePer1k" aria-label="输入 / 百万 Token" inputmode="decimal" required value="0" /></label>
      <label><span>输出 / 百万 Token</span><input name="outputPricePer1k" aria-label="输出 / 百万 Token" inputmode="decimal" required value="0" /></label>
      <label><span>缓存命中 / 百万 Token</span><input name="cachePricePer1k" aria-label="缓存命中 / 百万 Token" inputmode="decimal" required value="0" /></label>
      <label><span>币种</span><select name="currency" aria-label="币种"><option value="CNY">CNY</option><option value="USD">USD</option></select></label>
      <label><span>生效时间</span><input name="effectiveFrom" aria-label="生效时间" type="datetime-local" step="1" required /></label>
      <input name="priceSchemaVersion" type="hidden" value="" />
      <button type="submit" ${models.length ? "" : "disabled"}>新增价格</button>
      <span class="model-price-feedback" role="status">${escapeHtml(feedback ?? "")}</span>
    </form>
  `;
}

/**
 * 渲染模型和有效状态筛选，不把 tenant_id 放入 URL 或表单。
 */
function renderFilterForm(filters = {}, models) {
  return `
    <form id="model-price-filter-form" class="model-price-filter-form">
      <label><span>模型</span><select name="modelId" aria-label="模型"><option value="">全部模型</option>${renderModelOptions(models, filters.modelId)}</select></label>
      <label><span>状态</span><select name="effectiveStatus" aria-label="状态">
        ${renderStatusOption("all", "全部", filters.effectiveStatus)}
        ${renderStatusOption("current", "当前生效", filters.effectiveStatus)}
        ${renderStatusOption("scheduled", "待生效", filters.effectiveStatus)}
        ${renderStatusOption("historical", "历史", filters.effectiveStatus)}
      </select></label>
      <button type="submit">筛选</button>
      <button id="model-price-filter-reset" type="button">重置</button>
    </form>
  `;
}

/**
 * 渲染单条价格计划，只显示用户需要比较的价格和有效期。
 */
function renderPlanRow(plan) {
  return `
    <div class="model-price-row" role="row">
      <span><strong>${escapeHtml(plan.display_name ?? plan.internal_model_id)}</strong></span>
      <span><mark class="status-mark status-${escapeAttribute(plan.effective_status)}">${escapeHtml(STATUS_LABELS[plan.effective_status] ?? plan.effective_status)}</mark></span>
      <span>${escapeHtml(toMillionPrice(plan.input_price_per_1k))}</span>
      <span>${escapeHtml(toMillionPrice(plan.output_price_per_1k))}</span>
      <span>${escapeHtml(toMillionPrice(plan.cache_price_per_1k))}</span>
      <span>${escapeHtml(plan.currency)}</span>
      <span>${escapeHtml(formatDateTime(plan.effective_from))}</span>
      <span>${escapeHtml(plan.effective_to ? formatDateTime(plan.effective_to) : "持续有效")}</span>
    </div>
  `;
}

function toMillionPrice(value) {
  const number = Number(value);
  return (Number.isFinite(number) ? number : 0).toFixed(6).replace(/0+$/, "").replace(/\.$/, "") || "0";
}

/**
 * 渲染模型下拉选项，稳定 ID 作为提交值，展示文本使用内部模型 ID。
 */
function renderModelOptions(models, selectedId = "") {
  return models
    .map((model) => `<option value="${escapeAttribute(model.model_id)}" ${model.model_id === selectedId ? "selected" : ""}>${escapeHtml(model.display_name ?? model.internal_model_id)}</option>`)
    .join("");
}

/**
 * 渲染状态筛选选项并保持用户上一次选择。
 */
function renderStatusOption(value, label, selected) {
  return `<option value="${value}" ${value === (selected ?? "all") ? "selected" : ""}>${label}</option>`;
}

/**
 * 渲染操作错误，只显示下一步可执行的提示。
 */
function renderActionError(error) {
  if (!error) return "";
  return `<div class="model-price-action-error" role="alert">${escapeHtml(error.message ?? "模型价格操作失败，请稍后重试")}</div>`;
}

/**
 * 把 UTC 时间格式化为中国控制台易读文本，原始有效时刻仍由后端保存。
 */
function formatDateTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value ?? "-") : date.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 转义 HTML 文本，保护模型名称、版本和错误文案展示边界。
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
 * 转义 HTML 属性并额外处理反引号，防止稳定 ID 破坏属性边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
