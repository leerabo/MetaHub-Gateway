import { icon } from "../public-home/icons.js";
import { localizeConsoleStaticHtml } from "../../i18n/localizeConsoleStaticHtml.js";

// 默认导航定义控制控制台骨架的兜底菜单，后端返回的角色化导航仍是最终入口来源。
const DEFAULT_NAVIGATION = [
  {
    id: "overview",
    label: "概览",
    group: "总览",
    target_section: "overview",
    description: "查看本地服务状态和待处理事项。",
    empty_state_hint: "等待控制面导航接口响应。",
    order: 10,
  },
];

// 后端导航分组仍返回稳定业务标签，展示层在这里映射到 i18n key。
const GROUP_KEY_BY_LABEL = {
  home: "home",
  workspace: "workspace",
  gateway: "gateway",
  finance: "finance",
  governance: "governance",
  applications: "applications",
  "developer-center": "developer-center",
  advanced: "advanced",
  "高级工具": "advanced",
  "Advanced tools": "advanced",
  系统: "system",
  System: "system",
  SYSTEM: "system",
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
  // Legacy feature renderers still emit a few static English labels. Project
  // the complete content boundary once so Chinese users never see mixed
  // labels while user data, URLs, and backend identifiers remain untouched.
  const localizedContentHtml = localizeConsoleStaticHtml(contentHtml, i18n);
  const pageContentHtml = localizedContentHtml?.trim()
    ? localizedContentHtml
    : ["quota-buckets", "policy-templates", "session-risks"].includes(
      consoleShellState?.activeNavigationItemId,
    )
      ? (consoleShellState?.activeNavigationItemId === "quota-buckets"
        ? '<div class="console-page-unavailable" role="status">组织账户额度暂不可用，请稍后重试。模型调用成本预算可在“预算管理”中查看和设置。</div>'
        : '<div class="console-page-unavailable" role="status">当前功能暂不可用，请稍后重试或联系组织管理员。</div>')
      : localizedContentHtml;
  const roleCode = consoleShellState?.navigation?.role_code
    ?? consoleShellState?.workspaceContext?.role
    ?? authContext?.workspace?.role;
  const navigationItems = navigationItemsFromState(consoleShellState).map((item) =>
    localizeNavigationItem(item, i18n, roleCode),
  );
  const activeItem = activeNavigationItem(consoleShellState, navigationItems);
  const contentOwnsTitle = activeItem.id === "overview" && pageContentHtml?.includes('class="workspace-overview"');
  const shellCopy = workspaceRoleShellCopy(roleCode, i18n);
  const homeItemOrder = ["owner", "admin"].includes(roleCode)
    ? ["overview", "quickstart"]
    : ["quickstart", "overview"];
  const homeItem = homeItemOrder.map((id) => navigationItems.find((item) =>
    item.id === id && item.visible !== false && !item.disabled)).find(Boolean);
  const sidebarCollapsed = sidebarCollapsedFromState(consoleShellState);
  const expandedGroupKey = String(consoleShellState?.sidebarExpandedGroupKey ?? "").trim();
  const sidebarToggleLabel = sidebarCollapsed
    ? t(i18n, "shell.sidebar_expand", "展开导航")
    : t(i18n, "shell.sidebar_collapse", "收缩导航");
  return `
    <section class="topbar console-topbar">
      ${renderBrandMark(branding)}
      <div>
        <h1>${escapeHtml(brandingText(branding?.product_name, t(i18n, "shell.title", "MetaHub 控制台")))}</h1>
      </div>
      ${renderUserMenu(authContext, i18n, consoleShellState?.navigation, consoleShellState)}
    </section>

    <section class="console-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}" data-active-navigation="${escapeAttribute(activeItem.id)}" data-active-route="${escapeAttribute(activeItem.route_code ?? activeItem.id)}" data-sidebar-collapsed="${sidebarCollapsed ? "true" : "false"}">
      <aside id="console-sidebar" class="console-sidebar" aria-label="${escapeAttribute(t(i18n, "shell.nav_aria", "控制台导航"))}">
        <div class="sidebar-title">
          <div class="sidebar-title-copy">
            <strong>${escapeHtml(shellCopy.sidebarTitle)}</strong>
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
        ${renderNavigationGroups(navigationItems, activeItem.id, expandedGroupKey, i18n)}
        ${renderAdvancedToolsEntry(consoleShellState?.navigation, i18n, consoleShellState?.navigation?.role_code)}
      </aside>
      <main class="console-main" aria-labelledby="console-main-title" data-workspace-role="${escapeAttribute(roleCode ?? "")}">
        ${contentOwnsTitle ? "" : `<header class="console-main-heading">
          ${homeItem && activeItem.id !== homeItem.id
            ? `<a class="console-home-return" href="#${homeItem.id === "quickstart" ? "workspace.home.getting-started" : "workspace.home.overview"}" aria-label="${escapeAttribute(t(i18n, "shell.home_return_aria", "返回空间首页"))}" title="${escapeAttribute(t(i18n, "shell.home_return_aria", "返回空间首页"))}">${icon("arrow-left")}</a>` : ""}
          <div>
            <h2 id="console-main-title">${escapeHtml(activeItem.label)}</h2>
            ${activeItem.description ? `<p>${escapeHtml(activeItem.description)}</p>` : ""}
          </div>
        </header>`}
        ${renderShellMessage(consoleShellState, i18n)}
        <div class="console-page-body">
          ${pageContentHtml}
        </div>
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
  if (isCompactNavigationViewport()) {
    return consoleShellState?.mobileSidebarCollapsed !== false;
  }
  const preference = consoleShellState?.preferences?.preference?.sidebar_collapsed
    ?? consoleShellState?.preferences?.sidebar_collapsed;
  return preference === true;
}

export function bindConsoleSidebarViewport({ shell, state, applyCollapsed, onCompact,
  windowObject = globalThis.window } = {}) {
  const media = windowObject?.matchMedia?.("(max-width: 820px)");
  if (!shell || !media?.addEventListener) return () => {};
  const changed = () => {
    if (!shell.isConnected) return;
    if (media.matches) {
      state.mobileSidebarCollapsed = true;
      onCompact?.();
    }
    const desktopCollapsed = state?.preferences?.preference?.sidebar_collapsed
      ?? state?.preferences?.sidebar_collapsed;
    applyCollapsed(shell, media.matches || desktopCollapsed === true);
  };
  media.addEventListener("change", changed);
  return () => media.removeEventListener("change", changed);
}

export function renderConsoleEntryLoading(container) {
  if (!container) return;
  container.innerHTML = '<section class="console-entry-loading" role="status"><strong>MetaHub</strong><p>正在加载组织空间...</p><a href="./">返回首页</a></section>';
}

/** Reveal the current item inside the sidebar without scrolling the page or moving focus. */
export function revealConsoleSidebarItem(container) {
  const sidebar = container?.querySelector?.(".console-sidebar");
  const current = sidebar?.querySelector?.('.sidebar-link[aria-current="page"]');
  if (!sidebar || !current || !current.getClientRects().length) return;
  const bounds = sidebar.getBoundingClientRect();
  const item = current.getBoundingClientRect();
  if (item.bottom > bounds.bottom) sidebar.scrollTop += item.bottom - bounds.bottom + 8;
  else if (item.top < bounds.top) sidebar.scrollTop -= bounds.top - item.top + 8;
}

function isCompactNavigationViewport() {
  const width = Number(globalThis.window?.innerWidth ?? 0);
  return width > 0 && width <= 820;
}

/**
 * 从导航状态里取出当前可见菜单，后端不可用时使用最小概览入口降级。
 */
export function navigationItemsFromState(consoleShellState) {
  const items = consoleShellState?.navigation?.navigation_items;
  return Array.isArray(items) && items.length > 0
    ? [...items].sort((left, right) => navigationGroupOrder(left) - navigationGroupOrder(right)
      || Number(left.order ?? 0) - Number(right.order ?? 0)
      || String(left.id ?? "").localeCompare(String(right.id ?? "")))
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
function localizeNavigationItem(item, i18n, roleCode = null) {
  const key = navigationMessageKey(item);
  const description = t(i18n, `${key}.description`, item.description ?? "");
  const defaultLabel = t(i18n, `nav.task.${item.id}`, t(i18n, key, item.label ?? item.id));
  const label = item.id === "virtual-keys" && ["owner", "admin"].includes(roleCode)
    ? t(i18n, "nav.task.organization-keys", "KEY 管理")
    : defaultLabel;
  // 数据生命周期由平台管理员负责。组织管理员不应在侧栏看到一个点开后
  // 只能得到“无权限”的入口；后端仍保留深链和权限校验，便于平台运维。
  const hideForOrganizationAdmin = item.id === "tenant-data-lifecycle"
    && ["owner", "admin"].includes(roleCode);
  return {
    ...item,
    label,
    group: localizeGroupLabel(item.group, i18n),
    description,
    empty_state_hint: t(i18n, `${key}.description`, item.empty_state_hint ?? description),
    ...(hideForOrganizationAdmin ? { sidebar_visible: false, placement: "hidden" } : {}),
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
export function localizeConsoleGroup(group, i18n) {
  const fallback = String(group ?? "");
  const normalized = fallback.trim();
  const groupKey = GROUP_KEY_BY_LABEL[normalized]
    ?? GROUP_KEY_BY_LABEL[normalized.toLowerCase()]
    ?? GROUP_KEY_BY_LABEL[normalized.toUpperCase()];
  return groupKey ? t(i18n, `nav.group.${groupKey}`, fallback) : fallback;
}

const localizeGroupLabel = localizeConsoleGroup;

const CANONICAL_GROUP_ORDER = Object.freeze([
  "home",
  "workspace",
  "gateway",
  "finance",
  "governance",
  "applications",
  "developer-center",
  "system",
  "advanced",
]);

function navigationGroupOrder(item) {
  const group = String(item?.group_code ?? item?.group ?? "").trim().toLowerCase();
  const index = CANONICAL_GROUP_ORDER.indexOf(group);
  return index === -1 ? CANONICAL_GROUP_ORDER.length : index;
}

/**
 * 按 group 渲染侧边栏导航，保证不同角色只看到后端返回的可见入口。
 */
function renderNavigationGroups(items, activeId, expandedGroupKey = "", i18n) {
  const groups = [];
  const orderedItems = [...items]
    .filter((candidate) => candidate.sidebar_visible !== false)
    .sort((left, right) => navigationGroupOrder(left) - navigationGroupOrder(right)
      || Number(left.order ?? 0) - Number(right.order ?? 0)
      || String(left.id ?? "").localeCompare(String(right.id ?? "")));
  for (const item of orderedItems) {
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
      const isExpanded = key === expandedGroupKey;
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
            aria-label="${escapeAttribute(`${isCollapsed ? t(i18n, "shell.expand", "展开") : t(i18n, "shell.collapse", "收起")}${group.name}`)}"
            title="${escapeAttribute(`${isCollapsed ? t(i18n, "shell.expand", "展开") : t(i18n, "shell.collapse", "收起")}${group.name}`)}"
          >
            <span class="sidebar-group-label">${escapeHtml(group.name)}</span>
            <span class="sidebar-group-chevron" aria-hidden="true">⌄</span>
          </button>
          <div id="${itemsId}" class="sidebar-group-items"${isCollapsed ? " hidden" : ""}>
            ${group.items.map((item) => renderNavigationItem(item, activeId, true)).join("")}
          </div>
        </nav>
      `;
    })
    .join("");
}

