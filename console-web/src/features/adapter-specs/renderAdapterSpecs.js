/**
 * 模块职责：渲染 PAGE-090 国内模型 Endpoint Adapter 规范目录。
 *
 * 页面只消费 Control API 返回的公开白名单 DTO；真实 Provider Endpoint、凭据、Prompt、Response、
 * 租户选择和可执行 Adapter 代码都不属于此页面。后端权限校验仍是唯一授权边界。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { canonicalAdapterSpecFilters } from "./adapterSpecPageState.js";

// 分类标签来自冻结目录枚举，只负责解释来源边界，不能据此推导执行权限或厂家优先级。
const CLASSIFICATION_LABELS = Object.freeze({
  model_vendor_cloud: "模型厂家 / 云服务",
  local_compatible: "客户本地兼容 Endpoint",
  customer_managed: "客户自管 Endpoint",
  migration_platform: "竞品 / 迁移兼容平台",
});
// Provider 展示名是公开目录的固定映射，不接收后端自由文本作为 HTML 或可执行配置。
const PROVIDER_LABELS = Object.freeze({
  zhipu: "智谱",
  dashscope: "通义千问",
  wenxin: "文心",
  volcengine: "火山引擎",
  hunyuan: "腾讯混元",
  deepseek: "DeepSeek",
  openai_compatible: "OpenAI-compatible",
  custom: "客户自管",
  migration_compatible: "迁移兼容",
});
// 三个视图只改变前端分类筛选，租户身份和读取权限始终由后端 Session 决定。
const VIEW_LABELS = Object.freeze({
  default: "默认上游",
  customer: "客户 Endpoint",
  migration: "迁移兼容",
});

/**
 * 渲染 Adapter 规范页。
 *
 * @param {object|null} state API 成功、错误或 loading 状态。
 * @param {object|null} authContext 后端签发的实时权限快照。
 * @returns {string} 安全 HTML；缺少 provider.health.read 时返回空字符串。
 */
export function renderAdapterSpecs(state, authContext) {
  // 前端必须同步移除失权 DOM，但该检查只缩小展示面，不能替代 Control API 的实时权限校验。
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_READ)) return "";
  if (!state) {
    return `<div class="adapter-spec-page" data-adapter-spec-root>
      ${renderHeading()}
      <div class="empty-state adapter-spec-loading" aria-live="polite">正在读取 Adapter 规范。</div>
    </div>`;
  }
  // 错误态只展示固定消息与 request_id，禁止拼接后端自由正文、Endpoint 或凭据信息。
  if (state.state === "error") {
    return `<div class="adapter-spec-page" data-adapter-spec-root>
      ${renderHeading()}
      <div class="empty-state error-state adapter-spec-loading" role="alert">${escapeHtml(state.message ?? "Adapter 规范读取失败")} · request_id: ${escapeHtml(state.requestId ?? "-")}</div>
    </div>`;
  }

  const filters = canonicalAdapterSpecFilters(state.filters);
  const allSpecs = Array.isArray(state.specs) ? state.specs : [];
  const visibleSpecs = allSpecs.filter((spec) => specMatchesView(spec, filters));
  return `<div class="adapter-spec-page" data-adapter-spec-root>
    ${renderHeading()}
    ${renderCatalogIdentity(state.catalog, state.request_id)}
    ${renderViewTabs(filters.view)}
    ${filters.view === "migration" ? renderMigrationBoundary() : ""}
    ${renderFilters(filters)}
    <div class="adapter-spec-table-wrap">
      <div class="adapter-spec-table" role="table" aria-label="Provider Adapter 规范">
        ${renderTableHead()}
        ${visibleSpecs.length
          ? visibleSpecs.map(renderSpecRow).join("")
          : `<div class="empty-state adapter-spec-empty" role="row">当前筛选条件下没有 Adapter 规范。</div>`}
      </div>
    </div>
  </div>`;
}

/** 标题明确这是执行合同页，不把竞品或迁移平台宣传为模型厂家。 */
function renderHeading() {
  return `<header class="adapter-spec-heading">
    <div>
      <p class="eyebrow">Provider 治理 / PAGE-090</p>
      <h2 id="adapter-specs-title">国内模型 Endpoint 适配规范</h2>
      <p>鉴权、模型映射、Usage、错误、流式与价格字段的版本化执行合同</p>
    </div>
  </header>`;
}

/** 展示当前租户已激活目录身份；hash 完整值保留在 title，正文只显示短摘要。 */
function renderCatalogIdentity(catalog = {}, requestId = "-") {
  const hash = String(catalog.hash ?? "-");
  const shortHash = /^[a-f0-9]{64}$/.test(hash) ? `${hash.slice(0, 12)}…${hash.slice(-8)}` : "-";
  return `<dl class="adapter-spec-catalog" aria-label="已激活规范目录">
    <div><dt>目录版本</dt><dd>${escapeHtml(catalog.version ?? "-")}</dd></div>
    <div><dt>目录哈希</dt><dd class="break-anywhere" title="${escapeAttribute(hash)}">${escapeHtml(shortHash)}</dd></div>
    <div><dt>激活时间</dt><dd>${escapeHtml(formatDate(catalog.activated_at))}</dd></div>
    <div><dt>请求追踪</dt><dd class="break-anywhere">${escapeHtml(requestId ?? "-")}</dd></div>
  </dl>`;
}

/** 使用原生分段按钮表达三个产品分类，不用颜色暗示厂家优先级。 */
function renderViewTabs(activeView) {
  return `<div class="adapter-spec-tabs" role="group" aria-label="Adapter 规范分类">${Object.entries(VIEW_LABELS).map(
    ([value, label]) => `<button type="button" aria-pressed="${value === activeView}" data-adapter-spec-view="${escapeAttribute(value)}">${escapeHtml(label)}</button>`,
  ).join("")}</div>`;
}

