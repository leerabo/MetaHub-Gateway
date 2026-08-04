import { AuthApiError } from "./auth.js";

/**
 * PAGE-095 行业/场景策略模板库的严格 Control API 客户端。
 *
 * 浏览器只提交模板路径、服务端预览返回的 CAS 事实和固定绑定。tenant、actor、策略表达式、
 * 动作、优先级、生成资源 ID 与排序结论均由后端恢复或派生，不能通过通用对象透传。
 */

// 以下协议正则只承担出网前的格式收敛，租户归属、权限和资源存在性仍由后端复核。
const SAFE_TEMPLATE_ID = /^ptpl_[A-Za-z0-9_-]{1,120}$/;
const SAFE_RESOURCE_ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const SAFE_HASH = /^[0-9a-f]{64}$/;
const SAFE_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const SAFE_HEAD_ETAG = /^"[1-9][0-9]{0,9}"$/;
const SAFE_CANDIDATE_ETAG = /^"[0-9a-f]{64}"$/;
const SAFE_REQUEST_ID = /^[A-Za-z0-9_.:-]{1,128}$/;
let requestSequence = 0;

/** 读取固定五模板目录；提供 templateId 时同时请求精确影响预览。 */
export async function listPolicyTemplates({
  controlApiUrl,
  token,
  templateId = "",
  bindings = null,
  signal,
  requestId = createPolicyTemplateRequestId("list"),
}) {
  const normalizedTemplateId = String(templateId ?? "").trim();
  const url = new URL(`${controlApiUrl}/policy-templates`);
  if (normalizedTemplateId) {
    requireTemplateId(normalizedTemplateId);
    const normalizedBindings = normalizeBindings(bindings);
    url.searchParams.set("template_id", normalizedTemplateId);
    // 查询字段按后端冻结顺序逐个写入，禁止 tenant、actor 或任意未知键进入 URL。
    url.searchParams.set("project_id", normalizedBindings.project_id);
    if (normalizedBindings.model_alias_id) {
      url.searchParams.set("model_alias_id", normalizedBindings.model_alias_id);
    }
    if (normalizedBindings.threshold_cny) {
      url.searchParams.set("threshold_cny", normalizedBindings.threshold_cny);
    }
  } else if (bindings !== null && bindings !== undefined) {
    // 未指定模板时携带绑定没有明确语义，必须在出网前拒绝以免后端误用查询事实。
    throw clientError("policy_template.query_invalid", "策略模板查询条件无效", 422, requestId);
  }
  return policyTemplateRequest(url.toString(), {
    headers: authorizationHeaders(token),
    signal,
  }, requestId);
}

/**
 * 应用一次精确预览。
 *
 * 稳定 requestId 与 preview nonce 由页面状态机保留，网络结果未知时只能原样重放；任何表单
 * 字段变化都必须重新预览并换号。前端校验只防止错误出网，后端仍在写锁内复核权限和事实。
 */
export async function applyPolicyTemplate({
  controlApiUrl,
  token,
  policyTemplateId,
  templateVersion,
  previewNonce,
  previewAsOf,
  impactHash,
  bindings,
  reason,
  headEtag,
  candidateEtag,
  signal,
  requestId,
}) {
  const templateId = requireTemplateId(policyTemplateId);
  const stableRequestId = requireRequestId(requestId);
  const normalizedReason = String(reason ?? "").trim();
  if (
    !Number.isSafeInteger(templateVersion)
    || templateVersion < 1
    || templateVersion > 2_147_483_647
    || !SAFE_HASH.test(String(previewNonce ?? ""))
    || !SAFE_TIMESTAMP.test(String(previewAsOf ?? ""))
    || !SAFE_HASH.test(String(impactHash ?? ""))
    || normalizedReason.length < 1
    || normalizedReason.length > 256
    || containsControlCharacter(normalizedReason)
    || !SAFE_HEAD_ETAG.test(String(headEtag ?? ""))
    || !SAFE_CANDIDATE_ETAG.test(String(candidateEtag ?? ""))
  ) {
    // 预览事实、原因或双 CAS 任一不规范都禁止写出，避免前端拼装出可绕过确认的请求。
    throw clientError("policy_template.payload_invalid", "策略模板应用参数无效", 422, stableRequestId);
  }
  const payload = {
    template_version: templateVersion,
    preview_nonce: previewNonce,
    preview_as_of: previewAsOf,
    impact_hash: impactHash,
    bindings: normalizeBindings(bindings),
    reason: normalizedReason,
  };
  return policyTemplateRequest(
    `${controlApiUrl}/policy-templates/${encodeURIComponent(templateId)}/apply`,
    {
      method: "POST",
      headers: {
        ...authorizationHeaders(token),
        "Content-Type": "application/json",
        "If-Match": headEtag,
        "If-Policy-Candidate-Match": candidateEtag,
      },
      body: JSON.stringify(payload),
      signal,
    },
    stableRequestId,
  );
}

/** 为读写 lane 生成不含 tenant、用户、模板绑定或正文的公开 request_id。 */
export function createPolicyTemplateRequestId(kind = "request") {
  requestSequence += 1;
  const safeKind = /^[a-z]{2,16}$/.test(String(kind ?? "")) ? kind : "request";
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 16)
    ?? Math.random().toString(36).slice(2, 18);
  return `console_policy_template_${safeKind}_${Date.now()}_${requestSequence}_${random}`;
}

