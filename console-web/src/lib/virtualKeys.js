import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 查询当前租户的 Virtual Key 列表。
 *
 * 列表只展示前缀、状态、环境等安全元数据；Virtual Key 明文绝不从后端返回。
 */
export async function listVirtualKeys({ controlApiUrl, authentication, token, signal, status, environmentId, environment }) {
  const url = new URL(`${controlApiUrl}/virtual-keys`);
  if (status) {
    url.searchParams.set("status", status);
  }
  if (environment) {
    url.searchParams.set("environment", environment);
  }
  if (environmentId) {
    url.searchParams.set("environment_id", environmentId);
  }
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  return virtualKeysRequest(url.toString(), {
    authentication, token, signal,
  });
}

/**
 * 创建 Virtual Key。
 *
 * 后端只在创建响应中返回一次明文 Key；调用方必须立即展示给用户，不能写入缓存。
 */
export async function createVirtualKey({ controlApiUrl, authentication, token, signal, name, environmentId, environment, description, projectId, applicationId }) {
  return virtualKeysRequest(`${controlApiUrl}/virtual-keys`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name,
      environment_id: emptyStringToNull(environmentId),
      environment,
      description,
      project_id: emptyStringToNull(projectId),
      application_id: emptyStringToNull(applicationId),
    }),
  });
}

/**
 * 更新 Virtual Key 元数据或状态。
 *
 * 请求体不包含明文 Key，防止控制台编辑流程意外泄露调用凭证。
 */
export async function updateVirtualKey({ controlApiUrl, authentication, token, signal, virtualKeyId, name, environmentId, environment, description, status, projectId, applicationId }) {
  return virtualKeysRequest(`${controlApiUrl}/virtual-keys/${encodeURIComponent(virtualKeyId)}`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name,
      environment_id: environmentId === undefined ? undefined : emptyStringToNull(environmentId),
      environment,
      description,
      status,
      project_id: projectId === undefined ? undefined : emptyStringToNull(projectId),
      application_id: applicationId === undefined ? undefined : emptyStringToNull(applicationId),
    }),
  });
}

/**
 * 更新 Virtual Key 的基础限流设置。
 *
 * 限流配置只提交 RPM、TPM、并发和状态，不包含明文 Key；后端仍按登录态租户校验归属。
 */
export async function updateVirtualKeyLimits({
  controlApiUrl,
  token,
  authentication,
  signal,
  virtualKeyId,
  rpmLimit,
  tpmLimit,
  concurrencyLimit,
  status,
}) {
  return virtualKeysRequest(`${controlApiUrl}/virtual-keys/${encodeURIComponent(virtualKeyId)}/limits`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      rpm_limit: emptyToNull(rpmLimit),
      tpm_limit: emptyToNull(tpmLimit),
      concurrency_limit: emptyToNull(concurrencyLimit),
      status,
    }),
  });
}

/**
 * 禁用 Virtual Key。
 *
 * 当前实现是软禁用，历史调用日志仍可通过 key 前缀和 id 追溯，不做物理删除。
 */
export async function disableVirtualKey({ controlApiUrl, authentication, token, signal, virtualKeyId }) {
  return virtualKeysRequest(`${controlApiUrl}/virtual-keys/${encodeURIComponent(virtualKeyId)}`, {
    method: "DELETE",
    authentication, token, signal,
  });
}

/**
 * 发送 Virtual Key API 请求并统一错误结构。
 *
 * 每次请求都带控制台 request_id，便于和后端审计日志对应。
 */
async function virtualKeysRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_virtual_keys_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // Key 管理错误必须保留 request_id，方便确认是权限、校验还是后端异常。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Virtual Key 接口返回 ${response.status}`,
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
 *
 * 若响应不是 JSON，返回空对象让统一错误包装继续提供 status/request_id。
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
 * 把空 ID 字段转换为 null。
 *
 * Project/Application 绑定 ID 是字符串，不能走数字转换，否则会破坏后端 ID 匹配。
 */
function emptyStringToNull(value) {
  const normalized = String(value ?? "").trim();
  return normalized || null;
}

/**
 * 把空表单值转换为 null。
 *
 * null 在后端语义是清除对应限制；非空值转为数字，让非法字符串由后端统一错误码处理。
 */
function emptyToNull(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return Number(value);
}
