/**
 * 模块职责：构造并渲染 OpenRouter、硅基流动与 MetaHub 的中立定位和能力映射帮助页。
 * 维护边界：页面只使用版本化静态内容和后端已授权导航，不把竞品注册为模型厂商、默认上游或自动路由目标。
 * 来源文件：console-web/src/features/competitor-comparison/renderCompetitorComparison.js
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 内容版本和核验日期用于识别静态资料时效，不表示对第三方能力、采购或合规结论作持续保证。
export const COMPETITOR_MAPPING_VERSION = "1.0.0";
export const COMPETITOR_MAPPING_VERIFIED_AT = "2026-07-15";

// 三个固定标签页同时约束渲染、ARIA 关联和键盘导航，未知值统一回退到定位关系。
const COMPETITOR_TABS = Object.freeze(["positioning", "capabilities", "sales"]);

// 公开资料链接固定为 HTTPS 且不接受运行时输入，避免帮助页加载第三方脚本、图片或 iframe。
const OFFICIAL_SOURCES = Object.freeze([
  { label: "OpenRouter API 文档", url: "https://openrouter.ai/docs/api/reference/overview" },
  { label: "OpenRouter 模型与路由文档", url: "https://openrouter.ai/docs/guides/overview/models" },
  { label: "硅基流动 AI Gateway", url: "https://siliconflow.cn/ai-gateway" },
  { label: "硅基流动模型广场", url: "https://www.siliconflow.cn/models" },
]);

// 平台定位只记录中立、可核验的产品关注点，禁止使用“全面领先”或“竞品没有治理”等失真结论。
const PLATFORM_MAPPINGS = Object.freeze([
  {
    id: "openrouter",
    displayName: "OpenRouter",
    objectKind: "统一模型 API 与路由平台",
    relationship: "对标 / 竞品 / 迁移兼容对象",
    focus: "统一 API、模型目录、Provider 路由和开发者接入体验。",
    commonPattern: "应用通过统一接口选择模型，并由平台执行可配置路由。",
    metahubNavigationIds: ["api-docs", "models", "model-aliases", "request-logs"],
  },
  {
    id: "siliconflow",
    displayName: "硅基流动",
    objectKind: "模型 API 与企业 AI Gateway 平台",
    relationship: "对标 / 竞品 / 迁移兼容对象",
    focus: "模型服务、推理平台、统一接入，以及公开页面描述的流量、配额和可观测治理。",
    commonPattern: "企业通过平台 API 或网关接入多种模型服务，并按公开能力进行治理。",
    metahubNavigationIds: ["providers", "models", "provider-health", "usage"],
  },
  {
    id: "metahub",
    displayName: "MetaHub",
    objectKind: "企业级 AI Gateway + 治理控制面",
    relationship: "本产品",
    focus: "中国境内企业场景下的租户、权限、成本、审计、调用日志与调试闭环。",
    commonPattern: "默认上游是模型厂商、云端模型 Endpoint 或客户本地 OpenAI-compatible Endpoint。",
    metahubNavigationIds: ["projects", "virtual-keys", "usage", "audit-logs", "request-logs", "debug-console"],
  },
]);

// 能力矩阵描述当前公开资料与本版本已实现入口，不把未核验内容解释为竞品缺失能力。
const CAPABILITY_ROWS = Object.freeze([
  ["统一 OpenAI-compatible 接入", "公开 API 与快速开始", "公开模型 API / Gateway", "API 文档、迁移指南"],
  ["模型与路由", "模型目录与 Provider 路由", "模型服务与网关路由", "模型目录、模型别名、Provider 健康"],
  ["企业治理", "公开数据策略、路由与用量能力", "公开流量、配额、观测与私有部署能力", "租户 RBAC、Virtual Key、DLP、预算与 Webhook"],
  ["成本与可追踪性", "公开用量和成本信息", "公开用量/治理能力", "价格快照、用量成本、调用日志、request_id"],
  ["审计与调试", "以公开文档当前说明为准", "以公开页面当前说明为准", "审计日志、调用日志、OpenAI 调试台"],
]);

/**
 * 构造页面展示模型，并用后端实际下发的可见导航过滤 MetaHub 跳转。
 * 静态映射不伪造 tenant_id；访问所属租户由事件 API 根据当前登录态写入。
 */
