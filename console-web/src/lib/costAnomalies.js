/**
 * 模块职责：封装 PAGE-077/078 使用的成本异常查询与治理动作 API。
 * 安全边界：客户端只发送冻结的筛选和动作白名单，绝不接受或上送 tenant、actor、状态、
 * 绝对到期时间、检测证据或链路标识正文；真实租户隔离与 admin 权限始终由 Control API 强制执行。
 */

import { AuthApiError } from "./auth.js";

// 查询枚举与后端 wire 合同保持一致；前端校验只减少误请求，不能替代后端 RBAC 和租户校验。
const STATUS_VALUES = new Set(["open", "acknowledged", "dismissed", "mitigated"]);
const RULE_TYPE_VALUES = new Set([
  "tenant_spend_spike",
  "virtual_key_spend_spike",
  "virtual_key_off_hours",
]);
const SEVERITY_VALUES = new Set(["warning", "critical"]);
const ACTION_VALUES = new Set([
  "acknowledge",
  "dismiss",
  "apply_temporary_rate_limit",
  "release_temporary_rate_limit",
]);
// 忽略原因是审计字段，只接受有限业务枚举且不允许自由文本。
const DISMISS_REASON_VALUES = new Set([
  "false_positive",
  "expected_activity",
  "resolved_without_action",
  "other",
]);
// ID 和幂等键只允许稳定 ASCII 形状，避免代理规范化差异改变路径或幂等摘要。
const ANOMALY_ID_PATTERN = /^can_[0-9a-f]{32}$/;
const IDEMPOTENCY_PATTERN = /^[A-Za-z0-9._:-]{8,128}$/;
const CANONICAL_INTEGER_PATTERN = /^(?:0|[1-9][0-9]*)$/;
const MAX_LIMIT_VALUE = 100_000_000;

/**
 * 查询当前认证租户的成本异常快照。
 *
 * @param {object} options Control 地址、Bearer Token、筛选、opaque cursor 和 AbortSignal。
 * @returns {Promise<object>} 后端返回的异常列表、汇总和链路标识。
 *
 * 关键约束：tenantId、actorUserId、任意排序字段和未知对象属性不会进入 URL；cursor 由页面
 * 内存提供且不得写入地址栏、DOM 或 Storage。
 */
export async function listCostAnomalies({
  controlApiUrl,
  token,
  status,
  ruleType,
  severity,
  environmentId,
  virtualKeyId,
  fromTime,
  toTime,
  limit = 20,
  cursor,
  operationRequestId,
  operationTraceId,
  signal,
}) {
  const safeLimit = requireQueryLimit(limit);
  const url = new URL(`${controlApiUrl}/cost-anomalies`);
  setOptionalEnum(url, "status", status, STATUS_VALUES);
  setOptionalEnum(url, "rule_type", ruleType, RULE_TYPE_VALUES);
  setOptionalEnum(url, "severity", severity, SEVERITY_VALUES);
  setOptionalParam(url, "environment_id", environmentId);
  setOptionalParam(url, "virtual_key_id", virtualKeyId);
  setOptionalParam(url, "from_time", fromTime);
  setOptionalParam(url, "to_time", toTime);
  url.searchParams.set("limit", String(safeLimit));
  setOptionalParam(url, "cursor", cursor);

  const response = await fetch(url.toString(), {
    signal,
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      "X-Request-ID": operationRequestId || createCostAnomalyRequestId("list"),
      "X-Trace-ID": operationTraceId || createCostAnomalyTraceId(),
    },
  });
  const payload = await readJson(response);
  throwCostAnomalyApiError(response, payload, "成本异常读取失败");
  return { state: "ok", ...payload };
}

/**
 * 对一条当前租户异常执行显式治理动作。
 *
 * @param {object} options 异常 ID、动作参数、幂等键、认证信息和 AbortSignal。
 * @returns {Promise<object>} 更新后的异常、append-only 动作和临时限流摘要。
 *
 * 请求体由动作白名单重新构造，不会透传 tenant、actor、status、expires_at 或 request/trace ID。
 */
export async function applyCostAnomalyAction({
  controlApiUrl,
  token,
  anomalyId,
  action,
  dismissReason,
  durationMinutes,
  rpmLimit,
  tpmLimit,
  concurrencyLimit,
  idempotencyKey,
  operationRequestId,
  operationTraceId,
  signal,
}) {
  const safeAnomalyId = requireAnomalyId(anomalyId);
  const body = normalizeActionBody({
    action,
    dismissReason,
    durationMinutes,
    rpmLimit,
    tpmLimit,
    concurrencyLimit,
  });
  const safeIdempotencyKey = requireIdempotencyKey(idempotencyKey);
  const response = await fetch(`${controlApiUrl}/cost-anomalies/${safeAnomalyId}/action`, {
    method: "PATCH",
    signal,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      "Idempotency-Key": safeIdempotencyKey,
      "X-Request-ID": operationRequestId || createCostAnomalyRequestId("action"),
      "X-Trace-ID": operationTraceId || createCostAnomalyTraceId(),
    },
    body: JSON.stringify(body),
  });
  const payload = await readJson(response);
  throwCostAnomalyApiError(response, payload, "成本异常动作提交失败");
  return { state: "ok", ...payload };
}

