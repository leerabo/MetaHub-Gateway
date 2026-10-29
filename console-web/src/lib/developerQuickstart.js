import { AuthApiError } from "./auth.js";

/**
 * 读取开发者快速开始数据。
 * 请求只携带登录 Token，不能拼接 tenant_id；后端会按认证上下文生成 base_url、模型别名和安全 Key 占位。
 */
export async function getDeveloperQuickstartData({ controlApiUrl, token }) {
  return developerQuickstartRequest(`${controlApiUrl}/developer/quickstart-data`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 调用开发者门户 API 并归一化错误对象。
 * 统一错误对象保留 request_id/trace_id，便于从快速开始页跳到调用日志或后端日志排查。
 */
async function developerQuickstartRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_quickstart_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 控制面统一错误体优先，响应头兜底，保证页面错误态不丢 request_id。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `快速开始接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 安全读取 JSON 响应体。
 * 开发代理或网络错误可能返回非 JSON，空对象能让上层继续走统一错误分支。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
