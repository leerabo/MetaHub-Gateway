/**
 * PAGE-092 角色首页渲染器。
 *
 * 页面只消费 roleDashboardPageState 已白名单投影的聚合值；真实租户隔离、视图授权和卡片跳转权限
 * 仍由后端执行。所有服务端文本再次按 HTML 上下文转义，未知响应字段不会被遍历到 DOM。
 */

import { hasPermission, PERMISSIONS } from "../../lib/permissions.js";

// 状态和单位仅负责展示，不在浏览器重新计算后端治理阈值或成本口径。
const STATUS_LABELS = Object.freeze({
  ok: "正常",
  warning: "关注",
  critical: "异常",
  empty: "待接入",
});

/**
 * 渲染角色首页完整根节点。
 *
 * 参数：state 是独立状态机快照；authContext 是当前认证权限快照；consoleShellState 是同轮后端导航。
 * 返回：可直接放入 overview 内容区的安全 HTML。缺少 dashboard.read 时返回空串，不能按角色名补权。
 */
export function renderRoleDashboardPanel(state, authContext, consoleShellState = null) {
  if (!hasPermission(authContext, PERMISSIONS.DASHBOARD_READ)) return "";
  if (!state || ["idle", "loading"].includes(state.state)) {
    return renderLoading(state?.selectedView);
  }
  // 错误快照不包含配置、分段或卡片，必须走独立模板避免旧受保护命令残留。
  if (state.state === "error") return renderError(state);

  const selectedView = state.config?.selected_view_code ?? "";
  const availableViews = Array.isArray(state.config?.available_views)
    ? state.config.available_views
    : [];
  const cards = Array.isArray(state.cards) ? state.cards : [];
  return `
    <section class="role-dashboard-panel" data-role-dashboard-root data-role-dashboard-view="${escapeAttribute(selectedView)}" aria-labelledby="role-dashboard-title">
      <header class="role-dashboard-heading">
        <div>
          <h2 id="role-dashboard-title">角色首页</h2>
          <p>${escapeHtml(formatWindow(state.window))}</p>
        </div>
        <span class="role-dashboard-updated">更新 ${escapeHtml(formatDateTime(state.window?.generated_at))}</span>
      </header>
      ${renderPartialWarning(state)}
      ${renderEventWarning(state.eventWarning)}
      ${renderViewSegments(availableViews, selectedView)}
      ${renderEmptyState(state.empty_state)}
      <div class="role-dashboard-grid" aria-live="polite">
        ${cards.map((card) => renderCard(card, consoleShellState)).join("")}
      </div>
    </section>
  `;
}

/** 渲染无旧卡片的 loading；显式切换时可以提示目标视图，但不把选择写入 URL 或 Storage。 */
function renderLoading(selectedView) {
  return `
    <section class="role-dashboard-panel" data-role-dashboard-root aria-labelledby="role-dashboard-title" aria-busy="true">
      <header class="role-dashboard-heading">
        <div><h2 id="role-dashboard-title">角色首页</h2><p>正在读取当前角色可见指标。</p></div>
      </header>
      <div class="role-dashboard-loading empty-state">${selectedView ? "正在切换角色视图。" : "正在加载今日角色首页。"}</div>
    </section>
  `;
}

/** 错误态只展示统一公开字段；403/配置异常状态不含旧配置、视图、卡片或导航命令。 */
function renderError(state) {
  return `
    <section class="role-dashboard-panel" data-role-dashboard-root aria-labelledby="role-dashboard-title">
      <header class="role-dashboard-heading">
        <div><h2 id="role-dashboard-title">角色首页</h2><p>当前指标暂不可用。</p></div>
      </header>
      <div class="role-dashboard-error empty-state error-state" role="alert">
        ${escapeHtml(state.message ?? "角色首页读取失败，请稍后重试")}
      </div>
    </section>
  `;
}

/** 503 只会由状态机在同 owner、同 selected view 下生成 partial，这里明确标识陈旧快照。 */
function renderPartialWarning(state) {
  if (state.state !== "partial") return "";
  return `<div class="role-dashboard-warning" role="status">实时刷新暂不可用，以下为当前视图最近一次安全快照。</div>`;
}

/** 遥测失败不阻断阅读，只展示安全 message 和 request_id，不触发自动重试。 */
function renderEventWarning(warning) {
  if (!warning) return "";
  return `<div class="role-dashboard-event-warning" data-role-dashboard-event-warning role="status">访问记录暂不可用：${escapeHtml(warning.message ?? "事件写入失败，请稍后重试")}</div>`;
}

/** 五视图分段控制只遍历后端 available_views，前端不根据角色补回未授权选项。 */
function renderViewSegments(views, selectedView) {
  if (!views.length) return "";
  return `
    <div class="role-dashboard-view-segments" role="tablist" aria-label="角色首页视图">
      ${views.map((view) => {
        const selected = view.code === selectedView;
        return `<button type="button" role="tab" data-role-dashboard-view-option="${escapeAttribute(view.code)}" aria-selected="${selected}" tabindex="${selected ? "0" : "-1"}">${escapeHtml(view.label)}</button>`;
      }).join("")}
    </div>
  `;
}

