/**
 * PAGE-105 模型实验 API 客户端。
 *
 * 本模块只接受后端实验域的精确 DTO，并把所有网络、缓存和响应合同异常折叠为固定错误；
 * tenant、权限结论、Prompt、Response、Provider Key 与 Token 正文均不得进入页面状态。
 */

import { AuthApiError } from "./auth.js";

// 实验、资源、链路和时间标识使用与 Control 公共 DTO 一致的白名单，拒绝正文型字段混入。
const EXPERIMENT_ID = /^exp_[0-9a-f]{32}$/;
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const SAFE_CODE = /^[a-z][a-z0-9_.-]{0,127}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,1024}$/;
const UTC_TIME = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?Z$/;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f-\u009f]/u;

// 页面只认识单向实验状态与两种公开完成原因，未知枚举必须使整个响应失败关闭。
const STATUSES = new Set(["active", "collecting_feedback", "completed"]);
const COMPLETION_REASONS = new Set(["target_reached", "sampling_deadline_reached"]);
// Experiment DTO 精确字段集禁止后端内部指纹、Endpoint 或请求级 owner 被浏览器接收。
const EXPERIMENT_KEYS = Object.freeze([
  "experiment_id", "name", "project_id", "environment_id", "model_alias_id", "model_a",
  "model_b", "target_sample_count", "traffic_sample_bps", "sampling_duration_seconds",
  "feedback_collection_enabled", "feedback_window_seconds", "algorithm_version", "status",
  "assigned_count", "facts_version", "started_at", "sampling_deadline", "sampling_ended_at",
  "feedback_deadline", "settlement_deadline", "completed_at", "created_at", "updated_at",
]);
// A/B arm 与统计字段独立列白名单，避免页面通过宽松对象扩展意外展示内部诊断。
const ARM_KEYS = Object.freeze([
  "variant", "model_id", "provider_id", "provider_code", "allocation_bps", "target_count",
  "assigned_count",
]);
const RESULT_KEYS = Object.freeze([
  "variant", "model_id", "provider_id", "provider_code", "assigned_count",
  "gateway_success_count", "gateway_failure_count", "unresolved_count",
  "gateway_success_rate_bps", "exposed_count", "not_exposed_count", "exposure_unknown_count",
  "quality_success_count", "quality_failure_count", "quality_success_rate_bps",
  "latency_sample_count", "total_latency_ms", "average_latency_micros", "p95_latency_ms",
  "settlement_recorded_count", "settlement_not_exposed_count", "settlement_unavailable_count",
  "settlement_pending_count", "gross_cost_cny_micros", "cost_known_count", "cost_unknown_count",
  "average_cost_cny_micros", "feedback_collection_enabled", "feedback_count", "rating_sum",
  "average_rating_milli", "accepted_count", "adoption_rate_bps",
]);
// 四类响应 envelope 与创建正文采用 exact-set，request_id/trace_id 也是成功合同的一部分。
const LIST_KEYS = Object.freeze(["experiments", "next_cursor", "request_id", "trace_id"]);
const DETAIL_KEYS = Object.freeze(["experiment", "request_id", "trace_id"]);
const CREATE_KEYS = Object.freeze([
  "experiment", "idempotent_replay", "request_id", "trace_id",
]);
const RESULTS_KEYS = Object.freeze([
  "experiment_id", "status", "is_final", "completion_reason", "facts_version",
  "facts_through_at", "next_state_at", "pending_execution_count", "pending_settlement_count",
  "results", "request_id", "trace_id",
]);
const CREATE_BODY_KEYS = Object.freeze([
  "name", "project_id", "environment_id", "model_alias_id", "model_a_id", "model_b_id",
  "target_sample_count", "traffic_sample_bps", "model_a_allocation_bps",
  "sampling_duration_seconds", "feedback_collection_enabled", "feedback_window_seconds",
]);

// 服务端 message、diagnosis 与原始 body 永不透传，页面只使用固定本地错误文案。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有模型评测权限",
  "experiment.input_invalid": "实验配置不合法",
  "experiment.query_invalid": "实验查询条件不合法",
  "experiment.idempotency_required": "创建实验需要幂等键",
  "experiment.model_invalid": "请选择两个不同且可用的 Chat 模型",
  "experiment.not_found": "当前租户中找不到该实验",
  "experiment.idempotency_conflict": "该幂等键已用于其他实验配置",
  "experiment.scope_conflict": "当前作用域已有正在采样的实验",
  "experiment.unavailable": "实验服务暂不可用",
  "experiment.statistics_unavailable": "实验统计暂不可用",
  "experiment.response_invalid": "实验响应合同不完整",
});

