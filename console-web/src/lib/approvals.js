/**
 * PAGE-108/109 审批 API 的浏览器安全合同。
 *
 * tenant、actor、目标版本和权限都由后端恢复；浏览器只提交固定筛选或单次决策意图。密码、step-up
 * token 与幂等键不会进入返回 DTO、错误消息、URL、Storage 或日志。
 */
import { AuthApiError } from "./auth.js";

// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const APPROVAL_ID = /^apr_[0-9a-f]{32}$/;
const DECISION_ID = /^apd_[0-9a-f]{32}$/;
const SAFE_ID = /^[A-Za-z0-9_.:-]{1,256}$/;
const PRIVATE_CURSOR = /^[A-Za-z0-9_-]{1,1024}$/;
const ERROR_CODE = /^[a-z][a-z0-9_.]{1,127}$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TARGET_TYPES = new Set(["budget_block_release", "session_risk_action"]);
const TARGET_ACTIONS = Object.freeze({
  budget_block_release: new Set(["release"]),
  session_risk_action: new Set([
    "freeze_session",
    "freeze_virtual_key",
    "dismiss",
    "release_session",
    "release_virtual_key",
  ]),
});
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const REQUIRED_PERMISSION = Object.freeze({
  budget_block_release: "billing.budget.write",
  session_risk_action: "session_risk.manage",
});
const STATUSES = new Set(["pending", "approved", "rejected"]);
const RESOLUTION_REASONS = new Set(["approved", "user_rejected", "timeout"]);
const WRITEBACK_STATUSES = new Set(["pending", "completed", "failed"]);
const DECISIONS = new Set(["approve", "reject", "timeout"]);
const USER_REASON_CODES = Object.freeze({
  approve: new Set(["approved_business_need"]),
  reject: new Set(["risk_unacceptable", "budget_not_approved", "no_longer_needed"]),
});
const ALL_REASON_CODES = new Set([
  ...USER_REASON_CODES.approve,
  ...USER_REASON_CODES.reject,
  "timeout",
]);
const SUMMARY_KEYS = Object.freeze([
  "approval_id",
  "target_type",
  "target_action",
  "risk_level",
  "status",
  "resolution_reason",
  "state_revision",
  "writeback_status",
  "required_permission_code",
  "requires_step_up",
  "created_at",
  "expires_at",
  "decided_at",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const TARGET_SUMMARY_KEYS = Object.freeze([
  "target_type",
  "target_id",
  "target_action",
  "target_version",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const DECISION_KEYS = Object.freeze([
  "decision_id",
  "decision",
  "reason_code",
  "actor_type",
  "actor_id",
  "revision_before",
  "revision_after",
  "step_up_used",
  "writeback_status",
  "created_at",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const LIST_KEYS = Object.freeze(["approvals", "next_cursor", "request_id", "trace_id"]);
const DETAIL_KEYS = Object.freeze([
  "approval",
  "target_summary",
  "allowed_actions",
  "decisions",
  "request_id",
  "trace_id",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const STEP_UP_KEYS = Object.freeze(["step_up_token", "expires_at", "request_id", "trace_id"]);
const ACTION_KEYS = Object.freeze([
  "approval",
  "decision",
  "idempotent_replay",
  "request_id",
  "trace_id",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const ERROR_KEYS = Object.freeze([
  "code",
  "message",
  "request_id",
  "trace_id",
  "suggestion",
  "diagnosis",
]);
// 审批协议常量集中限定允许值与显示映射，服务端仍是权限和状态的权威来源。
const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.token_expired": "登录状态已过期",
  "auth.session_revoked": "登录状态已撤销",
  "auth.permission_denied": "当前账号没有审批权限",
  "approval.input_invalid": "审批请求不合法",
  "approval.idempotency_required": "审批动作缺少幂等凭据",
  "approval.not_found": "当前租户中找不到该审批任务",
  "approval.state_conflict": "审批任务状态已变化，请刷新后重试",
  "approval.idempotency_conflict": "该操作凭据已用于其他审批意图",
  "approval.target_conflict": "审批目标已变化，请重新发起审批",
  "approval.expired": "审批任务已超时并自动拒绝",
  "approval.step_up_required": "该高风险操作需要二次验证",
  "approval.step_up_invalid": "二次验证失败或已失效",
  "approval.step_up_rate_limited": "二次验证尝试过多，请稍后重试",
  "approval.capacity_full": "当前租户待审批任务已达上限",
  "approval.response_invalid": "审批响应合同不完整",
  "approval.unavailable": "审批服务暂不可用",
  "approval.writeback_unavailable": "审批结果正在等待恢复回写",
  "approval.event_unavailable": "审批通知正在等待恢复投递",
});

/** 返回符合服务端幂等合同的随机 UUIDv4；只驻留当前动作 closure。 */
export function createApprovalIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.().toLowerCase();
  if (!UUID_V4.test(String(value ?? ""))) {
    // 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
    throw clientError("approval.unavailable", 503);
  }
  return value;
}

/** 列出当前租户内、且已由后端按目标域权限裁剪的审批任务。 */
export async function listApprovals({
  controlApiUrl,
  token,
  status = "pending",
  targetType = "",
  limit = 50,
  cursor = null,
  signal,
}) {
  if (!["pending", "approved", "rejected", "all"].includes(status)) {
    // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    throw clientError("approval.input_invalid", 400);
  }
  if (targetType && !TARGET_TYPES.has(targetType)) {
    // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    throw clientError("approval.input_invalid", 400);
  }
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !nullableCursor(cursor)) {
    // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    throw clientError("approval.input_invalid", 400);
  }
  const url = new URL(`${controlApiUrl}/approvals`);
  url.searchParams.set("status", status);
  if (targetType) url.searchParams.set("target_type", targetType);
  url.searchParams.set("limit", String(limit));
  if (cursor) url.searchParams.set("cursor", cursor);
  const payload = await requestJson(url.toString(), {
    headers: authorizationHeaders(token),
    signal,
  }, { expectedStatus: 200 });
  return normalizeApprovalList(payload);
}

/** 读取一个审批详情；无权、跨租户和不存在由后端统一成同形 404。 */
export async function getApproval({ controlApiUrl, token, approvalId, signal }) {
  const id = requireApprovalId(approvalId);
  const payload = await requestJson(
    `${controlApiUrl}/approvals/${encodeURIComponent(id)}`,
    { headers: authorizationHeaders(token), signal },
    { expectedStatus: 200 },
  );
  return normalizeApprovalDetail(payload, id);
}

/**
 * 使用当前用户密码换取一次性高风险动作票据。
 * 调用方必须在 Promise settle 后清空 password 变量和输入框；本函数从不把它加入错误对象。
 */
export async function stepUpApproval({ controlApiUrl, token, approvalId, action, password, signal }) {
  const id = requireApprovalId(approvalId);
  if (!["approve", "reject"].includes(action) || typeof password !== "string" || !password) {
    // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    throw clientError("approval.input_invalid", 400);
  }
  const body = JSON.stringify({ action, password });
  // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
  if (utf8Length(body) > 4 * 1024) throw clientError("approval.input_invalid", 400);
  const payload = await requestJson(
    `${controlApiUrl}/approvals/${encodeURIComponent(id)}/step-up`,
    {
      method: "POST",
      headers: { ...authorizationHeaders(token), "Content-Type": "application/json" },
      body,
      signal,
    },
    { expectedStatus: 200, requireNoCache: true },
  );
  return normalizeStepUp(payload);
}

/** 提交 approve/reject；两条真实路由不会把 Excel 中的竖线简写当成路径。 */
export async function decideApproval({
  controlApiUrl,
  token,
  approvalId,
  action,
  expectedRevision,
  reasonCode,
  idempotencyKey,
  stepUpToken,
  signal,
}) {
  const id = requireApprovalId(approvalId);
  if (
    !["approve", "reject"].includes(action)
    || !Number.isSafeInteger(expectedRevision)
    || expectedRevision < 1
    || !USER_REASON_CODES[action]?.has(reasonCode)
    || !UUID_V4.test(String(idempotencyKey ?? ""))
    || !(stepUpToken === undefined || validStepUpToken(stepUpToken))
  ) throw clientError("approval.input_invalid", 400);
  const request = { expected_revision: expectedRevision, reason_code: reasonCode };
  // 二次验证票据仅校验 HMAC 摘要、租户、用户、会话、动作、修订和时效，原文不得持久化或回显。
  if (stepUpToken !== undefined) request.step_up_token = stepUpToken;
  const body = JSON.stringify(request);
  // 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
  if (utf8Length(body) > 4 * 1024) throw clientError("approval.input_invalid", 400);
  const payload = await requestJson(
    `${controlApiUrl}/approvals/${encodeURIComponent(id)}/${action}`,
    {
      method: "POST",
      headers: {
        ...authorizationHeaders(token),
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body,
      signal,
    },
    { expectedStatus: 200 },
  );
  return normalizeAction(payload, id, action, expectedRevision);
}

/** 严格归一化列表响应，任何额外字段、重复任务或非法 cursor 都失败关闭。 */
export function normalizeApprovalList(payload) {
  if (
    !exact(payload, LIST_KEYS)
    || !Array.isArray(payload.approvals)
    || payload.approvals.length > 100
    || !nullableCursor(payload.next_cursor)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const approvals = payload.approvals.map(normalizeSummary);
  if (approvals.some((item) => !item)) {
    // 请求链路标识必须与持久化来源事实一致；不一致视为数据污染并以固定错误终止处理。
    throw clientError("approval.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  if (new Set(approvals.map((item) => item.approval_id)).size !== approvals.length) {
    // 请求链路标识必须与持久化来源事实一致；不一致视为数据污染并以固定错误终止处理。
    throw clientError("approval.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  return deepFreeze({ ...payload, approvals });
}

/** 严格归一化详情、目标摘要、动作集合和 append-only 决策时间线。 */
export function normalizeApprovalDetail(payload, expectedApprovalId = null) {
  if (
    !exact(payload, DETAIL_KEYS)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
    || !Array.isArray(payload.allowed_actions)
    || payload.allowed_actions.length > 2
    || payload.allowed_actions.some((action) => !["approve", "reject"].includes(action))
    || new Set(payload.allowed_actions).size !== payload.allowed_actions.length
    || !Array.isArray(payload.decisions)
    || payload.decisions.length > 32
  ) throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const approval = normalizeSummary(payload.approval);
  const target = normalizeTargetSummary(payload.target_summary);
  const decisions = payload.decisions.map(normalizeDecision);
  if (
    !approval
    || (expectedApprovalId && approval.approval_id !== expectedApprovalId)
    || !target
    || target.target_type !== approval.target_type
    || target.target_action !== approval.target_action
    || decisions.some((item) => !item)
    || new Set(decisions.map((item) => item.decision_id)).size !== decisions.length
    || (approval.status === "pending" && decisions.length !== 0)
    || (approval.status !== "pending" && decisions.length !== 1)
    || (approval.status !== "pending" && decisions[0]?.revision_after !== approval.state_revision)
    || (approval.status !== "pending"
      && !validWritebackProgress(decisions[0]?.writeback_status, approval.writeback_status))
    || (approval.status !== "pending" && payload.allowed_actions.length !== 0)
  ) throw clientError("approval.response_invalid", 502, payload.request_id, payload.trace_id);
  return deepFreeze({ ...payload, approval, target_summary: target, decisions });
}

/** 只接纳一次性 step-up 明文和短 TTL 元数据；token 不进入任何页面 snapshot。 */
export function normalizeStepUp(payload) {
  if (
    !exact(payload, STEP_UP_KEYS)
    || !validStepUpToken(payload.step_up_token)
    || !utc(payload.expires_at)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return Object.freeze({ ...payload });
}

/** 校验动作结果与原 task/action/revision 一致，防止错误幂等记录被当前 owner 接受。 */
export function normalizeAction(payload, expectedApprovalId = null, action = null, revision = null) {
  if (
    !exact(payload, ACTION_KEYS)
    || typeof payload.idempotent_replay !== "boolean"
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const approval = normalizeSummary(payload.approval);
  const decision = normalizeDecision(payload.decision);
  if (
    !approval
    || !decision
    || (expectedApprovalId && approval.approval_id !== expectedApprovalId)
    || (action && decision.decision !== action)
    || (revision !== null && decision.revision_before !== revision)
    || decision.revision_after !== approval.state_revision
    || decision.writeback_status !== approval.writeback_status
    || (action === "approve" && approval.status !== "approved")
    || (action === "reject" && approval.status !== "rejected")
    || approval.status === "pending"
  ) throw clientError("approval.response_invalid", 502, payload.request_id, payload.trace_id);
  return deepFreeze({ ...payload, approval, decision });
}

/** 校验 ApprovalSummary 的状态、时间和目标权限不变量。 */
function normalizeSummary(value) {
  if (
    !exact(value, SUMMARY_KEYS)
    || !APPROVAL_ID.test(String(value.approval_id ?? ""))
    || !TARGET_TYPES.has(value.target_type)
    || !TARGET_ACTIONS[value.target_type]?.has(value.target_action)
    || !["standard", "high"].includes(value.risk_level)
    || !STATUSES.has(value.status)
    || !(value.resolution_reason === null || RESOLUTION_REASONS.has(value.resolution_reason))
    || !integer(value.state_revision, 1, Number.MAX_SAFE_INTEGER)
    || !WRITEBACK_STATUSES.has(value.writeback_status)
    || value.required_permission_code !== REQUIRED_PERMISSION[value.target_type]
    || typeof value.requires_step_up !== "boolean"
    || value.requires_step_up !== (value.risk_level === "high")
    || !utc(value.created_at)
    || !utc(value.expires_at)
    || !(value.decided_at === null || utc(value.decided_at))
  ) return null;
  const created = Date.parse(value.created_at);
  const expires = Date.parse(value.expires_at);
  const decided = value.decided_at === null ? null : Date.parse(value.decided_at);
  if (
    expires <= created
    || (value.status === "pending" && (value.resolution_reason !== null || decided !== null))
    || (value.status === "approved" && (value.resolution_reason !== "approved" || decided === null))
    || (value.status === "rejected" && !["user_rejected", "timeout"].includes(value.resolution_reason))
    || (value.status === "rejected" && decided === null)
    || (decided !== null && decided < created)
  ) return null;
  return Object.freeze({ ...value });
}

/** 校验不含目标正文的安全目标摘要。 */
function normalizeTargetSummary(value) {
  if (
    !exact(value, TARGET_SUMMARY_KEYS)
    || !TARGET_TYPES.has(value.target_type)
    || !SAFE_ID.test(String(value.target_id ?? ""))
    || !TARGET_ACTIONS[value.target_type]?.has(value.target_action)
    || !safeText(value.target_version, 160)
  ) return null;
  return Object.freeze({ ...value });
}

/** 校验 append-only 决策摘要，不接受备注、票据摘要或目标正文扩展字段。 */
function normalizeDecision(value) {
  if (
    !exact(value, DECISION_KEYS)
    || !DECISION_ID.test(String(value.decision_id ?? ""))
    || !DECISIONS.has(value.decision)
    || !ALL_REASON_CODES.has(value.reason_code)
    || !["user", "system"].includes(value.actor_type)
    || !SAFE_ID.test(String(value.actor_id ?? ""))
    || !integer(value.revision_before, 1, Number.MAX_SAFE_INTEGER)
    || value.revision_after !== value.revision_before + 1
    || typeof value.step_up_used !== "boolean"
    || !WRITEBACK_STATUSES.has(value.writeback_status)
    || !utc(value.created_at)
    || (value.decision === "approve" && value.reason_code !== "approved_business_need")
    || (value.decision === "reject" && !USER_REASON_CODES.reject.has(value.reason_code))
    || (value.decision === "timeout" && value.reason_code !== "timeout")
    || (value.actor_type === "system") !== (value.decision === "timeout")
  ) return null;
  return Object.freeze({ ...value });
}

/** Decision 保存决策提交时快照；Task 允许异步回写从 pending 单向进入终态。 */
function validWritebackProgress(decisionStatus, taskStatus) {
  return decisionStatus === taskStatus
    || (decisionStatus === "pending" && ["completed", "failed"].includes(taskStatus));
}

/** 发起私有 JSON 请求，并把任何代理正文折叠为固定公开错误。 */
async function requestJson(url, init, { expectedStatus, requireNoCache = false }) {
  let response;
  try {
    response = await fetch(url, {
      ...init,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
  } catch (error) {
    // 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    if (error?.name === "AbortError") throw error;
    // 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
    throw clientError("approval.unavailable", 503);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    // JSON 与响应合同必须完整且拒绝未知形状；解析失败不得回显上游正文或内部字段。
    throw clientError("approval.response_invalid", response.ok ? 502 : response.status);
  }
  verifyPrivateHeaders(response, payload, requireNoCache);
  if (!response.ok) {
    const validEnvelope = exact(payload, ERROR_KEYS)
      && chain(payload.request_id)
      && chain(payload.trace_id);
    const code = validEnvelope && ERROR_CODE.test(String(payload.code ?? ""))
      ? payload.code
      : "approval.unavailable";
    // 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    throw clientError(
      code,
      response.status,
      validEnvelope ? payload.request_id : "-",
      validEnvelope ? payload.trace_id : "-",
    );
  }
  if (response.status !== expectedStatus) {
    // 请求链路标识必须与持久化来源事实一致；不一致视为数据污染并以固定错误终止处理。
    throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  return payload;
}

/** 所有审批事实禁止共享缓存；step-up 额外要求 no-cache 与 Authorization Vary。 */
function verifyPrivateHeaders(response, payload, requireNoCache) {
  const cacheControl = String(response.headers?.get?.("Cache-Control") ?? "").trim().toLowerCase();
  const pragma = String(response.headers?.get?.("Pragma") ?? "").trim().toLowerCase();
  const varyTokens = String(response.headers?.get?.("Vary") ?? "")
      .toLowerCase()
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  const vary = new Set(varyTokens);
  const headerRequestId = response.headers?.get?.("X-Request-ID");
  const headerTraceId = response.headers?.get?.("X-Trace-ID");
  if (
    cacheControl !== "no-store, private"
    || pragma !== "no-cache"
    || varyTokens.length !== vary.size
    || !vary.has("authorization")
    || [...vary].some((item) => !["authorization", "origin"].includes(item))
    || !chain(headerRequestId)
    || !chain(headerTraceId)
    || headerRequestId !== payload?.request_id
    || headerTraceId !== payload?.trace_id
    || (requireNoCache && pragma !== "no-cache")
  ) throw clientError("approval.response_invalid", 502, payload?.request_id, payload?.trace_id);
}

/** 构造仅包含 Bearer 和 JSON Accept 的认证头。 */
/**
 * 执行 authorizationHeaders 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function authorizationHeaders(token) {
  if (typeof token !== "string" || !token || /\s/.test(token)) {
    // 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
    throw clientError("auth.token_missing", 401);
  }
  // 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

/** 非法 ID 在网络前收敛到同形 404，不拼入路径或错误消息。 */
function requireApprovalId(value) {
  if (!APPROVAL_ID.test(String(value ?? ""))) throw clientError("approval.not_found", 404);
  return value;
}

/** step-up token 仅做格式/长度约束，不解析、持久化或展示其正文。 */
function validStepUpToken(value) {
  return typeof value === "string"
    && value.length >= 32
    && value.length <= 512
    && /^[A-Za-z0-9_-]+$/.test(value);
}

function nullableCursor(value) {
  return value === null || (typeof value === "string" && PRIVATE_CURSOR.test(value));
}

function exact(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/**
 * 执行 integer 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/**
 * 执行 safeText 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function safeText(value, maximum) {
  return typeof value === "string"
    && value.length > 0
    && [...value].length <= maximum
    && !/[\u0000-\u001f\u007f]/.test(value);
}

/**
 * 执行 chain 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function chain(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

/**
 * 执行 utc 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function utc(value) {
  return typeof value === "string"
    && UTC_TIME.test(value)
    && Number.isFinite(Date.parse(value))
    && new Date(Date.parse(value)).toISOString().replace(".000Z", "Z") === value;
}

/**
 * 执行 clientError 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "approval.unavailable";
  // 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "审批请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
    suggestion: "",
    diagnosis: null,
  });
}

/**
 * 执行 utf8Length 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function utf8Length(value) {
  return new TextEncoder().encode(value).byteLength;
}

/**
 * 执行 deepFreeze 对应审批协议处理；严格校验响应形状且不得持久化令牌原文。
 */
function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}
