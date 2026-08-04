/**
 * 模块职责：封装 Project/Application 控制面 API，供控制台项目归属页面和 Key 绑定选择器复用。
 * 维护边界：前端只提交业务字段，不提交 tenant_id；租户隔离、角色权限和状态校验必须由后端完成。
 * 来源文件：console-web/src/lib/projects.js
 */

import { AuthApiError } from "./auth.js";

/**
 * 查询当前租户可见的项目列表。
 * status/search 只是筛选条件，不能影响后端从登录态恢复 tenant_id 的安全边界。
 */
export async function listProjects({ controlApiUrl, token, status = "all", search } = {}) {
  const url = new URL(`${controlApiUrl}/projects`);
  setOptionalParam(url, "status", status);
  setOptionalParam(url, "search", search);
  return projectsRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 创建项目归属。
 * 项目编码会进入日志、用量和预算筛选口径，前端只负责提交原始表单值，合法性由后端统一错误码裁决。
 */
export async function createProject({ controlApiUrl, token, projectCode, name, description }) {
  return projectsRequest(`${controlApiUrl}/projects`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      compactPayload({
        project_code: projectCode,
        name,
        description,
      }),
    ),
  });
}

/**
 * 更新项目元数据。
 * 该函数保留给后续编辑入口复用；即使前端隐藏编辑按钮，后端仍必须校验 admin 权限。
 */
export async function updateProject({ controlApiUrl, token, projectId, name, description, ownerUserId, status }) {
  return projectsRequest(`${controlApiUrl}/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      compactPayload({
        name,
        description,
        owner_user_id: ownerUserId,
        status,
      }),
    ),
  });
}

/**
 * 查询当前租户可见的应用列表。
 * project_id 是业务筛选条件，不是安全边界；后端仍按登录态 tenant_id 过滤。
 */
export async function listApplications({ controlApiUrl, token, projectId, status = "all", search } = {}) {
  const url = new URL(`${controlApiUrl}/applications`);
  setOptionalParam(url, "project_id", projectId);
  setOptionalParam(url, "status", status);
  setOptionalParam(url, "search", search);
  return projectsRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 创建项目下的应用归属。
 * 应用必须挂在项目下面，前端不允许绕过 project_id 提交独立应用。
 */
export async function createApplication({ controlApiUrl, token, projectId, applicationCode, name, description }) {
  return projectsRequest(`${controlApiUrl}/applications`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      compactPayload({
        project_id: projectId,
        application_code: applicationCode,
        name,
        description,
      }),
    ),
  });
}

/**
 * 更新应用元数据或迁移所属项目。
 * 项目迁移会影响已绑定 Key 的后续归因，真实一致性修正必须由后端事务保证。
 */
export async function updateApplication({ controlApiUrl, token, applicationId, projectId, name, description, status }) {
  return projectsRequest(`${controlApiUrl}/applications/${encodeURIComponent(applicationId)}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(
      compactPayload({
        project_id: projectId,
        name,
        description,
        status,
      }),
    ),
  });
}

/**
 * 调用 Project/Application API 并统一错误结构。
 * 错误对象只展示 code/request_id/trace_id 等排障字段，不包含任何 Key 或租户外数据。
 */
async function projectsRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_projects_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 项目归属错误会影响 Key、日志和预算链路，必须保留后端标准 code 与 request_id。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `项目/应用接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return { state: "ok", ...payload };
}

/**
 * 设置可选查询参数，空字符串不进入 URL，避免后端收到无意义筛选条件。
 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 压缩写入 payload，丢弃空字符串、null 和 undefined，但保留明确数字和状态枚举。
 */
function compactPayload(payload) {
  return Object.fromEntries(
    Object.entries(payload).filter(([, value]) => value !== null && value !== undefined && String(value).trim() !== ""),
  );
}

/**
 * 容错读取 JSON 响应体，避免空错误响应造成前端二次异常。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