/**
 * 查询当前授权 owner 的实验页。
 *
 * status、limit 与私有 cursor 在发请求前校验；tenant 只来自 Bearer Session，绝不进入 query。
 */
export async function listExperiments({
  controlApiUrl,
  token,
  status = "",
  limit = 50,
  cursor = null,
  signal,
}) {
  // 非法筛选不得发出网络请求，避免把模糊 query 交给后端猜测。
  if ((status && !STATUSES.has(status)) || !integer(limit, 1, 100)) {
    throw clientError("experiment.query_invalid", 422);
  }
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (cursor !== null) query.set("cursor", requireCursor(cursor));
  const payload = await requestJson(`${controlApiUrl}/experiments?${query}`, {
    headers: authorizationHeaders(token),
    signal,
  }, 200);
  const normalized = normalizeExperimentListResponse(payload);
  // 列表成功体违反 exact DTO 时必须固定失败，不能渲染未知字段或不一致租户事实。
  if (!normalized) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/** 读取一个实验的不可变配置，并校验 path owner 与响应 ID 完全一致。 */
export async function getExperiment({ controlApiUrl, token, experimentId, signal }) {
  const id = requireExperimentId(experimentId);
  const payload = await requestJson(
    `${controlApiUrl}/experiments/${encodeURIComponent(id)}`,
    { headers: authorizationHeaders(token), signal },
    200,
  );
  const normalized = normalizeExperimentDetailResponse(payload, id);
  // 详情 path owner 与响应对象不一致时整包拒绝，避免跨对象内容进入页面。
  if (!normalized) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/** 读取后端单 snapshot 生成的 A/B 权威统计，浏览器不拼接 Usage 或 Feedback。 */
export async function getExperimentResults({ controlApiUrl, token, experimentId, signal }) {
  const id = requireExperimentId(experimentId);
  const payload = await requestJson(
    `${controlApiUrl}/experiments/${encodeURIComponent(id)}/results`,
    { headers: authorizationHeaders(token), signal },
    200,
  );
  const normalized = normalizeExperimentResultsResponse(payload, id);
  // A/B 结果内部计数或响应 owner 损坏时不能返回部分统计。
  if (!normalized) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return normalized;
}

/**
 * 创建不可变在线实验。
 *
 * 调用方提供仅驻留内存的 UUIDv4 幂等键；正文只含公开配置且不允许客户端声明 tenant 或 actor。
 */
export async function createExperiment({
  controlApiUrl,
  token,
  idempotencyKey,
  request,
  signal,
}) {
  const body = normalizeCreateExperimentRequest(request);
  const key = requireIdempotencyKey(idempotencyKey);
  const payload = await requestJson(`${controlApiUrl}/experiments`, {
    method: "POST",
    headers: {
      ...authorizationHeaders(token),
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
    signal,
  }, 201);
  // 创建成功 envelope 必须同时带 exact 字段和明确幂等重放标记。
  if (!exact(payload, CREATE_KEYS) || typeBoolean(payload.idempotent_replay) === null) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  const experiment = normalizeExperiment(payload.experiment);
  // 成功对象和两条链路标识缺一不可，避免无法追踪的实验进入页面。
  if (
    !experiment
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || !experimentMatchesCreateRequest(experiment, body)
    || (!payload.idempotent_replay && (
      experiment.status !== "active"
      || experiment.assigned_count !== 0
      || experiment.facts_version !== 0
    ))
  ) {
    // 创建响应与本次正文、初始状态或链路 owner 任一不一致时整包失败，不能接纳错误幂等记录。
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return Object.freeze({
    experiment,
    idempotent_replay: payload.idempotent_replay,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 校验列表 envelope、唯一实验 ID 和每条配置的内部 A/B 约束。 */
export function normalizeExperimentListResponse(payload) {
  if (
    !exact(payload, LIST_KEYS)
    || !Array.isArray(payload.experiments)
    || payload.experiments.length > 100
    || !nullableCursor(payload.next_cursor)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  const experiments = payload.experiments.map(normalizeExperiment);
  if (
    experiments.some((value) => !value)
    || new Set(experiments.map((value) => value.experiment_id)).size !== experiments.length
  ) return null;
  return Object.freeze({
    experiments: Object.freeze(experiments),
    next_cursor: payload.next_cursor,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 校验详情响应，并拒绝 path ID 与 DTO ID 不一致的跨对象结果。 */
export function normalizeExperimentDetailResponse(payload, expectedExperimentId = null) {
  if (!exact(payload, DETAIL_KEYS) || !chain(payload.request_id) || !chain(payload.trace_id)) {
    return null;
  }
  const experiment = normalizeExperiment(payload.experiment);
  if (!experiment || (expectedExperimentId && experiment.experiment_id !== expectedExperimentId)) {
    return null;
  }
  return Object.freeze({
    experiment,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 校验结果 envelope、状态终态关系以及两个 arm 的整数统计矩阵。 */
export function normalizeExperimentResultsResponse(payload, expectedExperimentId = null) {
  if (
    !exact(payload, RESULTS_KEYS)
    || !EXPERIMENT_ID.test(String(payload.experiment_id ?? ""))
    || (expectedExperimentId && payload.experiment_id !== expectedExperimentId)
    || !STATUSES.has(payload.status)
    || typeBoolean(payload.is_final) === null
    || !(payload.completion_reason === null || COMPLETION_REASONS.has(payload.completion_reason))
    || !nonnegative(payload.facts_version)
    || !utc(payload.facts_through_at)
    || !nullableUtc(payload.next_state_at)
    || !nonnegative(payload.pending_execution_count)
    || !nonnegative(payload.pending_settlement_count)
    || !Array.isArray(payload.results)
    || payload.results.length !== 2
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  if (payload.is_final !== (payload.status === "completed")) return null;
  if (payload.is_final !== (payload.completion_reason !== null)) return null;
  if (
    (payload.is_final && (
      payload.next_state_at !== null
      || payload.pending_execution_count !== 0
      || payload.pending_settlement_count !== 0
    ))
    || (!payload.is_final && payload.next_state_at === null)
  ) return null;
  const results = payload.results.map(normalizeArmResult);
  if (
    !results[0]
    || !results[1]
    || results[0].variant !== "a"
    || results[1].variant !== "b"
    || results[0].model_id === results[1].model_id
    || results[0].feedback_collection_enabled !== results[1].feedback_collection_enabled
    || payload.pending_settlement_count
      !== results.reduce((total, item) => total + item.settlement_pending_count, 0)
    || payload.pending_execution_count
      !== results.reduce((total, item) => total + item.unresolved_count, 0)
  ) {
    return null;
  }
  return Object.freeze({
    ...payload,
    results: Object.freeze(results),
  });
}

/** 把表单投影收紧为 POST exact body，并以整数 half-up 验证双方目标配额非零。 */
export function normalizeCreateExperimentRequest(value) {
  // 未知字段可能携带客户端身份或敏感正文，必须在任何值转换前拒绝。
  if (!exact(value, CREATE_BODY_KEYS)) throw clientError("experiment.input_invalid", 422);
  const name = String(value.name ?? "");
  if (
    !name
    || name !== name.trim()
    || [...name].length > 120
    || CONTROL_CHARACTER.test(name)
    || ![value.project_id, value.environment_id, value.model_alias_id, value.model_a_id, value.model_b_id]
      .every(resource)
    || value.model_a_id === value.model_b_id
    || !integer(value.target_sample_count, 2, 100_000)
    || !integer(value.traffic_sample_bps, 1, 10_000)
    || !integer(value.model_a_allocation_bps, 1, 9_999)
    || !integer(value.sampling_duration_seconds, 60, 604_800)
    || typeBoolean(value.feedback_collection_enabled) === null
    || !integer(value.feedback_window_seconds, 0, 604_800)
    || (value.feedback_collection_enabled
      ? value.feedback_window_seconds < 60
      : value.feedback_window_seconds !== 0)
  ) throw clientError("experiment.input_invalid", 422);
  const modelATarget = Math.floor(
    (value.target_sample_count * value.model_a_allocation_bps + 5_000) / 10_000,
  );
  // A/B 双方至少各有一个目标样本，不能以极端配额制造空 arm。
  if (modelATarget < 1 || modelATarget >= value.target_sample_count) {
    throw clientError("experiment.input_invalid", 422);
  }
  return Object.freeze({ ...value });
}

/** 生成不包含 tenant、表单值或时间戳的 canonical lowercase UUIDv4 幂等键。 */
export function createExperimentIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.()?.toLowerCase();
  // 缺少安全随机源时失败关闭，不能用 Date.now 或 Math.random 代替幂等 owner。
  if (!UUID_V4.test(String(value ?? ""))) {
    throw clientError("experiment.idempotency_required", 422);
  }
  return value;
}

/** 计算仅用于当前 Dialog closure 的稳定正文指纹，正文变化才轮换幂等键。 */
export function experimentCreateFingerprint(request) {
  const normalized = normalizeCreateExperimentRequest(request);
  return JSON.stringify(CREATE_BODY_KEYS.map((key) => [key, normalized[key]]));
}

/** 校验 ExperimentInfo 的配置、配额、计数和单向状态时间关系。 */
function normalizeExperiment(value) {
  if (
    !exact(value, EXPERIMENT_KEYS)
    || !EXPERIMENT_ID.test(String(value.experiment_id ?? ""))
    || typeof value.name !== "string"
    || !value.name
    || value.name !== value.name.trim()
    || [...value.name].length > 120
    || CONTROL_CHARACTER.test(value.name)
    || ![value.project_id, value.environment_id, value.model_alias_id].every(resource)
    || !integer(value.target_sample_count, 2, 100_000)
    || !integer(value.traffic_sample_bps, 1, 10_000)
    || !integer(value.sampling_duration_seconds, 60, 604_800)
    || typeBoolean(value.feedback_collection_enabled) === null
    || !integer(value.feedback_window_seconds, 0, 604_800)
    || !nonnegative(value.algorithm_version)
    || value.algorithm_version < 1
    || !STATUSES.has(value.status)
    || !integer(value.assigned_count, 0, 100_000)
    || !nonnegative(value.facts_version)
    || ![value.started_at, value.sampling_deadline, value.created_at, value.updated_at].every(utc)
    || ![value.sampling_ended_at, value.feedback_deadline, value.settlement_deadline, value.completed_at]
      .every(nullableUtc)
  ) return null;
  const modelA = normalizeArm(value.model_a, "a");
  const modelB = normalizeArm(value.model_b, "b");
  const expectedATarget = variantATarget(
    value.target_sample_count,
    modelA?.allocation_bps,
  );
  if (
    !modelA
    || !modelB
    || modelA.model_id === modelB.model_id
    || modelA.allocation_bps + modelB.allocation_bps !== 10_000
    || expectedATarget === null
    || modelA.target_count !== expectedATarget
    || modelB.target_count !== value.target_sample_count - expectedATarget
    || modelA.target_count + modelB.target_count !== value.target_sample_count
    || modelA.assigned_count + modelB.assigned_count !== value.assigned_count
    || (value.status === "active" && value.assigned_count >= value.target_sample_count)
    || !validFeedbackWindow(value)
    || !validExperimentTimeline(value)
  ) return null;
  return Object.freeze({ ...value, model_a: modelA, model_b: modelB });
}

/** 校验单个 variant 的固定模型/Provider owner 与目标、已分配计数边界。 */
function normalizeArm(value, expectedVariant) {
  if (
    !exact(value, ARM_KEYS)
    || value.variant !== expectedVariant
    || !resource(value.model_id)
    || !resource(value.provider_id)
    || typeof value.provider_code !== "string"
    || !SAFE_CODE.test(value.provider_code)
    || !integer(value.allocation_bps, 1, 9_999)
    || !integer(value.target_count, 1, 100_000)
    || !integer(value.assigned_count, 0, 100_000)
    || value.assigned_count > value.target_count
  ) return null;
  return Object.freeze({ ...value });
}

/** 校验一个 arm 的统计分母、结算分区、反馈区间和 half-up 派生值。 */
function normalizeArmResult(value) {
  if (!exact(value, RESULT_KEYS) || !["a", "b"].includes(value.variant)) return null;
  const boundedBps = [
    value.gateway_success_rate_bps,
    value.quality_success_rate_bps,
  ].every((item) => item === null || integer(item, 0, 10_000));
  const nullableCounts = [
    value.average_latency_micros,
    value.p95_latency_ms,
    value.average_cost_cny_micros,
  ].every((item) => item === null || nonnegative(item));
  const counts = RESULT_KEYS.filter((key) => (
    key.endsWith("_count")
    || key === "total_latency_ms"
    || key === "gross_cost_cny_micros"
    || key === "rating_sum"
    || key === "average_rating_milli"
    || key === "adoption_rate_bps"
  ));
  if (
    !resource(value.model_id)
    || !resource(value.provider_id)
    || !SAFE_CODE.test(String(value.provider_code ?? ""))
    || typeBoolean(value.feedback_collection_enabled) === null
    || !boundedBps
    || !nullableCounts
    || !counts.every((key) => nonnegative(value[key]))
    || value.adoption_rate_bps > 10_000
    || value.gateway_success_count + value.gateway_failure_count + value.unresolved_count
      !== value.assigned_count
    || value.exposed_count + value.not_exposed_count + value.exposure_unknown_count
      !== value.assigned_count
    || value.quality_success_count + value.quality_failure_count !== value.latency_sample_count
    || value.latency_sample_count > value.exposed_count
    || value.settlement_recorded_count
      + value.settlement_not_exposed_count
      + value.settlement_unavailable_count
      + value.settlement_pending_count !== value.assigned_count
    || value.cost_known_count > value.settlement_recorded_count
    || value.cost_unknown_count > value.assigned_count
    || value.feedback_count > value.assigned_count
    || value.accepted_count > value.feedback_count
    || value.rating_sum < value.feedback_count
    || value.rating_sum > value.feedback_count * 5
    || !matchesOptionalRatio(
      value.gateway_success_rate_bps,
      value.gateway_success_count,
      value.gateway_success_count + value.gateway_failure_count,
      10_000,
    )
    || !matchesOptionalRatio(
      value.quality_success_rate_bps,
      value.quality_success_count,
      value.quality_success_count + value.quality_failure_count,
      10_000,
    )
    || !matchesOptionalRatio(
      value.average_latency_micros,
      value.total_latency_ms,
      value.latency_sample_count,
      1_000,
    )
    || !matchesOptionalRatio(
      value.average_cost_cny_micros,
      value.gross_cost_cny_micros,
      value.cost_known_count,
      1,
    )
    || value.average_rating_milli
      !== ratioHalfUp(value.rating_sum, value.feedback_count, 1_000)
    || value.adoption_rate_bps
      !== ratioHalfUp(value.accepted_count, value.feedback_count, 10_000)
    || (value.latency_sample_count === 0) !== (value.p95_latency_ms === null)
    || (value.latency_sample_count === 0 && value.total_latency_ms !== 0)
    || (value.cost_known_count === 0 && value.gross_cost_cny_micros !== 0)
    || (!value.feedback_collection_enabled && (
      value.feedback_count !== 0
      || value.rating_sum !== 0
      || value.average_rating_milli !== 0
      || value.accepted_count !== 0
      || value.adoption_rate_bps !== 0
    ))
  ) return null;
  return Object.freeze({ ...value });
}

/**
 * 执行不落浏览器缓存、也不附带 Cookie 的 JSON 请求。
 *
 * AbortError 原样交给 owner 生命周期识别；其他网络异常、非 JSON 和可缓存响应只返回固定错误。
 */
async function requestJson(url, init, expectedStatus) {
  let response;
  try {
    response = await fetch(url, { ...init, cache: "no-store", credentials: "omit" });
  } catch (error) {
    // Abort 只表示当前 owner 被替换，不能误报为服务失败或写入页面错误状态。
    if (error?.name === "AbortError") throw error;
    throw clientError("experiment.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 或空响应不允许把原始 body 当成错误文案透传到页面。
    throw clientError("experiment.response_invalid", response.ok ? 502 : response.status);
  }
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const directives = cacheDirectives(cacheControl);
  // 实验配置与统计属于租户数据，缺少 no-store/private 的响应即使 2xx 也必须拒绝。
  if (!directives.has("no-store") || !directives.has("private")) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  // 成功响应还必须精确命中 GET=200、POST=201，避免代理改写造成幂等语义漂移。
  if (response.ok && response.status !== expectedStatus) {
    throw clientError("experiment.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  // 非成功响应只读取公开 code 和链路 ID，服务端 message/diagnosis 永不透传。
  if (!response.ok) {
    const code = ERROR_CODE.test(String(payload?.code ?? ""))
      ? payload.code
      : "experiment.unavailable";
    // 统一错误对象仅保留白名单 code、HTTP status 与安全链路标识。
    throw clientError(
      code,
      response.status,
      payload?.request_id || response.headers.get("X-Request-ID"),
      payload?.trace_id || response.headers.get("X-Trace-ID"),
    );
  }
  return payload;
}

/** 构造只含 Bearer 与 JSON Accept 的认证头，空白 Token 在网络前失败。 */
function authorizationHeaders(token) {
  // Token 属于 Session 边界，非法值不得进入 Header、错误或日志。
  if (typeof token !== "string" || !token || /\s/.test(token)) {
    throw clientError("auth.token_missing", 401);
  }
  // 合法 Token 只进入本次 Authorization Header，不会写入状态或缓存键。
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

/** 校验公开实验 ID，非法 path 与当前租户不可见对象统一映射为 not_found。 */
function requireExperimentId(value) {
  if (!EXPERIMENT_ID.test(String(value ?? ""))) {
    throw clientError("experiment.not_found", 404);
  }
  return value;
}

/** 校验不透明 cursor 的长度和无空白合同，浏览器不解析其正文。 */
function requireCursor(value) {
  if (!nullableCursor(value) || value === null) {
    throw clientError("experiment.query_invalid", 422);
  }
  return value;
}

/** 校验创建请求必须携带 canonical lowercase UUIDv4。 */
function requireIdempotencyKey(value) {
  // 非 canonical UUIDv4 不能成为创建 owner，避免服务端接收可预测或歧义键。
  if (!UUID_V4.test(String(value ?? ""))) {
    throw clientError("experiment.idempotency_required", 422);
  }
  return value;
}

/** 判断对象键集合是否与公开 DTO 完全一致。 */
function exact(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/** 判断值是否为指定闭区间内的 JavaScript 安全整数。 */
function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 判断值是否为非负 JavaScript 安全整数，防止 JSON 大整数静默失真。 */
function nonnegative(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

/** 只接受真正的 JSON boolean，禁止 0/1 或字符串替代。 */
function typeBoolean(value) {
  return typeof value === "boolean" ? value : null;
}

/** 校验租户内公开资源 ID 的字符和长度边界。 */
function resource(value) {
  return typeof value === "string" && RESOURCE_ID.test(value);
}

/** 校验 request_id/trace_id 的安全可展示字符集。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

/** 校验 canonical UTC 时间并拒绝 JavaScript 无法解析的日期。 */
function utc(value) {
  return utcMicros(value) !== null;
}

/** 校验可空 UTC 时间字段。 */
function nullableUtc(value) {
  return value === null || utc(value);
}

/** 将 canonical UTC 时间解析为整数微秒，并拒绝 Date 自动归一化的非法日历日期。 */
function utcMicros(value) {
  if (typeof value !== "string") return null;
  const match = UTC_TIME.exec(value);
  if (!match) return null;
  const milliseconds = Date.parse(`${match[1]}Z`);
  // 解析后年月日时分秒必须原样一致，2 月 30 日或 24:00 等归一化输入不能被接纳。
  if (
    !Number.isFinite(milliseconds)
    || new Date(milliseconds).toISOString().slice(0, 19) !== match[1]
  ) return null;
  const fraction = (match[2] ?? "").padEnd(6, "0") || "0";
  return BigInt(milliseconds) * 1_000n + BigInt(fraction);
}

/** 校验只在内存和 query 中短暂存在的私有 cursor。 */
function nullableCursor(value) {
  return value === null || (typeof value === "string" && PRIVATE_CURSOR.test(value));
}

/** 用固定字典创建 AuthApiError，不透传服务端 message、diagnosis 或响应正文。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "experiment.unavailable";
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "模型评测请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

/** 只提取 Cache-Control 裸 directive；带参数的 no-store/private 不能伪装成本合同安全头。 */
function cacheDirectives(value) {
  return new Set(
    String(value)
      .split(",")
      .map((directive) => directive.trim())
      .filter((directive) => directive && !directive.includes("=")),
  );
}

/** 校验创建响应仍是本次 exact body 的同一冻结配置，防止错误幂等记录被页面接受。 */
function experimentMatchesCreateRequest(experiment, request) {
  const expectedATarget = variantATarget(
    request.target_sample_count,
    request.model_a_allocation_bps,
  );
  return expectedATarget !== null
    && experiment.name === request.name
    && experiment.project_id === request.project_id
    && experiment.environment_id === request.environment_id
    && experiment.model_alias_id === request.model_alias_id
    && experiment.model_a.model_id === request.model_a_id
    && experiment.model_b.model_id === request.model_b_id
    && experiment.model_a.allocation_bps === request.model_a_allocation_bps
    && experiment.model_b.allocation_bps === 10_000 - request.model_a_allocation_bps
    && experiment.model_a.target_count === expectedATarget
    && experiment.model_b.target_count === request.target_sample_count - expectedATarget
    && experiment.target_sample_count === request.target_sample_count
    && experiment.traffic_sample_bps === request.traffic_sample_bps
    && experiment.sampling_duration_seconds === request.sampling_duration_seconds
    && experiment.feedback_collection_enabled === request.feedback_collection_enabled
    && experiment.feedback_window_seconds === request.feedback_window_seconds;
}

/** 按设计的整数 half-up 计算 A 侧目标数，非法配置返回 null。 */
function variantATarget(targetCount, allocationBps) {
  if (!integer(targetCount, 2, 100_000) || !integer(allocationBps, 1, 9_999)) return null;
  const target = ratioHalfUp(targetCount, 10_000, allocationBps);
  return integer(target, 1, targetCount - 1) ? target : null;
}

/** 验证反馈开关与窗口秒数必须成对，关闭时固定为零。 */
function validFeedbackWindow(value) {
  return value.feedback_collection_enabled
    ? integer(value.feedback_window_seconds, 60, 604_800)
    : value.feedback_window_seconds === 0;
}

/** 验证 active、collecting_feedback、completed 三阶段的 nullable 时间矩阵。 */
function validExperimentTimeline(value) {
  const started = utcMicros(value.started_at);
  const samplingDeadline = utcMicros(value.sampling_deadline);
  const created = utcMicros(value.created_at);
  const updated = utcMicros(value.updated_at);
  const samplingEnded = value.sampling_ended_at === null ? null : utcMicros(value.sampling_ended_at);
  const feedbackDeadline = value.feedback_deadline === null ? null : utcMicros(value.feedback_deadline);
  const settlementDeadline = value.settlement_deadline === null ? null : utcMicros(value.settlement_deadline);
  const completed = value.completed_at === null ? null : utcMicros(value.completed_at);
  if (
    [started, samplingDeadline, created, updated].some((timestamp) => timestamp === null)
    || started !== created
    || samplingDeadline !== started + BigInt(value.sampling_duration_seconds) * 1_000_000n
    || updated < created
  ) return false;
  if (value.status === "active") {
    return [samplingEnded, feedbackDeadline, settlementDeadline, completed]
      .every((timestamp) => timestamp === null);
  }
  if (
    samplingEnded === null
    || feedbackDeadline === null
    || settlementDeadline === null
    || samplingEnded < started
    || samplingEnded > samplingDeadline
    || feedbackDeadline !== samplingEnded + BigInt(value.feedback_window_seconds) * 1_000_000n
    || settlementDeadline < feedbackDeadline
  ) return false;
  if (updated < samplingEnded) return false;
  return value.status === "collecting_feedback"
    ? completed === null
    : completed !== null && completed >= feedbackDeadline && updated >= completed;
}

/** 校验 nullable 指标是否等于相同分子分母的整数 half-up 结果。 */
function matchesOptionalRatio(value, numerator, denominator, scale) {
  if (denominator === 0) return value === null;
  return value === ratioHalfUp(numerator, denominator, scale);
}

/** 使用 BigInt 保持与 Shared 任意精度整数 half-up 完全一致，再收口为安全 Number。 */
function ratioHalfUp(numerator, denominator, scale) {
  if (!nonnegative(numerator) || !nonnegative(denominator) || !nonnegative(scale)) return null;
  if (denominator === 0) return 0;
  const result = (
    2n * BigInt(numerator) * BigInt(scale) + BigInt(denominator)
  ) / (2n * BigInt(denominator));
  return result <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(result) : null;
}
