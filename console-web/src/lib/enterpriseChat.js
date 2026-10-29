import { AuthApiError } from "./auth.js";

/**
 * PAGE-093 企业 Chat 的严格 Control API 客户端。
 *
 * Prompt/Response 只存在于当前 fetch 调用栈和调用方内存；本模块不重试、不记录请求体，也不把正文
 * 写入 URL、Storage 或错误对象。tenant_id、用户、Virtual Key、Provider 与 Endpoint 始终由后端恢复。
 */

// 公开会话路径只接受受限资源标识，避免任意路径片段越过 Control API 的资源边界。
const SAFE_RESOURCE_ID = /^[A-Za-z0-9_-]{1,128}$/;

/** 读取当前租户可用于企业 Chat 的最小安全目录。 */
export async function getEnterpriseChatOptions({ controlApiUrl, token, signal }) {
  return enterpriseChatRequest(`${controlApiUrl}/chat/options`, {
    headers: authorizationHeaders(token),
    signal,
  });
}

/** 创建绑定当前用户、登录 Session、项目、环境和模型别名的短生命周期会话。 */
export async function createEnterpriseChatSession({
  controlApiUrl,
  token,
  projectId,
  environmentId,
  modelAliasId,
  signal,
}) {
  // 请求体只提交公开选择 ID；tenant_id、用户与密钥必须由后端认证上下文恢复并校验。
  return enterpriseChatRequest(`${controlApiUrl}/chat/sessions`, {
    method: "POST",
    headers: jsonHeaders(token),
    body: JSON.stringify({
      project_id: projectId,
      environment_id: environmentId,
      model_alias_id: modelAliasId,
    }),
    signal,
  });
}

/**
 * 发送一次非流式消息。
 *
 * clientMessageId 由页面为单次点击生成，客户端绝不自动重试；网络结果不确定时必须由用户显式开始
 * 新消息，避免同一 Prompt 因重试产生二次计费。
 */
export async function sendEnterpriseChatMessage({
  controlApiUrl,
  token,
  chatSessionId,
  clientMessageId,
  history,
  promptTemplateVersionId = null,
  templateVariables = null,
  signal,
}) {
  const sessionId = requireResourceId(chatSessionId);
  const hasTemplateVersion = promptTemplateVersionId !== null && promptTemplateVersionId !== undefined;
  const hasTemplateVariables = templateVariables !== null && templateVariables !== undefined;
  if (hasTemplateVersion !== hasTemplateVariables) {
    // 两字段必须同时出现或同时省略；禁止残缺模板 wire 被静默降级成普通 Chat。
    throw clientError("chat.request_invalid", "模板调用参数不完整", 422);
  }
  const body = {
    client_message_id: clientMessageId,
    history,
  };
  if (hasTemplateVersion) {
    const versionId = requireTemplateVersionId(promptTemplateVersionId);
    // 变量必须是普通 map；数组、null 或缺失不能被 JSON 序列化后交给后端猜测语义。
    if (
      !templateVariables
      || typeof templateVariables !== "object"
      || Array.isArray(templateVariables)
    ) {
      // 固定错误不回显变量对象，避免 Prompt 派生值进入异常或日志。
      throw clientError("chat.request_invalid", "模板变量无效", 422);
    }
    // 即使状态机已经净化变量，API 边界仍逐键重建 null-prototype map；禁止普通对象展开把
    // ``__proto__`` 等未来合同漂移重新解释为对象继承语义，也不把非法值拼入错误信息。
    const safeVariables = Object.create(null);
    const entries = Object.entries(templateVariables);
    if (entries.length > 32 || entries.some(([name, value]) => (
      !/^[a-z][a-z0-9_]{0,63}$/.test(name)
      || ["constructor", "prototype", "__proto__"].includes(name)
      || typeof value !== "string"
    ))) {
      // 发送边界再次失败关闭，固定错误不能拼接变量名、值或对象原型信息。
      throw clientError("chat.request_invalid", "模板变量无效", 422);
    }
    for (const [name, value] of entries) safeVariables[name] = value;
    // 客户端只提交精确 version ID 和变量 map；template ID、正文、tenant 与 owner 均由后端反查。
    body.prompt_template_version_id = versionId;
    body.template_variables = safeVariables;
  }
  return enterpriseChatRequest(
    `${controlApiUrl}/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    {
      method: "POST",
      headers: jsonHeaders(token),
      body: JSON.stringify(body),
      signal,
    },
  );
}

/** 清空并终止当前短生命周期会话；204 响应不尝试解析 JSON。 */
export async function clearEnterpriseChatSession({
  controlApiUrl,
  token,
  chatSessionId,
  signal,
}) {
  const sessionId = requireResourceId(chatSessionId);
  // 清理操作只携带受校验的会话 ID 和当前 Token，禁止把正文或租户字段拼入 URL。
  return enterpriseChatRequest(`${controlApiUrl}/chat/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    headers: authorizationHeaders(token),
    signal,
  });
}

