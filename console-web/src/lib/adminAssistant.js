// 本模块定义 PAGE-107 的严格浏览器协议边界；请求、响应与来源链接都不得绕过后端授权事实。
import { AuthApiError } from "./auth.js";
import {
  authorizationFingerprint,
  hasPermission,
  permissionCodesFromAuthContext,
  permissionRevision,
  PERMISSIONS,
} from "./permissions.js";

/**
 * PAGE-107 允许展示的来源是后端 typed source 合同的前端镜像。
 *
 * href、导航 ID 和源权限必须作为一个不可拆分的三元组维护。回答、问题或任意服务端字段都不能扩展
 * 这个集合；即使后端响应通过 JSON 解析，只要三元组不完全一致，整份成功响应也会失败关闭。
 */
export const ADMIN_ASSISTANT_SOURCE_DEFINITIONS = Object.freeze({
  overview: freezeSource("overview", "/#overview", PERMISSIONS.DASHBOARD_READ, "运行概览"),
  request_logs: freezeSource(
    "request-logs",
    "/#request-logs",
    PERMISSIONS.REQUEST_LOG_READ,
    "调用日志",
  ),
  request_explanations: freezeSource(
    "request-explanations",
    "/#request-explanations",
    PERMISSIONS.REQUEST_EXPLANATION_READ,
    "请求解释",
  ),
  usage: freezeSource("usage", "/#usage", PERMISSIONS.BILLING_USAGE_READ, "用量账本"),
  finops_ledger: freezeSource(
    "finops-ledger",
    "/#finops-ledger",
    PERMISSIONS.BILLING_FINOPS_READ,
    "FinOps 账本",
  ),
  budgets: freezeSource("budgets", "/#budgets", PERMISSIONS.BILLING_BUDGET_READ, "预算"),
  cost_anomalies: freezeSource(
    "cost-anomalies",
    "/#cost-anomalies",
    PERMISSIONS.BILLING_ANOMALY_READ,
    "成本异常",
  ),
  model_prices: freezeSource(
    "model-prices",
    "/#model-prices",
    PERMISSIONS.MODEL_PRICE_READ,
    "模型价格",
  ),
  policies: freezeSource("policies", "/#policies", PERMISSIONS.POLICY_READ, "策略"),
  policy_snapshots: freezeSource(
    "policy-snapshots",
    "/#policy-snapshots",
    PERMISSIONS.POLICY_READ,
    "策略快照",
  ),
});

// 成功响应必须精确包含这些顶层字段，任何扩展字段都按协议漂移失败关闭。
const RESPONSE_KEYS = Object.freeze([
  "assistant_query_id",
  "assistant_audit_id",
  "query_type",
  "answer",
  "sources",
  "redactions",
  "request_id",
  "trace_id",
]);
// 回答、声明、建议和来源分别使用 exact-key 集合，防止隐藏字段夹带原始正文或自由 URL。
const ANSWER_KEYS = Object.freeze(["summary", "claims", "recommendations"]);
const CLAIM_KEYS = Object.freeze(["claim_id", "claim_type", "label", "value", "unit", "source_ids"]);
const RECOMMENDATION_KEYS = Object.freeze(["code", "message", "source_ids"]);
const SOURCE_KEYS = Object.freeze([
  "source_id",
  "source_type",
  "label",
  "href",
  "record_count",
  "window_started_at",
  "window_ended_at",
  "snapshot_at",
]);
// 错误信封和查询类型均为固定公开协议，未知值不会直接展示给用户。
const ERROR_KEYS = Object.freeze(["code", "message", "request_id", "trace_id", "suggestion", "diagnosis"]);
const QUERY_TYPES = new Set([
  "usage_statistics",
  "error_explanation",
  "cost_advice",
  "policy_explanation",
]);
// claim 类型与每类查询允许引用的来源建立一一边界，浏览器不接受跨域来源拼接。
const CLAIM_TYPES = new Set(["integer", "decimal", "percentage", "text"]);
const QUERY_SOURCE_TYPES = Object.freeze({
  usage_statistics: new Set(["overview", "usage"]),
  error_explanation: new Set(["request_logs"]),
  cost_advice: new Set([
    "overview",
    "usage",
    "finops_ledger",
    "budgets",
    "cost_anomalies",
    "model_prices",
  ]),
  policy_explanation: new Set(["policies", "policy_snapshots", "request_explanations"]),
});
// 建议码只能映射到固定公开文案，服务端不能借建议字段回传自由文本。
const RECOMMENDATION_MESSAGES = Object.freeze({
  high_failure_rate: "失败率偏高，建议检查固定错误分类和对应调用日志。",
  high_estimated_cost_share: "估算成本占比较高，建议先补齐正式成本证据再调整预算。",
  high_model_cost_concentration: "成本集中于少数模型，建议结合质量与价格证据复核路由。",
  priority_collision_review: "存在同优先级策略候选组，建议进行人工复核。",
  insufficient_evidence: "当前证据不足以给出优化建议。",
});
// 脱敏枚举、链路标识和数值格式共同限制可进入页面内存的服务端数据形状。
const REDACTION_CODES = new Set([
  "content_omitted",
  "credentials_omitted",
  "raw_provider_error_omitted",
]);
const SAFE_CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const SAFE_CLAIM_ID = /^[a-z][a-z0-9_]{0,63}$/;
const SAFE_UNIT = /^[A-Za-z][A-Za-z0-9._/%-]{0,31}$/;
const CANONICAL_DECIMAL = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

