import { AuthApiError } from "./auth.js";
import { IdentityApiError } from "./identity.js";
import { listWorkspaceMemberships, removeWorkspaceMembership, updateWorkspaceMembership } from "./workspaces.js";

export function listWorkspaceMembers(input) {
  return workspaceMembersRequest(listWorkspaceMemberships, input, { retryUnavailable: true });
}

export function updateWorkspaceMember(input) {
  return workspaceMembersRequest(updateWorkspaceMembership, input);
}

export function removeWorkspaceMember(input) {
  return workspaceMembersRequest(removeWorkspaceMembership, input);
}

async function workspaceMembersRequest(request, input, { retryUnavailable = false } = {}) {
  let attempt = 0;
  while (true) {
    try {
      return await request(input);
    } catch (error) {
      if (retryUnavailable && attempt === 0 && error instanceof IdentityApiError
        && error.status === 503 && error.code === "membership.unavailable") {
        attempt += 1;
        await waitForMembershipRetry(input?.signal);
        continue;
      }
      if (!(error instanceof IdentityApiError)) throw error;
      throw new AuthApiError({ status: error.status, code: error.code, message: error.message,
        requestId: error.requestId, traceId: error.traceId });
    }
  }
}

function waitForMembershipRetry(signal) {
  if (signal?.aborted) return Promise.reject(new DOMException("The operation was aborted.", "AbortError"));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, 120);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * 查询当前租户用户列表。
 *
 * 用户管理只能看当前 Session 的 tenant_id，前端不拼接租户参数，后端也会做 RBAC 校验。
 */
export async function listUsers({ controlApiUrl, token }) {
  return usersRequest(`${controlApiUrl}/users`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 创建当前租户下的新用户。
 *
 * 初始密码只在本次请求中发送给后端，前端不会保存；后端负责哈希和审计。
 */
export async function createUser({ controlApiUrl, token, email, displayName, password, role }) {
  return usersRequest(`${controlApiUrl}/users`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      email,
      display_name: displayName,
      password,
      role,
    }),
  });
}

/**
 * 修改用户角色。
 *
 * 只允许提交 role 字段，避免表单夹带 tenant_id/status 等字段绕过服务层边界。
 */
export async function updateUserRole({ controlApiUrl, token, userId, role }) {
  return usersRequest(`${controlApiUrl}/users/${encodeURIComponent(userId)}/role`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ role }),
  });
}

/**
 * 发送用户管理 API 请求并统一错误结构。
 *
 * 管理员操作需要可追踪 request_id；非 2xx 响应统一抛 AuthApiError。
 */
async function usersRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_users_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 错误体缺失时仍用响应头里的 request_id 支撑排障。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `用户接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 安全读取 JSON 响应。
 *
 * 用户管理接口正常返回 JSON；异常空响应转为空对象交给统一错误包装。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