/** 为一次发送生成规范 UUID；缺失 Web Crypto 时失败关闭，不使用时间戳充当幂等 ID。 */
export function createEnterpriseChatClientMessageId() {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw clientError("chat.request_invalid", "当前浏览器无法生成安全消息标识", 422);
  }
  return globalThis.crypto.randomUUID().toLowerCase();
}

/** 统一执行 no-store 请求并把后端错误折叠为不含正文的 AuthApiError。 */
async function enterpriseChatRequest(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      cache: "no-store",
      credentials: "omit",
      // ``cache: no-store`` 已同时约束浏览器读写 HTTP 缓存；不要再附加 Cache-Control
      // 请求头，否则跨源调用会把它纳入 CORS 预检并在后端允许列表之外失败。响应侧仍由
      // Control API 统一返回 ``Cache-Control: no-store``，因此不会放宽正文或目录的缓存边界。
      headers: options.headers ?? {},
    });
  } catch (error) {
    // AbortError 代表主动取消，必须原样交给 owner 状态机识别，不能伪装成网关故障。
    if (error?.name === "AbortError") throw error;
    // 网络异常对象可能包含 URL 或代理信息，只返回固定错误，不附带原始异常文本。
    throw clientError("chat.gateway_unavailable", "企业 Chat 暂时不可用", 503);
  }
  if (response.status === 204) return { state: "ok" };
  const payload = await readJson(response);
  if (!response.ok) {
    // 只映射统一公开错误字段，明确丢弃代理正文、Provider 细节和其他未批准响应内容。
    throw new AuthApiError({
      status: response.status,
      code: payload.code || "chat.gateway_unavailable",
      message: payload.message || "企业 Chat 请求失败",
      requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: null,
    });
  }
  return { state: "ok", ...payload };
}

/** 认证头只在当前请求栈使用，不复制到任何可渲染状态。 */
function authorizationHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

/** JSON 写接口固定媒体类型，后端仍会在读取正文前执行实时 ``chat.use`` 校验。 */
function jsonHeaders(token) {
  return {
    ...authorizationHeaders(token),
    "Content-Type": "application/json",
  };
}

/** 路径 ID 必须满足公开资源格式；非法值在出网前使用固定错误失败。 */
function requireResourceId(value) {
  const normalized = String(value ?? "").trim();
  if (!SAFE_RESOURCE_ID.test(normalized)) {
    // 非法路径标识在发起网络请求前失败关闭，避免路径穿透或错误资源被后端误解析。
    throw clientError("chat.request_invalid", "企业 Chat 会话标识无效", 422);
  }
  return normalized;
}

/** 模板调用只接受不可变 ptv_* ID；客户端永远不得以模板 ID 替代版本 owner。 */
function requireTemplateVersionId(value) {
  const normalized = String(value ?? "").trim();
  // 非 ptv_* 值在出网前失败，避免 template ID 与不可变 version owner 发生身份歧义。
  if (!/^ptv_[A-Za-z0-9_-]{1,120}$/.test(normalized)) {
    throw clientError("chat.request_invalid", "模板版本标识无效", 422);
  }
  return normalized;
}

/** 代理或开发服务器返回非 JSON 时只产生空对象，禁止把 HTML/正文写入错误。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 构造本地固定错误，不接受原始异常、Token、URL 或消息正文。 */
function clientError(code, message, status) {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}
