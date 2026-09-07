import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 读取当前租户 Environment 列表。
 * tenant_id 只由登录 Token 在后端恢复，前端不会把租户参数放入 URL。
 */
export async function listEnvironments({ controlApiUrl, authentication, token, signal, status = "all", code }) {
  const url = new URL(`${controlApiUrl}/environments`);
  if (status) {
    url.searchParams.set("status", status);
  }
  if (code) {
    url.searchParams.set("code", code);
  }
  return environmentRequest(url, {
    authentication, token, signal,
  });
}

/**
 * 创建当前租户标准 Environment。
 * 请求体不包含 tenant_id，也不携带任何 Key、Token、Prompt 或响应内容。
 */
export async function createEnvironment({ controlApiUrl, authentication, token, signal, code, name, description }) {
  return environmentRequest(`${controlApiUrl}/environments`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ code, name, description: optionalText(description) }),
  });
}

/**
 * 更新 Environment 的名称、说明或状态。
 * code 不在可编辑字段中，防止历史 Key、别名和日志快照发生漂移。
 */
export async function updateEnvironment({ controlApiUrl, authentication, token, signal, environmentId, name, description, status }) {
  const body = {};
  if (name !== undefined) body.name = name;
  if (description !== undefined) body.description = optionalText(description);
  if (status !== undefined) body.status = status;
  return environmentRequest(`${controlApiUrl}/environments/${encodeURIComponent(environmentId)}`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

/**
 * 发送 Environment API 请求并保留统一错误体中的 request_id/trace_id。
 */
async function environmentRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_environment_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 后端业务错误优先进入 AuthApiError，页面不得展示网络堆栈或数据库细节。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `环境接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON；代理返回非 JSON 时使用空对象进入统一错误分支。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}

/**
 * 把空说明转换为 null，保持 PATCH/POST 的可清空语义。
 */
function optionalText(value) {
  const text = String(value ?? "").trim();
  return text || null;
}