/** Group identity is derived only from backend navigation IDs, not display labels. */
function sidebarGroupKey(group) {
  const canonical = String(group.items[0]?.group_code ?? "").trim();
  if (canonical) return canonical;
  const ids = group.items
    .map((item) => String(item?.id ?? "").trim())
    .filter(Boolean);
  return ids.join("|") || String(group.name ?? "").trim();
}

/**
 * 渲染单个导航入口。
 * 使用 button 而不是普通链接，便于主应用统一保存偏好并写入 console.page_viewed。
 */
function renderNavigationItem(item, activeId, defaultVisible = item.sidebar_visible !== false) {
  const active = item.id === activeId;
  const label = String(item.label ?? item.id ?? "").trim();
  return `
    <button
      type="button"
      class="sidebar-link${active ? " active" : ""}"
      data-console-nav="${escapeAttribute(item.id)}"
      data-console-route="${escapeAttribute(item.route_code ?? item.id)}"
      data-console-target="${escapeAttribute(item.target_section)}"
      data-sidebar-visible="${item.sidebar_visible === false ? "false" : "true"}"
      data-sidebar-default="${defaultVisible ? "true" : "false"}"
      data-console-nav-label="${escapeAttribute(label)}"
      data-console-group="${escapeAttribute(item.group ?? "")}"
      data-console-description="${escapeAttribute(item.description ?? "")}"
      aria-current="${active ? "page" : "false"}"
      aria-label="${escapeAttribute(label)}"
      title="${escapeAttribute(item.empty_state_hint || item.description || label)}"
    >
      <span class="sidebar-link-label">${escapeHtml(label)}</span>
      <span class="sidebar-link-short" aria-hidden="true">${escapeHtml(shortNavigationLabel(item))}</span>
      <small>${escapeHtml(item.description ?? "")}</small>
    </button>
  `;
}

