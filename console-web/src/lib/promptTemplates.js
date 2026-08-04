import { AuthApiError } from "./auth.js";

/**
 * PAGE-094 Prompt 模板管理的严格 Control API 客户端。
 *
 * 模板正文和变量值只存在于调用方内存及单次 fetch 请求体。本模块不记录、不持久化、不自动重试，
 * tenant、actor 与资源 owner 始终由后端 Session 恢复。写操作的稳定 request_id 由页面状态机持有，
 * 网络结果未知时只有相同 canonical intent 才能复用。
 */

// 路径 ID、链路 ID 和筛选状态均使用固定白名单，禁止正文或任意路径片段进入 URL/Header。
const SAFE_TEMPLATE_ID = /^ptpl_[A-Za-z0-9_-]{1,120}$/;
const SAFE_VERSION_ID = /^ptv_[A-Za-z0-9_-]{1,120}$/;
const SAFE_REQUEST_ID = /^[A-Za-z0-9_.:-]{1,256}$/;
const ALLOWED_LIST_STATUS = new Set(["", "active", "disabled"]);
let requestSequence = 0;

/** 读取当前租户的模板安全列表；正文、变量定义和绑定明细不会出现在该响应中。 */
export async function listPromptTemplates({
  controlApiUrl,
  token,
  status = "",
  search = "",
  limit = 50,
  offset = 0,
  signal,
  requestId = createPromptTemplateRequestId("list"),
}) {
  const normalizedStatus = String(status ?? "").trim();
  const normalizedSearch = String(search ?? "").trim();
  if (
    !ALLOWED_LIST_STATUS.has(normalizedStatus)
    || normalizedSearch.length > 120
    || !Number.isSafeInteger(limit)
    || limit < 1
    || limit > 100
    || !Number.isSafeInteger(offset)
    || offset < 0
  ) {
    // 本地失败不发网络请求，也不把非法 search 原文复制进错误消息。
    throw promptTemplateClientError("prompt_template.request_invalid", "模板查询条件无效", 422, requestId);
  }
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (normalizedStatus) query.set("status", normalizedStatus);
  if (normalizedSearch) query.set("search", normalizedSearch);
  return promptTemplateRequest(`${controlApiUrl}/prompt-templates?${query.toString()}`, {
    headers: authorizationHeaders(token),
    signal,
  }, requestId);
}

/** 读取模板表单专用的项目和模型别名最小目录，不借此获得其他领域管理权限。 */
export async function getPromptTemplateOptions({
  controlApiUrl,
  token,
  signal,
  requestId = createPromptTemplateRequestId("options"),
}) {
  return promptTemplateRequest(`${controlApiUrl}/prompt-templates/options`, {
    headers: authorizationHeaders(token),
    signal,
  }, requestId);
}

/** 精确读取一个同租户模板及其不可变版本历史；跨租户与不存在资源由后端同形返回 404。 */
export async function getPromptTemplate({
  controlApiUrl,
  token,
  promptTemplateId,
  signal,
  requestId = createPromptTemplateRequestId("detail"),
}) {
  const templateId = requireTemplateId(promptTemplateId);
  return promptTemplateRequest(
    `${controlApiUrl}/prompt-templates/${encodeURIComponent(templateId)}`,
    { headers: authorizationHeaders(token), signal },
    requestId,
  );
}

/**
 * 精确读取一个已发布版本的解密定义。
 *
 * 响应正文只能交给 PAGE-094 私有状态与 DOM 文本 hydration，禁止写入 URL、Storage、日志或错误详情。
 */
export async function getPromptTemplateVersion({
  controlApiUrl,
  token,
  promptTemplateId,
  promptTemplateVersionId,
  signal,
  requestId = createPromptTemplateRequestId("definition"),
}) {
  const templateId = requireTemplateId(promptTemplateId);
  const versionId = requireVersionId(promptTemplateVersionId);
  return promptTemplateRequest(
    `${controlApiUrl}/prompt-templates/${encodeURIComponent(templateId)}/versions/${encodeURIComponent(versionId)}`,
    { headers: authorizationHeaders(token), signal },
    requestId,
  );
}

/**
 * 创建模板并在同一事务封印 v1。
 *
 * requestId 必须来自状态机的 mutation ticket；同意图网络重试复用，任何字段变化都会换新 ID。
 */
export async function createPromptTemplate({
  controlApiUrl,
  token,
  payload,
  signal,
  requestId,
}) {
  const stableRequestId = requireRequestId(requestId);
  return promptTemplateRequest(`${controlApiUrl}/prompt-templates`, {
    method: "POST",
    headers: jsonHeaders(token),
    body: JSON.stringify(payload),
    signal,
  }, stableRequestId);
}

/** 使用 metadata revision CAS 修改名称、描述或启停状态，不允许客户端提交 tenant 或 actor。 */
export async function updatePromptTemplate({
  controlApiUrl,
  token,
  promptTemplateId,
  payload,
  signal,
  requestId,
}) {
  const templateId = requireTemplateId(promptTemplateId);
  const stableRequestId = requireRequestId(requestId);
  return promptTemplateRequest(`${controlApiUrl}/prompt-templates/${encodeURIComponent(templateId)}`, {
    method: "PATCH",
    headers: jsonHeaders(token),
    body: JSON.stringify(payload),
    signal,
  }, stableRequestId);
}

