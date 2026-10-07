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
  // 错误态只展示普通用户可理解的恢复提示，内部定位号保留在日志中。
  if (state.state === "error") {
    return `<div class="adapter-spec-page" data-adapter-spec-root>
      ${renderHeading()}
      <div class="empty-state error-state adapter-spec-loading" role="alert">${escapeHtml(state.message ?? "模型服务规范暂不可用，请稍后重试")}</div>
    </div>`;
  }

  const filters = canonicalAdapterSpecFilters(state.filters);
  const allSpecs = Array.isArray(state.specs) ? state.specs : [];
  const visibleSpecs = allSpecs.filter((spec) => specMatchesView(spec, filters));
  return `<div class="adapter-spec-page" data-adapter-spec-root>
    ${renderHeading()}
    ${renderCatalogIdentity(state.catalog)}
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

/** 标题说明接入能力，不把内部合同术语带给组织用户。 */
function renderHeading() {
  return `<header class="adapter-spec-heading">
    <div>
      <p class="eyebrow">模型服务</p>
      <h2 id="adapter-specs-title">模型服务兼容性</h2>
      <p>查看已支持的模型服务、连接方式、用量统计和价格能力。</p>
    </div>
  </header>`;
}

/** 只显示最后更新时间；目录版本和摘要属于系统内部合同。 */
function renderCatalogIdentity(catalog = {}) {
  return `<dl class="adapter-spec-catalog" aria-label="已激活规范目录">
    <div><dt>最后更新</dt><dd>${escapeHtml(formatDate(catalog.activated_at))}</dd></div>
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
    <span>OpenRouter、硅基流动仅用于迁移兼容。</span>
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
    <span role="columnheader">模型服务</span><span role="columnheader">连接方式</span><span role="columnheader">用量统计</span>
    <span role="columnheader">错误处理</span><span role="columnheader">流式支持</span><span role="columnheader">价格能力</span>
  </div>`;
}

/** 渲染单条冻结 spec；全部字符串均按 HTML 上下文转义，数组只来自客户端白名单 DTO。 */
function renderSpecRow(spec = {}) {
  const examples = Array.isArray(spec.platform_examples) ? spec.platform_examples : [];
  return `<div class="adapter-spec-row" role="row" data-provider-type="${escapeAttribute(spec.provider_type ?? "")}">
    <span role="cell" data-label="模型服务"><strong>${escapeHtml(spec.display_name ?? PROVIDER_LABELS[spec.provider_type] ?? "-")}</strong><small>${escapeHtml(CLASSIFICATION_LABELS[spec.classification] ?? "")}</small></span>
    <span role="cell" data-label="连接方式">${spec.allowed_auth_types?.includes?.("none") ? "无需额外认证" : "已支持认证"}</span>
    <span role="cell" data-label="用量统计">${escapeHtml(spec.usage_profile ?? "已支持")}</span>
    <span role="cell" data-label="错误处理">${escapeHtml(spec.error_profile ?? "已支持")}</span>
    <span role="cell" data-label="流式支持">${escapeHtml(spec.stream_profile ?? "已支持")}</span>
    <span role="cell" data-label="价格能力"><small>${escapeHtml(spec.price_fields?.length ? "已支持" : "未配置")}</small>
      <details class="adapter-spec-details"><summary>合同详情</summary><dl>
        <div><dt>状态</dt><dd>${escapeHtml(spec.status ?? "-")}</dd></div>
        <div><dt>健康检查</dt><dd>${spec.probe_profile ? "已支持" : "未配置"}</dd></div>
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