function renderAdvancedToolsEntry(navigation, i18n, roleCode = null) {
  if (navigation?.advanced_tools_available !== true || roleCode === "viewer") return "";
  return `
    <button
      type="button"
      class="sidebar-advanced-entry"
      data-workspace-advanced-open
      aria-haspopup="dialog"
    >
      <span aria-hidden="true">+</span>
      <strong>${escapeHtml(t(i18n, "shell.advanced_tools", "高级工具"))}</strong>
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
        ${escapeHtml(error.message ?? t(i18n, "shell.navigation_failed", "控制台导航操作失败"))}
      </div>
    `;
  }
  if (warning) {
    return `<div class="empty-state console-shell-message">${escapeHtml(t(i18n, "shell.navigation_fallback_warning", warning))}</div>`;
  }
  return "";
}

/**
 * Keep the current account and organization visible without session internals.
 */
function renderUserMenu(authContext, i18n, navigation, consoleShellState) {
  if (consoleShellState?.workspaceNavigationContract === true) {
    const identity = consoleShellState.workspaceIdentity;
    const workspace = consoleShellState.workspaceContext;
    if (identity?.account?.status !== "active" || workspace?.session_status !== "active") return "";
    return `<div class="user-menu" aria-label="${escapeAttribute(t(i18n, "shell.user_menu_aria", "个人菜单"))}">
      <div><strong>${escapeHtml(identity.account.display_name || identity.account.email || "")}</strong>
        <span>${escapeHtml(workspace.name ?? "")}</span><small>${escapeHtml(workspaceRoleLabel(workspace.role, i18n))}</small></div>
      <a class="user-menu-preferences" href="identity.html?view=account#security">${escapeHtml(t(i18n, "shell.account_settings", "账号设置"))}</a>
      <a class="user-menu-preferences" href="identity.html?view=workspaces">${escapeHtml(t(i18n, "shell.switch_workspace", "切换组织"))}</a>
      <button id="logout-button" type="button">${escapeHtml(t(i18n, "shell.logout", "登出"))}</button>
    </div>`;
  }
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
      ${navigation?.account_action_codes?.includes("identity.preferences")
        ? `<a class="user-menu-preferences" href="identity.html#account-preferences">${escapeHtml(t(i18n, "shell.account_preferences", "账号偏好"))}</a>`
        : ""}
      <button id="logout-button" type="button">${escapeHtml(t(i18n, "shell.logout", "登出"))}</button>
    </div>
  `;
}

function workspaceRoleShellCopy(roleCode, i18n) {
  const label = workspaceRoleLabel(roleCode, i18n);
  if (roleCode === "owner" || roleCode === "admin") {
    return { sidebarTitle: t(i18n, "shell.org_management", "组织管理") };
  }
  if (roleCode === "finance_admin") {
    return { sidebarTitle: t(i18n, "shell.finance_workspace", "财务工作台") };
  }
  if (["security_admin", "legal_admin", "auditor"].includes(roleCode)) {
    return { sidebarTitle: `${label}工作台` };
  }
  if (roleCode === "developer") {
    return { sidebarTitle: t(i18n, "shell.developer_workspace", "开发工作台") };
  }
  return { sidebarTitle: t(i18n, "shell.employee_workspace", "我的组织空间") };
}

function workspaceRoleLabel(roleCode, i18n) {
  return ({
    owner: t(i18n, "workspace.role.owner", "组织所有者"),
    admin: t(i18n, "workspace.role.admin", "组织管理员"),
    developer: t(i18n, "workspace.role.developer", "开发者"),
    security_admin: t(i18n, "workspace.role.security_admin", "安全管理员"),
    finance_admin: t(i18n, "workspace.role.finance_admin", "财务管理员"),
    legal_admin: t(i18n, "workspace.role.legal_admin", "法务管理员"),
    auditor: t(i18n, "workspace.role.auditor", "审计员"),
    viewer: t(i18n, "workspace.role.viewer", "成员"),
  })[roleCode] ?? t(i18n, "workspace.role.member", "组织成员");
}

/**
 * 读取 i18n 文案，缺少 i18n 上下文时保留中文 fallback。
 */
function t(i18n, key, fallback, params = {}) {
  const text = i18n?.t?.(key, params, fallback);
  return text == null || text === key ? fallback : text;
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
