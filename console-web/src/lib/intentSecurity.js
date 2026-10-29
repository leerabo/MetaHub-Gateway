/**
 * 模块职责：调用轻量敏感意图分类接口，并把响应收敛为不含正文的固定安全摘要。
 * 安全边界：分类正文只存在于当前调用栈和 fetch 请求体；不得进入 URL、链路 Header、缓存或错误对象。
 */

import { AuthApiError } from "./auth.js";

// 响应字段、枚举与标识格式按 PRD-069 固定，未知结构失败关闭，避免服务端漂移把正文夹带进页面状态。
const RESPONSE_KEYS = Object.freeze(["intent_risk_result", "request_id", "trace_id"]);
const RESULT_KEYS = Object.freeze([
  "intent_risk_result_id",
  "status",
  "classification_mode",
  "risk_type",
  "risk_score",
  "risk_score_scale",
  "action",
  "reason_code",
  "detector_version",
  "ruleset_version",
  "input_code_points",
  "input_truncated",
  "duration_ms",
  "completed_at",
]);
// 成功响应的状态、风险和动作枚举共同用于验证手工分类状态矩阵。
const RESULT_STATUSES = new Set(["clean", "detected"]);
const RISK_TYPES = new Set([
  "none",
  "credential_exfiltration",
  "system_prompt_extraction",
  "policy_bypass",
  "jailbreak",
]);
// 手工分类不会返回 Gateway 的 observe/block，出现时必须拒绝整包。
const RESULT_ACTIONS = new Set(["allow", "review"]);
// 对象、链路和枚举格式限制阻止自由正文借响应字段进入页面状态。
const RESULT_ID_PATTERN = /^irr_[0-9a-f]{32}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,160}$/;
const SAFE_ENUM_PATTERN = /^[a-z][a-z0-9_.-]{0,127}$/;
const MAX_INPUT_CODE_POINTS = 20_000;
// 公开错误码映射固定中文文案，客户端永不采用可能回显输入的服务端自由 message。
const ERROR_MESSAGES = Object.freeze({
  "auth.unauthorized": "登录状态已失效",
  "auth.permission_denied": "当前账号没有分类权限",
  "intent_risk.media_type_unsupported": "分类请求格式不受支持",
  "intent_risk.payload_too_large": "分类文本超过请求上限",
  "intent_risk.payload_invalid": "分类请求结构不合法",
  "intent_risk.text_invalid": "分类文本不合法",
  "intent_risk.classifier_unavailable": "本地分类器暂不可用",
  "intent_risk.result_store_unavailable": "分类结果暂无法保存",
  "intent_risk.result_integrity_failed": "分类结果完整性校验失败",
});

/**
 * 对一次短生命周期文本执行本地规则分类。
 *
 * 参数只允许控制面地址、Session Token、正文和 AbortSignal；函数不会生成或发送客户端
 * request_id/trace_id，服务端响应中的内部链路标识才是本次操作的唯一可信身份。
 */
export async function classifyIntent({ controlApiUrl, token, text, signal }) {
  let sensitiveText = typeof text === "string" ? text : "";
  let serializedBody = "";
  // 本地边界只减少无意义请求；真实格式、长度、权限与租户仍由后端逐请求校验。
  if (!sensitiveText.trim() || inputCodePointLength(sensitiveText) > MAX_INPUT_CODE_POINTS) {
    sensitiveText = "";
    throw intentClientError(422, "intent_risk.text_invalid", "分类文本不合法");
  }
  try {
    serializedBody = JSON.stringify({ text: sensitiveText });
    const response = await fetch(`${controlApiUrl}/security/classify-intent`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: serializedBody,
      signal,
      cache: "no-store",
    });
    const payload = await readJson(response);
    if (!response.ok) {
      // 错误只保留统一公开字段；代理正文、输入、请求体和任意响应扩展都不得进入异常对象。
      const code = safeErrorCode(payload.code) || "intent_risk.unavailable";
      throw new AuthApiError({
        status: response.status,
        code,
        message: ERROR_MESSAGES[code] || "语义风险分类请求未完成",
        requestId: safeId(payload.request_id) || "-",
        traceId: safeId(payload.trace_id) || "-",
        suggestion: "",
        diagnosis: null,
      });
    }
    const normalized = normalizeClassificationResponse(payload);
    if (!normalized) {
      // 成功响应合同异常时失败关闭；不把原始 payload 或未知字段带入页面和错误详情。
      throw intentClientError(
        502,
        "intent_risk.response_invalid",
        "语义风险分类响应不完整",
        safeId(payload.request_id) || "-",
        safeId(payload.trace_id) || "-",
      );
    }
    return normalized;
  } finally {
    // JavaScript 字符串不可原位擦除；覆盖局部引用可避免模块缓存、错误对象和后续回调继续持有正文。
    serializedBody = "";
    sensitiveText = "";
    text = "";
  }
}

