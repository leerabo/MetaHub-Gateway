import { safeBrandingLogoUrl } from "../../lib/branding.js";

// 默认导航定义控制控制台骨架的兜底菜单，后端返回的角色化导航仍是最终入口来源。
const DEFAULT_NAVIGATION = [
  {
    id: "overview",
    label: "概览",
    group: "总览",
    target_section: "overview",
    description: "查看本地服务状态和下一步引导。",
    empty_state_hint: "等待控制面导航接口响应。",
    order: 10,
  },
];

// 后端导航分组仍返回稳定业务标签，展示层在这里映射到 i18n key。
const GROUP_KEY_BY_LABEL = {
  系统: "system",
  System: "system",
  // PAGE-071 位于调用与成本分组，英文偏好只改变展示标签，不改变后端导航 ID 或权限集合。
  调用与成本: "calls-and-cost",
  "Calls & Cost": "calls-and-cost",
};

/**
 * 渲染控制台主框架。
 * 该组件只负责布局、导航和当前内容容器，不替代任何业务 API 的后端权限校验。
 */
export function renderConsoleShell({
  authContext,
  consoleShellState,
  contentHtml,
  i18n,
  branding = null,
}) {
  const navigationItems = navigationItemsFromState(consoleShellState).map((item) =>
    localizeNavigationItem(item, i18n),
  );
  const activeItem = activeNavigationItem(consoleShellState, navigationItems);
  return `
    <section class="topbar console-topbar">
      ${renderBrandMark(branding)}
      <div>
        <p class="eyebrow">${escapeHtml(brandingText(branding?.product_name, t(i18n, "shell.eyebrow", "中国境内企业级 AI Gateway")))}</p>
        <h1>${escapeHtml(brandingText(branding?.product_name, t(i18n, "shell.title", "MetaHub 控制台")))}</h1>
      </div>
      ${renderUserMenu(authContext, i18n)}
    </section>

    <section class="console-shell" data-active-navigation="${escapeAttribute(activeItem.id)}">
      <aside class="console-sidebar" aria-label="${escapeAttribute(t(i18n, "shell.nav_aria", "控制台导航"))}">
        <div class="sidebar-title">
          <strong>${escapeHtml(t(i18n, "shell.sidebar_title", "治理入口"))}</strong>
          <span>${escapeHtml(authContext?.user?.role ?? t(i18n, "shell.guest", "guest"))}</span>
        </div>
        ${renderNavigationGroups(navigationItems, activeItem.id)}
      </aside>
      <main class="console-main" aria-labelledby="console-main-title">
        <div class="console-main-heading">
          <div>
            <p class="eyebrow">${escapeHtml(activeItem.group)}</p>
            <h2 id="console-main-title">${escapeHtml(activeItem.label)}</h2>
            <p>${escapeHtml(activeItem.description ?? "")}</p>
          </div>
          <span class="active-section-pill">${escapeHtml(t(i18n, "shell.active_id", "当前入口"))}: ${escapeHtml(activeItem.id)}</span>
        </div>
        ${renderShellMessage(consoleShellState, i18n)}
        ${contentHtml}
      </main>
    </section>
  `;
}

/** 渲染已由客户端 Blob 校验的 Logo；默认几何标记不受租户输入影响。 */
function renderBrandMark(branding) {
  const logoUrl = safeBrandingLogoUrl(branding?.logoUrl);
  if (logoUrl && branding?.logoPresent === true) {
    return `<div class="brand-mark brand-mark-image"><img class="brand-logo" src="${escapeAttribute(logoUrl)}" alt="${escapeAttribute(branding.product_name ?? "")}"></div>`;
  }
  return `<div class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></div>`;
}

function brandingText(value, fallback) {
  const text = String(value ?? "").trim();
  return text && text.length <= 120 ? text : fallback;
}

/**
 * 从导航状态里取出当前可见菜单，后端不可用时使用最小概览入口降级。
 */
export function navigationItemsFromState(consoleShellState) {
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.length > 0
    ? [...items].sort((left, right) => Number(left.order ?? 0) - Number(right.order ?? 0))
    : DEFAULT_NAVIGATION;
}

/**
 * 获取当前激活导航项，非法或缺失时回退到概览。
 */
export function activeNavigationItem(consoleShellState, navigationItems) {
  const activeId =
    consoleShellState?.activeNavigationItemId ??
    consoleShellState?.navigation?.active_navigation_item_id ??
    "overview";
  return navigationItems.find((item) => item.id === activeId) ?? navigationItems[0] ?? DEFAULT_NAVIGATION[0];
}

