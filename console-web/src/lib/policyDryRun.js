/**
 * 模块职责：调用策略 Dry-Run 管理 API，并把统一错误转换为控制台既有 AuthApiError。
 * 安全边界：Prompt 只能存在于当前函数参数和 fetch 请求体，不得进入 URL、浏览器存储、日志或模块缓存。
 */

import { AuthApiError } from "./auth.js";

/**
 * 生成一次策略预演 request_id。
 * 返回值只包含随机链路标识，不混入 Prompt、模型、Project 或租户信息。
 */
export function createPolicyDryRunRequestId() {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? `${Date.now()}_${Math.random().toString(36).slice(2)}`;
  return `req_console_policy_dry_run_${random}`;
}

/**
 * 执行当前管理员的一次策略预演。
 * 参数中的 tenant 永远不由前端提供；后端从 Bearer Session 恢复租户并再次执行 RBAC。
 */
export async function executePolicyDryRun({
  controlApiUrl,
  token,
  prompt,
  model,
  projectId,
  environmentId,
  dataClass,
  maxOutputTokens,
  requestId = createPolicyDryRunRequestId(),
  signal,
}) {
  // payload 只在当前调用栈生存；禁止把它提升到模块变量或加入错误对象。
  const payload = {
    prompt,
    model,
    project_id: projectId,
    environment_id: environmentId,
    data_class: dataClass,
  };
  // 只有正整数才进入请求；非法或缺失值交由后端默认值处理，避免前端伪造宽松资源边界。
  if (Number.isInteger(maxOutputTokens) && maxOutputTokens > 0) {
    payload.max_output_tokens = maxOutputTokens;
  }
  const response = await fetch(`${controlApiUrl}/policies/dry-run`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Request-ID": requestId,
    },
    body: JSON.stringify(payload),
    signal,
  });
  const body = await readJson(response);
  if (!response.ok) {
    // 错误对象只保存统一 code 与链路 ID，绝不附加请求 payload 或原始响应正文。
    throw new AuthApiError({
      status: response.status,
      code: body.code || "policy.dry_run_unavailable",
      message: body.message || `策略预演接口返回 ${response.status}`,
      requestId: body.request_id || response.headers.get("X-Request-ID") || requestId,
      traceId: body.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: body.suggestion || "",
      diagnosis: body.diagnosis ?? null,
    });
  }
  return {
    ...body,
    request_id: body.request_id || response.headers.get("X-Request-ID") || requestId,
    trace_id: body.trace_id || response.headers.get("X-Trace-ID") || "-",
  };
}

/**
 * 安全解析 JSON 响应。
 * 非 JSON 错误不能把 HTML、代理正文或底层异常带入页面，只返回空对象交给统一错误分支。
 */
async function readJson(response) {
  try {
    const body = await response.json();
    return body && typeof body === "object" && !Array.isArray(body) ? body : {};
  } catch {
    return {};
  }
}
