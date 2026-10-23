/**
 * 模块职责：封装 PAGE-098 高危会话列表与处置 API，并把 wire 响应投影为固定安全 DTO。
 * 安全边界：tenant、actor、任意 target、Session Header、Prompt、Response 和完整 HMAC
 * 都不得由浏览器提交或保存；真实权限、租户隔离、状态迁移与幂等仍由 Control API 强制执行。
 */

import { AuthApiError } from "./auth.js";
import { authenticatedResourceFetch } from "./resourceAuthentication.js";

// 查询、运行状态与处置枚举是冻结 wire 合同；未知值必须在 fetch 或 DOM 之前失败关闭。
const STATUS_VALUES = new Set(["high_risk", "frozen", "resolved", "expired"]);
const SCOPE_VALUES = new Set(["enterprise_chat", "api_session"]);
const RUNTIME_VALUES = new Set(["disabled", "degraded", "unavailable", "active"]);
const ACTION_VALUES = new Set([
  "freeze_session",
  "freeze_virtual_key",
  "dismiss",
  "release_session",
  "release_virtual_key",
]);
// 历史动作比可提交动作多出系统阈值和过期事实，不能复用 ACTION_VALUES 后静默漏审。
const ACTION_HISTORY_VALUES = new Set([
  "high_risk",
  ...ACTION_VALUES,
  "expire",
]);
// 原因码是审计证据合同，不接受服务端自由文案或未来未知枚举直接进入页面。
const REASON_VALUES = new Set([
  "threshold_reached",
  "confirmed_attack",
  "false_positive",
  "verified_safe",
  "incident_contained",
  "window_expired",
]);
// actor、overlay target/status 与历史状态各自独立校验，防止合法值跨字段混用。
const ACTOR_TYPES = new Set(["system", "user"]);
const FREEZE_TARGETS = new Set(["session", "virtual_key"]);
const FREEZE_STATUSES = new Set(["active", "released"]);
const HISTORY_STATUSES = new Set(["monitoring", ...STATUS_VALUES]);

// 每个对象都使用 exact-key 校验，防止服务端漂移把正文、摘要或网络元数据夹带进页面状态。
const LIST_KEYS = Object.freeze([
  "runtime_status", "summary", "session_risks", "next_cursor", "request_id", "trace_id",
]);
const SUMMARY_KEYS = Object.freeze(["high_risk_count", "frozen_count", "resolved_count"]);
const RISK_KEYS = Object.freeze([
  "session_risk_score_id",
  "scope_type",
  "scope_reference",
  "virtual_key_id",
  "status",
  "score",
  "score_scale",
  "risky_turn_count",
  "signal_counts",
  "state_revision",
  "threshold_version",
  "last_source_request_id",
  "window_expires_at",
  "first_high_risk_at",
  "frozen_at",
  "updated_at",
  "freezes",
  "actions",
]);
// 子对象同样执行 exact-key 校验，避免嵌套层夹带命中片段或内部目标摘要。
const SIGNAL_KEYS = Object.freeze(["jailbreak", "sensitive", "tool_inducement"]);
const FREEZE_KEYS = Object.freeze([
  "session_risk_freeze_id", "target_type", "status", "state_revision", "created_at", "released_at",
]);
// action 白名单只包含 Console 时间线可公开字段，不包含 trace、幂等摘要或 actor 内部 ID。
const ACTION_KEYS = Object.freeze([
  "session_risk_action_id",
  "action",
  "reason_code",
  "actor_type",
  "actor_reference",
  "session_risk_freeze_id",
  "status_before",
  "status_after",
  "score_snapshot",
  "state_revision",
  "source_request_id",
  "created_at",
]);
// 动作响应必须同时带当前 score 和不可变 action，供客户端复核处置意图与最新投影。
const ACTION_RESPONSE_KEYS = Object.freeze([
  "session_risk", "action", "idempotent_replay", "request_id", "trace_id",
]);

