/** PAGE-118 客户健康客户端只接受冻结的租户聚合合同，并在展示前完整重算公开投影。 */

import { AuthApiError } from "./auth.js";

export const CUSTOMER_HEALTH_SIGNAL_CODES = Object.freeze([
  "usage_trend",
  "reliability",
  "cost_anomaly_health",
  "budget_readiness",
  "team_activity",
  "ticket_health",
]);

export const CUSTOMER_HEALTH_RISK_TAGS = Object.freeze([
  "usage_declining",
  "high_error_rate",
  "cost_anomaly_open",
  "budget_missing",
  "team_inactive",
]);

// 六信号权重与来源类型属于版本化合同，客户端必须逐项核对且不得接受未知来源。
const SIGNAL_WEIGHTS = Object.freeze([2500, 2500, 1500, 1000, 1500, 1000]);
const SOURCE_KINDS = Object.freeze([
  "request_log_aggregate",
  "request_log_aggregate",
  "cost_anomaly_aggregate",
  "budget_rule_aggregate",
  "user_activity_aggregate",
  "support_ticket_unconfigured",
]);
// 风险标签只映射到冻结建议代码，不能携带任意地址或服务端返回的可执行内容。
const TAG_ACTIONS = Object.freeze({
  usage_declining: "restore_gateway_activity",
  high_error_rate: "review_failed_requests",
  cost_anomaly_open: "review_cost_anomalies",
  budget_missing: "configure_tenant_budget",
  team_inactive: "reengage_team",
});
// 建议目标采用固定导航白名单，最终仍需与后端实时导航镜像交叉核验。
const ACTION_TARGETS = Object.freeze({
  restore_gateway_activity: "request-logs",
  review_failed_requests: "request-logs",
  review_cost_anomalies: "cost-anomalies",
  configure_tenant_budget: "budgets",
  reengage_team: "users",
  connect_support_ticket_source: null,
  maintain_current_adoption: null,
});
// 顶层字段采用 exact DTO 校验，拒绝额外字段夹带敏感信息或未来未识别语义。
const TOP_LEVEL_KEYS = Object.freeze([
  "tenant_id", "customer_health_score_id", "score_version", "score_bps", "risk_level",
  "risk_tags", "missing_signals", "effective_weight_bps", "freshness_status",
  "recommended_actions", "window", "signals", "activation", "evaluated_at", "generated_at",
  "request_id", "trace_id",
]);
// 嵌套 DTO、错误状态和诊断字段均冻结键集合，防止宽松解析掩盖合同漂移。
const WINDOW_KEYS = Object.freeze(["start", "end", "comparison_start", "comparison_end", "timezone"]);
const SIGNAL_KEYS = Object.freeze([
  "code", "weight_bps", "availability", "health_status", "score_bps", "contribution_bps",
  "current_value", "comparison_value", "rate_bps", "source_kind",
]);
const ACTION_KEYS = Object.freeze(["code", "target_navigation_item_id"]);
const ACTIVATION_KEYS = Object.freeze(["completed_stage_count", "total_stage_count", "activated"]);
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
  "customer_health.request_invalid": 422,
  "customer_health.not_ready": 503,
  "customer_health.unavailable": 503,
});
// 页面错误文案由固定错误码选择，绝不透传异常正文、Token 或上游响应。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有客户健康读取权限",
  "customer_health.request_invalid": "客户健康请求不合法",
  "customer_health.not_ready": "客户健康评分尚未就绪",
  "customer_health.unavailable": "客户健康评分暂不可用",
  "customer_health.response_invalid": "客户健康响应合同不完整",
});
// 风险枚举、标识格式、计数上限与有效权重共同约束公开评分的安全边界。
const RISK_LEVELS = Object.freeze(["healthy", "watch", "at_risk", "critical"]);
const HEALTH_STATUSES = Object.freeze([...RISK_LEVELS, "unknown"]);
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$/;
const SCORE_ID = /^chs_[0-9a-f]{32}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UTC_SECOND = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const MAX_COUNT = 922337203685477;
const EFFECTIVE_WEIGHT_BPS = 9000;

