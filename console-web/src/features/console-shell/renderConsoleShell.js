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
  const sidebarCollapsed = sidebarCollapsedFromState(consoleShellState);
  const expandedGroupKey = String(consoleShellState?.sidebarExpandedGroupKey ?? "").trim();
  const sidebarToggleLabel = sidebarCollapsed
    ? t(i18n, "shell.sidebar_expand", "展开导航")
    : t(i18n, "shell.sidebar_collapse", "收缩导航");
  return `
    <section class="topbar console-topbar">
      ${renderBrandMark(branding)}
      <div>
        <p class="eyebrow">${escapeHtml(brandingText(branding?.product_name, t(i18n, "shell.eyebrow", "中国境内企业级 AI Gateway")))}</p>
        <h1>${escapeHtml(brandingText(branding?.product_name, t(i18n, "shell.title", "MetaHub 控制台")))}</h1>
      </div>
      ${renderUserMenu(authContext, i18n)}
    </section>

    <section class="console-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}" data-active-navigation="${escapeAttribute(activeItem.id)}" data-sidebar-collapsed="${sidebarCollapsed ? "true" : "false"}">
      <aside id="console-sidebar" class="console-sidebar" aria-label="${escapeAttribute(t(i18n, "shell.nav_aria", "控制台导航"))}">
        <div class="sidebar-title">
          <div class="sidebar-title-copy">
            <strong>${escapeHtml(t(i18n, "shell.sidebar_title", "治理入口"))}</strong>
            <span>${escapeHtml(authContext?.user?.role ?? t(i18n, "shell.guest", "guest"))}</span>
          </div>
          <button
            type="button"
            class="sidebar-toggle"
            data-sidebar-toggle
            aria-controls="console-sidebar"
            aria-expanded="${sidebarCollapsed ? "false" : "true"}"
            aria-label="${escapeAttribute(sidebarToggleLabel)}"
            title="${escapeAttribute(sidebarToggleLabel)}"
          >
            <span class="sidebar-toggle-icon" aria-hidden="true">${sidebarCollapsed ? "&#x203A;" : "&#x2039;"}</span>
            <span class="sidebar-toggle-text">${escapeHtml(sidebarToggleLabel)}</span>
          </button>
        </div>
        ${renderNavigationGroups(navigationItems, activeItem.id, expandedGroupKey, sidebarCollapsed)}
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

/** Render only the strict content-addressed path from the Host-scoped public brand. */
function renderBrandMark(branding) {
  const logoUrl = safePublicBrandAssetPath(branding?.logoUrl);
  if (logoUrl && branding?.logoPresent === true) {
    return `<div class="brand-mark brand-mark-image"><img class="brand-logo" src="${escapeAttribute(logoUrl)}" alt="${escapeAttribute(branding.product_name ?? "")}"></div>`;
  }
  return `<div class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></div>`;
}

function safePublicBrandAssetPath(value) {
  const path = String(value ?? "");
  return /^\/branding\/public\/assets\/bas_[0-9a-f]{32}$/u.test(path) ? path : "";
}

function brandingText(value, fallback) {
  const text = String(value ?? "").trim();
  return text && text.length <= 120 ? text : fallback;
}

/** 偏好接口返回 preference 包装对象；兼容单测和旧缓存中的扁平形态。 */
function sidebarCollapsedFromState(consoleShellState) {
  return consoleShellState?.preferences?.preference?.sidebar_collapsed === true
    || consoleShellState?.preferences?.sidebar_collapsed === true;
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
function renderNavigationGroups(items, activeId, expandedGroupKey = "", sidebarCollapsed = false) {
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
    .map((group, index) => {
      const key = sidebarGroupKey(group);
      // The full sidebar is an accordion. The compact icon rail still exposes every authorized item.
      const isExpanded = sidebarCollapsed || key === expandedGroupKey;
      const isCollapsed = !isExpanded;
      const itemsId = `sidebar-group-items-${index}`;
      return `
        <nav class="sidebar-group${isCollapsed ? " is-collapsed" : ""}" aria-label="${escapeAttribute(group.name)}" data-sidebar-group="${escapeAttribute(key)}">
          <button
            type="button"
            class="sidebar-group-toggle"
            data-sidebar-group-toggle="${escapeAttribute(key)}"
            aria-controls="${itemsId}"
            aria-expanded="${isCollapsed ? "false" : "true"}"
            aria-label="${escapeAttribute(`${isCollapsed ? "展开" : "收起"}${group.name}`)}"
            title="${escapeAttribute(`${isCollapsed ? "展开" : "收起"}${group.name}`)}"
          >
            <span class="sidebar-group-label">${escapeHtml(group.name)}</span>
            <span class="sidebar-group-chevron" aria-hidden="true">⌄</span>
          </button>
          <div id="${itemsId}" class="sidebar-group-items"${isCollapsed ? " hidden" : ""}>
            ${group.items.map((item) => renderNavigationItem(item, activeId)).join("")}
          </div>
        </nav>
      `;
    })
    .join("");
}

/** Group identity is derived only from backend navigation IDs, not display labels. */
function sidebarGroupKey(group) {
  const ids = group.items
    .map((item) => String(item?.id ?? "").trim())
    .filter(Boolean);
  return ids.join("|") || String(group.name ?? "").trim();
}

/**
 * 渲染单个导航入口。
 * 使用 button 而不是普通链接，便于主应用统一保存偏好并写入 console.page_viewed。
 */
function renderNavigationItem(item, activeId) {
  const active = item.id === activeId;
  const label = String(item.label ?? item.id ?? "").trim();
  return `
    <button
      type="button"
      class="sidebar-link${active ? " active" : ""}"
      data-console-nav="${escapeAttribute(item.id)}"
      data-console-target="${escapeAttribute(item.target_section)}"
      data-console-nav-label="${escapeAttribute(label)}"
      data-console-group="${escapeAttribute(item.group ?? "")}"
      data-console-description="${escapeAttribute(item.description ?? "")}"
      aria-current="${active ? "page" : "false"}"
      aria-label="${escapeAttribute(label)}"
      title="${escapeAttribute(item.empty_state_hint ?? item.description ?? "")}"
    >
      <span class="sidebar-link-label">${escapeHtml(label)}</span>
      <span class="sidebar-link-short" aria-hidden="true">${escapeHtml(shortNavigationLabel(item))}</span>
      <small>${escapeHtml(item.description ?? "")}</small>
    </button>
  `;
}

function shortNavigationLabel(item) {
  const source = String(item.label ?? item.id ?? "").trim();
  const letters = Array.from(source).filter((character) => /[\p{L}\p{N}]/u.test(character));
  const compact = letters.slice(0, 2).join("");
  return compact || String(item.id ?? "nav").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "NA";
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