/** 空租户说明来自后端固定文案；页面不自行生成未经导航复核的操作链接。 */
function renderEmptyState(emptyState) {
  if (!emptyState?.is_empty) return "";
  return `
    <div class="role-dashboard-empty">
      <strong>${escapeHtml(emptyState.title || "当前角色暂无聚合数据")}</strong>
      <span>${escapeHtml(emptyState.description || "完成一次受控调用后即可看到今日指标。")}</span>
    </div>
  `;
}

/** 渲染单层重复卡片，并保持 API 返回顺序；前端不按角色或状态二次排序。 */
function renderCard(card, consoleShellState) {
  const navigationTarget = authorizedNavigationTarget(
    card?.target_navigation_item_id,
    consoleShellState,
  );
  return `
    <article class="role-dashboard-card status-${escapeAttribute(card?.status ?? "empty")}" data-role-dashboard-card="${escapeAttribute(card?.code ?? "")}">
      <header>
        <div><h3>${escapeHtml(card?.label ?? "指标")}</h3><span>${escapeHtml(card?.summary ?? "")}</span></div>
        <mark>${escapeHtml(STATUS_LABELS[card?.status] ?? "待确认")}</mark>
      </header>
      <strong class="role-dashboard-primary">${escapeHtml(formatValue(card?.primary_value, card?.primary_unit))}</strong>
      ${renderFacts(card?.facts)}
      ${navigationTarget ? `<button type="button" class="role-dashboard-card-link" data-console-nav="${escapeAttribute(navigationTarget)}" data-console-target="${escapeAttribute(navigationTarget)}" title="打开详情" aria-label="打开${escapeAttribute(card?.label ?? "指标")}详情">打开详情 <span aria-hidden="true">→</span></button>` : ""}
    </article>
  `;
}

/** 渲染固定上限的事实网格，值和单位始终使用后端给出的聚合口径。 */
function renderFacts(facts) {
  const items = Array.isArray(facts) ? facts : [];
  return `<dl class="role-dashboard-facts">${items.map((fact) => `
    <div><dt>${escapeHtml(fact.label ?? "-")}</dt><dd>${escapeHtml(formatValue(fact.value, fact.unit))}</dd></div>
  `).join("")}</dl>`;
}

/**
 * 卡片命令必须再次与同轮后端导航相交。
 * target 不存在、被裁剪、disabled 或 target_section 漂移时只展示指标，不渲染可点击入口。
 */
function authorizedNavigationTarget(target, consoleShellState) {
  const normalized = String(target ?? "");
  if (!/^[A-Za-z0-9_.:-]{1,80}$/.test(normalized)) return "";
  const items = consoleShellState?.navigation?.navigation_items;
  const owned = Array.isArray(items) && items.some((item) => (
    item?.id === normalized
    && item?.target_section === normalized
    && item?.visible !== false
    && item?.disabled !== true
  ));
  return owned ? normalized : "";
}

/** 根据固定单位格式化安全数值；非法值显示短横线，不能伪造成 0。 */
function formatValue(value, unit) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "-";
  if (unit === "cny") return `¥${formatNumber(number, 4)}`;
  if (unit === "percent") return `${formatNumber(number, 2)}%`;
  if (unit === "ms") return `${formatNumber(number, 0)} ms`;
  // Token 单位只展示聚合计数，不得与认证 Token 或任何密钥文本混用。
  if (unit === "tokens") return `${formatNumber(number, 0)} Token`;
  if (unit === "steps") return `${formatNumber(number, 0)} / 5`;
  return formatNumber(number, 0);
}

/** 使用中文分组格式展示数值，保留后端精度上限但不改变治理口径。 */
function formatNumber(value, maximumFractionDigits) {
  return value.toLocaleString("zh-CN", { maximumFractionDigits });
}

/** 紧凑展示上海业务日窗口，缺失字段失败关闭为待加载文案。 */
function formatWindow(windowInfo) {
  if (!windowInfo?.start_at || !windowInfo?.end_at) return "上海今日窗口待加载";
  return `上海今日 · ${formatDateTime(windowInfo.start_at)} 至 ${formatDateTime(windowInfo.end_at)} · ${windowInfo.timezone || "Asia/Shanghai"}`;
}

/** 格式化 ISO 时间文本，不依赖浏览器本地时区重新解释后端业务日边界。 */
function formatDateTime(value) {
  if (!value) return "-";
  return String(value).replace("T", " ").replace(/\.\d+(?=Z|[+-]\d\d:\d\d$)/, "");
}

/** 所有 API 文本都经过 HTML 转义，避免标签、引号或反引号突破文本边界。 */
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;")
    .replaceAll("`", "&#096;");
}

/** 属性值使用同一严格转义，data 属性和 aria-label 不能成为脚本注入入口。 */
function escapeAttribute(value) {
  return escapeHtml(value);
}