/**
 * 按 Unicode code point 计算文本长度。
 * HTML/JavaScript 的原生 length 使用 UTF-16 code unit；这里显式迭代才能与后端 20,000 code point 合同一致。
 */
function inputCodePointLength(value) {
  let count = 0;
  for (const _character of value) {
    count += 1;
    // 超过后端上限即可提前结束，避免为明显超限的浏览器输入继续扫描。
    if (count > MAX_INPUT_CODE_POINTS) return count;
  }
  return count;
}

/**
 * 校验并投影成功响应。
 * 只复制 PAGE-096 允许展示的字段，任何额外字段、非法枚举或状态矩阵都拒绝整包。
 */
export function normalizeClassificationResponse(payload) {
  if (!hasExactKeys(payload, RESPONSE_KEYS)) return null;
  const result = payload.intent_risk_result;
  if (!hasExactKeys(result, RESULT_KEYS)) return null;
  const completedAt = safeIsoDateTime(result.completed_at);
  const status = String(result.status ?? "");
  const riskType = String(result.risk_type ?? "");
  const action = String(result.action ?? "");
  const riskScore = result.risk_score;
  const inputCodePoints = result.input_code_points;
  const durationMs = result.duration_ms;
  const validMatrix = status === "clean"
    ? riskType === "none" && riskScore === 0 && action === "allow"
    : riskType !== "none" && riskScore > 0 && action === "review";
  if (
    !RESULT_ID_PATTERN.test(String(result.intent_risk_result_id ?? ""))
    || !RESULT_STATUSES.has(status)
    || result.classification_mode !== "manual_sync"
    || !RISK_TYPES.has(riskType)
    || !Number.isSafeInteger(riskScore)
    || riskScore < 0
    || riskScore > 10_000
    || result.risk_score_scale !== 10_000
    || !RESULT_ACTIONS.has(action)
    || !validMatrix
    || !safeEnum(result.reason_code)
    || !safeEnum(result.detector_version)
    || !safeEnum(result.ruleset_version)
    || !Number.isSafeInteger(inputCodePoints)
    || inputCodePoints < 1
    || inputCodePoints > MAX_INPUT_CODE_POINTS
    || result.input_truncated !== false
    || !Number.isSafeInteger(durationMs)
    || durationMs < 0
    || durationMs > 86_400_000
    || !completedAt
    || !safeId(payload.request_id)
    || !safeId(payload.trace_id)
  ) {
    // 任一矩阵字段异常都拒绝整体，不能把“检测到”误显示为“已阻断”或接受可注入自由文本。
    return null;
  }
  return Object.freeze({
    request_id: payload.request_id,
    trace_id: payload.trace_id,
    intent_risk_result: Object.freeze({
      intent_risk_result_id: result.intent_risk_result_id,
      status,
      classification_mode: "manual_sync",
      risk_type: riskType,
      risk_score: riskScore,
      risk_score_scale: 10_000,
      action,
      reason_code: result.reason_code,
      detector_version: result.detector_version,
      ruleset_version: result.ruleset_version,
      input_code_points: inputCodePoints,
      input_truncated: false,
      duration_ms: durationMs,
      completed_at: completedAt,
    }),
  });
}

/** 读取 JSON 对象；解析失败时只返回空对象，不暴露代理错误页或底层解析异常。 */
async function readJson(response) {
  try {
    const payload = await response.json();
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
  } catch {
    // 非 JSON 响应不能作为错误 message 或诊断详情进入前端状态。
    return {};
  }
}

/** 检查普通对象是否精确包含冻结字段集合，阻止未知扩展字段穿透安全投影。 */
function hasExactKeys(value, expectedKeys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length === expectedKeys.length
    && keys.every((key, index) => key === [...expectedKeys].sort()[index]);
}

/** 把统一错误码限制为有界 ASCII 枚举，非法值不会进入 DOM。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** 只接受固定格式的公开链路或对象标识。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return SAFE_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 版本和原因码必须是有界 ASCII 枚举，不能夹带正文或 HTML。 */
function safeEnum(value) {
  return SAFE_ENUM_PATTERN.test(String(value ?? ""));
}

/** 校验 ISO 时间并返回原始规范值；非法时间不得由浏览器猜测或修复。 */
function safeIsoDateTime(value) {
  const normalized = String(value ?? "");
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(normalized)
    && !Number.isNaN(Date.parse(normalized))
    ? normalized
    : "";
}

/** 构造不含请求正文、原始响应或浏览器异常文本的本地统一错误。 */
function intentClientError(status, code, message, requestId = "-", traceId = "-") {
  return new AuthApiError({
    status,
    code,
    message,
    requestId,
    traceId,
    suggestion: "",
    diagnosis: null,
  });
}