/**
 * 发起一次独立的只读管理员助手查询。
 *
 * 请求 body 精确只有 question。tenant、角色、权限、来源 URL、Idempotency-Key 和上一轮回答都不能由
 * 浏览器提交；每个 HTTP attempt 由后端独立审计，客户端只在当前 owner 内阻止重复点击，不自动重试。
 */
export async function queryAdminAssistant({ controlApiUrl, token, question, signal }) {
  const normalizedQuestion = validateQuestion(question);
  const body = JSON.stringify({ question: normalizedQuestion });
  // 客户端先限制编码后请求体大小，后端仍会独立执行同类约束与权限校验。
  if (utf8Length(body) > 16 * 1024) {
    throw clientError("admin_assistant.request_invalid", "问题长度超出限制", 422);
  }

  let response;
  try {
    response = await fetch(`${controlApiUrl}/admin-assistant/query`, {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body,
      signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch (error) {
    // Abort 是 owner 主动销毁的一部分，必须交给状态机识别；其他异常对象可能携带 URL 或代理正文。
    if (error?.name === "AbortError") throw error;
    throw clientError("admin_assistant.network_unavailable", "管理助手响应状态未知", 0);
  }

  const payload = await readJson(response);
  // 成功与错误响应共享同一私有 Header 门禁；缺失、代理改写或链路混接都先失败关闭，
  // 不能因为正文恰好是合法错误信封就绕过缓存与 request/trace 关联校验。
  verifyPrivateResponseHeaders(response, payload);
  // 非成功响应只经过固定错误信封折叠，不把原始响应正文抛给页面。
  if (!response.ok) throw responseError(response, payload);
  const normalized = normalizeAdminAssistantResponse(payload);
  // 成功 HTTP 但协议不完整时也失败关闭，避免渲染未验证的来源和回答。
  if (!normalized) {
    throw clientError(
      "admin_assistant.response_invalid",
      "管理助手响应合同无效",
      502,
      safeChain(payload?.request_id),
      safeChain(payload?.trace_id),
    );
  }
  verifyResponseChain(response, normalized);
  return normalized;
}

/**
 * 对成功响应执行 exact DTO、typed value 和 claim/source 图校验。
 * 返回值只属于调用方当前页面内存；本函数不写日志、Storage、URL、缓存或错误诊断正文。
 */
export function normalizeAdminAssistantResponse(payload) {
  if (
    !exactObject(payload, RESPONSE_KEYS)
    || !/^asq_[0-9a-f]{32}$/.test(String(payload.assistant_query_id ?? ""))
    || !/^asa_[0-9a-f]{32}$/.test(String(payload.assistant_audit_id ?? ""))
    || !QUERY_TYPES.has(payload.query_type)
    || !safeChain(payload.request_id)
    || !safeChain(payload.trace_id)
    || !exactObject(payload.answer, ANSWER_KEYS)
    || !safeText(payload.answer.summary, 4000)
    || !Array.isArray(payload.answer.claims)
    || payload.answer.claims.length < 1
    || payload.answer.claims.length > 64
    || !Array.isArray(payload.answer.recommendations)
    || payload.answer.recommendations.length > 16
    || !Array.isArray(payload.sources)
    || payload.sources.length < 1
    || payload.sources.length > Object.keys(ADMIN_ASSISTANT_SOURCE_DEFINITIONS).length
  ) return null;

  const sources = payload.sources.map(normalizeSource);
  if (sources.some((source) => !source)) return null;
  if (
    payload.answer.summary !== `基于 ${sources.length} 个可验证来源生成。`
    || sources.some((source) => !QUERY_SOURCE_TYPES[payload.query_type].has(source.source_type))
  ) return null;
  const sourceIds = sources.map((source) => source.source_id);
  if (new Set(sourceIds).size !== sourceIds.length) return null;
  const sourceIdSet = new Set(sourceIds);

  const claims = payload.answer.claims.map((claim) => normalizeClaim(claim, sourceIdSet));
  if (claims.some((claim) => !claim)) return null;
  if (new Set(claims.map((claim) => claim.claim_id)).size !== claims.length) return null;

  const recommendations = payload.answer.recommendations.map(
    (recommendation) => normalizeRecommendation(recommendation, sourceIdSet),
  );
  if (recommendations.some((recommendation) => !recommendation)) return null;
  if (new Set(recommendations.map((item) => item.code)).size !== recommendations.length) return null;

  // 每个公开 source 都必须真正支撑 claim 或 recommendation，不能夹带未引用的资源存在性。
  const referencedSourceIds = new Set([
    ...claims.flatMap((claim) => claim.source_ids),
    ...recommendations.flatMap((recommendation) => recommendation.source_ids),
  ]);
  if (sourceIds.some((sourceId) => !referencedSourceIds.has(sourceId))) return null;

  const redactions = normalizeRedactions(payload.redactions, payload.query_type);
  if (!redactions) return null;
  return deepFreeze({
    assistant_query_id: payload.assistant_query_id,
    assistant_audit_id: payload.assistant_audit_id,
    query_type: payload.query_type,
    answer: {
      summary: payload.answer.summary,
      claims,
      recommendations,
    },
    sources,
    redactions,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/**
 * 将一个已校验 source 与“当前”认证和后端导航再次求交。
 *
 * source href 本身不是授权：目标页面权限、完整权限集合、revision、fingerprint 或导航合同任一漂移时，
 * renderer 只能显示静态标签；点击处理器还会再次调用本函数，防止迟到 answer 恢复已撤销入口。
 */
export function adminAssistantSourceAccess(source, authContext, consoleShellState) {
  const definition = ADMIN_ASSISTANT_SOURCE_DEFINITIONS[source?.source_type];
  const navigation = consoleShellState?.navigation;
  const authCodes = permissionCodesFromAuthContext(authContext);
  const navigationCodes = normalizePermissionCodes(navigation?.permission_codes);
  const revision = permissionRevision(authContext);
  const fingerprint = authorizationFingerprint(authContext);
  const item = Array.isArray(navigation?.navigation_items)
    ? navigation.navigation_items.find((candidate) => candidate?.id === definition?.navigationId)
    : null;
  const required = normalizePermissionCodes(item?.required_permissions);
  const allowed = Boolean(
    definition
    && source?.source_id === `src_${source.source_type}`
    && source?.href === definition.href
    && consoleShellState?.state === "ok"
    && hasPermission(authContext, definition.permission)
    && navigationCodes
    && JSON.stringify(navigationCodes) === JSON.stringify(authCodes)
    && Number(navigation?.permission_revision) === revision
    && String(navigation?.authorization_fingerprint ?? "") === fingerprint
    && item?.target_section === definition.navigationId
    && item?.visible === true
    && item?.disabled === false
    && JSON.stringify(required) === JSON.stringify([definition.permission])
  );
  return Object.freeze({
    allowed,
    href: definition?.href ?? "",
    navigationId: definition?.navigationId ?? "",
    permission: definition?.permission ?? "",
  });
}

/** 校验单个 claim 的精确字段、类型和值，并限定其引用到当前来源集合。 */
function normalizeClaim(value, sourceIds) {
  if (
    !exactObject(value, CLAIM_KEYS)
    || !SAFE_CLAIM_ID.test(String(value.claim_id ?? ""))
    || !CLAIM_TYPES.has(value.claim_type)
    || !safeText(value.label, 80)
    || !(value.unit === null || (typeof value.unit === "string" && SAFE_UNIT.test(value.unit)))
    || !validTypedValue(value.claim_type, value.value)
    || !validSourceIds(value.source_ids, sourceIds)
  ) return null;
  return Object.freeze({
    claim_id: value.claim_id,
    claim_type: value.claim_type,
    label: value.label,
    value: value.value,
    unit: value.unit,
    source_ids: Object.freeze([...value.source_ids]),
  });
}

/** 只接纳固定建议码、固定文案和已存在来源引用，拒绝自由建议正文。 */
function normalizeRecommendation(value, sourceIds) {
  const expectedMessage = RECOMMENDATION_MESSAGES[value?.code];
  if (
    !exactObject(value, RECOMMENDATION_KEYS)
    || !expectedMessage
    || value.message !== expectedMessage
    || !validSourceIds(value.source_ids, sourceIds)
  ) return null;
  return Object.freeze({
    code: value.code,
    message: value.message,
    source_ids: Object.freeze([...value.source_ids]),
  });
}

/** 将来源限制为冻结三元组及最长三十天窗口，不信任服务端提供的任意链接。 */
function normalizeSource(value) {
  if (!exactObject(value, SOURCE_KEYS)) return null;
  const definition = ADMIN_ASSISTANT_SOURCE_DEFINITIONS[value.source_type];
  if (
    !definition
    || value.source_id !== `src_${value.source_type}`
    || value.href !== definition.href
    || value.label !== definition.label
    || !Number.isSafeInteger(value.record_count)
    || value.record_count < 0
    || !validUtcTimestamp(value.window_started_at)
    || !validUtcTimestamp(value.window_ended_at)
    || !validUtcTimestamp(value.snapshot_at)
    || Date.parse(value.window_started_at) >= Date.parse(value.window_ended_at)
    || Date.parse(value.window_ended_at) - Date.parse(value.window_started_at) > 30 * 86400 * 1000
    || Date.parse(value.window_ended_at) > Date.parse(value.snapshot_at)
  ) return null;
  return Object.freeze({
    source_id: value.source_id,
    source_type: value.source_type,
    label: value.label,
    href: value.href,
    record_count: value.record_count,
    window_started_at: value.window_started_at,
    window_ended_at: value.window_ended_at,
    snapshot_at: value.snapshot_at,
  });
}

/** 要求脱敏枚举及顺序与查询类型精确匹配，错误解释必须额外省略原始 Provider 错误。 */
function normalizeRedactions(value, queryType) {
  const expected = queryType === "error_explanation"
    ? ["content_omitted", "credentials_omitted", "raw_provider_error_omitted"]
    : ["content_omitted", "credentials_omitted"];
  if (
    !Array.isArray(value)
    || value.some((code) => !REDACTION_CODES.has(code))
    || JSON.stringify(value) !== JSON.stringify(expected)
  ) return null;
  return Object.freeze([...value]);
}

/** 校验 claim 的整数或规范十进制定点表示，禁止浮点漂移和布尔值冒充数字。 */
function validTypedValue(claimType, value) {
  if (claimType === "integer") {
    return Number.isSafeInteger(value) && typeof value !== "boolean" && value >= 0;
  }
  if (claimType === "decimal" || claimType === "percentage") {
    if (typeof value !== "string" || value.length > 64 || !CANONICAL_DECIMAL.test(value)) {
      return false;
    }
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 && (claimType !== "percentage" || parsed <= 100);
  }
  return claimType === "text" && Boolean(safeText(value, 512));
}

/** 要求来源引用非空、无重复且完全属于本响应已经校验的来源集合。 */
function validSourceIds(value, available) {
  return Array.isArray(value)
    && value.length > 0
    && value.length <= Object.keys(ADMIN_ASSISTANT_SOURCE_DEFINITIONS).length
    && new Set(value).size === value.length
    && value.every((sourceId) => available.has(sourceId));
}

/** 在发请求前校验问题的字符、长度和编码大小，不记录或改写用户正文。 */
function validateQuestion(value) {
  // 非字符串、空问题或包含不安全字符时统一使用固定错误，不回显原问题。
  if (typeof value !== "string" || !value.trim() || !safeText(value, 2000) || utf8Length(value) > 8 * 1024) {
    throw clientError("admin_assistant.request_invalid", "问题格式不合法", 422);
  }
  return value;
}

/** 拒绝控制字符、双向覆盖、孤立代理项和 Unicode 非字符，保留合法文本原值。 */
function safeText(value, maxCodePoints) {
  if (typeof value !== "string" || [...value].length > maxCodePoints) return "";
  for (const symbol of value) {
    const codePoint = symbol.codePointAt(0);
    if (
      codePoint === 0
      || (codePoint < 0x20 && codePoint !== 0x09 && codePoint !== 0x0a)
      || (codePoint >= 0x7f && codePoint <= 0x9f)
      || (codePoint >= 0xd800 && codePoint <= 0xdfff)
      || (codePoint >= 0x202a && codePoint <= 0x202e)
      || (codePoint >= 0x2066 && codePoint <= 0x2069)
      || (codePoint >= 0xfdd0 && codePoint <= 0xfdef)
      || (codePoint & 0xffff) === 0xfffe
      || (codePoint & 0xffff) === 0xffff
    ) return "";
  }
  return value;
}

/** 只接受精确到秒的规范 UTC 时间戳，拒绝浏览器可宽松解析的其他表示。 */
function validUtcTimestamp(value) {
  if (typeof value !== "string" || !UTC_TIMESTAMP.test(value) || Number.isNaN(Date.parse(value))) {
    return false;
  }
  return new Date(value).toISOString().replace(".000Z", "Z") === value;
}

/** 将 request_id 与 trace_id 限定为短且可公开的字符集合。 */
function safeChain(value) {
  const normalized = String(value ?? "");
  return SAFE_CHAIN_ID.test(normalized) ? normalized : "";
}

/** 规范化权限快照并拒绝空集、重复项和非法代码，供导航求交使用。 */
function normalizePermissionCodes(value) {
  if (!Array.isArray(value) || value.length === 0) return null;
  const normalized = value.map((code) => String(code ?? ""));
  if (
    normalized.some((code) => !/^[a-z][a-z0-9_.]{1,127}$/.test(code))
    || new Set(normalized).size !== normalized.length
  ) return null;
  return normalized.sort();
}

/** 验证普通对象的键集合与合同完全一致，不允许额外字段。 */
function exactObject(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/** 强制所有状态码携带冻结私有 Header；Vary 只兼容 CORS 追加的 Origin。 */
function verifyPrivateResponseHeaders(response, payload) {
  const cacheControl = String(response.headers?.get?.("Cache-Control") ?? "").trim().toLowerCase();
  const pragma = String(response.headers?.get?.("Pragma") ?? "").trim().toLowerCase();
  const varyTokens = String(response.headers?.get?.("Vary") ?? "")
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);
  const uniqueVaryTokens = new Set(varyTokens);
  // 私有响应必须禁止缓存且只允许授权与跨域协商维度，避免租户回答被共享缓存复用。
  if (
    cacheControl !== "no-store, private"
    || pragma !== "no-cache"
    || varyTokens.length !== uniqueVaryTokens.size
    || !uniqueVaryTokens.has("authorization")
    || [...uniqueVaryTokens].some((item) => !["authorization", "origin"].includes(item))
  ) {
    // 任一缓存或 Vary 约束漂移都按统一隐私边界错误收敛，不暴露响应头原值。
    throw clientError("admin_assistant.response_invalid", "管理助手响应隐私边界无效", 502);
  }
  verifyResponseChain(response, payload);
}

/** 响应头链路 ID 必须存在并与 JSON 信封一致，避免代理层替换或混接响应。 */
function verifyResponseChain(response, payload) {
  const headerRequestId = safeChain(response.headers?.get?.("X-Request-ID"));
  const headerTraceId = safeChain(response.headers?.get?.("X-Trace-ID"));
  if (
    !headerRequestId
    || !headerTraceId
    || headerRequestId !== safeChain(payload?.request_id)
    || headerTraceId !== safeChain(payload?.trace_id)
  ) {
    // 任一链路标识缺失或冲突都使整个响应失效，页面不能选择性相信 Header 或正文。
    throw clientError("admin_assistant.response_invalid", "管理助手响应链路无效", 502);
  }
}

/** 把任意失败响应收敛为公开 AuthApiError，不保留原始 Provider 或代理正文。 */
function responseError(response, payload) {
  const payloadIsEnvelope = exactObject(payload, ERROR_KEYS);
  const code = payloadIsEnvelope && /^(?:auth|admin_assistant)\.[a-z0-9_.]{1,120}$/.test(payload.code)
    ? payload.code
    : "admin_assistant.source_unavailable";
  const message = payloadIsEnvelope && safeText(payload.message, 512)
    ? payload.message
    : "管理助手请求失败";
  // 仅 exact 错误信封能够提供公开字段，其余响应统一回退到固定不可用错误。
  return new AuthApiError({
    status: Number(response?.status) || 503,
    code,
    message,
    requestId: payloadIsEnvelope ? (safeChain(payload.request_id) || "-") : "-",
    traceId: payloadIsEnvelope ? (safeChain(payload.trace_id) || "-") : "-",
    suggestion: payloadIsEnvelope && safeText(payload.suggestion, 512) ? payload.suggestion : "",
    diagnosis: null,
  });
}

/** 尝试读取 JSON 信封；解析失败仅返回 null，绝不复制原始响应文本。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** 构造浏览器本地产生的固定错误，诊断字段保持为空以免混入敏感对象。 */
function clientError(code, message, status, requestId = "", traceId = "") {
  // 本地错误只携带调用方提供的固定文案和已经清洗的链路标识。
  return new AuthApiError({
    status,
    code,
    message,
    requestId: requestId || "-",
    traceId: traceId || "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** 使用标准 TextEncoder 计算真实 UTF-8 字节数，不以 UTF-16 长度替代后端限制。 */
function utf8Length(value) {
  // 缺少可信编码器时失败关闭，避免不同浏览器对请求大小产生不一致判断。
  if (typeof globalThis.TextEncoder !== "function") {
    throw clientError("admin_assistant.request_invalid", "当前浏览器不支持安全文本校验", 422);
  }
  return new TextEncoder().encode(String(value)).byteLength;
}

/** 冻结来源的导航、href、权限与标签四元组，防止运行时被响应数据篡改。 */
function freezeSource(navigationId, href, permission, label) {
  // 来源权限属于静态客户端协议镜像，实际放行仍必须与最新后端导航求交。
  return Object.freeze({ navigationId, href, permission, label });
}

/** 递归冻结已经校验的响应 DTO，防止页面生命周期内被监听器修改。 */
function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.values(value).forEach(deepFreeze);
  return Object.freeze(value);
}