export function buildCompetitorComparison({ consoleShellState } = {}) {
  const navigationItems = consoleShellState?.navigation?.navigation_items ?? [];
  const visibleItems = new Map(
    navigationItems
      .filter((item) => item?.visible !== false && item?.disabled !== true)
      .map((item) => [item.id, item]),
  );
  return {
    id: "competitor-positioning",
    version: COMPETITOR_MAPPING_VERSION,
    verifiedAt: COMPETITOR_MAPPING_VERIFIED_AT,
    status: "active",
    platforms: PLATFORM_MAPPINGS.map((platform) => ({
      ...platform,
      metahubMappings: platform.metahubNavigationIds
        .map((navigationId) => visibleItems.get(navigationId))
        .filter(Boolean)
        .map((item) => ({
          navigationId: item.id,
          targetSection: item.target_section ?? item.id,
          label: item.label,
          implementationStatus: "current",
        })),
    })),
    capabilities: CAPABILITY_ROWS,
    sources: OFFICIAL_SOURCES,
  };
}

/**
 * 渲染帮助中心竞品对比页；未知角色或缺少展示模型时返回空内容，避免绕过后端导航直接展示。
 */
export function renderCompetitorComparison({ comparison, authContext, activeTab = "positioning", eventWarning = null } = {}) {
  if (!comparison || !hasPermission(authContext, PERMISSIONS.CONSOLE_BASIC_READ)) {
    return "";
  }
  const tab = normalizeCompetitorTab(activeTab);
  return `
    <section class="panel competitor-comparison" data-competitor-comparison-root aria-labelledby="competitor-title">
      <header class="competitor-heading">
        <div>
          <p class="eyebrow">帮助中心 / 竞品对比 · v${escapeHtml(comparison.version)}</p>
          <h2 id="competitor-title">竞品定位与能力映射</h2>
        </div>
        <dl class="competitor-version"><dt>资料核验</dt><dd>${escapeHtml(comparison.verifiedAt)}</dd></dl>
      </header>
      <div class="competitor-event-warning" data-competitor-event-warning ${eventWarning ? "" : "hidden"} role="status">
        ${eventWarning ? `访问记录暂不可用：${escapeHtml(eventWarning.message ?? "未知错误")} · request_id: ${escapeHtml(eventWarning.requestId ?? "-")}` : ""}
      </div>
      <div class="competitor-tabs" role="tablist" aria-label="竞品对比视图">
        ${renderTab("positioning", "定位关系", tab)}
        ${renderTab("capabilities", "能力映射", tab)}
        ${renderTab("sales", "售前规范", tab)}
      </div>
      ${renderPositioningPanel(comparison, tab)}
      ${renderCapabilitiesPanel(comparison, tab)}
      ${renderSalesPanel(comparison, tab)}
    </section>
  `;
}

/**
 * 归一化标签页值，阻止篡改后的 data 属性产生无选中面板状态。
 */
export function normalizeCompetitorTab(tab) {
  return COMPETITOR_TABS.includes(tab) ? tab : "positioning";
}

/**
 * 将方向键、Home/End 与确认键映射为目标标签页；Tab 和未知按键保留浏览器默认行为。
 */
export function competitorTabForKey(currentTab, key) {
  const current = normalizeCompetitorTab(currentTab);
  const index = COMPETITOR_TABS.indexOf(current);
  if (["ArrowRight", "ArrowDown"].includes(key)) return COMPETITOR_TABS[(index + 1) % COMPETITOR_TABS.length];
  if (["ArrowLeft", "ArrowUp"].includes(key)) return COMPETITOR_TABS[(index - 1 + COMPETITOR_TABS.length) % COMPETITOR_TABS.length];
  if (key === "Home") return COMPETITOR_TABS[0];
  if (key === "End") return COMPETITOR_TABS[COMPETITOR_TABS.length - 1];
  if (["Enter", " "].includes(key)) return current;
  return null;
}

/**
 * 渲染一个 ARIA tab，并使用 roving tabindex 保证键盘焦点只落在当前标签。
 */
function renderTab(id, label, activeTab) {
  const selected = id === activeTab;
  return `<button type="button" id="competitor-${id}-tab" role="tab" aria-controls="competitor-${id}-panel" aria-selected="${selected}" tabindex="${selected ? 0 : -1}" data-competitor-tab="${id}">${label}</button>`;
}

/**
 * 渲染平台定位表和当前用户有权访问的 MetaHub 实现入口。
 */
function renderPositioningPanel(comparison, activeTab) {
  return `
    <section id="competitor-positioning-panel" role="tabpanel" aria-labelledby="competitor-positioning-tab" data-competitor-panel="positioning" ${activeTab === "positioning" ? "" : "hidden"}>
      <div class="competitor-boundary"><strong>统一定位</strong><p>OpenRouter 和硅基流动是对标平台 / 竞品平台 / 迁移兼容对象，不是模型厂商，也不是 MetaHub 默认上游。</p></div>
      <div class="competitor-table-wrap"><table class="competitor-table"><thead><tr><th>平台</th><th>对象类型</th><th>与 MetaHub 关系</th><th>公开关注点</th><th>常见接入形态</th><th>MetaHub 当前映射</th></tr></thead><tbody>
        ${comparison.platforms.map(renderPlatformRow).join("")}
      </tbody></table></div>
      <p class="competitor-note">客户明确要求桥接时，再评估迁移入口。</p>
    </section>
  `;
}

