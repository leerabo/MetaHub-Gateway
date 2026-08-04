/**
 * PAGE-091 CI 测试报告渲染器。
 *
 * 本模块只消费客户端白名单 DTO，并对每个动态文本/属性按 HTML 上下文转义。页面是只读证据面，
 * 故意不提供真实探测、重跑、Endpoint 输入或凭据输入，所有执行动作留在受保护 CI/Gateway 边界。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";
import { canonicalAdapterContractRunFilters } from "./adapterContractRunPageState.js";

/** 固定枚举只负责中文展示，不影响后端状态或合并判定。 */
const STATUS_LABELS = Object.freeze({ passed: "通过", failed: "失败" });
const SUITE_LABELS = Object.freeze({ merge_gate: "合并门禁", probe: "真实探测" });
const MODE_LABELS = Object.freeze({ mock: "离线 Mock", gateway_probe: "Gateway 探测" });
const CASE_KIND_LABELS = Object.freeze({
  request: "请求构造",
  non_stream: "非流式",
  stream: "流式",
  error: "错误归一",
  usage: "Usage",
});
/** Provider 名称只做固定展示映射，迁移兼容项不会被提升为默认模型厂家。 */
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

/**
 * 渲染 Provider Adapter 契约运行页。
 *
 * @param {object|null} state 页面状态机安全快照，null 表示首轮加载。
 * @param {object|null} authContext 后端签发的实时权限快照。
 * @returns {string} 安全 HTML；缺少 provider.health.read 时严格返回空字符串。
 */
export function renderAdapterContractRuns(state, authContext) {
  // viewer 或权限被撤销时必须同步做到零 DOM；真正读取权限仍由 Control API 再次校验。
  if (!hasPermission(authContext, PERMISSIONS.PROVIDER_HEALTH_READ)) return "";
  if (!state || state.state === "loading") {
    return `<div class="adapter-contract-page" data-adapter-contract-root>
      ${renderHeading()}
      <div class="empty-state adapter-contract-loading" aria-live="polite">正在读取契约测试报告。</div>
    </div>`;
  }
  // 顶层失败只渲染统一公开 message/request_id，不显示底层响应或诊断正文。
  if (state.state === "error") {
    return `<div class="adapter-contract-page" data-adapter-contract-root>
      ${renderHeading()}
      <div class="empty-state error-state" role="alert">${escapeHtml(state.error?.message ?? "契约测试运行暂不可用")} · request_id: ${escapeHtml(state.error?.requestId ?? state.request_id ?? "-")}</div>
    </div>`;
  }

  const filters = canonicalAdapterContractRunFilters(state.filters);
  const runs = Array.isArray(state.adapter_contract_runs) ? state.adapter_contract_runs : [];
  return `<div class="adapter-contract-page" data-adapter-contract-root>
    ${renderHeading()}
    ${renderMergeGateSummary(state.latest_merge_gate)}
    ${state.error ? `<div class="adapter-contract-notice error-state" role="alert">${escapeHtml(state.error.message)} · request_id: ${escapeHtml(state.error.requestId ?? "-")}</div>` : ""}
    ${renderSuiteSegments(filters.suite_kind)}
    ${renderFilters(filters, Boolean(state.refreshing))}
    ${renderRunTable(runs, state.selected_run_id)}
    ${renderDetail(state)}
    ${renderPager(state)}
    <p class="adapter-contract-request-id break-anywhere">request_id: ${escapeHtml(state.request_id ?? "-")}</p>
  </div>`;
}

/** PAGE-091 标题保持紧凑，不把真实探测描述成浏览器可执行命令。 */
function renderHeading() {
  return `<header class="adapter-contract-heading">
    <div><p class="eyebrow">Provider 治理 / PAGE-091</p><h2 id="adapter-contract-runs-title">Adapter 契约测试报告</h2></div>
  </header>`;
}

/** 顶部只展示最近一次可信 merge gate；probe 分段不能替代合并门禁结论。 */
function renderMergeGateSummary(run) {
  if (!run) {
    return `<div class="adapter-contract-summary" aria-label="最新合并门禁摘要">
      ${summaryItem("最近门禁", "暂无记录")}${summaryItem("目录", "-")}${summaryItem("完成时间", "-")}${summaryItem("用例", "-")}
    </div>`;
  }
  return `<div class="adapter-contract-summary status-${escapeAttribute(run.status)}" aria-label="最新合并门禁摘要">
    ${summaryItem("最近门禁", STATUS_LABELS[run.status] ?? run.status)}
    ${summaryItem("目录", `${run.catalog_version} · ${shortHash(run.catalog_hash)}`, run.catalog_hash)}
    ${summaryItem("完成时间", formatDate(run.completed_at))}
    ${summaryItem("用例", `通过 ${run.passed_cases} / 失败 ${run.failed_cases}`)}
  </div>`;
}

