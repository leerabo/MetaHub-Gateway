// 登录态存储键集中定义，便于统一清理本地会话且避免把 Bearer Token 写入日志。
const TOKEN_KEY = "metahub.auth_token";
const CONTEXT_KEY = "metahub.auth_context";

/**
 * 控制台认证 API 的统一异常。
 *
 * 后端所有错误都应带 request_id/trace_id；前端保留这些字段，便于用户把页面报错
 * 和控制面结构化日志串起来排查。
 */
export class AuthApiError extends Error {
  /**
   * 封装 前端 API 客户端 的 `constructor` 调用，统一处理请求参数、认证头和错误对象。
   * 边界：客户端只负责展示和发起请求，真实权限仍由后端 API 校验。
   */
  constructor({ status, code, message, requestId, traceId, suggestion, diagnosis }) {
    super(message || "认证请求失败");
    this.name = "AuthApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.traceId = traceId;
    this.suggestion = suggestion;
    this.diagnosis = diagnosis ?? null;
  }
}

/**
 * 登录控制台并换取短期会话 Token。
 *
 * tenant_slug 只来自登录表单，真正的 tenant_id 由后端认证结果决定，避免前端伪造
 * 业务租户边界。
 */
export async function loginAdmin({ controlApiUrl, tenantSlug, email, password }) {
  const payload = {
    tenant_slug: tenantSlug || "default",
    email,
    password,
  };
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  return authRequest(`${controlApiUrl}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/**
 * 读取当前登录用户、角色和租户上下文。
 *
 * 页面刷新后会先调用该接口校验本地 Token 是否仍有效，而不是相信 sessionStorage
 * 中缓存的角色。
 */
export async function getCurrentUser({ controlApiUrl, token }) {
  // 认证请求是访问控制边界，调用方必须透传统一错误和 request_id。
  return authRequest(`${controlApiUrl}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 注销当前控制台会话。
 *
 * 后端负责让 session 失效；前端随后清理本地缓存，避免已注销 Token 被继续复用。
 */
export async function logoutAdmin({ controlApiUrl, token }) {
  return authRequest(`${controlApiUrl}/auth/logout`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
}

/**
 * 保存认证上下文到浏览器会话级存储。
 *
 * 这里不使用 localStorage，是为了关闭浏览器后自然清理控制台登录态，降低共享机器上
 * 误用管理员 Session 的风险。
 */
export function saveAuthSession(token, context, storage = currentStorage()) {
  if (!storage) {
    return;
  }
  // 异步 owner 失效时只能清除认证槽位；禁止把 null、数组或其他畸形身份序列化成看似有效的 Session。
  if (typeof token !== "string" || !token || !context || typeof context !== "object" || Array.isArray(context)) {
    clearAuthSession(storage);
    return;
  }
  storage.setItem(TOKEN_KEY, token);
  storage.setItem(CONTEXT_KEY, JSON.stringify(context));
}

/**
 * 从会话级存储恢复认证上下文。
 *
 * 缓存内容解析失败时直接清理并返回空 Session，避免坏数据让控制台卡在半登录状态。
 */
export function readAuthSession(storage = currentStorage()) {
  if (!storage) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return { token: null, context: null };
  }
  const token = storage.getItem(TOKEN_KEY);
  const rawContext = storage.getItem(CONTEXT_KEY);
  // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
  if (!token || !rawContext) {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return { token: null, context: null };
  }
  try {
    // 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    return { token, context: JSON.parse(rawContext) };
  } catch {
    // 缓存被手工改坏时直接清理，后续页面会回到登录页。
    clearAuthSession(storage);
    return { token: null, context: null };
  }
}

/**
 * 清理本地认证缓存。
 *
 * 登录失败、Token 过期或用户主动登出都会走这里，保证页面后续请求不再携带旧 Token。
 */
export function clearAuthSession(storage = currentStorage()) {
  if (!storage) {
    return;
  }
  storage.removeItem(TOKEN_KEY);
  storage.removeItem(CONTEXT_KEY);
}

/**
 * 发送认证相关 HTTP 请求并归一化错误。
 *
 * 每次请求都带控制台生成的 X-Request-ID，后端会回传统一错误结构；这里把非 2xx
 * 响应转换为 AuthApiError，调用方只需处理一种异常类型。
 */
async function authRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": `console_auth_${Date.now()}`,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 错误响应优先使用后端 request_id；代理或网络异常时退回响应头或占位符。
    throw new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `认证接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
  }
  return payload;
}

/**
 * 安全读取 JSON 响应体。
 *
 * 某些网络错误或代理错误可能没有 JSON body；返回空对象可以让错误路径仍然带上
 * HTTP status 和响应头里的 request_id。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 获取当前浏览器会话存储对象。
 *
 * 单元测试会注入 fake storage；运行在非浏览器环境时返回 null，避免模块加载失败。
 */
function currentStorage() {
  return globalThis.sessionStorage ?? null;
}