// 对象 ID 与公开短引用采用有界 ASCII 形状；API session 明确拒绝 64 hex 完整 HMAC。
const SCORE_ID_PATTERN = /^srs_[0-9a-f]{32}$/;
const FREEZE_ID_PATTERN = /^srf_[0-9a-f]{32}$/;
const ACTION_ID_PATTERN = /^sra_[0-9a-f]{32}$/;
const CHAT_SESSION_ID_PATTERN = /^chs_[0-9a-f]{32}$/;
// API scope 只能显示独立 api_ 命名空间的 16 位前缀，不能接受内部 srh_ 完整摘要。
const API_SCOPE_REFERENCE_PATTERN = /^api_[0-9a-f]{16}$/;
// Control API 只接受 Python UUID 的 canonical 小写文本；前缀 ID、大小写和非规范变体均在 fetch 前拒绝。
const VIRTUAL_KEY_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// 普通链路/资源 ID 仍是有界安全 ASCII，不能复用 UUID 规则误拒 request_id。
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,160}$/;
const SAFE_ENUM_PATTERN = /^[a-z][a-z0-9_.-]{0,127}$/;
// 幂等键只驻留动作闭包，字符白名单同时排除换行和 Header 注入。
const IDEMPOTENCY_PATTERN = /^[A-Za-z0-9._:-]{8,128}$/;
const MAX_COLLECTION_SIZE = 1_000;

// 统一错误文案只由 code 决定；服务端自由 message、代理正文和异常消息均不进入页面。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有会话风险权限",
  "session_risk.query_invalid": "高危会话筛选不合法",
  "session_risk.not_found": "当前页面找不到该高危会话",
  "session_risk.payload_invalid": "风险处置请求不合法",
  "session_risk.idempotency_invalid": "风险处置幂等标识不合法",
  "session_risk.idempotency_conflict": "该处置标识已用于其他意图",
  "session_risk.state_conflict": "会话状态已变化，请刷新后重试",
  "session_risk.target_already_frozen": "该冻结目标已经生效",
  "session_risk.store_unavailable": "风险状态存储暂不可用",
  "session_risk.evidence_unavailable": "风险处置证据暂无法提交",
  "session_risk.response_invalid": "高危会话响应合同不完整",
});

/**
 * 查询当前认证租户的高危会话快照。
 *
 * @param {object} options Control 地址、Token、白名单筛选、内存 cursor 与 AbortSignal。
 * @returns {Promise<object>} 已 exact-key 校验并深度投影的安全列表响应。
 *
 * tenant、actor、排序字段和 include_prompt 即使由调用方附加，也不会进入 URL。
 */
export async function listSessionRisks({
  controlApiUrl,
  authentication,
  token,
  status,
  scopeType,
  virtualKeyId,
  fromTime,
  toTime,
  limit = 20,
  cursor,
  operationRequestId,
  operationTraceId,
  signal,
}) {
  const url = new URL(`${controlApiUrl}/security/session-risks`);
  setOptionalEnum(url, "status", status, STATUS_VALUES);
  setOptionalEnum(url, "scope_type", scopeType, SCOPE_VALUES);
  setOptionalVirtualKey(url, virtualKeyId);
  setOptionalTime(url, "from_time", fromTime);
  setOptionalTime(url, "to_time", toTime);
  url.searchParams.set("limit", String(requireLimit(limit)));
  setOptionalCursor(url, cursor);
  ensureTimeRange(fromTime, toTime);

  const response = await authenticatedResourceFetch(url.toString(), {
    authentication,
    token,
    signal,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "X-Request-ID": operationRequestId || createSessionRiskRequestId("list"),
      "X-Trace-ID": operationTraceId || createSessionRiskTraceId(),
    },
  });
  const payload = await readJson(response);
  throwSessionRiskApiError(response, payload, "高危会话读取失败");
  const normalized = normalizeSessionRiskListResponse(payload);
  if (!normalized) {
    // 代理返回成功但结构漂移时按 502 失败关闭，不把未经校验的部分数据交给页面。
    throw sessionRiskClientError(
      "session_risk.response_invalid",
      "高危会话响应合同不完整",
      502,
      safeId(payload.request_id),
      safeId(payload.trace_id),
    );
  }
  return normalized;
}

/**
 * 对一条当前页风险 score 执行固定处置。
 *
 * @param {object} options score ID、动作、固定原因、CAS revision、幂等键和请求 owner。
 * @returns {Promise<object>} 更新后的 score 与 append-only action 安全投影。
 *
 * 动作名已经编码 target，body 不接受 tenant、actor、target ID 或任意自由文本。
 */