/** 渲染一个稳定宽度摘要格；完整 hash 只放非敏感 title，正文保持短摘要。 */
function summaryItem(label, value, title = "") {
  const titleAttribute = title ? ` title="${escapeAttribute(title)}"` : "";
  return `<div><span>${escapeHtml(label)}</span><strong class="break-anywhere"${titleAttribute}>${escapeHtml(value)}</strong></div>`;
}

/** 分段按钮只切换只读数据集，不包含任何执行真实探测的命令。 */
function renderSuiteSegments(activeSuite) {
  return `<div class="adapter-contract-segments" role="group" aria-label="报告类型">
    ${Object.entries(SUITE_LABELS).map(([value, label]) => `<button type="button" data-adapter-contract-suite="${value}" aria-pressed="${value === activeSuite}">${label}</button>`).join("")}
  </div>`;
}

/** 状态和模式均来自冻结枚举；刷新使用熟悉符号并提供 tooltip/aria-label。 */
function renderFilters(filters, refreshing) {
  return `<form id="adapter-contract-filters" class="adapter-contract-filters">
    <label for="adapter-contract-status-filter">状态<select id="adapter-contract-status-filter" name="status">
      <option value="">全部</option><option value="passed"${selected(filters.status, "passed")}>通过</option><option value="failed"${selected(filters.status, "failed")}>失败</option>
    </select></label>
    <label for="adapter-contract-mode-filter">模式<select id="adapter-contract-mode-filter" name="mode">
      <option value="">全部</option><option value="mock"${selected(filters.mode, "mock")}>离线 Mock</option><option value="gateway_probe"${selected(filters.mode, "gateway_probe")}>Gateway 探测</option>
    </select></label>
    <button type="submit">应用筛选</button>
    <button type="reset" data-adapter-contract-reset>重置</button>
    <button type="button" class="icon-button adapter-contract-refresh" data-adapter-contract-refresh title="刷新报告" aria-label="刷新报告" ${refreshing ? "disabled" : ""}>↻</button>
  </form>`;
}

/** 运行表在宽屏使用稳定列轨，窄屏由 data-label 转为单列定义列表。 */
function renderRunTable(runs, selectedRunId) {
  return `<div class="adapter-contract-run-table" role="table" aria-label="Adapter 契约测试运行">
    <div class="adapter-contract-run-row adapter-contract-run-head" role="row">
      <span role="columnheader">状态</span><span role="columnheader">类型 / 模式</span><span role="columnheader">目录</span>
      <span role="columnheader">用例</span><span role="columnheader">完成时间</span><span role="columnheader">request_id</span><span role="columnheader">详情</span>
    </div>
    ${runs.length
      ? runs.map((run) => renderRunRow(run, run.adapter_contract_run_id === selectedRunId)).join("")
      : `<div class="empty-state adapter-contract-empty" role="row">当前筛选条件下没有契约测试运行。</div>`}
  </div>`;
}

/** 每行只展示不可变摘要；报告 hash、source revision、tenant 和 trace 不进入 DOM。 */
function renderRunRow(run, selectedRun) {
  return `<div class="adapter-contract-run-row status-${escapeAttribute(run.status)}" role="row">
    <span role="cell" data-label="状态"><strong>${escapeHtml(STATUS_LABELS[run.status] ?? run.status)}</strong></span>
    <span role="cell" data-label="类型 / 模式"><strong>${escapeHtml(SUITE_LABELS[run.suite_kind] ?? run.suite_kind)}</strong><small>${escapeHtml(MODE_LABELS[run.mode] ?? run.mode)}</small></span>
    <span role="cell" data-label="目录"><strong>${escapeHtml(run.catalog_version)}</strong><small class="break-anywhere">${escapeHtml(shortHash(run.catalog_hash))}</small></span>
    <span role="cell" data-label="用例"><strong>${escapeHtml(run.passed_cases)} / ${escapeHtml(run.total_cases)}</strong><small>失败 ${escapeHtml(run.failed_cases)}</small></span>
    <span role="cell" data-label="完成时间">${escapeHtml(formatDate(run.completed_at))}</span>
    <span role="cell" data-label="request_id" class="break-anywhere">${escapeHtml(run.source_request_id)}</span>
    <span role="cell" data-label="详情"><button type="button" class="icon-button" data-adapter-contract-detail="${escapeAttribute(run.adapter_contract_run_id)}" title="查看运行详情" aria-label="查看运行 ${escapeAttribute(run.adapter_contract_run_id)} 的详情" aria-pressed="${selectedRun}">⌄</button></span>
  </div>`;
}

