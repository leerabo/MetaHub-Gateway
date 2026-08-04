/**
 * 模块职责：封装 PRD-049 调用反馈写入与租户满意度汇总读取。
 * 安全边界：客户端只发送后端公开的筛选和反馈字段，tenant_id、actor、Token 明文与响应未知字段都不会进入业务 DTO。
 */

import { AuthApiError } from "./auth.js";

/** Feedback Summary 的分页默认值，与后端 `GET /feedback/summary` 合同保持一致。 */
const DEFAULT_SUMMARY_LIMIT = 20;

/**
 * 创建反馈 POST 的操作请求 ID。
 * 该 ID 是幂等键而不是被评价的源调用 ID；调用方必须在网络结果未知的同意图重试中复用它。
 */
export function createRequestFeedbackRequestId() {
  const randomPart = globalThis.crypto?.randomUUID?.().replaceAll("-", "")
    ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `console_feedback_${randomPart}`.slice(0, 128);
}

/**
 * 通过 Control API 为一条当前租户调用提交不可变反馈。
 * 参数中的 requestId 是源调用 ID，operationRequestId 才会写入 `X-Request-ID`，两者不得互换。
 */
export async function createRequestFeedback({
  controlApiUrl,
  token,
  requestId,
  rating,
  accepted,
  tags,
  operationRequestId,
  signal,
}) {
  const payload = await requestFeedbackRequest(`${controlApiUrl}/feedback`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      "X-Request-ID": operationRequestId,
    },
    // 请求体使用精确白名单，避免未来表单状态把 tenant、actor 或敏感正文意外序列化到 API。
    body: JSON.stringify({
      request_id: requestId,
      rating,
      accepted,
      tags: Array.isArray(tags) ? [...tags] : [],
    }),
    signal,
  });
  return {
    feedback: projectFeedbackInfo(payload.feedback),
    idempotent_replay: payload.idempotent_replay === true,
    request_id: text(payload.request_id),
    trace_id: text(payload.trace_id),
  };
}

/**
 * 读取当前 Session 租户的满意度和采纳率报表。
 * model/time/limit/offset 是唯一允许的查询参数，tenant_id 与 actor 永远由后端认证上下文恢复。
 */