/**
 * 把后端返回的导航项转换为展示文案。
 * 边界：只改变 label/description/group，不改变 id、target_section 或权限结果。
 */
function localizeNavigationItem(item, i18n) {
  const key = navigationMessageKey(item);
  const description = t(i18n, `${key}.description`, item.description ?? "");
  return {
    ...item,
    label: t(i18n, key, item.label ?? item.id),
    group: localizeGroupLabel(item.group, i18n),
    description,
    empty_state_hint: t(i18n, `${key}.description`, item.empty_state_hint ?? description),
  };
}

/**
 * 生成导航 i18n key，保持后端导航 id 是唯一来源。
 */
function navigationMessageKey(item) {
  return `nav.${String(item.id ?? "").replaceAll("_", "-")}`;
}

/**
 * 映射导航分组文案，未知分组保持后端返回值。
 */
function localizeGroupLabel(group, i18n) {
  const fallback = String(group ?? "");
  const groupKey = GROUP_KEY_BY_LABEL[fallback];
  return groupKey ? t(i18n, `nav.group.${groupKey}`, fallback) : fallback;
}

/**
 * 按 group 渲染侧边栏导航，保证不同角色只看到后端返回的可见入口。
 */
function renderNavigationGroups(items, activeId) {
  const groups = [];
  for (const item of items) {
    let group = groups.find((entry) => entry.name === item.group);
    if (!group) {
      group = { name: item.group, items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups
    .map(
      (group) => `
        <nav class="sidebar-group" aria-label="${escapeAttribute(group.name)}">
          <h3>${escapeHtml(group.name)}</h3>
          ${group.items.map((item) => renderNavigationItem(item, activeId)).join("")}
        </nav>
      `,
    )
    .join("");
}

/**
 * 渲染单个导航入口。
 * 使用 button 而不是普通链接，便于主应用统一保存偏好并写入 console.page_viewed。
 */
function renderNavigationItem(item, activeId) {
  const active = item.id === activeId;
  return `
    <button
      type="button"
      class="sidebar-link${active ? " active" : ""}"
      data-console-nav="${escapeAttribute(item.id)}"
      data-console-target="${escapeAttribute(item.target_section)}"
      aria-current="${active ? "page" : "false"}"
      title="${escapeAttribute(item.empty_state_hint ?? item.description ?? "")}"
    >
      <span>${escapeHtml(item.label)}</span>
      <small>${escapeHtml(item.description ?? "")}</small>
    </button>
  `;
}

/**
 * 渲染控制台导航错误或降级提示。
 */
function renderShellMessage(consoleShellState, i18n) {
  const error = consoleShellState?.actionError ?? consoleShellState?.error;
  const warning = consoleShellState?.warning;
  // 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
  if (error) {
    return `
      <div class="empty-state error-state console-shell-message">
        ${escapeHtml(error.message ?? t(i18n, "shell.navigation_failed", "控制台导航操作失败"))} request_id: ${escapeHtml(error.requestId ?? "-")}
      </div>
    `;
  }
  if (warning) {
    return `<div class="empty-state console-shell-message">${escapeHtml(warning)}</div>`;
  }
  return "";
}

/**
 * 渲染顶部用户菜单，展示当前租户、角色和 Session 到期时间。
 */
function renderUserMenu(authContext, i18n) {
  if (!authContext?.user) {
    return `<div class="version-pill">${escapeHtml(t(i18n, "shell.guest", "未登录"))}</div>`;
  }
  const expiresAt = authContext.session?.expires_at ?? "-";
  return `
    <div class="user-menu" aria-label="${escapeAttribute(t(i18n, "shell.user_menu_aria", "个人菜单"))}">
      <div>
        <strong>${escapeHtml(authContext.user.display_name)}</strong>
        <span>${escapeHtml(authContext.user.email)} · ${escapeHtml(authContext.user.role)}</span>
        <span>${escapeHtml(authContext.tenant?.name ?? authContext.user.tenant_id)}</span>
        <span>${escapeHtml(t(i18n, "shell.session_expires", "Session 到期：{value}", { value: expiresAt }))}</span>
      </div>
      <button id="logout-button" type="button">${escapeHtml(t(i18n, "shell.logout", "登出"))}</button>
    </div>
  `;
}

/**
 * 读取 i18n 文案，缺少 i18n 上下文时保留中文 fallback。
 */
function t(i18n, key, fallback, params = {}) {
  return i18n?.t?.(key, params, fallback) ?? fallback;
}

/**
 * 执行控制台主框架的 HTML 转义，避免租户名、用户名或错误消息形成 XSS。
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
 * 转义 HTML 属性值，额外处理反引号以覆盖内联属性场景。
 */
function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}
