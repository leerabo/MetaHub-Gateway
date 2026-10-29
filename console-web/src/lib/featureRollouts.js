// Strict Platform feature rollout client. Capability decisions remain server-authoritative.
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const FLAG_ID = /^ffg_[0-9a-f]{32}$/u;
const ROLLOUT_ID = /^fro_[0-9a-f]{32}$/u;
const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const FLAG_CODE = /^[a-z0-9][a-z0-9_.-]{2,79}$/u;
const REGION = /^[a-z0-9][a-z0-9-]{2,39}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const ENVIRONMENTS = new Set(["development", "staging", "production"]);
const FLAG_STATUSES = new Set(["active", "killed", "disabled"]);
const RISK_LEVELS = new Set(["standard", "p0"]);
const ROLLOUT_STATUSES = new Set([
  "pending_approval", "scheduled", "running", "paused", "completed",
  "rolled_back", "killed", "failed",
]);
const ROLLOUT_ACTIONS = new Set(["start", "approve", "pause", "resume", "rollback"]);
const REASONS = Object.freeze({
  start: "release_start",
  approve: "release_start",
  pause: "operator_pause",
  resume: "health_recovery",
  rollback: "release_rollback",
});
const ERROR_MESSAGES = Object.freeze({
  "platform.session_required": "需要平台会话。",
  "platform.session_invalid": "平台会话无效。",
  "platform.session_expired": "平台会话已过期。",
  "platform.session_revoked": "平台会话已撤销。",
  "platform.permission_denied": "当前平台角色无权执行该操作。",
  "platform.credential_conflict": "检测到冲突的凭据域。",
  "platform.csrf_invalid": "平台安全校验已失效。",
  "platform.origin_invalid": "请求来源无效。",
  "feature_flag.request_invalid": "功能开关请求字段无效。",
  "feature_flag.not_found": "功能开关或发布不存在。",
  "feature_flag.conflict": "当前状态不允许该发布操作。",
  "feature_flag.revision_conflict": "发布状态已变化，请刷新后重试。",
  "feature_flag.idempotency_conflict": "幂等键已绑定到其他发布操作。",
  "feature_flag.approval_conflict": "审批人不满足独立审批要求。",
  "feature_flag.unavailable": "功能开关服务暂不可用。",
  "feature_flag.response_invalid": "功能开关响应格式无效。",
});

export function listFeatureFlags({
  controlApiUrl,
  status = "",
  riskLevel = "",
  limit = 50,
  signal,
} = {}) {
  if (status && !FLAG_STATUSES.has(status)) invalidRequest();
  if (riskLevel && !RISK_LEVELS.has(riskLevel)) invalidRequest();
  if (!integer(limit, 1, 100)) invalidRequest();
  const query = new URLSearchParams({ limit: String(limit) });
  if (status) query.set("status", status);
  if (riskLevel) query.set("risk_level", riskLevel);
  return request(controlApiUrl, `/platform/feature-flags?${query}`, { signal }, validList);
}

export function createFeatureFlag({
  controlApiUrl,
  flag,
  revision = null,
  idempotencyKey,
  signal,
} = {}) {
  const payload = createPayload(flag);
  const expected = revision === null ? null : Number(revision);
  if ((payload.action === "version") !== integer(expected, 1)) invalidRequest();
  return request(
    controlApiUrl,
    "/platform/feature-flags",
    writeOptions(expected, idempotencyKey, payload, signal),
    validFlagResponse,
  );
}

export function commandFeatureRollout({
  controlApiUrl,
  flagId,
  rolloutId = null,
  action,
  versionNumber = null,
  percentageBps = null,
  environmentCode = null,
  regionCode = null,
  revision,
  idempotencyKey,
  signal,
} = {}) {
  const id = String(flagId ?? "");
  const rollout = rolloutId === null ? null : String(rolloutId);
  const expected = Number(revision);
  if (!FLAG_ID.test(id) || !ROLLOUT_ACTIONS.has(action) || !integer(expected, 1)) {
    invalidRequest();
  }
  const starting = action === "start";
  if (starting) {
    if (rollout !== null || !integer(Number(versionNumber), 1)
      || !integer(Number(percentageBps), 0, 10000)
      || !ENVIRONMENTS.has(environmentCode) || !REGION.test(String(regionCode ?? ""))) {
      invalidRequest();
    }
  } else if (!ROLLOUT_ID.test(String(rollout ?? ""))
    || versionNumber !== null || percentageBps !== null
    || environmentCode !== null || regionCode !== null) {
    invalidRequest();
  }
  const payload = {
    action,
    rollout_id: rollout,
    version_number: starting ? Number(versionNumber) : null,
    percentage_bps: starting ? Number(percentageBps) : null,
    environment_code: starting ? environmentCode : null,
    region_code: starting ? regionCode : null,
    reason_code: REASONS[action],
    confirmed: true,
  };
  return request(
    controlApiUrl,
    `/platform/feature-flags/${encodeURIComponent(id)}/rollouts`,
    writeOptions(expected, idempotencyKey, payload, signal),
    validRolloutResponse,
  );
}

export function killFeatureFlag({
  controlApiUrl,
  flagId,
  revision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  const id = String(flagId ?? "");
  const expected = Number(revision);
  if (!FLAG_ID.test(id) || !integer(expected, 1)
    || !new Set(["incident_containment", "security_containment", "release_rollback"])
      .has(reasonCode)) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    `/platform/feature-flags/${encodeURIComponent(id)}/kill`,
    writeOptions(
      expected,
      idempotencyKey,
      { reason_code: reasonCode, confirmed: true },
      signal,
    ),
    validFlagResponse,
  );
}

