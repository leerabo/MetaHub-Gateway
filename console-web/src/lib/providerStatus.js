import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * PRD-061 Provider 状态只读客户端。
 * 安全边界：tenant_id、窗口、Endpoint 和凭据都不能由浏览器提交；这里只发送固定筛选白名单。
 */

/** 浏览器允许提交的唯一筛选白名单，禁止 tenant_id、窗口、Endpoint 和凭据字段进入查询串。 */
const FILTER_KEYS = Object.freeze([
  "provider_id",
  "provider_type",
  "health_status",
  "provider_status",
  "limit",
  "offset",
]);

/** 查询当前认证租户的 Provider 状态快照。 */
export async function listProviderStatuses({ controlApiUrl, authentication, token, filters = {}, signal }) {
  const url = new URL(`${controlApiUrl}/status/providers`);
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const response = await authenticatedResourceFetch(url.toString(), {
    authentication, token, signal,
    headers: {
      "X-Request-ID": `console_provider_status_${Date.now()}`,
    },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    // 非 JSON 代理错误不能把 HTML/正文带入 DOM，统一降级为空 envelope。
    payload = {};
  }
  // 非 2xx 统一转换为 AuthApiError，只保留公开 envelope 和链路标识供页面追踪。
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Provider 状态接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  // 本地状态最后写入，避免异常代理响应用同名字段把成功请求伪装为错误或其他页面状态。
  return { ...payload, state: "ok" };
}