/** 读取已认证租户的最新已提交评分，请求不携带业务查询参数或正文。 */
export async function getCustomerHealth({ controlApiUrl, token, tenantId, signal }) {
  requireTenant(tenantId);
  const payload = await requestJson(`${controlApiUrl}/customer-health`, {
    headers: authorizationHeaders(token),
    signal,
  });
  const normalized = normalizeCustomerHealth(payload, tenantId);
  // 响应若未通过租户、字段和公式复算，统一转为固定合同错误，不能展示原始负载。
  if (!normalized) {
    throw clientError(
      "customer_health.response_invalid",
      502,
      payload?.request_id,
      payload?.trace_id,
    );
  }
  return normalized;
}

/** 校验每个公开字段并完整重算 customer-health-v1 投影，任何偏差都返回空结果。 */
export function normalizeCustomerHealth(payload, tenantId) {
  if (
    !exact(payload, TOP_LEVEL_KEYS)
    || payload.tenant_id !== tenantId
    || !safeId(payload.tenant_id)
    || !SCORE_ID.test(String(payload.customer_health_score_id ?? ""))
    || payload.score_version !== "customer-health-v1"
    || payload.effective_weight_bps !== EFFECTIVE_WEIGHT_BPS
    || !RISK_LEVELS.includes(payload.risk_level)
    || !["fresh", "stale"].includes(payload.freshness_status)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;

  const window = normalizeWindow(payload.window);
  const evaluatedAt = utcSecond(payload.evaluated_at);
  const generatedAt = utcSecond(payload.generated_at);
  if (
    !window
    || evaluatedAt === null
    || generatedAt === null
    || evaluatedAt < window.endEpoch
    || generatedAt < evaluatedAt
    || window.endEpoch > generatedAt
    || payload.freshness_status !== (generatedAt - window.endEpoch <= 2 * 60 * 60 * 1000 ? "fresh" : "stale")
  ) return null;

  if (!Array.isArray(payload.signals) || payload.signals.length !== CUSTOMER_HEALTH_SIGNAL_CODES.length) return null;
  const signals = [];
  for (let index = 0; index < CUSTOMER_HEALTH_SIGNAL_CODES.length; index += 1) {
    const signal = normalizeSignal(payload.signals[index], index);
    if (!signal) return null;
    signals.push(signal);
  }

  const expectedContributions = calculateContributions(signals);
  if (!expectedContributions) return null;
  for (let index = 0; index < signals.length; index += 1) {
    if (signals[index].contribution_bps !== expectedContributions.values[index]) return null;
  }
  if (
    !integer(payload.score_bps, 0, 10000)
    || payload.score_bps !== expectedContributions.score
  ) return null;

  const expectedTags = calculateRiskTags(signals);
  if (!sameArray(payload.risk_tags, expectedTags)) return null;
  if (!sameArray(payload.missing_signals, ["ticket_health"])) return null;
  if (payload.risk_level !== calculateRiskLevel(payload.score_bps, signals)) return null;

  const actions = normalizeActions(payload.recommended_actions, expectedTags, payload.missing_signals);
  const activation = normalizeActivation(payload.activation);
  if (!actions || !activation) return null;

  return Object.freeze({
    tenant_id: payload.tenant_id,
    customer_health_score_id: payload.customer_health_score_id,
    score_version: payload.score_version,
    score_bps: payload.score_bps,
    risk_level: payload.risk_level,
    risk_tags: Object.freeze([...payload.risk_tags]),
    missing_signals: Object.freeze([...payload.missing_signals]),
    effective_weight_bps: payload.effective_weight_bps,
    freshness_status: payload.freshness_status,
    recommended_actions: Object.freeze(actions),
    window: Object.freeze({
      start: payload.window.start,
      end: payload.window.end,
      comparison_start: payload.window.comparison_start,
      comparison_end: payload.window.comparison_end,
      timezone: payload.window.timezone,
    }),
    signals: Object.freeze(signals),
    activation,
    evaluated_at: payload.evaluated_at,
    generated_at: payload.generated_at,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 校验相邻两个七日 UTC 窗口及整点边界，拒绝本地时区或重叠区间。 */
function normalizeWindow(value) {
  if (!exact(value, WINDOW_KEYS) || value.timezone !== "UTC") return null;
  const startEpoch = utcSecond(value.start);
  const endEpoch = utcSecond(value.end);
  const comparisonStartEpoch = utcSecond(value.comparison_start);
  const comparisonEndEpoch = utcSecond(value.comparison_end);
  const sevenDays = 7 * 24 * 60 * 60 * 1000;
  if (
    startEpoch === null
    || endEpoch === null
    || comparisonStartEpoch === null
    || comparisonEndEpoch === null
    || endEpoch - startEpoch !== sevenDays
    || comparisonEndEpoch - comparisonStartEpoch !== sevenDays
    || comparisonEndEpoch !== startEpoch
    || new Date(endEpoch).getUTCMinutes() !== 0
    || new Date(endEpoch).getUTCSeconds() !== 0
  ) return null;
  return { startEpoch, endEpoch, comparisonStartEpoch, comparisonEndEpoch };
}

/** 按冻结顺序校验单个信号的字段、权重、来源和对应业务公式。 */
function normalizeSignal(value, index) {
  if (
    !exact(value, SIGNAL_KEYS)
    || value.code !== CUSTOMER_HEALTH_SIGNAL_CODES[index]
    || value.weight_bps !== SIGNAL_WEIGHTS[index]
    || value.source_kind !== SOURCE_KINDS[index]
    || !["available", "no_samples", "unavailable"].includes(value.availability)
    || !HEALTH_STATUSES.includes(value.health_status)
    || !integer(value.contribution_bps, 0, 10000)
  ) return null;
  const valid = [
    validUsageSignal,
    validReliabilitySignal,
    validCostSignal,
    validBudgetSignal,
    validTeamSignal,
    validTicketSignal,
  ][index](value);
  return valid ? Object.freeze({ ...value }) : null;
}

/** 校验用量趋势信号，并把双零样本固定解释为无样本而非正常增长。 */
function validUsageSignal(value) {
  if (!count(value.current_value) || !count(value.comparison_value)) return false;
  if (value.current_value === 0 && value.comparison_value === 0) {
    return value.availability === "no_samples"
      && value.health_status === "critical"
      && value.score_bps === 0
      && value.rate_bps === null;
  }
  const expectedRate = value.comparison_value === 0
    ? 10000
    : Math.min(10000, floorRatio(value.current_value, value.comparison_value));
  return value.availability === "available"
    && value.rate_bps === expectedRate
    && value.score_bps === expectedRate
    && value.health_status === healthForScore(expectedRate);
}

/** 校验成功数、请求数和可靠性比率，禁止成功数超过总请求数。 */
function validReliabilitySignal(value) {
  if (!count(value.current_value) || !count(value.comparison_value) || value.current_value > value.comparison_value) return false;
  if (value.comparison_value === 0) {
    return value.current_value === 0
      && value.availability === "no_samples"
      && value.health_status === "unknown"
      && value.score_bps === 5000
      && value.rate_bps === null;
  }
  const expectedRate = roundedRatio(value.current_value, value.comparison_value);
  return value.availability === "available"
    && value.rate_bps === expectedRate
    && value.score_bps === expectedRate
    && value.health_status === healthForScore(expectedRate);
}

/** 校验成本异常的未解决与严重计数，并复算对应离散健康分值。 */
function validCostSignal(value) {
  if (
    value.availability !== "available"
    || !count(value.current_value)
    || !count(value.comparison_value)
    || value.comparison_value > value.current_value
    || value.rate_bps !== null
  ) return false;
  const expectedScore = value.comparison_value > 0 ? 0 : value.current_value > 0 ? 4000 : 10000;
  return value.score_bps === expectedScore && value.health_status === healthForScore(expectedScore);
}

/** 校验有效预算规则计数，禁止比较值或比率进入预算就绪信号。 */
function validBudgetSignal(value) {
  if (
    value.availability !== "available"
    || !count(value.current_value)
    || value.comparison_value !== null
    || value.rate_bps !== null
  ) return false;
  const expectedScore = value.current_value > 0 ? 10000 : 3000;
  return value.score_bps === expectedScore && value.health_status === healthForScore(expectedScore);
}

/** 校验近期活跃用户与可用用户总数，并严格复算团队活跃比率。 */
function validTeamSignal(value) {
  if (!count(value.current_value) || !count(value.comparison_value) || value.current_value > value.comparison_value) return false;
  if (value.comparison_value === 0) {
    return value.current_value === 0
      && value.availability === "no_samples"
      && value.health_status === "critical"
      && value.score_bps === 0
      && value.rate_bps === null;
  }
  const expectedRate = roundedRatio(value.current_value, value.comparison_value);
  return value.availability === "available"
    && value.rate_bps === expectedRate
    && value.score_bps === expectedRate
    && value.health_status === healthForScore(expectedRate);
}

/** 固定工单信号为未接入且无计数，防止尚未交付的数据源被冒充可用。 */
function validTicketSignal(value) {
  return value.availability === "unavailable"
    && value.health_status === "unknown"
    && value.score_bps === null
    && value.contribution_bps === 0
    && value.current_value === null
    && value.comparison_value === null
    && value.rate_bps === null;
}

/** 使用整数最大余数法重算信号贡献，确保贡献和与四舍五入总分完全一致。 */
function calculateContributions(signals) {
  const available = signals.map((signal, index) => ({ signal, index })).filter(({ signal }) => signal.score_bps !== null);
  if (available.reduce((sum, { signal }) => sum + signal.weight_bps, 0) !== EFFECTIVE_WEIGHT_BPS) return null;
  const denominator = BigInt(EFFECTIVE_WEIGHT_BPS);
  const rows = available.map(({ signal, index }) => {
    const raw = BigInt(signal.score_bps) * BigInt(signal.weight_bps);
    return { index, floor: Number(raw / denominator), remainder: raw % denominator };
  });
  const rawTotal = available.reduce(
    (sum, { signal }) => sum + BigInt(signal.score_bps) * BigInt(signal.weight_bps),
    0n,
  );
  const score = Number((rawTotal + denominator / 2n) / denominator);
  const values = Array(signals.length).fill(0);
  for (const row of rows) values[row.index] = row.floor;
  let remaining = score - rows.reduce((sum, row) => sum + row.floor, 0);
  const ranked = [...rows].sort((left, right) => (
    left.remainder === right.remainder ? left.index - right.index : left.remainder > right.remainder ? -1 : 1
  ));
  for (let index = 0; index < remaining; index += 1) values[ranked[index].index] += 1;
  return { score, values };
}

/** 按冻结阈值和固定顺序重算风险标签，不信任服务端直接返回的标签数组。 */
function calculateRiskTags(signals) {
  const [usage, reliability, cost, budget, team] = signals;
  const tags = [];
  // 用量归零或下降过半都会形成采用风险，避免高总分掩盖核心使用流失。
  if (usage.current_value === 0 || (usage.rate_bps !== null && usage.rate_bps < 5000)) tags.push("usage_declining");
  if (
    reliability.comparison_value >= 5
    && reliability.rate_bps !== null
    && 10000 - reliability.rate_bps >= 500
  ) tags.push("high_error_rate");
  // 任一未解决成本异常都必须保留风险标签，以便用量与成本失败持续可追踪。
  if (cost.current_value > 0) tags.push("cost_anomaly_open");
  if (budget.current_value === 0) tags.push("budget_missing");
  if (
    team.current_value === 0
    || (team.comparison_value >= 2 && team.rate_bps !== null && team.rate_bps < 5000)
  ) tags.push("team_inactive");
  return tags;
}

/** 将总分等级与用量、失败率、成本异常下限合并，风险只能向更严重方向提升。 */
function calculateRiskLevel(score, signals) {
  let rank = riskRank(healthForScore(score));
  const [usage, reliability, cost] = signals;
  // 当前完全无用量时至少为 at_risk，不能被其他高分信号抵消。
  if (usage.current_value === 0) rank = Math.max(rank, riskRank("at_risk"));
  if (reliability.comparison_value >= 5 && reliability.rate_bps !== null) {
    const errorRate = 10000 - reliability.rate_bps;
    // 有足够样本时按失败率设置风险下限，保证失败请求在健康结论中可见。
    if (errorRate >= 1000) rank = Math.max(rank, riskRank("at_risk"));
    else if (errorRate >= 500) rank = Math.max(rank, riskRank("watch"));
  }
  if (cost.comparison_value > 0) rank = riskRank("critical");
  else if (cost.current_value > 0) rank = Math.max(rank, riskRank("watch"));
  return RISK_LEVELS[rank];
}

/** 从已复算风险标签推导建议列表，并逐项核对可选导航目标白名单。 */
function normalizeActions(value, tags, missingSignals) {
  if (!Array.isArray(value)) return null;
  const expectedCodes = tags.map((tag) => TAG_ACTIONS[tag]);
  if (missingSignals.includes("ticket_health")) expectedCodes.push("connect_support_ticket_source");
  if (expectedCodes.length === 0) expectedCodes.push("maintain_current_adoption");
  if (value.length !== expectedCodes.length) return null;
  const result = [];
  for (let index = 0; index < expectedCodes.length; index += 1) {
    const action = value[index];
    const code = expectedCodes[index];
    const expectedTarget = ACTION_TARGETS[code];
    if (
      !exact(action, ACTION_KEYS)
      || action.code !== code
      || !(action.target_navigation_item_id === null || action.target_navigation_item_id === expectedTarget)
      || (expectedTarget === null && action.target_navigation_item_id !== null)
    ) return null;
    result.push(Object.freeze({ ...action }));
  }
  return result;
}

/** 校验七阶段激活摘要，完成布尔值必须与完成阶段数严格一致。 */
function normalizeActivation(value) {
  if (
    !exact(value, ACTIVATION_KEYS)
    || !integer(value.completed_stage_count, 0, 7)
    || value.total_stage_count !== 7
    || typeof value.activated !== "boolean"
    || value.activated !== (value.completed_stage_count === 7)
  ) return null;
  return Object.freeze({ ...value });
}

/** 发送禁止缓存和凭据旁带的 GET，并把网络或合同异常收敛为固定错误码。 */
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
    // 主动中止要原样交给 owner 生命周期处理，其他网络错误不得泄露异常正文。
    if (error?.name === "AbortError") throw error;
    throw clientError("customer_health.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 响应无法满足统一错误合同，只返回固定响应无效错误。
    throw clientError("customer_health.response_invalid", 502);
  }
  // 头体 request_id、trace_id 或缓存边界不一致时，响应整体失败关闭。
  if (!validBoundary(response, payload)) throw clientError("customer_health.response_invalid", 502);
  if (response.ok) {
    // 成功负载只接受精确 200，避免其他成功状态绕过 DTO 语义。
    if (response.status !== 200) {
      throw clientError("customer_health.response_invalid", 502, payload.request_id, payload.trace_id);
    }
    return payload;
  }
  // 错误码必须与 HTTP 状态精确匹配，未知服务端错误不能直接进入页面。
  if (!validError(payload) || ERROR_STATUS[payload.code] !== response.status) {
    throw clientError("customer_health.response_invalid", 502);
  }
  // 已校验错误仍只投影固定字段，不携带服务端 message 或 diagnosis 正文。
  throw clientError(payload.code, response.status, payload.request_id, payload.trace_id);
}

/** 核验禁止缓存与请求链头体一致性，防止跨请求或代理缓存数据被接纳。 */
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

/** 校验统一错误和诊断键集合，仅确认结构而不展示诊断正文。 */
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

/** 只接受无空白的非空 Token 并构造认证头，非法值统一返回认证错误。 */
function authorizationHeaders(token) {
  // Token 格式不合法时在发起请求前失败，防止意外值进入网络或日志。
  if (typeof token !== "string" || !token || /\s/u.test(token)) {
    throw clientError("auth.token_missing", 401);
  }
  // Token 只进入 Authorization 请求头，客户端状态和错误对象均不保留其值。
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

/** 校验调用方租户标识格式，空值或异常字符直接阻断请求。 */
function requireTenant(value) {
  // tenant_id 仅用于核对响应归属，非法标识不能发起客户健康读取。
  if (!safeId(value)) throw clientError("customer_health.request_invalid", 422);
}

/** 构造固定 AuthApiError，只保留安全错误码和已校验的请求链标识。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "customer_health.unavailable";
  // 错误对象不接纳上游 message、Prompt、Response 或任意诊断正文。
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "客户健康请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}

/** 使用整数四舍五入计算基点比率，避免浮点差异破坏前后端一致性。 */
function roundedRatio(numerator, denominator) {
  return Number(
    (BigInt(numerator) * 10000n + BigInt(denominator) / 2n) / BigInt(denominator),
  );
}

/** 使用整数向下取整计算趋势比率，保持与服务端冻结公式一致。 */
function floorRatio(numerator, denominator) {
  return Number((BigInt(numerator) * 10000n) / BigInt(denominator));
}

/** 按冻结阈值把基点分数映射到四档健康状态。 */
function healthForScore(score) {
  if (score >= 8000) return "healthy";
  if (score >= 6000) return "watch";
  if (score >= 3000) return "at_risk";
  return "critical";
}

/** 将风险等级映射为单调序号，供风险下限只向严重方向提升。 */
function riskRank(value) {
  return RISK_LEVELS.indexOf(value);
}

/** 严格解析 UTC 整秒文本并拒绝无效日期或非规范时间表示。 */
function utcSecond(value) {
  if (typeof value !== "string" || !UTC_SECOND.test(value)) return null;
  const epoch = Date.parse(value);
  if (!Number.isFinite(epoch) || new Date(epoch).toISOString() !== `${value.slice(0, -1)}.000Z`) return null;
  return epoch;
}

/** 校验数据库安全计数范围，拒绝负数、浮点数和超大整数。 */
function count(value) {
  return integer(value, 0, MAX_COUNT);
}

/** 判断数值是否位于指定安全整数闭区间。 */
function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 判断对象是否精确包含冻结键集合，额外字段与缺失字段均不接受。 */
function exact(value, keys) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()));
}

/** 按顺序比较冻结数组，保证风险标签和缺失信号保持规范顺序。 */
function sameArray(value, expected) {
  return Array.isArray(value)
    && JSON.stringify(value) === JSON.stringify(expected);
}

/** 读取并清理单个响应头，非字符串值一律视为缺失。 */
function header(headers, name) {
  const value = headers?.get?.(name) ?? headers?.get?.(name.toLowerCase()) ?? "";
  return typeof value === "string" ? value.trim() : "";
}

/** 校验租户等公开标识只包含合同允许的安全字符。 */
function safeId(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

/** 校验 request_id 与 trace_id 的长度和安全字符集合。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}