/**
 * 生成一次动作意图使用的内存幂等键。
 * 网络或 5xx 结果不确定时调用方必须继续复用；只有明确 4xx 才能为修正后的意图换键。
 */
export function createCostAnomalyIdempotencyKey() {
  return `ca_${randomToken(32)}`;
}

/** 生成不承载租户、异常 ID 或动作参数的 Console request_id。 */
export function createCostAnomalyRequestId(action = "request") {
  return `console_cost_anomaly_${action}_${Date.now()}_${randomToken(12)}`;
}

/** 生成一次请求使用的随机 trace_id，重试时由页面内存决定是否复用。 */
export function createCostAnomalyTraceId() {
  return `trc_console_cost_anomaly_${randomToken(32)}`;
}

/**
 * 判断动作失败后是否必须保留原幂等键。
 * 原生网络异常和 5xx 可能发生在服务端提交之后，换键会制造第二个治理动作；明确 4xx 代表
 * 服务端已给出否定结论，用户修正输入后可使用新键。
 */
export function shouldRetainCostAnomalyIdempotencyKey(error) {
  if (!(error instanceof AuthApiError)) {
    return true;
  }
  const status = Number(error.status ?? 0);
  return status === 0 || status >= 500;
}

/**
 * 用 BigInt 把 canonical CNY micros 格式化为固定六位小数。
 * 无效、带前导零、负数或非字符串输入返回稳定占位，绝不退化为 Number 造成大金额精度丢失。
 */
export function formatCostMicros(value) {
  if (typeof value !== "string" || !CANONICAL_INTEGER_PATTERN.test(value)) {
    return "-";
  }
  try {
    const micros = BigInt(value);
    const whole = micros / 1_000_000n;
    const fraction = String(micros % 1_000_000n).padStart(6, "0");
    return `${whole}.${fraction}`;
  } catch {
    return "-";
  }
}

/**
 * 把 canonical 千分倍率格式化为三位小数倍数；只做 BigInt 除余，不用浮点重新判定异常。
 */
export function formatCostRatioMillis(value) {
  const normalized = typeof value === "number" && Number.isSafeInteger(value)
    ? String(value)
    : value;
  if (typeof normalized !== "string" || !CANONICAL_INTEGER_PATTERN.test(normalized)) {
    return "-";
  }
  try {
    const millis = BigInt(normalized);
    return `${millis / 1_000n}.${String(millis % 1_000n).padStart(3, "0")}x`;
  } catch {
    return "-";
  }
}

