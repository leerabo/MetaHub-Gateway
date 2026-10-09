import { AuthApiError } from "./auth.js";
import { IdentityApiError } from "./identity.js";
import { getAccountWorkspace, updateAccountWorkspace, switchIdentityWorkspace } from "./workspaces.js";

export function getWorkspaceProfile(input) {
  return profileRequest(getAccountWorkspace, input);
}

export function updateWorkspaceProfile({ workspaceId, revision, kind, name, ...options }) {
  if (kind !== "organization") {
    throw new AuthApiError({ status: 400, code: "workspace.update_invalid", message: "空间资料不可用。" });
  }
  return profileRequest(updateAccountWorkspace, {
    ...options, workspaceId, revision, changes: { name },
  });
}

export function reenterWorkspaceProfile(input) {
  return profileRequest(switchIdentityWorkspace, input);
}

async function profileRequest(request, { controlApiUrl, workspaceId, revision, changes, signal }) {
  try {
    return { state: "ok", ...await request({ controlApiUrl, workspaceId, revision, changes, signal }) };
  } catch (error) {
    if (!(error instanceof IdentityApiError)) throw error;
    throw new AuthApiError({ status: error.status, code: error.code, message: error.message,
      requestId: error.requestId, traceId: error.traceId });
  }
}

/**
 * 读取当前登录上下文所属租户。
 *
 * 控制台展示的 tenant_id/name/service_mode 都以后端 Session 为准，前端不提供
 * 任意租户查询能力。
 */
export async function getCurrentTenant({ controlApiUrl, token }) {
  // 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
  return tenantRequest(`${controlApiUrl}/tenant/current`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 更新当前租户的基础信息。
 *
 * 只发送用户实际修改的字段，避免空值覆盖现有配置；后端继续做 admin 权限校验。
 */
export async function updateCurrentTenant({ controlApiUrl, token, name, serviceMode }) {
  const payload = {};
  if (name !== undefined) {
    // name 未修改时不放入 PATCH body，防止空字符串误覆盖。
    payload.name = name;
  }
  if (serviceMode !== undefined) {
    payload.service_mode = serviceMode;
  }
  // 租户状态或租户筛选是隔离边界，命中异常时必须立即截断。
  return tenantRequest(`${controlApiUrl}/tenant/current`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
}

/**
 * 发送租户 API 请求并统一错误结构。
 *
 * request_id 由控制台生成并透传，后端错误体和响应头均可用于定位具体操作。
 */
async function tenantRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_tenant_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 租户配置错误直接转成 AuthApiError，页面可以复用统一错误展示组件。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `租户接口返回 ${response.status}`,
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
 * 当响应体不是 JSON 时返回空对象，避免错误路径再抛解析异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
