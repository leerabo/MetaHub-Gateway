/** PAGE-117 产品激活漏斗客户端；只读取当前认证租户，并严格校验无缓存响应信封。 */

import { AuthApiError } from "./auth.js";

export const PRODUCT_ANALYTICS_STAGE_TYPES = Object.freeze([
  "tenant.registered",
  "provider.created",
  "virtual_key.created",
  "gateway.first_call_succeeded",
  "budget.configured",
  "teammate.invited",
  "request_logs.viewed",
]);

// 这些正则与字段白名单共同封闭公开响应，防止额外租户字段、错误诊断正文或伪造链路标识进入页面状态。
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const RESPONSE_KEYS = Object.freeze([
  "tenant_id", "window", "cohort_tenant_count", "stages", "summary", "generated_at",
  "request_id", "trace_id",
]);
const WINDOW_KEYS = Object.freeze(["kind", "timezone"]);
const STAGE_KEYS = Object.freeze([
  "order", "event_type", "event_count", "reached", "cumulative_reached",
  "reached_tenant_count", "cumulative_tenant_count", "conversion_from_previous_bps",
  "first_occurred_at", "last_occurred_at",
]);
const SUMMARY_KEYS = Object.freeze([
  "completed_stage_count", "cumulative_stage_count", "total_stage_count", "completion_bps",
  "activated",
]);
const ERROR_KEYS = Object.freeze([
  "code", "message", "request_id", "trace_id", "suggestion", "diagnosis",
]);
const DIAGNOSIS_KEYS = Object.freeze([
  "rule_id", "category", "reason", "suggestion", "action", "severity", "retryable",
  "matched_by", "safe_summary",
]);
const ERROR_STATUS = Object.freeze({
  "auth.token_missing": 401,
  "auth.token_invalid": 401,
  "auth.token_expired": 401,
  "auth.session_revoked": 401,
  "auth.permission_denied": 403,
  "product_analytics.request_invalid": 422,
  "product_analytics.unavailable": 503,
});
// 页面只展示固定错误码对应的公开文案，服务端返回的任意消息正文不会直接进入 DOM。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有产品分析读取权限",
  "product_analytics.request_invalid": "产品分析请求不合法",
  "product_analytics.unavailable": "产品分析暂不可用",
  "product_analytics.response_invalid": "产品分析响应合同不完整",
});

/** 读取当前认证租户的全历史漏斗；请求不接受业务 query，也不允许客户端指定其他租户。 */
export async function getProductAnalyticsFunnel({ controlApiUrl, token, tenantId, signal }) {
  requireTenant(tenantId);
  const payload = await requestJson(`${controlApiUrl}/product-analytics/funnel`, {
    headers: authorizationHeaders(token),
    signal,
  });
  const normalized = normalizeProductAnalyticsFunnel(payload, tenantId);
  // 租户、阶段或链路不变量任一不符都按固定客户端错误收口，不能向调用方泄露原始响应正文。
  if (!normalized) {
    throw clientError(
      "product_analytics.response_invalid",
      502,
      payload?.request_id,
      payload?.trace_id,
    );
  }
  return normalized;
}