function createPayload(flag) {
  if (!plainObject(flag) || !exactKeys(flag, [
    "action", "flag_id", "flag_code", "display_name", "risk_level", "rules",
    "confirmed",
  ])) invalidRequest();
  const rules = flag.rules;
  if (!new Set(["create", "version"]).has(flag.action)
    || (flag.action === "create" ? flag.flag_id !== null : !FLAG_ID.test(String(flag.flag_id)))
    || !FLAG_CODE.test(String(flag.flag_code ?? ""))
    || typeof flag.display_name !== "string" || !flag.display_name.trim()
    || flag.display_name.length > 120 || !RISK_LEVELS.has(flag.risk_level)
    || flag.confirmed !== true || !validRules(rules)) {
    invalidRequest();
  }
  return structuredClone(flag);
}

function writeOptions(revision, idempotencyKey, payload, signal) {
  const key = String(idempotencyKey ?? "");
  if (!IDEMPOTENCY_KEY.test(key)) invalidRequest();
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": readPlatformCsrfCookie(),
    "Idempotency-Key": key,
  };
  if (revision !== null) headers["If-Match"] = `"${revision}"`;
  return { method: "POST", headers, body: JSON.stringify(payload), signal };
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
    throw clientError("feature_flag.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code
      : "feature_flag.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError("feature_flag.response_invalid", 502);
  return payload;
}

function validList(value) {
  return plainObject(value) && exactKeys(value, [
    "flags", "total", "request_id", "trace_id",
  ]) && Array.isArray(value.flags) && value.flags.length <= 100
    && value.flags.every(validFlag) && integer(value.total, 0) && links(value);
}

function validFlagResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "flag", "idempotent_replay", "request_id", "trace_id",
  ]) && validFlag(value.flag) && typeof value.idempotent_replay === "boolean" && links(value);
}

function validRolloutResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "flag", "rollout", "idempotent_replay", "request_id", "trace_id",
  ]) && validFlag(value.flag) && validRollout(value.rollout)
    && typeof value.idempotent_replay === "boolean" && links(value);
}

function validFlag(value) {
  return plainObject(value) && exactKeys(value, [
    "flag_id", "flag_code", "display_name", "risk_level", "status",
    "current_version", "rollout_percentage_bps", "revision", "kill_generation",
    "kill_reason_code", "exposure_count", "rules", "latest_rollout", "created_at",
    "updated_at", "versions",
  ]) && FLAG_ID.test(String(value.flag_id)) && FLAG_CODE.test(String(value.flag_code))
    && typeof value.display_name === "string" && value.display_name.length >= 1
    && value.display_name.length <= 120 && RISK_LEVELS.has(value.risk_level)
    && FLAG_STATUSES.has(value.status) && integer(value.current_version, 1)
    && integer(value.rollout_percentage_bps, 0, 10000) && integer(value.revision, 1)
    && integer(value.kill_generation, 0) && optionalCode(value.kill_reason_code)
    && integer(value.exposure_count, 0) && validRules(value.rules)
    && Array.isArray(value.versions) && value.versions.length >= 1
    && value.versions.length <= 1000 && value.versions.every(validVersion)
    && (value.latest_rollout === null || validRollout(value.latest_rollout))
    && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validVersion(value) {
  return plainObject(value) && exactKeys(value, [
    "version_number", "rules", "exposure_count", "created_at",
  ]) && integer(value.version_number, 1) && validRules(value.rules)
    && integer(value.exposure_count, 0) && timestamp(value.created_at);
}

function validRules(value) {
  return plainObject(value) && exactKeys(value, [
    "environments", "regions", "cohort_code", "percentage_bps",
    "prerequisite_flag_code",
  ]) && uniqueArray(value.environments, 1, 3)
    && value.environments.every((entry) => ENVIRONMENTS.has(entry))
    && uniqueArray(value.regions, 0, 16) && value.regions.every((entry) => REGION.test(entry))
    && (value.cohort_code === null || FLAG_CODE.test(String(value.cohort_code)))
    && integer(value.percentage_bps, 0, 10000)
    && (value.prerequisite_flag_code === null
      || FLAG_CODE.test(String(value.prerequisite_flag_code)));
}

function validRollout(value) {
  return plainObject(value) && exactKeys(value, [
    "rollout_id", "action", "status", "version_number", "target_percentage_bps",
    "environment_code", "region_code", "health_status", "approval_count", "revision",
    "health_source_watermark", "job_id", "created_at", "updated_at",
  ]) && ROLLOUT_ID.test(String(value.rollout_id))
    && new Set(["start", "resume", "rollback", "kill"]).has(value.action)
    && ROLLOUT_STATUSES.has(value.status) && integer(value.version_number, 1)
    && integer(value.target_percentage_bps, 0, 10000)
    && ENVIRONMENTS.has(value.environment_code) && REGION.test(String(value.region_code))
    && new Set(["pending", "healthy", "unhealthy", "unavailable"]).has(value.health_status)
    && (value.health_source_watermark === null || integer(value.health_source_watermark, 1))
    && integer(value.approval_count, 0, 2) && integer(value.revision, 1)
    && (value.job_id === null || JOB_ID.test(String(value.job_id)))
    && timestamp(value.created_at) && timestamp(value.updated_at);
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() { throw clientError("feature_flag.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code,
    status,
    requestId,
    message: ERROR_MESSAGES[code] ?? "功能开关请求失败。",
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
function uniqueArray(value, minimum, maximum) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && new Set(value).size === value.length;
}
function optionalCode(value) {
  return value === null || /^[a-z0-9][a-z0-9_.-]{2,95}$/u.test(String(value));
}
function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}
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