export async function applySessionRiskAction({
  controlApiUrl,
  authentication,
  token,
  sessionRiskScoreId,
  action,
  reasonCode,
  expectedRevision,
  idempotencyKey,
  operationRequestId,
  operationTraceId,
  signal,
}) {
  const scoreId = requireScoreId(sessionRiskScoreId);
  const body = normalizeSessionRiskActionBody({ action, reasonCode, expectedRevision });
  const safeIdempotencyKey = requireIdempotencyKey(idempotencyKey);
  const response = await authenticatedResourceFetch(`${controlApiUrl}/security/session-risks/${scoreId}/action`, {
    authentication,
    token,
    method: "POST",
    signal,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": safeIdempotencyKey,
      "X-Request-ID": operationRequestId || createSessionRiskRequestId("action"),
      "X-Trace-ID": operationTraceId || createSessionRiskTraceId(),
    },
    body: JSON.stringify(body),
  });
  const payload = await readJson(response);
  throwSessionRiskApiError(response, payload, "风险处置提交失败");
  const normalized = normalizeSessionRiskActionResponse(payload, {
    scoreId,
    action: body.action,
    reasonCode: body.reason_code,
    expectedRevision: body.expected_revision,
  });
  if (!normalized) {
    // 动作可能已经提交，响应结构异常仍需保留链路 ID 并由原幂等键恢复确认。
    throw sessionRiskClientError(
      "session_risk.response_invalid",
      "风险处置响应合同不完整",
      502,
      safeId(payload.request_id),
      safeId(payload.trace_id),
    );
  }
  return normalized;
}

