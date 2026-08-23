// Strict Platform job client: Cookie credentials only, exact DTOs, CAS and idempotency.
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const TYPE_CODE = /^[a-z0-9][a-z0-9_.-]{2,79}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const STATUSES = new Set([
  "queued", "leased", "running", "cancel_requested", "succeeded", "failed",
  "cancelled", "dead_lettered",
]);
const ERROR_MESSAGES = Object.freeze({
  "platform.session_required": "需要平台会话。",
  "platform.session_invalid": "平台会话无效。",
  "platform.session_expired": "平台会话已过期。",
  "platform.session_revoked": "平台会话已撤销。",
  "platform.permission_denied": "当前平台角色无权执行该操作。",
  "platform.credential_conflict": "检测到冲突的凭据域。",
  "platform.csrf_invalid": "平台安全校验已失效。",
  "platform.origin_invalid": "请求来源无效。",
  "platform_job.request_invalid": "任务请求字段无效。",
  "platform_job.not_found": "任务不存在或不可见。",
  "platform_job.type_unavailable": "任务类型当前不可用。",
  "platform_job.conflict": "任务当前状态不允许该操作。",
  "platform_job.revision_conflict": "任务状态已变化，请刷新后重试。",
  "platform_job.idempotency_conflict": "幂等键已绑定到其他操作。",
  "platform_job.unavailable": "任务运行时暂不可用。",
  "platform_job.response_invalid": "任务运行时响应格式无效。",
});

export function listPlatformJobQueues({ controlApiUrl, status = "", limit = 50, signal } = {}) {
  if (status && !STATUSES.has(status)) invalidRequest();
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) invalidRequest();
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  return request(controlApiUrl, `/platform/job-queues?${query}`, { signal }, validQueueResponse);
}

export function getPlatformJob({ controlApiUrl, jobId, signal } = {}) {
  const id = String(jobId ?? "");
  if (!JOB_ID.test(id)) invalidRequest();
  return request(
    controlApiUrl,
    `/platform/jobs/${encodeURIComponent(id)}`,
    { signal },
    validDetailResponse,
  );
}

export function retryPlatformJob(input = {}) {
  return jobCommand("retry", input);
}

export function cancelPlatformJob(input = {}) {
  return jobCommand("cancel", input);
}

export function commandPlatformJobType({
  controlApiUrl,
  typeCode,
  action,
  reasonCode,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const type = String(typeCode ?? "");
  const expected = Number(revision);
  const key = String(idempotencyKey ?? "");
  const reason = String(reasonCode ?? "");
  if (!TYPE_CODE.test(type) || !new Set(["activate", "disable"]).has(action)
    || !new Set(["release_activation", "operational_pause", "security_containment"])
      .has(reason)
    || !Number.isSafeInteger(expected) || expected < 1 || !IDEMPOTENCY_KEY.test(key)) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    "/platform/job-types",
    writeOptions(
      expected,
      key,
      { type_code: type, action, reason_code: reason, confirmed: true },
      signal,
    ),
    validTypeResponse,
  );
}

function jobCommand(action, {
  controlApiUrl,
  jobId,
  reasonCode,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const id = String(jobId ?? "");
  const expected = Number(revision);
  const key = String(idempotencyKey ?? "");
  const reason = String(reasonCode ?? "");
  const allowedReasons = action === "retry"
    ? new Set(["operator_retry", "incident_recovery"])
    : new Set(["operator_cancelled", "security_containment"]);
  if (!JOB_ID.test(id) || !allowedReasons.has(reason)
    || !Number.isSafeInteger(expected) || expected < 1 || !IDEMPOTENCY_KEY.test(key)) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    `/platform/jobs/${encodeURIComponent(id)}/${action}`,
    writeOptions(expected, key, { reason_code: reason, confirmed: true }, signal),
    validActionResponse,
  );
}

function writeOptions(revision, idempotencyKey, payload, signal) {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "If-Match": `"${revision}"`,
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
    signal,
  };
}

async function request(controlApiUrl, path, options, validator) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options?.method ?? "GET",
      headers: { Accept: "application/json", ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError("platform_job.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code
      : "platform_job.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) {
    throw clientError("platform_job.response_invalid", 502);
  }
  return payload;
}

function validQueueResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "queues", "recent_jobs", "job_types", "request_id", "trace_id",
  ]) && Array.isArray(value.queues) && value.queues.length === 3
    && value.queues.every(validQueue) && Array.isArray(value.recent_jobs)
    && value.recent_jobs.length <= 100 && value.recent_jobs.every(validJob)
    && Array.isArray(value.job_types) && value.job_types.length <= 32
    && value.job_types.every(validType) && links(value);
}

function validDetailResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "job", "attempts", "checkpoints", "request_id", "trace_id",
  ]) && validJob(value.job) && Array.isArray(value.attempts) && value.attempts.length <= 20
    && value.attempts.every(validAttempt) && Array.isArray(value.checkpoints)
    && value.checkpoints.length <= 1000 && value.checkpoints.every(validCheckpoint)
    && links(value);
}

function validActionResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "job", "idempotent_replay", "request_id", "trace_id",
  ]) && validJob(value.job) && typeof value.idempotent_replay === "boolean" && links(value);
}

function validTypeResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "job_type", "idempotent_replay", "request_id", "trace_id",
  ]) && validType(value.job_type) && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validQueue(value) {
  return plainObject(value) && exactKeys(value, [
    "queue_code", "priority_weight", "max_concurrency", "queued_count",
    "active_lease_count", "dead_letter_count", "oldest_wait_seconds",
  ]) && new Set(["critical", "standard", "bulk"]).has(value.queue_code)
    && integer(value.priority_weight, 1, 100) && integer(value.max_concurrency, 1, 1000)
    && integer(value.queued_count, 0) && integer(value.active_lease_count, 0)
    && integer(value.dead_letter_count, 0)
    && (value.oldest_wait_seconds === null || integer(value.oldest_wait_seconds, 0));
}

function validJob(value) {
  return plainObject(value) && exactKeys(value, [
    "job_id", "type_code", "queue_code", "priority", "status", "revision",
    "generation", "attempt_count", "max_attempts", "safe_outcome_code",
    "scheduled_at", "started_at", "completed_at", "created_at", "updated_at",
  ]) && JOB_ID.test(String(value.job_id)) && TYPE_CODE.test(String(value.type_code))
    && new Set(["critical", "standard", "bulk"]).has(value.queue_code)
    && integer(value.priority, 0, 100) && STATUSES.has(value.status)
    && integer(value.revision, 1) && integer(value.generation, 0)
    && integer(value.attempt_count, 0, 20) && integer(value.max_attempts, 1, 20)
    && optionalCode(value.safe_outcome_code) && timestamp(value.scheduled_at)
    && optionalTime(value.started_at) && optionalTime(value.completed_at)
    && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validType(value) {
  return plainObject(value) && exactKeys(value, [
    "type_code", "contract_version", "queue_code", "resource_class", "max_attempts",
    "timeout_seconds", "status", "revision",
  ]) && TYPE_CODE.test(String(value.type_code)) && integer(value.contract_version, 1)
    && new Set(["critical", "standard", "bulk"]).has(value.queue_code)
    && new Set(["control", "io", "compute"]).has(value.resource_class)
    && integer(value.max_attempts, 1, 20) && integer(value.timeout_seconds, 1, 86400)
    && new Set(["active", "disabled"]).has(value.status) && integer(value.revision, 1);
}

function validAttempt(value) {
  return plainObject(value) && exactKeys(value, [
    "attempt_id", "attempt_number", "generation", "status", "safe_outcome_code",
    "leased_at", "started_at", "completed_at",
  ]) && /^pja_[0-9a-f]{32}$/u.test(String(value.attempt_id))
    && integer(value.attempt_number, 1, 20) && integer(value.generation, 1)
    && new Set(["leased", "running", "succeeded", "failed", "cancelled", "stale"])
      .has(value.status)
    && optionalCode(value.safe_outcome_code) && timestamp(value.leased_at)
    && optionalTime(value.started_at) && optionalTime(value.completed_at);
}

function validCheckpoint(value) {
  return plainObject(value) && exactKeys(value, [
    "checkpoint_id", "generation", "sequence_number", "checkpoint_sha256", "created_at",
  ]) && /^pjc_[0-9a-f]{32}$/u.test(String(value.checkpoint_id))
    && integer(value.generation, 1) && integer(value.sequence_number, 1)
    && HASH.test(String(value.checkpoint_sha256)) && timestamp(value.created_at);
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() {
  throw clientError("platform_job.request_invalid", 0);
}

function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code,
    status,
    requestId,
    message: ERROR_MESSAGES[code] ?? "任务运行时请求失败。",
  });
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function optionalCode(value) {
  return value === null || /^[a-z0-9][a-z0-9_.-]{2,95}$/u.test(String(value));
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function optionalTime(value) { return value === null || timestamp(value); }
function links(value) {
  return LINK_ID.test(String(value.request_id ?? ""))
    && LINK_ID.test(String(value.trace_id ?? ""));
}

async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
