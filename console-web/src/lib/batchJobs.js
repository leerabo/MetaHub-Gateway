/** PRD-074 批任务控制台模块：负责页面状态与交互，最终权限、租户隔离和错误裁决均由后端完成。 */
/** Strict PAGE-104 Batch API client. */

import { AuthApiError } from "./auth.js";

// 该常量组约束批任务页面状态；权限和租户边界以后端响应为准。
const JOB_ID = /^bjob_[0-9a-f]{32}$/;
const TASK_ID = /^btsk_[0-9a-f]{32}$/;
const FILE_ID = /^file_[0-9a-f]{32}$/;
const RESOURCE_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
const CHAIN_ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const ERROR_CODE = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const MONEY = /^(?:0|[1-9][0-9]{0,9})\.[0-9]{6}$/;
const UTC_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;

const JOB_STATUSES = new Set([
  "queued", "running", "completed", "partial_failed", "failed", "cancelled", "expired",
]);
const TASK_STATUSES = new Set([
  "queued", "leased", "calling", "retry_wait", "succeeded", "failed", "cancelled", "expired",
  "outcome_unknown",
]);
const OPERATIONS = new Set(["summarize", "translate", "classify"]);
const PRIORITIES = new Set(["low", "normal", "high"]);
const JOB_KEYS = Object.freeze([
  "batch_job_id", "name", "project_id", "environment_id", "model_alias_id", "operation",
  "priority", "max_duration_seconds", "max_attempts", "max_output_tokens", "status", "progress",
  "cost", "deadline_at", "cancel_requested_at", "started_at", "completed_at", "failure_code",
  "created_at", "updated_at",
]);
// 该常量组约束批任务页面状态；权限和租户边界以后端响应为准。
const PROGRESS_KEYS = Object.freeze([
  "total", "queued", "running", "succeeded", "failed", "cancelled", "expired", "outcome_unknown",
]);
const COST_KEYS = Object.freeze([
  "estimated_cny", "posted_cny", "possible_cny", "reconciliation_pending",
]);
const TASK_KEYS = Object.freeze([
  "batch_task_id", "file_id", "task_index", "status", "attempt_count", "request_id", "trace_id",
  "failure_code", "result_available", "input_tokens", "output_tokens", "cost_cny", "created_at",
  "updated_at", "completed_at",
]);
const LIST_KEYS = Object.freeze(["jobs", "next_cursor", "request_id", "trace_id"]);
const JOB_RESPONSE_KEYS = Object.freeze(["job", "request_id", "trace_id"]);
const TASK_LIST_KEYS = Object.freeze(["tasks", "next_cursor", "request_id", "trace_id"]);
const RESULT_KEYS = Object.freeze([
  "batch_job_id", "batch_task_id", "assistant", "request_id", "trace_id",
]);

const ERROR_MESSAGES = Object.freeze({
  "auth.token_missing": "登录状态已失效",
  "auth.token_invalid": "登录状态已失效",
  "auth.session_revoked": "登录状态已失效",
  "auth.permission_denied": "当前账号没有批处理操作权限",
  "batch.request_invalid": "批处理请求格式无效",
  "batch.not_found": "当前租户中找不到该批处理任务",
  "batch.idempotency_conflict": "该幂等键已用于其他批处理请求",
  "batch.capacity_full": "当前租户的批处理队列已满",
  "batch.file_security_blocked": "文件未通过批处理安全准入",
  "batch.cost_estimate_unavailable": "当前模型缺少可信成本估算",
  "batch.budget_exceeded": "预计成本超过当前预算",
  "batch.result_not_ready": "任务结果尚未可读",
  "batch.result_unavailable": "任务结果不可用",
  "batch.provider_outcome_unknown": "上游执行结果待对账",
  "batch.query_unavailable": "批处理记录暂不可用",
  "batch.worker_unavailable": "批处理服务暂不可用",
  "batch.response_invalid": "批处理响应合同不完整",
});