/** 迁移分类固定展示产品边界，防止 OpenRouter 或硅基流动被误认为默认上游。 */
function renderMigrationBoundary() {
  return `<div class="adapter-spec-boundary" role="note">
    <strong>竞品 / 迁移兼容平台</strong>
    <span>OpenRouter、硅基流动只用于对标或受控迁移兼容，不是模型厂家，也不是默认上游。</span>
  </div>`;
}

/** 渲染状态和 Provider 类型筛选；选项全部来自冻结白名单，tenant_id 不存在于表单。 */
function renderFilters(filters) {
  return `<form id="adapter-spec-filters" class="adapter-spec-filters">
    <label for="adapter-spec-status-filter">状态<select id="adapter-spec-status-filter" name="status">
      <option value="">全部</option>
      <option value="active"${selected(filters.status, "active")}>启用</option>
      <option value="optional"${selected(filters.status, "optional")}>可选</option>
    </select></label>
    <label for="adapter-spec-provider-filter">Provider 类型<select id="adapter-spec-provider-filter" name="provider_type">
      <option value="">全部</option>
      ${Object.entries(PROVIDER_LABELS).map(([value, label]) => `<option value="${escapeAttribute(value)}"${selected(filters.provider_type, value)}>${escapeHtml(label)}</option>`).join("")}
    </select></label>
    <button type="submit">应用筛选</button>
    <button type="reset" data-adapter-spec-reset>重置</button>
  </form>`;
}

/** 固定八列合同表头；窄屏由每个 data-label 转换为键值布局。 */
function renderTableHead() {
  return `<div class="adapter-spec-row adapter-spec-head" role="row">
    <span role="columnheader">Provider</span><span role="columnheader">Adapter</span><span role="columnheader">鉴权</span>
    <span role="columnheader">模型 ID</span><span role="columnheader">Usage</span><span role="columnheader">错误</span>
    <span role="columnheader">流式</span><span role="columnheader">价格字段</span>
  </div>`;
}

/** 渲染单条冻结 spec；全部字符串均按 HTML 上下文转义，数组只来自客户端白名单 DTO。 */
function renderSpecRow(spec = {}) {
  const examples = Array.isArray(spec.platform_examples) ? spec.platform_examples : [];
  return `<div class="adapter-spec-row" role="row" data-provider-type="${escapeAttribute(spec.provider_type ?? "")}">
    <span role="cell" data-label="Provider"><strong>${escapeHtml(spec.display_name ?? PROVIDER_LABELS[spec.provider_type] ?? "-")}</strong><small>${escapeHtml(spec.provider_type ?? "-")}</small><small>${escapeHtml(CLASSIFICATION_LABELS[spec.classification] ?? spec.classification ?? "-")}</small></span>
    <span role="cell" data-label="Adapter"><strong>${escapeHtml(spec.adapter_name ?? "-")}</strong><small>v${escapeHtml(spec.adapter_version ?? "-")}</small><small>${escapeHtml(spec.protocol_family ?? "-")}</small></span>
    <span role="cell" data-label="鉴权">${escapeHtml(joinValues(spec.allowed_auth_types))}</span>
    <span role="cell" data-label="模型 ID">${escapeHtml(spec.model_id_strategy ?? "-")}</span>
    <span role="cell" data-label="Usage">${escapeHtml(spec.usage_profile ?? "-")}</span>
    <span role="cell" data-label="错误">${escapeHtml(spec.error_profile ?? "-")}</span>
    <span role="cell" data-label="流式">${escapeHtml(spec.stream_profile ?? "-")}</span>
    <span role="cell" data-label="价格字段"><small>${escapeHtml(joinValues(spec.price_fields))}</small>
      <details class="adapter-spec-details"><summary>合同详情</summary><dl>
        <div><dt>状态</dt><dd>${escapeHtml(spec.status ?? "-")}</dd></div>
        <div><dt>健康探测</dt><dd>${escapeHtml(spec.probe_profile ?? "-")}</dd></div>
        <div><dt>契约用例</dt><dd class="break-anywhere">${escapeHtml(spec.contract_case_id ?? "-")}</dd></div>
        ${examples.length ? `<div><dt>平台示例</dt><dd>${escapeHtml(joinValues(examples))}</dd></div>` : ""}
      </dl></details>
    </span>
  </div>`;
}

/** 按产品视图和页面 Provider 筛选取交集；服务端 status 过滤结果无需前端重新推导。 */
function specMatchesView(spec, filters) {
  const classificationMatches = (
    (filters.view === "default" && spec.classification === "model_vendor_cloud")
    || (filters.view === "customer" && ["local_compatible", "customer_managed"].includes(spec.classification))
    || (filters.view === "migration" && spec.classification === "migration_platform")
  );
  return classificationMatches && (!filters.provider_type || spec.provider_type === filters.provider_type);
}

/** 生成 select 选中属性；值均为本模块固定枚举。 */
function selected(current, expected) {
  return current === expected ? " selected" : "";
}

/** 将固定字符串数组转换为紧凑展示文本，非数组失败关闭为短横线。 */
function joinValues(value) {
  return Array.isArray(value) && value.length ? value.join(" / ") : "-";
}

/** 格式化后端 UTC 时间；非法值不让浏览器异常或显示 Invalid Date。 */
function formatDate(value) {
  const date = new Date(value ?? "");
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

/** 转义 HTML 文本，阻止目录展示名或合同字段破坏页面结构。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值沿用同一转义，并用独立函数标记安全上下文。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
