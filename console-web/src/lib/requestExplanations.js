/**
 * 模块职责：调用 PAGE-079 请求解释只读 API，并统一转换公开错误合同。
 * 维护边界：不提交 tenant、actor、Prompt、Response 或 include_raw；真实 RBAC 与租户隔离由后端执行。
 */
import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 读取当前登录用户有权查看的一条请求解释。
 *
 * 参数中的 requestId 只用于 URL path；tenant_id、角色和用户身份一律由后端 Session 恢复。
 * 返回值仅保留后端白名单 DTO 与统一 request_id/trace_id，不能在这里拼接 Prompt、Response
 * 或任何“include_raw”开关扩大敏感数据暴露面。
 */
export async function getRequestExplanation({
  controlApiUrl,
  token,
  authentication,
  requestId,
  signal,
}) {
  const sourceRequestId = String(requestId ?? "");
  return requestExplanationRequest(
    `${controlApiUrl}/request-explain/${encodeURIComponent(sourceRequestId)}`,
    {
      token,
      authentication,
      csrfRequired: true,
      signal,
    },
  );
}

/**
 * 调用 Request Explainability API，并把所有非 2xx 响应收敛为控制台统一错误对象。
 *
 * 异常正文、数据库消息和响应 Header 不进入 UI；只保留后端公开的 code、message、
 * suggestion 以及 operation request_id/trace_id，便于在不泄露源请求内容的前提下排障。
 */
async function requestExplanationRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_request_explanation_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  // 非 2xx 只保留统一错误合同和链路标识，响应正文、Header 与上游细节不得进入页面状态。
  if (!response.ok) {
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `请求解释接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/** 容错读取 JSON；空响应或非 JSON 错误不能在前端再制造一条解析异常。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
