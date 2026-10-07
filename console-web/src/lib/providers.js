import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

/**
 * 查询当前租户 Provider 列表。
 *
 * status/provider_type 是控制面过滤参数；租户范围由 Token 决定，前端不允许传 tenant_id。
 */
export async function listProviders({ controlApiUrl, authentication, token, status, providerType, signal }) {
  const url = new URL(`${controlApiUrl}/providers`);
  if (status) {
    url.searchParams.set("status", status);
  }
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  if (providerType) {
    url.searchParams.set("provider_type", providerType);
  }
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  return providersRequest(url.toString(), {
    signal,
    authentication, token,
  });
}

/**
 * 查询某个 Provider 的 KeyPool 安全元数据。
 *
 * 响应只包含 key_prefix/fingerprint 等脱敏字段，Provider Key 明文不会回到控制台。
 */
export async function listProviderKeys({ controlApiUrl, authentication, token, providerId, signal }) {
  return providersRequest(`${controlApiUrl}/providers/${encodeURIComponent(providerId)}/keys`, {
    signal,
    authentication, token,
  });
}

/**
 * 创建 Provider Endpoint 配置。
 *
 * 这里只提交 Endpoint 元数据；真正的 Provider Key 通过 KeyPool 接口单独创建，便于
 * 审计和后续轮换。
 */
export async function createProvider({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerCode,
  name,
  providerType,
  baseUrl,
  authType,
  description,
}) {
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  return providersRequest(`${controlApiUrl}/providers`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      provider_code: providerCode,
      name,
      provider_type: providerType,
      base_url: baseUrl,
      auth_type: authType,
      description,
    }),
  });
}

/**
 * 创建 Provider Key。
 *
 * plainValue 只在本次 POST body 中出现，后端落库前会加密，前端不缓存明文。
 */
export async function createProviderKey({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerId,
  name,
  plainValue,
  weight,
}) {
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  return providersRequest(`${controlApiUrl}/providers/${encodeURIComponent(providerId)}/keys`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name,
      plain_value: plainValue,
      weight,
    }),
  });
}

/**
 * 更新 Provider 元数据或启停状态。
 *
 * 后端会校验 admin 权限和租户归属，前端只负责把表单字段转换成 API 字段。
 */
export async function updateProvider({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerId,
  name,
  providerType,
  baseUrl,
  authType,
  description,
  status,
}) {
  // Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
  return providersRequest(`${controlApiUrl}/providers/${encodeURIComponent(providerId)}`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name,
      provider_type: providerType,
      base_url: baseUrl,
      auth_type: authType,
      description,
      status,
    }),
  });
}

/**
 * 删除一个尚未被模型、调用或历史记录引用的 Provider。
 * 后端会在同一事务中检查引用并拒绝破坏历史数据的删除。
 */
export async function deleteProvider({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerId,
}) {
  return providersRequest(`${controlApiUrl}/providers/${encodeURIComponent(providerId)}`, {
    method: "DELETE",
    authentication,
    token,
    signal,
  });
}

/**
 * 更新 Provider Key 元数据或状态。
 *
 * 该接口不接收明文 Key；轮换 Key 应创建新记录再禁用旧记录，避免明文回显。
 */
export async function updateProviderKey({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerKeyId,
  name,
  weight,
  status,
}) {
  // 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
  return providersRequest(`${controlApiUrl}/provider-keys/${encodeURIComponent(providerKeyId)}`, {
    method: "PATCH",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      name,
      weight,
      status,
    }),
  });
}

/** 删除一把未被当前运行绑定引用的 Provider Key。 */
export async function deleteProviderKey({
  controlApiUrl,
  token,
  authentication,
  signal,
  providerKeyId,
}) {
  return providersRequest(`${controlApiUrl}/provider-keys/${encodeURIComponent(providerKeyId)}`, {
    method: "DELETE",
    authentication,
    token,
    signal,
  });
}

/**
 * 触发 Provider Endpoint 连通性测试。
 *
 * timeoutSeconds 限制本地调试等待时间，避免控制台因为一个不可达 Endpoint 卡住。
 */
export async function testProviderConnection({ controlApiUrl, authentication, token, signal, providerId, timeoutSeconds = 5 }) {
  return providersRequest(`${controlApiUrl}/providers/${encodeURIComponent(providerId)}/test`, {
    method: "POST",
    authentication, token, signal,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ timeout_seconds: timeoutSeconds }),
  });
}

/**
 * 发送 Provider API 请求并归一化错误。
 *
 * 所有请求都带 X-Request-ID，方便把控制台操作和控制面审计日志对应起来。
 */
async function providersRequest(url, options = {}) {
  const response = await authenticatedResourceFetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_providers_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // Provider 配置属于高风险操作，错误路径必须保留后端 request_id。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `Provider 接口返回 ${response.status}`,
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
 * 如果开发代理返回非 JSON 内容，返回空对象让统一错误包装继续工作。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    return {};
  }
}
