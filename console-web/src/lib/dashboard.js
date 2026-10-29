import { AuthApiError } from "./auth.js";

// PAGE-092 只接受服务端固定目录中的视图代码；角色、租户和权限从认证上下文恢复，不能进入查询串。
const ROLE_DASHBOARD_VIEWS = new Set([
  "executive",
  "finance",
  "security",
  "developer",
  "operations",
]);

/**
 * 读取当前租户的 Dashboard 总览。
 * 前端不传 tenant_id 或日期范围；后端按登录态恢复租户，并固定使用中国业务日今日窗口。
 */
export async function getDashboardOverview({ controlApiUrl, token }) {
  return dashboardRequest(`${controlApiUrl}/dashboard/overview`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 读取 PAGE-092 当前角色首页。
 *
 * 参数：view 为空时由后端选择角色默认视图；非空值必须同时属于固定目录和上一份后端响应下发的
 * availableViewCodes。signal 用于在切换视图、离页或授权变化时立即取消旧请求。
 * 返回：统一的 Dashboard 成功状态；失败抛出只包含公开错误字段的 AuthApiError。
 *
 * 安全约束：本函数绝不接受 tenant_id、role、user_id 或任意时间窗口，也不会把 Token 写入 URL。
 */
export async function getDashboardRoleView({
  controlApiUrl,
  token,
  view,
  availableViewCodes = [],
  signal,
}) {
  const url = new URL(`${controlApiUrl}/dashboard/role-view`);
  const selectedView = typeof view === "string" ? view : "";
  if (view != null && typeof view !== "string") {
    // 非字符串视图不得通过隐式 toString 进入查询串，避免篡改对象扩大后端探测面。
    throw dashboardClientError(
      "dashboard.query_invalid",
      "角色首页视图参数不合法",
      422,
    );
  }
  if (selectedView) {
    const allowed = new Set(
      Array.isArray(availableViewCodes)
        ? availableViewCodes.filter((code) => ROLE_DASHBOARD_VIEWS.has(code))
        : [],
    );
    // 视图按钮只是体验门禁；这里再次要求代码来自后端目录，减少被篡改 DOM 制造的越权探测请求。
    if (!ROLE_DASHBOARD_VIEWS.has(selectedView) || !allowed.has(selectedView)) {
      throw dashboardClientError(
        "dashboard.query_invalid",
        "角色首页视图参数不合法",
        422,
      );
    }
    url.searchParams.set("view", selectedView);
  }
  return dashboardRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/**
 * 调用控制面 Dashboard API，并把统一错误结构转换为控制台可展示对象。
 * Dashboard 只读取聚合摘要，错误对象也不能包含 Prompt、Response、Provider Key 或 Virtual Key 明文。
 */
async function dashboardRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_dashboard_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // Dashboard 是首页高频接口，失败时必须保留 request_id 方便定位控制面聚合查询问题。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Dashboard 接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免空响应导致 Dashboard 页面二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 构造不携带调用方原始输入的本地统一错误。
 * 本地门禁只用于阻止明显被篡改的请求，真实角色权限和租户隔离仍由后端执行。
 */
function dashboardClientError(code, message, status) {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}