/** 处理 listBatchJobs 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function listBatchJobs({
  controlApiUrl,
  token,
  status = "",
  limit = 50,
  cursor = null,
  signal,
}) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if ((status && !JOB_STATUSES.has(status)) || !integer(limit, 1, 100)) throw clientError("batch.request_invalid", 400);
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (cursor !== null) query.set("cursor", requireCursor(cursor));
  const payload = await requestJson(`${controlApiUrl}/batch-jobs?${query}`, {
    headers: authorizationHeaders(token), signal,
  });
  return normalizeBatchJobListResponse(payload);
}

/** 处理 getBatchJob 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function getBatchJob({ controlApiUrl, token, batchJobId, signal }) {
  const jobId = requireJobId(batchJobId);
  const payload = await requestJson(`${controlApiUrl}/batch-jobs/${encodeURIComponent(jobId)}`, {
    headers: authorizationHeaders(token), signal,
  });
  return normalizeBatchJobResponse(payload, jobId);
}

/** 处理 listBatchTasks 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function listBatchTasks({
  controlApiUrl,
  token,
  batchJobId,
  status = "",
  limit = 50,
  cursor = null,
  signal,
}) {
  const jobId = requireJobId(batchJobId);
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if ((status && !TASK_STATUSES.has(status)) || !integer(limit, 1, 100)) throw clientError("batch.request_invalid", 400);
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (cursor !== null) query.set("cursor", requireCursor(cursor));
  const payload = await requestJson(
    `${controlApiUrl}/batch-jobs/${encodeURIComponent(jobId)}/tasks?${query}`,
    { headers: authorizationHeaders(token), signal },
  );
  const normalized = normalizeBatchTaskListResponse(payload);
  return Object.freeze({ ...normalized, batch_job_id: jobId });
}

/** 处理 getBatchTaskResult 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function getBatchTaskResult({
  controlApiUrl,
  token,
  batchJobId,
  batchTaskId,
  signal,
}) {
  const jobId = requireJobId(batchJobId);
  const taskId = requireTaskId(batchTaskId);
  const payload = await requestJson(
    `${controlApiUrl}/batch-jobs/${encodeURIComponent(jobId)}/tasks/${encodeURIComponent(taskId)}/result`,
    { headers: authorizationHeaders(token), signal },
  );
  const normalized = normalizeBatchTaskResultResponse(payload, jobId, taskId);
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!normalized) throw clientError("batch.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return normalized;
}

/** 处理 createBatchJob 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function createBatchJob({ controlApiUrl, token, idempotencyKey, request, signal }) {
  const body = normalizeCreateRequest(request);
  const key = requireIdempotencyKey(idempotencyKey);
  const payload = await requestJson(`${controlApiUrl}/batch-jobs`, {
    method: "POST",
    headers: {
      ...authorizationHeaders(token),
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
    signal,
  });
  return normalizeBatchJobResponse(payload);
}

/** Generate a canonical one-shot create idempotency key without embedding tenant or form data. */
/** 处理 createBatchJobIdempotencyKey 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export function createBatchJobIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.idempotency_required", 400);
  }
  const value = globalThis.crypto.randomUUID().toLowerCase();
  return requireIdempotencyKey(value);
}

/** 处理 cancelBatchJob 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export async function cancelBatchJob({ controlApiUrl, token, batchJobId, signal }) {
  const jobId = requireJobId(batchJobId);
  const payload = await requestJson(`${controlApiUrl}/batch-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST", headers: authorizationHeaders(token), signal,
  });
  return normalizeBatchJobResponse(payload, jobId);
}

/** 处理 normalizeBatchJobListResponse 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export function normalizeBatchJobListResponse(payload) {
  if (
    !exact(payload, LIST_KEYS)
    || !Array.isArray(payload.jobs)
    || payload.jobs.length > 100
    || !nullableCursor(payload.next_cursor)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) throw clientError("batch.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const jobs = payload.jobs.map(normalizeJob);
  if (jobs.some((job) => !job) || new Set(jobs.map((job) => job.batch_job_id)).size !== jobs.length) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  return Object.freeze({
    state: "ok",
    jobs: Object.freeze(jobs),
    next_cursor: payload.next_cursor,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 处理 normalizeBatchJobResponse 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export function normalizeBatchJobResponse(payload, expectedJobId) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!exact(payload, JOB_RESPONSE_KEYS) || !chain(payload.request_id) || !chain(payload.trace_id)) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  const job = normalizeJob(payload.job);
  if (!job || (expectedJobId !== undefined && job.batch_job_id !== expectedJobId)) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  return Object.freeze({ state: "ok", job, request_id: payload.request_id, trace_id: payload.trace_id });
}

/** 处理 normalizeBatchTaskListResponse 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export function normalizeBatchTaskListResponse(payload) {
  if (
    !exact(payload, TASK_LIST_KEYS)
    || !Array.isArray(payload.tasks)
    || payload.tasks.length > 100
    || !nullableCursor(payload.next_cursor)
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) throw clientError("batch.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const tasks = payload.tasks.map(normalizeTask);
  if (tasks.some((task) => !task) || new Set(tasks.map((task) => task.batch_task_id)).size !== tasks.length) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  return Object.freeze({
    state: "ok",
    tasks: Object.freeze(tasks),
    next_cursor: payload.next_cursor,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 处理 normalizeBatchTaskResultResponse 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
export function normalizeBatchTaskResultResponse(payload, expectedJobId, expectedTaskId) {
  if (
    !exact(payload, RESULT_KEYS)
    || !JOB_ID.test(String(payload.batch_job_id ?? ""))
    || !TASK_ID.test(String(payload.batch_task_id ?? ""))
    || payload.batch_job_id !== expectedJobId
    || payload.batch_task_id !== expectedTaskId
    || typeof payload.assistant !== "string"
    || payload.assistant.length < 1
    || new TextEncoder().encode(payload.assistant).length > 1_048_576
    || !chain(payload.request_id)
    || !chain(payload.trace_id)
  ) return null;
  return Object.freeze({
    state: "ok",
    batch_job_id: payload.batch_job_id,
    batch_task_id: payload.batch_task_id,
    assistant_text: payload.assistant,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

/** 处理 normalizeJob 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function normalizeJob(value) {
  if (
    !exact(value, JOB_KEYS)
    || !JOB_ID.test(String(value.batch_job_id ?? ""))
    || typeof value.name !== "string"
    || !value.name.trim()
    || value.name.length > 120
    || [...value.name].some((character) => /[\u0000-\u001f\u007f]/.test(character))
    || ![value.project_id, value.environment_id, value.model_alias_id].every((id) => RESOURCE_ID.test(String(id ?? "")))
    || !OPERATIONS.has(value.operation)
    || !PRIORITIES.has(value.priority)
    || !integer(value.max_duration_seconds, 30, 3600)
    || !integer(value.max_attempts, 1, 3)
    || !integer(value.max_output_tokens, 16, 4096)
    || !JOB_STATUSES.has(value.status)
    || !validProgress(value.progress)
    || !validCost(value.cost)
    || !utc(value.deadline_at)
    || !nullableUtc(value.cancel_requested_at)
    || !nullableUtc(value.started_at)
    || !nullableUtc(value.completed_at)
    || !nullableBatchError(value.failure_code)
    || !utc(value.created_at)
    || !utc(value.updated_at)
  ) return null;
  return Object.freeze({
    ...value,
    progress: Object.freeze({ ...value.progress }),
    cost: Object.freeze({ ...value.cost }),
  });
}

/** 处理 normalizeTask 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function normalizeTask(value) {
  if (
    !exact(value, TASK_KEYS)
    || !TASK_ID.test(String(value.batch_task_id ?? ""))
    || !FILE_ID.test(String(value.file_id ?? ""))
    || !integer(value.task_index, 0, 99)
    || !TASK_STATUSES.has(value.status)
    || !integer(value.attempt_count, 0, 3)
    || !nullableChain(value.request_id)
    || !nullableChain(value.trace_id)
    || !nullableBatchError(value.failure_code)
    || typeof value.result_available !== "boolean"
    || !nullableInteger(value.input_tokens, 0, Number.MAX_SAFE_INTEGER)
    || !nullableInteger(value.output_tokens, 0, Number.MAX_SAFE_INTEGER)
    || !(value.cost_cny === null || MONEY.test(String(value.cost_cny)))
    || !utc(value.created_at)
    || !utc(value.updated_at)
    || !nullableUtc(value.completed_at)
  ) return null;
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if ((value.request_id === null) !== (value.trace_id === null)) return null;
  if (value.result_available && value.status !== "succeeded") return null;
  return Object.freeze({ ...value });
}

/** 处理 validProgress 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function validProgress(value) {
  if (!exact(value, PROGRESS_KEYS) || !integer(value.total, 1, 100)) return false;
  const keys = PROGRESS_KEYS.filter((key) => key !== "total");
  if (keys.some((key) => !integer(value[key], 0, value.total))) return false;
  return keys.reduce((sum, key) => sum + value[key], 0) === value.total;
}

/** 处理 validCost 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function validCost(value) {
  // 成败请求均须结算用量和成本；异常时保留事实，不得静默丢账。
  return exact(value, COST_KEYS)
    && MONEY.test(String(value.estimated_cny ?? ""))
    && MONEY.test(String(value.posted_cny ?? ""))
    && MONEY.test(String(value.possible_cny ?? ""))
    && typeof value.reconciliation_pending === "boolean"
    && (value.possible_cny === "0.000000" || value.reconciliation_pending);
}

/** 处理 normalizeCreateRequest 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function normalizeCreateRequest(value) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!value || typeof value !== "object" || Array.isArray(value)) throw clientError("batch.request_invalid", 400);
  const operation = value.operation;
  const baseKeys = [
    "name", "project_id", "environment_id", "model_alias_id", "operation", "file_ids", "priority",
    "max_duration_seconds", "max_attempts", "max_output_tokens",
  ];
  const operationKeys = operation === "summarize"
    ? (Object.hasOwn(value, "output_language") ? ["output_language"] : [])
    : operation === "translate"
      ? ["target_language"]
      : operation === "classify"
        ? ["classification_labels"]
        : [];
  if (
    !exact(value, [...baseKeys, ...operationKeys])
    || typeof value.name !== "string"
    || !value.name.trim()
    || value.name.length > 120
    || ![value.project_id, value.environment_id, value.model_alias_id].every((id) => RESOURCE_ID.test(String(id ?? "")))
    || !OPERATIONS.has(operation)
    || !Array.isArray(value.file_ids)
    || !integer(value.file_ids.length, 1, 100)
    || value.file_ids.some((id) => !FILE_ID.test(String(id ?? "")))
    || new Set(value.file_ids).size !== value.file_ids.length
    || !PRIORITIES.has(value.priority)
    || !integer(value.max_duration_seconds, 30, 3600)
    || !integer(value.max_attempts, 1, 3)
    || !integer(value.max_output_tokens, 16, 4096)
    || (operation === "summarize" && Object.hasOwn(value, "output_language") && !language(value.output_language))
    || (operation === "translate" && !language(value.target_language))
    || (operation === "classify" && !labels(value.classification_labels))
  ) throw clientError("batch.request_invalid", 400);
  return Object.freeze({ ...value, file_ids: Object.freeze([...value.file_ids]) });
}

/** 处理 requestJson 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
async function requestJson(url, options) {
  let response;
  try {
    response = await fetch(url, { ...options, cache: "no-store", credentials: "omit" });
  } catch (error) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    if (error?.name === "AbortError") throw error;
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.worker_unavailable", 503);
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", response.ok ? 502 : response.status);
  }
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  if (!cacheControl.includes("no-store")) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  if (!response.ok) {
    const code = ERROR_CODE.test(String(payload?.code ?? "")) ? payload.code : "batch.worker_unavailable";
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError(
      code,
      response.status,
      payload?.request_id || response.headers.get("X-Request-ID"),
      payload?.trace_id || response.headers.get("X-Trace-ID"),
    );
  }
  return payload;
}

/** 处理 authorizationHeaders 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function authorizationHeaders(token) {
  // 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
  if (typeof token !== "string" || !token || /\s/.test(token)) throw clientError("auth.token_missing", 401);
  // 凭据仅用于校验或内存解密；异常输入必须拒绝，严禁回显或记录明文。
  return { Authorization: `Bearer ${token}`, Accept: "application/json" };
}

/** 处理 requireJobId 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function requireJobId(value) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!JOB_ID.test(String(value ?? ""))) throw clientError("batch.not_found", 404);
  return value;
}

/** 处理 requireTaskId 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function requireTaskId(value) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!TASK_ID.test(String(value ?? ""))) throw clientError("batch.not_found", 404);
  return value;
}

/** 处理 requireCursor 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function requireCursor(value) {
  if (typeof value !== "string" || !value || value.length > 1024 || /\s/.test(value)) {
    // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    throw clientError("batch.request_invalid", 400);
  }
  return value;
}

/** 处理 requireIdempotencyKey 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function requireIdempotencyKey(value) {
  // 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
  if (!UUID.test(String(value ?? ""))) throw clientError("batch.idempotency_required", 400);
  return value;
}

/** 处理 language 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function language(value) {
  return typeof value === "string" && value.trim() && value.length <= 35 && !/[\u0000-\u001f\u007f]/.test(value);
}

/** 处理 labels 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function labels(value) {
  return Array.isArray(value)
    && integer(value.length, 2, 20)
    && value.every((item) => typeof item === "string" && item.trim() && item.length <= 80)
    && new Set(value).size === value.length;
}

/** 处理 exact 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function exact(value, keys) {
  return Boolean(
    value
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()),
  );
}

/** 处理 integer 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function integer(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

/** 处理 nullableInteger 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function nullableInteger(value, minimum, maximum) {
  return value === null || integer(value, minimum, maximum);
}

/** 处理 chain 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function chain(value) {
  return typeof value === "string" && CHAIN_ID.test(value);
}

/** 处理 nullableChain 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function nullableChain(value) {
  return value === null || chain(value);
}

/** 处理 nullableCursor 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function nullableCursor(value) {
  return value === null || (typeof value === "string" && value.length >= 1 && value.length <= 1024 && !/\s/.test(value));
}

/** 处理 utc 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function utc(value) {
  return typeof value === "string" && UTC_TIME.test(value) && Number.isFinite(Date.parse(value));
}

/** 处理 nullableUtc 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function nullableUtc(value) {
  return value === null || utc(value);
}

/** 处理 nullableBatchError 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function nullableBatchError(value) {
  return value === null || (typeof value === "string" && value.startsWith("batch.") && ERROR_CODE.test(value));
}

/** 处理 clientError 对应的批任务页面状态或交互；界面结果不替代后端权限与租户校验。 */
function clientError(code, status, requestId = "-", traceId = "-") {
  const safeCode = ERROR_CODE.test(String(code ?? "")) ? code : "batch.worker_unavailable";
  // 权限以后端校验结果为准；失败时拒绝并保留脱敏审计事实。
  return new AuthApiError({
    status,
    code: safeCode,
    message: ERROR_MESSAGES[safeCode] ?? "批处理请求失败",
    requestId: chain(requestId) ? requestId : "-",
    traceId: chain(traceId) ? traceId : "-",
  });
}