/** 校验精确响应信封、固定七阶段顺序、租户 owner、UTC 时间及全部聚合不变量。 */
export function normalizeProductAnalyticsFunnel(payload, tenantId) {
  if (
    !exact(payload, RESPONSE_KEYS)
    || payload.tenant_id !== tenantId
    || !safeId(payload.tenant_id)
    || !exact(payload.window, WINDOW_KEYS)
    || payload.window.kind !== "all_time"
    || payload.window.timezone !== "UTC"
    || payload.cohort_tenant_count !== 1
    || !Array.isArray(payload.stages)
    || payload.stages.length !== PRODUCT_ANALYTICS_STAGE_TYPES.length
    || !exact(payload.summary, SUMMARY_KEYS)
    || !utc(payload.generated_at)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;

  let prefixReached = true;
  let completedStageCount = 0;
  let cumulativeStageCount = 0;
  const stages = [];
  for (let index = 0; index < PRODUCT_ANALYTICS_STAGE_TYPES.length; index += 1) {
    const source = payload.stages[index];
    const normalized = normalizeStage(source, index, prefixReached, payload.stages[index - 1]);
    if (!normalized) return null;
    stages.push(normalized);
    if (normalized.reached) completedStageCount += 1;
    prefixReached = prefixReached && normalized.reached;
    if (prefixReached) cumulativeStageCount += 1;
  }

  const expectedCompletionBps = Math.floor((completedStageCount * 10000) / 7 + 0.5);
  if (
    !integer(payload.summary.completed_stage_count, 0, 7)
    || payload.summary.completed_stage_count !== completedStageCount
    || !integer(payload.summary.cumulative_stage_count, 0, 7)
    || payload.summary.cumulative_stage_count !== cumulativeStageCount
    || payload.summary.total_stage_count !== 7
    || !integer(payload.summary.completion_bps, 0, 10000)
    || payload.summary.completion_bps !== expectedCompletionBps
    || typeof payload.summary.activated !== "boolean"
    || payload.summary.activated !== (completedStageCount === 7)
  ) return null;

  return Object.freeze({
    tenant_id: payload.tenant_id,
    window: Object.freeze({ ...payload.window }),
    cohort_tenant_count: payload.cohort_tenant_count,
    stages: Object.freeze(stages),
    summary: Object.freeze({ ...payload.summary }),
    generated_at: payload.generated_at,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 校验单个漏斗阶段及相邻转化关系，拒绝越序累计、伪造计数和不一致时间范围。 */
function normalizeStage(value, index, prefixBeforeStage, previous) {
  const reached = value?.reached;
  const cumulativeReached = prefixBeforeStage && reached === true;
  const previousCumulativeCount = index === 0 ? 1 : previous?.cumulative_tenant_count;
  const expectedConversion = previousCumulativeCount === 0
    ? null
    : (cumulativeReached ? 10000 : 0);
  if (
    !exact(value, STAGE_KEYS)
    || value.order !== index + 1
    || value.event_type !== PRODUCT_ANALYTICS_STAGE_TYPES[index]
    || !integer(value.event_count, 0)
    || typeof reached !== "boolean"
    || reached !== (value.event_count > 0)
    || typeof value.cumulative_reached !== "boolean"
    || value.cumulative_reached !== cumulativeReached
    || !integer(value.reached_tenant_count, 0, 1)
    || value.reached_tenant_count !== (reached ? 1 : 0)
    || !integer(value.cumulative_tenant_count, 0, 1)
    || value.cumulative_tenant_count !== (cumulativeReached ? 1 : 0)
    || !(value.conversion_from_previous_bps === null
      || integer(value.conversion_from_previous_bps, 0, 10000))
    || value.conversion_from_previous_bps !== expectedConversion
    || !validTimes(value.event_count, value.first_occurred_at, value.last_occurred_at)
  ) return null;
  return Object.freeze({ ...value });
}

/** 执行唯一只读请求并核对状态、无缓存头和链路信封；Abort 保留给页面 owner 生命周期处理。 */
async function requestJson(url, init) {
  let response;
  try {
    response = await fetch(url, {
      ...init,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  } catch (error) {
    // owner 切换触发的 Abort 必须原样上抛，不能被误报为后端不可用或写入错误面板。
    if (error?.name === "AbortError") throw error;
    throw clientError("product_analytics.unavailable", 503);
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 内容不是合法公开信封，统一转成固定错误且不保留响应正文。
    throw clientError("product_analytics.response_invalid", 502);
  }
  // Cache-Control、请求链路和响应链路必须与 JSON 信封完全一致，避免代理混接租户结果。
  if (!validBoundary(response, payload)) {
    throw clientError("product_analytics.response_invalid", 502);
  }
  if (response.ok) {
    // 成功分支只接受精确 200，其他二百段状态不能绕过漏斗响应合同。
    if (response.status !== 200) {
      throw clientError("product_analytics.response_invalid", 502, payload.request_id, payload.trace_id);
    }
    return payload;
  }
  // 非成功响应必须同时匹配固定错误结构和状态映射，未知服务端正文一律丢弃。
  if (!validError(payload) || ERROR_STATUS[payload.code] !== response.status) {
    throw clientError("product_analytics.response_invalid", 502);
  }
  // 已验证的公开错误只传递固定 code 与链路标识，不携带 diagnosis 或原始消息正文。
  throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
}

/** 核对 no-store 响应头及 request_id、trace_id 的头体一致性，阻断跨请求信封混接。 */
function validBoundary(response, payload) {
  const cacheControl = header(response?.headers, "Cache-Control");
  const requestId = header(response?.headers, "X-Request-ID");
  const traceId = header(response?.headers, "X-Trace-ID");
  return cacheControl === "no-store, private"
    && chain(requestId)
    && chain(traceId)
    && payload?.request_id === requestId
    && payload?.trace_id === traceId;
}

/** 校验统一错误信封的精确字段和公开诊断类型，额外字段或未知形状均视为无效。 */
function validError(value) {
  return Boolean(
    exact(value, ERROR_KEYS)
    && ERROR_CODE.test(String(value.code ?? ""))
    && typeof value.message === "string"
    && typeof value.suggestion === "string"
    && chain(value.request_id)
    && chain(value.trace_id)
    && exact(value.diagnosis, DIAGNOSIS_KEYS)
    && DIAGNOSIS_KEYS.every((key) => (
      key === "retryable" ? typeof value.diagnosis[key] === "boolean" : typeof value.diagnosis[key] === "string"
    )),
  );
}

/** 约束事件计数与首次、最近 UTC 时间成对出现，并保证时间顺序不会倒置。 */
function validTimes(count, first, last) {
  if (count === 0) return first === null && last === null;
  const firstTime = parseUtc(first);
  const lastTime = parseUtc(last);
  return Boolean(firstTime && lastTime && firstTime.sortKey <= lastTime.sortKey);
}

/** 构造只含 Bearer 与 JSON 接受类型的认证头，拒绝空白或可拆分的会话令牌。 */
function authorizationHeaders(token) {
  // 非单段令牌可能改变 Authorization 语义，必须在发出网络请求前按缺失会话拒绝。
  if (typeof token !== "string" || !token || /\s/u.test(token)) {
    throw clientError("auth.token_missing", 401);
  }
  // Token 只进入请求头并由 Fetch 生命周期持有，不拼入 URL、query 或页面证据。
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

/** 在请求前验证本地租户 owner 标识；该值只用于校验响应归属，不会发送到 API。 */
function requireTenant(value) {
  if (!safeId(value)) throw clientError("product_analytics.request_invalid", 422);
  return value;
}

/** 把已验证的固定错误码和安全链路标识裁剪为统一 AuthApiError，不保留响应对象。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "product_analytics.unavailable";
  // 统一错误对象仅携带固定公开字段，调用方不能从异常中读取响应正文或诊断载荷。
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "产品分析请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

/** 比较对象的精确字段集合，阻止服务端新增字段在未审计时进入前端状态。 */
function exact(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()));
}

/** 兼容标准 Headers 与测试替身读取单个响应头，并裁剪首尾空白。 */
function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

/** 校验租户等公开标识的字符范围，避免控制字符进入 owner 比较。 */
function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

/** 校验 request_id 与 trace_id 的有限字符集和长度，保证链路值可安全显示。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

/** 判断时间值是否为真实存在且规范化为 Z 后缀的 UTC 时间。 */
function utc(value) {
  return Boolean(parseUtc(value));
}

/** 解析并复核 UTC 日历字段，避免 JavaScript Date 自动纠正非法日期。 */
function parseUtc(value) {
  if (typeof value !== "string") return null;
  if (!UTC_TIME.test(value)) return null;
  const [date, timeWithFraction] = value.slice(0, -1).split("T");
  const [yearText, monthText, dayText] = date.split("-");
  const [hourText, minuteText, secondWithFraction] = timeWithFraction.split(":");
  const [secondText, fraction = ""] = secondWithFraction.split(".");
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  const calendar = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (
    calendar.getUTCFullYear() !== year
    || calendar.getUTCMonth() !== month - 1
    || calendar.getUTCDate() !== day
    || calendar.getUTCHours() !== hour
    || calendar.getUTCMinutes() !== minute
    || calendar.getUTCSeconds() !== second
  ) return null;
  return Object.freeze({
    sortKey: `${date}T${hourText}:${minuteText}:${secondText}.${fraction.padEnd(6, "0")}Z`,
  });
}

/** 校验聚合计数处于安全整数区间，拒绝浮点、负数和溢出值。 */
function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}