/** 详情按 Provider 分组，失败只展示固定 failure code，不展示证据 hash 或断言正文。 */
function renderDetail(state) {
  if (!state.selected_run_id) return "";
  if (state.detail_loading) {
    return `<section class="adapter-contract-detail" aria-live="polite"><div class="empty-state">正在读取运行详情。</div></section>`;
  }
  // 详情错误与列表独立，用户仍可扫描当前页运行摘要并关闭失败详情。
  if (state.detail_error) {
    return `<section class="adapter-contract-detail"><div class="empty-state error-state" role="alert">${escapeHtml(state.detail_error.message)} · request_id: ${escapeHtml(state.detail_error.requestId ?? "-")}</div>${renderCloseDetail()}</section>`;
  }
  const detail = state.detail;
  if (!detail) return "";
  const groups = groupCases(detail.cases);
  return `<section class="adapter-contract-detail" aria-labelledby="adapter-contract-detail-title">
    <header><div><p class="eyebrow">运行详情</p><h3 id="adapter-contract-detail-title" class="break-anywhere">${escapeHtml(detail.adapter_contract_run.adapter_contract_run_id)}</h3></div>${renderCloseDetail()}</header>
    ${groups.map(([providerType, cases]) => renderProviderCases(providerType, cases)).join("")}
    <p class="adapter-contract-request-id break-anywhere">request_id: ${escapeHtml(detail.request_id ?? "-")}</p>
  </section>`;
}

/** 使用图标关闭详情，避免长按钮文案挤压移动端标题。 */
function renderCloseDetail() {
  return `<button type="button" class="icon-button" data-adapter-contract-close-detail title="关闭详情" aria-label="关闭详情">×</button>`;
}

/** 把 case 按固定 provider_type 分组，保持后端返回的 case 顺序。 */
function groupCases(cases) {
  const groups = new Map();
  for (const item of Array.isArray(cases) ? cases : []) {
    const key = String(item.provider_type ?? "");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return [...groups.entries()];
}

/** 渲染一个 Provider 的 case 定义表；移动端复用 data-label 变为键值行。 */
function renderProviderCases(providerType, cases) {
  return `<section class="adapter-contract-provider-group" aria-labelledby="adapter-contract-provider-${escapeAttribute(providerType)}">
    <h4 id="adapter-contract-provider-${escapeAttribute(providerType)}">${escapeHtml(PROVIDER_LABELS[providerType] ?? providerType)} <small>${escapeHtml(providerType)}</small></h4>
    <div class="adapter-contract-case-table" role="table" aria-label="${escapeAttribute(providerType)} 契约用例">
      <div class="adapter-contract-case-row adapter-contract-case-head" role="row"><span role="columnheader">类型</span><span role="columnheader">场景</span><span role="columnheader">鉴权</span><span role="columnheader">状态</span><span role="columnheader">失败码</span><span role="columnheader">耗时</span></div>
      ${cases.map(renderCaseRow).join("")}
    </div>
  </section>`;
}

/** case 行所有自由文本已经过客户端模式校验，渲染时仍执行上下文转义形成纵深防护。 */
function renderCaseRow(item) {
  return `<div class="adapter-contract-case-row status-${escapeAttribute(item.status)}" role="row">
    <span role="cell" data-label="类型">${escapeHtml(CASE_KIND_LABELS[item.case_kind] ?? item.case_kind)}</span>
    <span role="cell" data-label="场景" class="break-anywhere">${escapeHtml(item.variant)}</span>
    <span role="cell" data-label="鉴权">${escapeHtml(item.auth_type === null ? "-" : (item.auth_type === "none" ? "无认证" : item.auth_type))}</span>
    <span role="cell" data-label="状态">${escapeHtml(STATUS_LABELS[item.status] ?? item.status)}</span>
    <span role="cell" data-label="失败码" class="break-anywhere">${escapeHtml(item.failure_code ?? "-")}</span>
    <span role="cell" data-label="耗时">${escapeHtml(item.duration_ms)} ms</span>
  </div>`;
}

/** 分页按钮不携带 opaque cursor；事件层只能让状态机从闭包取下一页身份。 */
function renderPager(state) {
  if (!state.can_previous && !state.can_next) return "";
  return `<nav class="adapter-contract-pager" aria-label="契约测试运行分页">
    <button type="button" data-adapter-contract-previous ${state.can_previous ? "" : "disabled"}>上一页</button>
    <button type="button" data-adapter-contract-next ${state.can_next ? "" : "disabled"}>下一页</button>
  </nav>`;
}

/** 生成 select 选中属性；current/expected 都来自固定枚举。 */
function selected(current, expected) {
  return current === expected ? " selected" : "";
}

/** 目录 hash 只显示首尾摘要，避免 64 字符值破坏紧凑表格。 */
function shortHash(value) {
  const hash = String(value ?? "");
  return /^[a-f0-9]{64}$/.test(hash) ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : "-";
}

/** 格式化后端时间；非法值失败关闭为短横线，不能渲染 Invalid Date。 */
function formatDate(value) {
  const date = new Date(value ?? "");
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

/** 转义所有契约报告动态文本，阻止链路标识、场景或失败分类破坏页面 DOM 结构。 */
function escapeHtml(value) {
  // 即使客户端 DTO 已通过模式白名单，渲染边界仍必须转义以防未来合同漂移形成 XSS。
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 属性值沿用 HTML 转义，并单独命名以标记属性上下文。 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#96;");
}
