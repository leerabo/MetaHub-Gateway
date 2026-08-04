/**
 * 模块职责：封装 PAGE-069 模型质量评分的租户内读取与人工评分更新请求。
 * 安全边界：tenant_id、actor 和统计字段永不由前端提交；权限、CAS 与租户隔离由后端再次校验。
 */

import { AuthApiError } from "./auth.js";

// request_id 只承担幂等与排障关联；随机段和单调序列都不包含租户、模型或评分业务值。
let modelQualityRequestSequence = 0;

/**
 * 查询当前登录租户可见的模型质量评分。
 * 参数只允许模型过滤和分页，tenant_id 必须由 Bearer Session 在后端恢复。
 */
export async function listModelQualityScores({
  controlApiUrl,
  token,
  modelId,
  limit = 100,
  offset = 0,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/model-quality-scores`);
  if (String(modelId ?? "").trim()) {
    url.searchParams.set("model_id", String(modelId).trim());
  }
  url.searchParams.set("limit", String(limit));
  url.searchParams.set("offset", String(offset));
  return modelQualityRequest(url.toString(), {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
}

/**
 * 生成人工评分 mutation 的稳定 request_id。
 * 调用方在网络结果未知时复用同一值，避免重复人工反馈、审计和 Webhook 事实。
 */
export function createModelQualityScoreRequestId() {
  return `req_console_model_quality_${createRequestNonce()}`;
}

/**
 * 更新或清除一个模型的人工质量评分。
 * manualQualityBps 为 null 时表示清除；统计计数和 tenant_id 不属于可提交字段。
 */
export async function updateModelQualityScore({
  controlApiUrl,
  token,
  modelId,
  manualQualityBps,
  expectedFeedbackVersion,
  requestId = createModelQualityScoreRequestId(),
  signal,
}) {
  return modelQualityRequest(
    `${controlApiUrl}/model-quality-scores`,
    {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model_id: modelId,
        manual_quality_bps: manualQualityBps,
        expected_feedback_version: expectedFeedbackVersion,
      }),
      signal,
    },
    requestId,
  );
}

/**
 * 发送评分 API 请求并把统一错误转换为 AuthApiError。
 * 错误对象只附带后端公开的当前 Score 摘要，SQL、响应原文和任意额外字段不会进入页面状态。
 */
async function modelQualityRequest(url, options = {}, requestId = createModelQualityScoreRequestId()) {
  const response = await fetch(url, {
    ...options,
    headers: {
      "X-Request-ID": requestId,
      ...(options.headers ?? {}),
    },
  });
  const payload = await readJson(response);
  if (!response.ok) {
    // 统一错误体优先，响应头只用于非 JSON 代理错误兜底，不能把原始响应正文写进异常。
    const error = new AuthApiError({
      status: response.status,
      code: payload.code,
      message: payload.message || `模型评分接口返回 ${response.status}`,
      requestId: payload.request_id || response.headers.get("X-Request-ID") || requestId || "-",
      traceId: payload.trace_id || response.headers.get("X-Trace-ID") || "-",
      suggestion: payload.suggestion || "",
      diagnosis: payload.diagnosis ?? null,
    });
    error.currentModelQualityScore = safeCurrentScore(payload);
    // 只抛出统一错误与白名单 Score 摘要，原始响应正文和未知敏感字段不会离开客户端边界。
    throw error;
  }
  return { state: "ok", ...payload };
}

/**
 * 从 409 错误体提取可用于显式 rebase 的评分白名单。
 * 只复制 PAGE-069 已公开字段，避免未来后端附加内部诊断时被无意渲染。
 */
function safeCurrentScore(payload) {
  const score = payload?.model_quality_score
    ?? payload?.current_model_quality_score
    ?? payload?.details?.model_quality_score
    ?? payload?.details?.current_model_quality_score;
  if (!score || typeof score !== "object" || Array.isArray(score)) {
    return null;
  }
  return copyScoreFields(score);
}

/**
 * 复制模型评分公开字段；字段名单与 GET/PATCH DTO 对齐，不保留未知嵌套对象。
 */
function copyScoreFields(score) {
  const fields = [
    "model_quality_score_id", "tenant_id", "model_id", "internal_model_id", "display_name",
    "provider_code", "provider_name", "model_status", "status", "sample_count", "success_count",
    "error_count", "latency_sample_count", "total_latency_ms", "success_rate_bps", "error_rate_bps",
    "average_latency_ms", "manual_quality_bps", "effective_quality_bps", "observation_status",
    "formula_version", "statistics_version", "feedback_version", "first_observed_at", "last_observed_at",
    "feedback_updated_by", "feedback_updated_at", "request_feedback_count", "request_feedback_rating_sum",
    "request_feedback_average_milli", "request_feedback_accepted_count",
    "request_feedback_adoption_rate_bps", "request_feedback_statistics_version",
    "request_feedback_rating_distribution", "created_at", "updated_at",
  ];
  const copied = Object.fromEntries(fields.map((field) => [field, score?.[field]]));
  // 五档分布是 PRD-049 唯一允许的嵌套统计；逐键复制可阻止未来内部聚合字段随 409 摘要进入页面状态。
  copied.request_feedback_rating_distribution = copyRequestFeedbackDistribution(
    score?.request_feedback_rating_distribution,
  );
  return copied;
}

/** 只复制 1..5 五个固定评分档位，未知 key 和嵌套值不能穿过客户端白名单。 */
function copyRequestFeedbackDistribution(value) {
  const source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return Object.fromEntries([1, 2, 3, 4, 5].map((rating) => [String(rating), source[String(rating)]]));
}

/**
 * 生成不含身份或业务值的请求随机段。
 * Web Crypto 不可用时使用时间和进程内序列兜底；request_id 从不作为认证凭据。
 */
function createRequestNonce() {
  const sequence = (++modelQualityRequestSequence).toString(36);
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") {
    return `${cryptoApi.randomUUID().replaceAll("-", "")}_${sequence}`;
  }
  if (typeof cryptoApi?.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    const random = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${random}_${sequence}`;
  }
  return `${Date.now().toString(36)}_${sequence}`;
}

/**
 * 容错读取 JSON；空体或代理 HTML 统一转为空对象，随后仍走带 request_id 的结构化错误分支。
 */
async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