/** 只把非空筛选写入 URL，避免空字符串改变后端 cursor 筛选摘要。 */
function setOptionalParam(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/** 校验固定枚举后写入查询参数，未知值在 fetch 前以统一错误失败关闭。 */
function setOptionalEnum(url, name, value, allowedValues) {
  const normalized = String(value ?? "").trim();
  if (!normalized) {
    return;
  }
  if (!allowedValues.has(normalized)) {
    // 未知筛选必须在 fetch 前失败关闭，避免改变 cursor 签名语义。
    throw costAnomalyClientError("cost_anomaly.query_invalid", "成本异常筛选无效");
  }
  url.searchParams.set(name, normalized);
}

/** 校验页面大小，前端固定上限避免意外构造无界列表；后端仍会独立执行同类校验。 */
function requireQueryLimit(value) {
  if (!Number.isInteger(value) || value < 1 || value > 100) {
    throw costAnomalyClientError("cost_anomaly.query_invalid", "分页大小无效");
  }
  return value;
}

/** 校验不透明异常 ID 形状，避免被篡改的 DOM 值闭合路径或生成额外路径段。 */
function requireAnomalyId(value) {
  const normalized = String(value ?? "");
  if (!ANOMALY_ID_PATTERN.test(normalized)) {
    // 非法或注入 ID 不进入 URL，统一投影为当前页不可见。
    throw costAnomalyClientError("cost_anomaly.not_found", "当前页面找不到该成本异常", 404);
  }
  return normalized;
}

/** 根据动作类型生成精确 JSON 字段集合，禁止无关字段进入幂等 intent。 */
function normalizeActionBody(values) {
  const action = String(values.action ?? "").trim();
  if (!ACTION_VALUES.has(action)) {
    // 未知动作不能形成请求体或新的幂等意图。
    throw costAnomalyClientError("cost_anomaly.action_invalid", "成本异常动作无效");
  }
  if (action === "acknowledge") {
    requireNoActionParameters(values);
    return { action };
  }
  if (action === "dismiss") {
    const dismissReason = String(values.dismissReason ?? "").trim();
    if (!DISMISS_REASON_VALUES.has(dismissReason)) {
      // 忽略原因必须映射到后端审计枚举，禁止自由文本透传。
      throw costAnomalyClientError("cost_anomaly.action_invalid", "请选择有效的忽略原因");
    }
    requireNoRateLimitParameters(values);
    return { action, dismiss_reason: dismissReason };
  }
  if (action === "release_temporary_rate_limit") {
    requireNoActionParameters(values);
    return { action };
  }

  const durationMinutes = requireBoundedInteger(values.durationMinutes, "duration_minutes", 5, 1440, false);
  const rpmLimit = requireBoundedInteger(values.rpmLimit, "rpm_limit", 1, MAX_LIMIT_VALUE, true);
  const tpmLimit = requireBoundedInteger(values.tpmLimit, "tpm_limit", 1, MAX_LIMIT_VALUE, true);
  const concurrencyLimit = requireBoundedInteger(
    values.concurrencyLimit,
    "concurrency_limit",
    1,
    MAX_LIMIT_VALUE,
    true,
  );
  // 至少一个维度是显式正限制，实际是否比永久/其他临时规则更严格仍只能由后端权威判断。
  if (rpmLimit === null && tpmLimit === null && concurrencyLimit === null) {
    throw costAnomalyClientError("cost_anomaly.action_invalid", "临时限流至少填写一个限制维度");
  }
  return {
    action,
    duration_minutes: durationMinutes,
    rpm_limit: rpmLimit,
    tpm_limit: tpmLimit,
    concurrency_limit: concurrencyLimit,
  };
}

/** acknowledge/release 不能夹带原因、时长或限制，避免相同动作形成多个歧义摘要。 */
function requireNoActionParameters(values) {
  if (hasValue(values.dismissReason) || hasAnyRateLimitParameter(values)) {
    throw costAnomalyClientError("cost_anomaly.action_invalid", "该动作不能携带额外参数");
  }
}

/** dismiss 只接受原因，不能借可选字段夹带临时限流参数。 */
function requireNoRateLimitParameters(values) {
  if (hasAnyRateLimitParameter(values)) {
    throw costAnomalyClientError("cost_anomaly.action_invalid", "忽略动作不能携带限流参数");
  }
}

/** 判断任一限流字段是否由调用方提供，空字符串视为未填写。 */
function hasAnyRateLimitParameter(values) {
  return [values.durationMinutes, values.rpmLimit, values.tpmLimit, values.concurrencyLimit].some(hasValue);
}

/** 区分未提供与合法数字零；零随后会由有界整数校验明确拒绝。 */
function hasValue(value) {
  return value !== undefined && value !== null && value !== "";
}

/**
 * 校验 wire integer，严格拒绝 bool、float、指数和数字字符串。
 * DOM FormData 会先由 binder 显式转换为整数；其他调用者必须同样提供真正 number。
 */
function requireBoundedInteger(value, fieldName, minimum, maximum, nullable) {
  if (nullable && !hasValue(value)) {
    return null;
  }
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    // wire integer 超界或类型不精确时必须在 fetch 前拒绝，不能截断或宽松转换。
    throw costAnomalyClientError("cost_anomaly.action_invalid", `${fieldName} 超出允许范围`);
  }
  return value;
}

/** 校验动作幂等键；原文只进入 Header，不进入 URL、正文、DOM 或普通日志。 */
function requireIdempotencyKey(value) {
  const normalized = String(value ?? "");
  if (!IDEMPOTENCY_PATTERN.test(normalized)) {
    // 幂等键格式失败时绝不发送 Header，避免代理规范化产生歧义。
    throw costAnomalyClientError("cost_anomaly.action_invalid", "幂等键格式无效");
  }
  return normalized;
}

/** 把非成功响应投影为公开统一错误，不传播原始 body、请求头或服务端异常。 */
function throwCostAnomalyApiError(response, payload, fallbackMessage) {
  if (response.ok) {
    return;
  }
  // 非 2xx 只保留服务端公开统一错误字段，原始正文不会进入异常对象。
  throw new AuthApiError({
    status: response.status,
    code: payload.code || "cost_anomaly.unavailable",
    message: payload.message || fallbackMessage,
    requestId: payload.request_id || response.headers.get("X-Request-ID") || "-",
    traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
    suggestion: payload.suggestion || "",
    diagnosis: payload.diagnosis ?? null,
  });
}

/** 构造固定前端校验错误，不反射原始输入或请求载荷。 */
function costAnomalyClientError(code, message, status = 422) {
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

/** 容错读取 JSON；代理空正文或 HTML 错误页只会得到固定安全回退文案。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/**
 * 生成 Header 友好的密码学随机片段。
 * 缺少安全随机源时失败关闭，不能用 Math.random 生成可能碰撞的治理幂等键。
 */
function randomToken(length) {
  const uuid = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  if (uuid?.length >= length) {
    return uuid.slice(0, length);
  }
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi?.getRandomValues) {
    // 没有密码学随机源时拒绝生成治理标识，不能退化到可碰撞的 Math.random。
    throw costAnomalyClientError("cost_anomaly.action_invalid", "当前环境缺少安全随机数能力");
  }
  const bytes = new Uint8Array(Math.ceil(length / 2));
  cryptoApi.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, length);
}
