import { AuthApiError } from "./auth.js";

/**
 * 解析控制台导航 hash，并区分“没有 hash”和“存在但非法”。
 *
 * 参数：rawHash 为 location.hash 原值。
 * 返回：value 是解码后的候选 ID；rejected 表示非空值因超长、空解码或非法百分号编码被拒绝。
 * 安全：这里只做语法约束，角色可见性仍必须与后端裁剪的导航集合求交。
 */
export function parseNavigationHash(rawHash) {
  const encoded = String(rawHash ?? "").replace(/^#/, "").trim();
  if (!encoded) {
    return { value: undefined, rejected: false };
  }
  // 编码串先做硬长度限制，避免 decodeURIComponent 处理无意义的大输入。
  if (encoded.length > 80) {
    return { value: undefined, rejected: true };
  }
  try {
    const decoded = decodeURIComponent(encoded).trim();
    return decoded && decoded.length <= 80
      ? { value: decoded, rejected: false }
      : { value: undefined, rejected: true };
  } catch {
    // 非法百分号编码必须保留 rejected=true，调用方才能清理 URL 并显式回退概览。
    return { value: undefined, rejected: true };
  }
}

/**
 * 使用后端已裁剪的可见导航解析当前 hash 候选。
 *
 * 参数：navigation 为后端导航响应，requestedActiveItemId 为 URL hash 候选，options 标记解析阶段是否已拒绝原值。
 * 返回：activeItemId 和 rejectedRequestedItem；非法候选回退 overview，且不向后端发送一次必然失败的越权请求。
 * 安全：可见集合只取后端响应，前端不硬编码角色权限；直接调用导航 API 时后端仍会独立拒绝越权 ID。
 */
export function resolveVisibleNavigation(
  navigation,
  requestedActiveItemId,
  { requestedRejected = false } = {},
) {
  const items = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.filter((item) => item?.visible !== false && item?.disabled !== true)
    : [];
  const visibleIds = new Set(items.map((item) => String(item.id ?? "")).filter(Boolean));
  const requested = String(requestedActiveItemId ?? "").trim();
  const overviewFallback = visibleIds.has("overview") ? "overview" : (items[0]?.id ?? "overview");
  // 畸形或超长非空 hash 不是“无候选”，必须清理并固定回退概览，不能恢复旧偏好。
  if (requestedRejected) {
    return { activeItemId: overviewFallback, rejectedRequestedItem: true };
  }
  // 非空 hash 必须先与后端可见集合求交；越权值不应被发回 API 制造可避免的 422 控制台错误。
  if (requested) {
    return visibleIds.has(requested)
      ? { activeItemId: requested, rejectedRequestedItem: false }
      : { activeItemId: overviewFallback, rejectedRequestedItem: true };
  }
  const backendActive = String(navigation?.active_navigation_item_id ?? "").trim();
  return {
    activeItemId: visibleIds.has(backendActive) ? backendActive : overviewFallback,
    rejectedRequestedItem: false,
  };
}

/**
 * 绑定同页导航 hash 的重新校验。
 *
 * 参数：target 是浏览器 window 等事件目标，refreshNavigation 是重新读取后端导航并渲染的回调。
 * 返回：成功绑定返回 true；非浏览器或回调无效时返回 false，便于 Node 单测安全导入。
 * 安全：手写 hash 和同页锚点不会重新加载 ES 模块，必须主动重新读取后端可见导航，不能只靠前端 DOM 隐藏受限入口。
 */
export function bindNavigationHashRefresh(target, refreshNavigation) {
  if (typeof target?.addEventListener !== "function" || typeof refreshNavigation !== "function") {
    return false;
  }
  target.addEventListener("hashchange", () => {
    // refresh 内部会再次请求后端角色化导航；这里不根据 hash 自行推断角色权限。
    void refreshNavigation();
  });
  return true;
}

/**
 * 读取当前登录用户可见的控制台导航。
 * 前端只传当前 hash 对应的候选 active item，不传 tenant_id 或 role，真实可见性由后端认证上下文决定。
 */
export async function getConsoleNavigation({ controlApiUrl, token, activeItemId }) {
  const url = new URL(`${controlApiUrl}/console/navigation`);
  setOptionalParam(url, "active_item_id", activeItemId);
  return consoleShellRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 读取当前用户控制台偏好。
 * 偏好只用于恢复导航位置和侧边栏状态，不承载业务筛选条件或任何密钥上下文。
 */
export async function getConsolePreferences({ controlApiUrl, token }) {
  return consoleShellRequest(`${controlApiUrl}/console/preferences`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 更新当前用户控制台偏好。
 * 请求体只允许 UI 偏好字段；tenant_id、user_id 和角色都必须由后端登录态恢复。
 */
export async function updateConsolePreferences({
  controlApiUrl,
  token,
  activeNavigationItemId,
  sidebarCollapsed,
}) {
  const payload = {};
  if (activeNavigationItemId !== undefined) {
    payload.active_navigation_item_id = activeNavigationItemId;
  }
  if (sidebarCollapsed !== undefined) {
    payload.sidebar_collapsed = Boolean(sidebarCollapsed);
  }
  return consoleShellRequest(`${controlApiUrl}/console/preferences`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 记录控制台页面访问事件。
 * payload 只放低风险 UI 元数据，严禁放 Token、Provider Key、Virtual Key 明文、Prompt 或 Response。
 */
export async function recordConsolePageView({
  controlApiUrl,
  token,
  navigationItemId,
  targetSection,
  path,
  payload = {},
  eventType = "console.page_viewed",
}) {
  return consoleShellRequest(`${controlApiUrl}/console/events`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      event_type: eventType,
      navigation_item_id: navigationItemId,
      target_section: targetSection,
      path,
      payload,
    }),
  });
}

/**
 * 写入可选查询参数，空字符串不发送，避免后端收到无意义 active item。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 调用控制台骨架 API 并归一化错误对象。
 * 这里保留 request_id/trace_id 供页面展示，避免把底层 fetch 异常直接暴露给用户。
 */
async function consoleShellRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_shell_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 统一错误对象便于主应用复用登录失效、权限拒绝和表单错误处理路径。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `控制台接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应，保证非 JSON 错误也能被转换成结构化前端错误。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
