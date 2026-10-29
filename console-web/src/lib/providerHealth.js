import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

const FILTER_KEYS = ["provider_id", "provider_type", "health_status", "provider_status", "limit", "offset"];

/** 选择与 KeyPool 聚合结论一致的代表性探测尝试，避免首把失效 Key 的 401 覆盖后续成功。 */
export function selectRepresentativeProbeAttempt(result) {
  const attempts = Array.isArray(result?.attempts) ? result.attempts : [];
  const preferred = result?.result_status === "success"
    ? ["success", "inconclusive", "failed"]
    : result?.result_status === "inconclusive"
      ? ["inconclusive", "failed", "success"]
      : ["failed", "inconclusive", "success"];
  for (const status of preferred) {
    const attempt = attempts.find((item) => item?.result_status === status);
    if (attempt) return attempt;
  }
  return attempts[0] ?? null;
}

/**
 * 查询当前租户 Provider 健康列表。
 * 边界：tenant_id 从实际会话恢复，调用方不能提交 Header 或任意探测 URL。
 */
export async function listProviderHealth({ controlApiUrl, authentication, token, filters = {}, signal }) {
  const url = new URL(`${controlApiUrl}/provider-health`);
  for (const key of FILTER_KEYS) {
    const value = filters[key];
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return providerHealthRequest(url.toString(), {
    authentication, token, signal,
  });
}

/** 触发一次无客户端参数的安全探测，URL 和 Key 均由后端可信配置读取。 */
export async function probeProvider({ controlApiUrl, authentication, token, providerId, signal }) {
  return providerHealthRequest(
    `${controlApiUrl}/providers/${encodeURIComponent(providerId)}/probe`,
    {
      method: "POST",
      authentication, token, signal,
      headers: {
        "Content-Type": "application/json",
      },
      body: "{}",
    },
  );
}

/** 读取厂家模型目录；仅发送已保存的服务 ID，凭据始终由服务端读取。 */
export async function discoverProviderModels({ controlApiUrl, authentication, token, providerId, signal }) {
  return providerHealthRequest(
    `${controlApiUrl}/providers/${encodeURIComponent(providerId)}/discover-models`,
    { method: "POST", authentication, token, signal,
      headers: { "Content-Type": "application/json" }, body: "{}" },
  );
}

/** 把统一错误响应转换为现有控制台 AuthApiError。 */
async function providerHealthRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_provider_health_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    payload = {};
  }
  if (!response.ok) {
    // 统一包装保留 request_id/trace_id，同时拒绝把原始响应对象或请求头传到 UI。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Provider 健康接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { ...payload, state: "ok" };
}
