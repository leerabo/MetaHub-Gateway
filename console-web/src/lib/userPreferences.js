import { AuthApiError } from "./auth.js";

/**
 * 读取当前用户语言偏好。
 * 前端不传 tenant_id 或 user_id，真实租户和用户边界由后端登录态恢复。
 */
export async function getUserPreferences({ controlApiUrl, token }) {
  return userPreferenceRequest(`${controlApiUrl}/user/preferences`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 更新当前用户语言偏好。
 * 请求体只发送 locale，不能夹带 tenant_id、role、Token 或业务上下文。
 */
export async function updateUserPreferences({ controlApiUrl, token, locale }) {
  return userPreferenceRequest(`${controlApiUrl}/user/preferences`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ locale }),
  });
}

/**
 * 调用用户偏好 API，并把统一错误结构转换为前端异常对象。
 */
async function userPreferenceRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_user_preferences_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 用户偏好失败必须保留 request_id，方便定位 locale 迁移或权限问题。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `用户偏好接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体，避免空响应导致语言设置页二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