/** 按 PAGE-098 exact-key 合同校验并深度冻结列表响应；非法结构返回 null。 */
export function normalizeSessionRiskListResponse(payload) {
  if (!hasExactKeys(payload, LIST_KEYS)) return null;
  const summary = normalizeSummary(payload.summary);
  if (
    !RUNTIME_VALUES.has(payload.runtime_status)
    || !summary
    || !Array.isArray(payload.session_risks)
    || payload.session_risks.length > 100
    || !safeCursor(payload.next_cursor)
    || !safeId(payload.request_id)
    || !safeId(payload.trace_id)
  ) return null;
  const sessionRisks = payload.session_risks.map(normalizeRisk);
  // 任一嵌套对象不满足合同就拒绝整页，不能把部分可信列表误呈现为完整治理视图。
  if (sessionRisks.some((item) => !item)) return null;
  const ids = sessionRisks.map((item) => item.session_risk_score_id);
  if (new Set(ids).size !== ids.length) return null;
  return Object.freeze({
    state: "ok",
    runtime_status: payload.runtime_status,
    summary,
    session_risks: Object.freeze(sessionRisks),
    next_cursor: payload.next_cursor,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 按动作意图交叉校验 action 响应，防止旧响应或其他 score 被写入当前页。 */
export function normalizeSessionRiskActionResponse(payload, expected = {}) {
  if (!hasExactKeys(payload, ACTION_RESPONSE_KEYS)) return null;
  const risk = normalizeRisk(payload.session_risk);
  const action = normalizeHistoryAction(payload.action);
  if (
    !risk
    || !action
    || typeof payload.idempotent_replay !== "boolean"
    || !safeId(payload.request_id)
    || !safeId(payload.trace_id)
    || risk.session_risk_score_id !== expected.scoreId
    || action.action !== expected.action
    || action.reason_code !== expected.reasonCode
    || action.actor_type !== "user"
    || action.state_revision !== expected.expectedRevision + 1
    || risk.state_revision < action.state_revision
    // 首次响应必须与本动作精确同 revision；幂等回放允许返回已经继续演进的当前 score。
    || (!payload.idempotent_replay && risk.state_revision !== action.state_revision)
    || (risk.state_revision === action.state_revision && risk.status !== action.status_after)
    || risk.score !== action.score_snapshot
    || !risk.actions.some((item) => item.session_risk_action_id === action.session_risk_action_id)
  ) return null;
  return Object.freeze({
    state: "ok",
    session_risk: risk,
    action,
    idempotent_replay: payload.idempotent_replay,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 根据当前 score 和 active overlay 计算前端可生成的固定动作；后端仍会再次权威校验。 */
export function availableSessionRiskActions(risk) {
  if (!risk || !STATUS_VALUES.has(risk.status)) return Object.freeze([]);
  const freezes = Array.isArray(risk.freezes) ? risk.freezes : [];
  const sessionActive = freezes.some((item) => item?.target_type === "session" && item?.status === "active");
  const keyActive = freezes.some((item) => item?.target_type === "virtual_key" && item?.status === "active");
  const actions = [];
  if (risk.status === "high_risk") {
    // active overlay 已存在时隐藏同目标重复动作；真正并发约束仍由后端 CAS 强制执行。
    if (!sessionActive) actions.push("freeze_session");
    if (!keyActive && risk.virtual_key_id) actions.push("freeze_virtual_key");
    if (!sessionActive && !keyActive) actions.push("dismiss");
  }
  if (risk.status === "frozen") {
    // frozen 可只由 Key overlay 形成；此时 Session 仍是独立可冻结目标，不能被状态名误判为已冻结。
    if (!sessionActive) actions.push("freeze_session");
    if (!keyActive && risk.virtual_key_id) actions.push("freeze_virtual_key");
    if (sessionActive) actions.push("release_session");
    if (keyActive) actions.push("release_virtual_key");
  }
  return Object.freeze(actions);
}

/** 返回一个动作允许的固定原因集合；结果只用于 select 和 client 预校验。 */
export function sessionRiskReasonCodes(action) {
  const matrix = {
    freeze_session: ["confirmed_attack"],
    freeze_virtual_key: ["confirmed_attack"],
    dismiss: ["false_positive", "verified_safe"],
    release_session: ["false_positive", "incident_contained"],
    release_virtual_key: ["false_positive", "incident_contained"],
  };
  return Object.freeze([...(matrix[action] ?? [])]);
}

/** 把动作输入收敛为 exact JSON；非法动作、原因或 revision 在 fetch 前失败关闭。 */
export function normalizeSessionRiskActionBody({ action, reasonCode, expectedRevision }) {
  const normalizedAction = String(action ?? "").trim();
  const normalizedReason = String(reasonCode ?? "").trim();
  if (
    !ACTION_VALUES.has(normalizedAction)
    || !sessionRiskReasonCodes(normalizedAction).includes(normalizedReason)
    || !Number.isSafeInteger(expectedRevision)
    || expectedRevision < 1
  ) {
    // 动作、原因和 CAS revision 必须同时合法，不能由浏览器自动修正成另一治理意图。
    throw sessionRiskClientError("session_risk.payload_invalid", "风险处置请求不合法");
  }
  return {
    action: normalizedAction,
    reason_code: normalizedReason,
    expected_revision: expectedRevision,
  };
}

/** 生成只驻留当前动作草稿的幂等键；没有密码学随机源时失败关闭。 */
export function createSessionRiskIdempotencyKey() {
  return `sr_action_${randomToken(32)}`;
}

/** 生成不包含 tenant、score、scope 或动作名的 Console request_id。 */
export function createSessionRiskRequestId(kind = "request") {
  const safeKind = /^[a-z]{2,16}$/.test(kind) ? kind : "request";
  // ID 仅组合固定页面域、时间和随机数，不含 tenant、score 或筛选条件。
  return `console_session_risk_${safeKind}_${Date.now()}_${randomToken(12)}`;
}

/** 生成本次请求使用的随机 trace_id，不复用 Session Header 或业务 ID。 */
export function createSessionRiskTraceId() {
  return `trc_console_session_risk_${randomToken(32)}`;
}

/** 网络和 5xx 结果可能已提交，必须保留原幂等键；明确 4xx 才允许形成修正后的新意图。 */
export function shouldRetainSessionRiskIdempotencyKey(error) {
  if (!(error instanceof AuthApiError)) return true;
  const status = Number(error.status ?? 0);
  return status === 0 || status >= 500;
}

/** 校验并冻结顶部计数摘要。 */
function normalizeSummary(value) {
  if (!hasExactKeys(value, SUMMARY_KEYS)) return null;
  if (!SUMMARY_KEYS.every((key) => safeCount(value[key]))) return null;
  return Object.freeze({
    high_risk_count: value.high_risk_count,
    frozen_count: value.frozen_count,
    resolved_count: value.resolved_count,
  });
}

/** 校验一条 SessionRiskInfo，并执行 scope、overlay 与状态矩阵交叉检查。 */
function normalizeRisk(value) {
  if (!hasExactKeys(value, RISK_KEYS)) return null;
  const signals = normalizeSignals(value.signal_counts);
  if (
    !SCORE_ID_PATTERN.test(String(value.session_risk_score_id ?? ""))
    || !SCOPE_VALUES.has(value.scope_type)
    || !validScopeReference(value.scope_type, value.scope_reference)
    || !VIRTUAL_KEY_ID_PATTERN.test(String(value.virtual_key_id ?? ""))
    || !STATUS_VALUES.has(value.status)
    || !safeBoundedInteger(value.score, 0, 10_000)
    || value.score_scale !== 10_000
    || !safeBoundedInteger(value.risky_turn_count, 1, Number.MAX_SAFE_INTEGER)
    || !signals
    || !safeBoundedInteger(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !safeEnum(value.threshold_version)
    || !safeId(value.last_source_request_id)
    // 活跃 high-risk cycle 必须有窗口；终态和冻结态由数据库约束为 null，不能伪造自动解冻时间。
    || !validWindowExpiry(value.status, value.window_expires_at)
    || !safeNullableIsoDateTime(value.first_high_risk_at)
    || !safeNullableIsoDateTime(value.frozen_at)
    || !safeIsoDateTime(value.updated_at)
    || !Array.isArray(value.freezes)
    || value.freezes.length > MAX_COLLECTION_SIZE
    || !Array.isArray(value.actions)
    || value.actions.length > MAX_COLLECTION_SIZE
  ) return null;
  const freezes = value.freezes.map(normalizeFreeze);
  const actions = value.actions.map(normalizeHistoryAction);
  if (freezes.some((item) => !item) || actions.some((item) => !item)) return null;
  if (
    freezes.some((item) => item.state_revision > value.state_revision)
    || actions.some((item) => item.state_revision > value.state_revision)
  ) return null;
  const activeFreeze = freezes.some((item) => item.status === "active");
  if ((value.status === "frozen") !== activeFreeze || (value.status === "frozen" && !value.frozen_at)) {
    return null;
  }
  // 重复子对象会让状态矩阵和时间线产生歧义，整条 score 必须失败关闭。
  if (new Set(freezes.map((item) => item.session_risk_freeze_id)).size !== freezes.length) return null;
  if (new Set(actions.map((item) => item.session_risk_action_id)).size !== actions.length) return null;
  return Object.freeze({
    session_risk_score_id: value.session_risk_score_id,
    scope_type: value.scope_type,
    scope_reference: value.scope_reference,
    virtual_key_id: value.virtual_key_id,
    status: value.status,
    score: value.score,
    score_scale: 10_000,
    risky_turn_count: value.risky_turn_count,
    signal_counts: signals,
    state_revision: value.state_revision,
    threshold_version: value.threshold_version,
    last_source_request_id: value.last_source_request_id,
    window_expires_at: value.window_expires_at,
    first_high_risk_at: value.first_high_risk_at,
    frozen_at: value.frozen_at,
    updated_at: value.updated_at,
    freezes: Object.freeze(freezes),
    actions: Object.freeze(actions),
  });
}

/** 校验固定三类信号计数，拒绝自由 signal 名或非整数。 */
function normalizeSignals(value) {
  if (!hasExactKeys(value, SIGNAL_KEYS) || !SIGNAL_KEYS.every((key) => safeCount(value[key]))) {
    return null;
  }
  return Object.freeze({
    jailbreak: value.jailbreak,
    sensitive: value.sensitive,
    tool_inducement: value.tool_inducement,
  });
}

/** 校验一条 overlay 摘要；active 与 released_at 必须保持单义。 */
function normalizeFreeze(value) {
  if (
    !hasExactKeys(value, FREEZE_KEYS)
    || !FREEZE_ID_PATTERN.test(String(value.session_risk_freeze_id ?? ""))
    || !FREEZE_TARGETS.has(value.target_type)
    || !FREEZE_STATUSES.has(value.status)
    || !safeBoundedInteger(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !safeIsoDateTime(value.created_at)
    || !safeNullableIsoDateTime(value.released_at)
    || (value.status === "active") !== (value.released_at === null)
  ) return null;
  return Object.freeze({
    session_risk_freeze_id: value.session_risk_freeze_id,
    target_type: value.target_type,
    status: value.status,
    state_revision: value.state_revision,
    created_at: value.created_at,
    released_at: value.released_at,
  });
}

/** 校验 append-only action 摘要；actor display 仅允许有界无控制字符文本。 */
function normalizeHistoryAction(value) {
  if (
    !hasExactKeys(value, ACTION_KEYS)
    || !ACTION_ID_PATTERN.test(String(value.session_risk_action_id ?? ""))
    || !ACTION_HISTORY_VALUES.has(value.action)
    || !REASON_VALUES.has(value.reason_code)
    || !ACTOR_TYPES.has(value.actor_type)
    || !safeDisplayText(value.actor_reference)
    || !(value.session_risk_freeze_id === null
      || FREEZE_ID_PATTERN.test(String(value.session_risk_freeze_id ?? "")))
    || !HISTORY_STATUSES.has(value.status_before)
    || !HISTORY_STATUSES.has(value.status_after)
    || !safeBoundedInteger(value.score_snapshot, 0, 10_000)
    || !safeBoundedInteger(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !safeId(value.source_request_id)
    || !safeIsoDateTime(value.created_at)
    || !validHistoryMatrix(value)
  ) return null;
  return Object.freeze({
    session_risk_action_id: value.session_risk_action_id,
    action: value.action,
    reason_code: value.reason_code,
    actor_type: value.actor_type,
    actor_reference: value.actor_reference,
    session_risk_freeze_id: value.session_risk_freeze_id,
    status_before: value.status_before,
    status_after: value.status_after,
    score_snapshot: value.score_snapshot,
    state_revision: value.state_revision,
    source_request_id: value.source_request_id,
    created_at: value.created_at,
  });
}

/** 校验 action/reason/actor/status/overlay 的历史状态矩阵，拒绝合法枚举的非法组合。 */
function validHistoryMatrix(value) {
  if (value.action === "high_risk") {
    return value.reason_code === "threshold_reached"
      && value.actor_type === "system"
      && value.session_risk_freeze_id === null
      && value.status_before === "monitoring"
      && value.status_after === "high_risk";
  }
  // 自动冻结与人工冻结共用动作名，但 actor/reason 必须精确区分且都绑定 overlay。
  if (value.action === "freeze_session") {
    const validActorReason = (value.actor_type === "system" && value.reason_code === "threshold_reached")
      || (value.actor_type === "user" && value.reason_code === "confirmed_attack");
    return validActorReason
      && value.session_risk_freeze_id !== null
      && value.status_before === "high_risk"
      && value.status_after === "frozen";
  }
  if (value.action === "freeze_virtual_key") {
    return value.actor_type === "user"
      && value.reason_code === "confirmed_attack"
      && value.session_risk_freeze_id !== null
      && ["high_risk", "frozen"].includes(value.status_before)
      && value.status_after === "frozen";
  }
  if (value.action === "dismiss") {
    return value.actor_type === "user"
      && ["false_positive", "verified_safe"].includes(value.reason_code)
      && value.session_risk_freeze_id === null
      && value.status_before === "high_risk"
      && value.status_after === "resolved";
  }
  // 释放动作只能由用户执行，且允许另一 overlay 仍 active 时保持 frozen。
  if (["release_session", "release_virtual_key"].includes(value.action)) {
    return value.actor_type === "user"
      && ["false_positive", "incident_contained"].includes(value.reason_code)
      && value.session_risk_freeze_id !== null
      && value.status_before === "frozen"
      && ["frozen", "resolved"].includes(value.status_after);
  }
  return value.action === "expire"
    && value.actor_type === "system"
    && value.reason_code === "window_expired"
    && value.session_risk_freeze_id === null
    && ["monitoring", "high_risk"].includes(value.status_before)
    && value.status_after === "expired";
}

/** 验证 enterprise Chat 精确 ID 或 API HMAC 的短安全显示前缀。 */
function validScopeReference(scopeType, value) {
  const normalized = String(value ?? "");
  return scopeType === "enterprise_chat"
    ? CHAT_SESSION_ID_PATTERN.test(normalized)
    : API_SCOPE_REFERENCE_PATTERN.test(normalized);
}

/** 校验风险窗口的状态矩阵；人工冻结和所有终态都必须显式返回 null。 */
function validWindowExpiry(status, value) {
  return status === "high_risk" ? safeIsoDateTime(value) : value === null;
}

/** 只把已知枚举写入 query，非法筛选在网络请求前返回统一错误。 */
function setOptionalEnum(url, name, value, allowedValues) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return;
  if (!allowedValues.has(normalized)) {
    // 未知筛选值不发到后端，避免不同版本之间宽松解释出不同租户视图。
    throw sessionRiskClientError("session_risk.query_invalid", "高危会话筛选不合法", 400);
  }
  url.searchParams.set(name, normalized);
}

/** 校验 Virtual Key 筛选，仅允许不透明 ID，不接受 Key 前缀或明文。 */
function setOptionalVirtualKey(url, value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return;
  if (!VIRTUAL_KEY_ID_PATTERN.test(normalized)) {
    // 仅 canonical UUID 可进入 query，浏览器不替调用方修复大小写或非规范字符串。
    throw sessionRiskClientError("session_risk.query_invalid", "Virtual Key 筛选不合法", 400);
  }
  url.searchParams.set("virtual_key_id", normalized);
}

/** 校验 ISO UTC 时间后写入 query，禁止浏览器猜测含时区歧义的字符串。 */
function setOptionalTime(url, name, value) {
  const normalized = String(value ?? "").trim();
  if (!normalized) return;
  if (!safeIsoDateTime(normalized)) {
    // 含本地时区或宽松日期的值会破坏服务端快照边界，因此在网络前拒绝。
    throw sessionRiskClientError("session_risk.query_invalid", "时间筛选不合法", 400);
  }
  url.searchParams.set(name, normalized);
}

/** 校验闭开时间窗口；起止相同代表空集合，必须与反向窗口一样在 fetch 前拒绝。 */
function ensureTimeRange(fromTime, toTime) {
  const from = String(fromTime ?? "").trim();
  const to = String(toTime ?? "").trim();
  if (from && to && Date.parse(from) >= Date.parse(to)) {
    // 空窗口或反向窗口都不交给后端猜测，保持闭开区间、筛选签名和 cursor 绑定稳定。
    throw sessionRiskClientError("session_risk.query_invalid", "时间范围不合法", 400);
  }
}

/** 校验 opaque cursor 后写入 API query；值不会进入浏览器地址栏、DOM 或 Storage。 */
function setOptionalCursor(url, value) {
  if (value === null || value === undefined || value === "") return;
  if (!safeCursor(value)) {
    // cursor 是不透明签名；只检查传输边界，不解析或展示其内部内容。
    throw sessionRiskClientError("session_risk.query_invalid", "分页标识不合法", 400);
  }
  url.searchParams.set("cursor", value);
}

/** 校验分页大小，避免无界列表请求。 */
function requireLimit(value) {
  if (!Number.isSafeInteger(value) || value < 1 || value > 100) {
    throw sessionRiskClientError("session_risk.query_invalid", "分页大小不合法", 400);
  }
  return value;
}

/** 校验路径 score ID，DOM 注入值不会形成额外路径段。 */
function requireScoreId(value) {
  const normalized = String(value ?? "");
  if (!SCORE_ID_PATTERN.test(normalized)) {
    // 路径参数只接受固定 score ID，斜杠和编码变体不得影响 URL 结构。
    throw sessionRiskClientError("session_risk.not_found", "当前页面找不到该高危会话", 404);
  }
  return normalized;
}

/** 校验幂等键；原文仅进入请求 Header，不会进入 body、URL、状态快照或日志。 */
function requireIdempotencyKey(value) {
  const normalized = String(value ?? "");
  if (!IDEMPOTENCY_PATTERN.test(normalized)) {
    // 不合规值不能进入 Header；失败后由调用方保留或重新生成闭包内键。
    throw sessionRiskClientError("session_risk.idempotency_invalid", "风险处置幂等标识不合法", 400);
  }
  return normalized;
}

/** 对非成功响应只保留固定公开 code 与链路 ID。 */
function throwSessionRiskApiError(response, payload, fallbackMessage) {
  if (response.ok) return;
  // 自由 message 与 diagnosis 不可信，只读取白名单 code 和安全链路 ID。
  const code = safeErrorCode(payload.code) || "session_risk.unavailable";
  throw new AuthApiError({
    status: response.status,
    code,
    message: ERROR_MESSAGES[code] || fallbackMessage,
    requestId: safeId(payload.request_id) || safeId(response.headers.get("X-Request-ID")) || "-",
    traceId: safeId(payload.trace_id) || safeId(response.headers.get("X-Trace-ID")) || "-",
    suggestion: "",
    diagnosis: null,
  });
}

/** 构造不携带原始输入、响应正文或异常消息的本地统一错误。 */
function sessionRiskClientError(code, message, status = 400, requestId = "-", traceId = "-") {
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

/** 容错读取 JSON object；代理空正文和 HTML 错误页统一收敛为空对象。 */
async function readJson(response) {
  try {
    const payload = await response.json();
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
  } catch {
    return {};
  }
}

/** 判断普通对象是否精确包含冻结字段集合。 */
function hasExactKeys(value, expectedKeys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = [...expectedKeys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

/** 校验非负数量并设置合理上限，阻止异常大整数破坏页面布局。 */
function safeCount(value) {
  return safeBoundedInteger(value, 0, Number.MAX_SAFE_INTEGER);
}

/** 校验有界安全整数，严格拒绝数字字符串、浮点与布尔值。 */
function safeBoundedInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 校验 UTC ISO 时间；响应非法时不由浏览器宽松修复。 */
function safeIsoDateTime(value) {
  const normalized = String(value ?? "");
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(normalized)
    && !Number.isNaN(Date.parse(normalized));
}

/** 校验可空时间；只有 null 表示不存在，空字符串不被接受。 */
function safeNullableIsoDateTime(value) {
  return value === null || safeIsoDateTime(value);
}

/** 校验公开对象与链路 ID。 */
function safeId(value) {
  const normalized = String(value ?? "").trim();
  return SAFE_ID_PATTERN.test(normalized) ? normalized : "";
}

/** 校验版本类 ASCII 枚举，禁止控制字符和自由正文进入状态。 */
function safeEnum(value) {
  return SAFE_ENUM_PATTERN.test(String(value ?? ""));
}

/** 校验统一错误码；非法服务端值使用本地固定兜底。 */
function safeErrorCode(value) {
  const normalized = String(value ?? "").trim();
  return /^[a-z][a-z0-9_.]{1,127}$/.test(normalized) ? normalized : "";
}

/** actor display 允许国际化名称，但拒绝空值、控制字符和无界文本。 */
function safeDisplayText(value) {
  return typeof value === "string"
    && value.length >= 1
    && value.length <= 160
    && !/\p{C}/u.test(value);
}

/** cursor 只接受有界可见 ASCII；完整值只能驻留页面闭包。 */
function safeCursor(value) {
  return value === null
    || (typeof value === "string" && value.length >= 1 && value.length <= 4_096 && /^[\x21-\x7e]+$/.test(value));
}

/** 生成 Header 友好的密码学随机十六进制；缺少安全随机源时拒绝治理请求。 */
function randomToken(length) {
  const uuid = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  if (uuid?.length >= length) return uuid.slice(0, length);
  if (!globalThis.crypto?.getRandomValues) {
    // 没有密码学随机源时拒绝动作，不能用 Math.random 生成可碰撞治理幂等键。
    throw sessionRiskClientError("session_risk.idempotency_invalid", "当前环境缺少安全随机数能力", 500);
  }
  const bytes = new Uint8Array(Math.ceil(length / 2));
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("").slice(0, length);
}