/**
 * 渲染能力映射矩阵和可追溯官方来源，避免把时效性事实硬编码成永久结论。
 */
function renderCapabilitiesPanel(comparison, activeTab) {
  return `
    <section id="competitor-capabilities-panel" role="tabpanel" aria-labelledby="competitor-capabilities-tab" data-competitor-panel="capabilities" ${activeTab === "capabilities" ? "" : "hidden"}>
      <div class="competitor-table-wrap"><table class="competitor-table"><thead><tr><th>能力域</th><th>OpenRouter 公开信息</th><th>硅基流动公开信息</th><th>MetaHub 当前实现</th></tr></thead><tbody>
        ${comparison.capabilities.map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div>
      <div class="competitor-sources"><h3>官方资料</h3><ul>${comparison.sources.map(renderSource).join("")}</ul><p>第三方功能会变化；对外陈述前应复核链接与核验日期。未知或未核验不等于缺失。</p></div>
    </section>
  `;
}

/**
 * 渲染售前建议与禁用表述，明确禁止攻击性、绝对化和绕过治理的承诺。
 */
function renderSalesPanel(comparison, activeTab) {
  return `
    <section id="competitor-sales-panel" role="tabpanel" aria-labelledby="competitor-sales-tab" data-competitor-panel="sales" ${activeTab === "sales" ? "" : "hidden"}>
      <div class="competitor-sales-grid">
        <section aria-labelledby="sales-recommended"><h3 id="sales-recommended">建议表述</h3><ul><li>“两者是对标、竞品和迁移兼容对象，不是 MetaHub 默认模型上游。”</li><li>“MetaHub 当前重点是企业租户、权限、成本、审计、调用日志和调试闭环。”</li><li>“具体能力以双方当前官方资料和现场验证为准。”</li><li>“客户明确要求保留既有聚合入口时，再评估受控 Adapter。”</li></ul></section>
        <section aria-labelledby="sales-forbidden"><h3 id="sales-forbidden">禁止表述</h3><ul><li>禁止声称竞品没有治理、审计、私有部署或成本能力。</li><li>禁止使用“全面领先”“完全替代”“绝对合规”等无法验证的结论。</li><li>禁止把 OpenRouter 或硅基流动登记为模型厂商或默认上游。</li><li>禁止承诺自动回直连或绕过预算、DLP、权限与审计策略。</li></ul></section>
      </div>
      <div class="competitor-decision"><strong>售前判断顺序</strong><ol><li>确认客户要解决的是模型聚合、开发者接入，还是企业治理。</li><li>按当前公开资料核验双方能力，不以历史印象作缺失判断。</li><li>用 MetaHub 已实现入口演示租户、权限、成本、审计和调试链路。</li><li>涉及迁移桥接时单独评估安全、稳定性、计费和责任边界。</li></ol></div>
    </section>
  `;
}

/**
 * 渲染单个平台行；内部跳转仅来自后端已下发的可见导航项。
 */
function renderPlatformRow(platform) {
  const mappings = platform.metahubMappings.length
    ? platform.metahubMappings.map(renderNavigationLink).join("")
    : '<span class="competitor-mapping-empty">当前角色无可见映射入口</span>';
  return `<tr><th scope="row">${escapeHtml(platform.displayName)}</th><td>${escapeHtml(platform.objectKind)}</td><td>${escapeHtml(platform.relationship)}</td><td>${escapeHtml(platform.focus)}</td><td>${escapeHtml(platform.commonPattern)}</td><td><div class="competitor-mappings">${mappings}</div></td></tr>`;
}

/**
 * 渲染已授权的内部导航按钮；点击后仍复用主应用偏好写入和后端事件校验。
 */
function renderNavigationLink(mapping) {
  return `<button type="button" class="competitor-nav-link" data-console-nav="${escapeAttribute(mapping.navigationId)}" data-console-target="${escapeAttribute(mapping.targetSection)}">${escapeHtml(mapping.label)}</button>`;
}

/**
 * 渲染固定 HTTPS 官方链接；新窗口始终携带 noopener/noreferrer 隔离来源页面。
 */
function renderSource(source) {
  return `<li><a href="${escapeAttribute(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.label)}</a></li>`;
}

/**
 * 转义 HTML 文本，防止后端导航标签或扩展数据破坏帮助页结构。
 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/**
 * 转义属性值；当前实现与文本转义一致，单独命名用于显式标记属性安全边界。
 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