/** 发布完整新定义；历史版本不可局部更新，expected_revision 只表示模板 metadata CAS。 */
export async function publishPromptTemplateVersion({
  controlApiUrl,
  token,
  promptTemplateId,
  payload,
  signal,
  requestId,
}) {
  const templateId = requireTemplateId(promptTemplateId);
  const stableRequestId = requireRequestId(requestId);
  return promptTemplateRequest(
    `${controlApiUrl}/prompt-templates/${encodeURIComponent(templateId)}/versions`,
    {
      method: "POST",
      headers: jsonHeaders(token),
      body: JSON.stringify(payload),
      signal,
    },
    stableRequestId,
  );
}

/** 为一次读或写生成不含 tenant、模板名称和正文的公开 request_id。 */
export function createPromptTemplateRequestId(kind = "request") {
  requestSequence += 1;
  const safeKind = /^[a-z]{2,16}$/.test(String(kind ?? "")) ? kind : "request";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 16)
    ?? Math.random().toString(36).slice(2, 18);
  return `console_prompt_template_${safeKind}_${Date.now()}_${requestSequence}_${random}`;
}

/** 统一执行 no-store 请求，并仅保留公开统一错误字段。 */
async function promptTemplateRequest(url, options, requestId) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      cache: "no-store",
      credentials: "omit",
      headers: {
        "X-Request-ID": requestId,
        ...(options?.headers ?? {}),
      },
    });
  } catch (error) {
    // 主动 abort 必须原样交给 owner 状态机，不能伪装成服务故障或生成可重试写错误。
    if (error?.name === "AbortError") throw error;
    // 原始网络异常可能携带 URL、代理或请求体，只返回固定公开错误。
    throw promptTemplateClientError(
      "prompt_template.unavailable",
      "Prompt 模板服务暂时不可用",
      503,
      requestId,
    );
  }
  const payload = await readJson(response);
  if (!response.ok) {
    // 错误只复制统一公开字段；代理正文、密文、Prompt 和未知 details 全部丢弃。
    throw new AuthApiError({
      status: response.status,
      code: safeErrorCode(payload.code) || "prompt_template.unavailable",
      message: safeText(payload.message, 240) || "Prompt 模板请求失败",
      requestId: safeRequestId(payload.request_id)
        || safeRequestId(response.headers.get("X-Request-ID"))
        || safeRequestId(requestId)
        || "-",
      traceId: safeRequestId(payload.trace_id)
        || safeRequestId(response.headers.get("X-Trace-ID"))
        || "-",
      suggestion: safeText(payload.suggestion, 240),
      diagnosis: null,
    });
  }
  return { state: "ok", ...payload };
}

/** 构造认证头；Token 只存在于当前 fetch 调用栈。 */
function authorizationHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

/** 构造严格 JSON 写请求头；后端仍会在解析业务正文前执行实时权限校验。 */
function jsonHeaders(token) {
  return { ...authorizationHeaders(token), "Content-Type": "application/json" };
}

/** 校验模板路径 ID，非法输入在出网前固定失败，避免路径穿透。 */
function requireTemplateId(value) {
  const normalized = String(value ?? "").trim();
  // 非规范模板 ID 在拼接 URL 前失败，避免路径穿透或跨资源歧义。
  if (!SAFE_TEMPLATE_ID.test(normalized)) {
    throw promptTemplateClientError("prompt_template.request_invalid", "模板标识无效", 422);
  }
  return normalized;
}

/** 校验版本路径 ID，客户端不能以 template ID 替代不可变 version ID。 */
function requireVersionId(value) {
  const normalized = String(value ?? "").trim();
  // version 路径必须使用 ptv_*；模板 ID 不能替代不可变版本 owner。
  if (!SAFE_VERSION_ID.test(normalized)) {
    throw promptTemplateClientError("prompt_template.request_invalid", "模板版本标识无效", 422);
  }
  return normalized;
}

/** 写接口必须显式持有稳定 request_id，缺失时失败关闭，禁止在 API client 内静默换号。 */
function requireRequestId(value) {
  const normalized = safeRequestId(value);
  // 写接口缺少状态机冻结的稳定 ID 时失败关闭，禁止 client 临时换号破坏幂等语义。
  if (!normalized) {
    throw promptTemplateClientError("prompt_template.request_invalid", "写请求标识无效", 422);
  }
  return normalized;
}

/** 容错读取 JSON；代理 HTML 和未知响应正文不会进入错误对象或页面状态。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 构造不含原始异常或业务输入的本地统一错误。 */
function promptTemplateClientError(code, message, status, requestId = "-") {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: safeRequestId(requestId) || "-",
    traceId: "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** 只允许稳定公开错误码进入页面状态。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 链路 ID 只接受有界安全字符，拒绝把响应正文或 Header 注入 DOM。 */
function safeRequestId(value) {
  const normalized = String(value ?? "").trim();
  return SAFE_REQUEST_ID.test(normalized) ? normalized : "";
}

/** 裁剪公开短文本并删除控制字符，未知扩展字段不会被复制。 */
function safeText(value, maximum) {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f-\u009f]/g, "").slice(0, maximum);
}