/** 只接受协议声明的三个绑定键，并拒绝空 Project 与非规范资源 ID。 */
function normalizeBindings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw clientError("policy_template.binding_invalid", "策略模板绑定无效", 422);
  }
  const keys = Object.keys(value);
  // 绑定白名单禁止 tenant、actor、动作和策略表达式借由通用对象进入后端。
  if (keys.some((key) => !["project_id", "model_alias_id", "threshold_cny"].includes(key))) {
    throw clientError("policy_template.binding_invalid", "策略模板绑定无效", 422);
  }
  const projectId = String(value.project_id ?? "").trim();
  const modelAliasId = String(value.model_alias_id ?? "").trim();
  const thresholdCny = String(value.threshold_cny ?? "").trim();
  // 资源标识和金额必须先规范化，避免路径控制字符或非确定小数污染预览哈希。
  if (
    !SAFE_RESOURCE_ID.test(projectId)
    || (modelAliasId && !SAFE_RESOURCE_ID.test(modelAliasId))
    || (thresholdCny && !/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?$/.test(thresholdCny))
  ) {
    // 非规范绑定不得进入请求体，后端仍会在当前租户和写锁内再次验证资源归属。
    throw clientError("policy_template.binding_invalid", "策略模板绑定无效", 422);
  }
  return {
    project_id: projectId,
    ...(modelAliasId ? { model_alias_id: modelAliasId } : {}),
    ...(thresholdCny ? { threshold_cny: thresholdCny } : {}),
  };
}

/** 统一执行 no-store 请求，只保留公开错误字段和两条预览 CAS Header。 */
async function policyTemplateRequest(url, options, requestId) {
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
    // Abort 由页面 owner 消化；其他网络异常不能把 URL、Token 或请求体复制到错误对象。
    if (error?.name === "AbortError") throw error;
    throw clientError(
      "policy_template.unavailable",
      "策略模板服务暂时不可用",
      503,
      requestId,
    );
  }
  const payload = await readJson(response);
  if (!response.ok) {
    // 失败响应只接纳固定公开字段，原始响应、Header 和请求正文均不得挂到异常对象。
    throw new AuthApiError({
      status: response.status,
      code: safeErrorCode(payload.code) || "policy_template.unavailable",
      message: safeText(payload.message, 240) || "策略模板请求失败",
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
  return {
    state: "ok",
    ...payload,
    headEtag: safeHeader(response.headers.get("ETag"), SAFE_HEAD_ETAG),
    candidateEtag: safeHeader(
      response.headers.get("X-Policy-Candidate-ETag"),
      SAFE_CANDIDATE_ETAG,
    ),
  };
}

/** 构造认证头；Session Token 只在当前 fetch 调用栈使用。 */
function authorizationHeaders(token) {
  return { Authorization: `Bearer ${token}` };
}

/** 校验模板路径，非法输入在出网前失败，避免路径穿透。 */
function requireTemplateId(value) {
  const normalized = String(value ?? "").trim();
  if (!SAFE_TEMPLATE_ID.test(normalized)) {
    // 模板 ID 会进入 URL 路径，格式不合法时本地拒绝以阻断路径穿透和歧义编码。
    throw clientError("policy_template.payload_invalid", "策略模板标识无效", 422);
  }
  return normalized;
}

/** 写接口必须显式持有状态机冻结的 request_id，禁止客户端临时换号。 */
function requireRequestId(value) {
  const normalized = safeRequestId(value);
  if (!normalized) {
    // 写请求必须复用状态机冻结的 request_id，临时换号会破坏未知结果重放的幂等语义。
    throw clientError("policy_template.payload_invalid", "策略模板写请求标识无效", 422);
  }
  return normalized;
}

/** 容错读取 JSON；代理 HTML 或空正文不会进入页面状态。 */
async function readJson(response) {
  try {
    const value = await response.json();
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch {
    return {};
  }
}

/** 创建浏览器本地固定错误，不保留原始参数或异常对象。 */
function clientError(code, message, status, requestId = "-") {
  return new AuthApiError({
    status,
    code,
    message,
    requestId: safeRequestId(requestId) || "-",
    traceId: "-",
    suggestion: "请刷新模板目录并重新预览",
    diagnosis: null,
  });
}

/** 过滤后端统一错误码，未知形态按服务不可用处理。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "");
  return /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/.test(normalized) ? normalized : "";
}

/** 过滤公开短文本，控制字符和代理字符不能进入 DOM 或日志。 */
function safeText(value, maximum) {
  const normalized = String(value ?? "");
  return normalized.length <= maximum && !containsControlCharacter(normalized) ? normalized : "";
}

/** 只接受公开 request/trace ID 的有界字符集。 */
function safeRequestId(value) {
  const normalized = String(value ?? "");
  return SAFE_REQUEST_ID.test(normalized) ? normalized : "";
}

/** Header 必须精确符合冻结的强 ETag 形态。 */
function safeHeader(value, pattern) {
  const normalized = String(value ?? "");
  return pattern.test(normalized) ? normalized : null;
}

/** 识别 C0/C1 控制字符和 Unicode 代理字符。 */
function containsControlCharacter(value) {
  return /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value);
}