export async function getRequestFeedbackSummary({
  controlApiUrl,
  token,
  modelId,
  fromTime,
  toTime,
  limit = DEFAULT_SUMMARY_LIMIT,
  offset = 0,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/feedback/summary`);
  setOptionalQuery(url, "model_id", modelId);
  setOptionalQuery(url, "from_time", fromTime);
  setOptionalQuery(url, "to_time", toTime);
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  const payload = await requestFeedbackRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    signal,
  });
  return projectFeedbackSummary(payload);
}

/**
 * 投影 RequestFeedback 公开 DTO。
 * 可选字段用稳定空值收敛，既兼容并行后端开发，又不会把 Provider、Key、actor ID 或未知诊断渲染到 DOM。
 */
export function projectFeedbackInfo(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  return {
    request_feedback_id: text(value.request_feedback_id),
    request_id: text(value.request_id ?? value.source_request_id),
    model_id: text(value.model_id),
    rating: integer(value.rating),
    accepted: typeof value.accepted === "boolean" ? value.accepted : null,
    tags: stringArray(value.tags, 5),
    actor_type: text(value.actor_type),
    status: text(value.status),
    created_at: text(value.created_at),
  };
}

/**
 * 将 Summary 响应裁剪成页面需要的固定字段。
 * 数值只接受安全整数，异常值回到 0；页面不会用 JavaScript 浮点重新计算服务端权威统计。
 */
function projectFeedbackSummary(value) {
  const summary = value?.summary && typeof value.summary === "object" ? value.summary : {};
  const models = Array.isArray(value?.models) ? value.models.map(projectModelSummary) : [];
  return {
    state: "ok",
    scope_type: text(value?.scope_type),
    from_time: text(value?.from_time),
    to_time: text(value?.to_time),
    summary: {
      feedback_count: nonnegativeInteger(summary.feedback_count),
      rating_sum: nonnegativeInteger(summary.rating_sum),
      average_rating_milli: nonnegativeInteger(summary.average_rating_milli),
      accepted_count: nonnegativeInteger(summary.accepted_count),
      adoption_rate_bps: nonnegativeInteger(summary.adoption_rate_bps),
      rating_distribution: projectRatingDistribution(summary.rating_distribution),
      tag_counts: Array.isArray(summary.tag_counts)
        ? summary.tag_counts.map(projectTagCount).filter(Boolean)
        : [],
    },
    models,
    total: nonnegativeInteger(value?.total),
    limit: positiveInteger(value?.limit, DEFAULT_SUMMARY_LIMIT),
    offset: nonnegativeInteger(value?.offset),
    request_id: text(value?.request_id),
    trace_id: text(value?.trace_id),
  };
}

/** 将单个模型汇总裁剪为 PAGE-071 可展示的统计字段。 */
function projectModelSummary(value) {
  const source = value && typeof value === "object" ? value : {};
  return {
    model_id: text(source.model_id),
    internal_model_id: text(source.internal_model_id),
    display_name: text(source.display_name),
    feedback_count: nonnegativeInteger(source.feedback_count),
    average_rating_milli: nonnegativeInteger(source.average_rating_milli),
    accepted_count: nonnegativeInteger(source.accepted_count),
    adoption_rate_bps: nonnegativeInteger(source.adoption_rate_bps),
    rating_distribution: projectRatingDistribution(source.rating_distribution),
  };
}

/** 投影一个固定标签计数；空标签不会进入报表 DOM。 */
function projectTagCount(value) {
  const tag = text(value?.tag).trim();
  return tag ? { tag, count: nonnegativeInteger(value?.count) } : null;
}

/** 五档分布始终返回完整 1..5 键，缺字段不允许导致渲染器崩溃。 */
function projectRatingDistribution(value) {
  const source = value && typeof value === "object" ? value : {};
  return Object.fromEntries([1, 2, 3, 4, 5].map((rating) => [
    String(rating),
    nonnegativeInteger(source[String(rating)]),
  ]));
}

/** 设置非空查询参数；空时间对交给后端使用默认 30 天窗口。 */
function setOptionalQuery(url, name, value) {
  const normalized = text(value).trim();
  if (normalized) {
    url.searchParams.set(name, normalized);
  }
}

/**
 * 执行 Feedback API 请求并转换统一错误。
 * 错误对象只保留 code/message/suggestion/request_id/trace_id，不附带 URL、请求体、Token 或原始响应正文。
 */
async function requestFeedbackRequest(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await readJson(response);
  if (!response.ok) {
    // 统一错误转换必须发生在业务层之前，避免表单直接读取可能包含敏感字段的原始响应。
    throw new AuthApiError({
      status: response.status,
      code: text(payload.code) || "feedback.unavailable",
      message: text(payload.message) || `反馈接口返回 ${response.status}`,
      requestId: text(payload.request_id) || response.headers.get("X-Request-ID") || "-",
      traceId: text(payload.trace_id) || response.headers.get("X-Trace-ID") || "-",
      suggestion: text(payload.suggestion),
      diagnosis: null,
    });
  }
  return payload && typeof payload === "object" ? payload : {};
}

/** 安全读取 JSON；空响应仍由 HTTP 状态生成可追踪错误，不向页面传播解析异常。 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

/** 复制最多 limit 个字符串，拒绝对象和其他可隐式字符串化的未知值。 */
function stringArray(value, limit) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string").slice(0, limit)
    : [];
}

/** 仅接纳 JavaScript 可精确表达的整数，非法值返回 null。 */
function integer(value) {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : null;
}

/** 统计计数只接受非负安全整数，未知或越界值不会参与页面运算。 */
function nonnegativeInteger(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

/** 分页大小必须为正安全整数，异常后端值回退到合同默认值。 */
function positiveInteger(value, fallback) {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

/** 将可空公开字段收敛为文本，不对对象执行 JSON 序列化。 */
function text(value) {
  return value === null || value === undefined ? "" : String(value);
}
