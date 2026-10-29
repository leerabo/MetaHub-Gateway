import { AuthApiError } from "./auth.js";

/**
 * 调用控制面内置 API 调试台。
 *
 * 前端只提交 Virtual Key 的记录 ID、模型别名和临时 messages；tenant_id、Virtual Key 明文与 Provider Key
 * 都不允许从浏览器传入，真实权限和租户边界由后端登录态校验。
 */
export async function sendDebugChatCompletion({
  controlApiUrl,
  token,
  virtualKeyId,
  environmentId,
  modelAlias,
  messages,
  temperature,
  maxTokens,
}) {
  return debugConsoleRequest(`${controlApiUrl}/debug/chat-completions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      virtual_key_id: virtualKeyId,
      environment_id: environmentId || null,
      model_alias: modelAlias,
      messages,
      temperature,
      max_tokens: maxTokens,
    }),
  });
}

/**
 * 发送 Debug Console API 请求并统一错误结构。
 *
 * 控制面所有失败响应都应包含 request_id/trace_id；这里保留这些字段给页面展示和日志跳转，不读取任何敏感头。
 */
async function debugConsoleRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_debug_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 非 2xx 响应必须优先使用后端统一错误体，避免把网络或 Provider 细节直接暴露到页面。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `调试台接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 容错读取 JSON 响应体。
 *
 * 代理错误或开发服务器异常可能返回非 JSON 内容；空对象让调用方仍可按统一错误分支展示 request_id。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
