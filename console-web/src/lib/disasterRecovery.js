// Strict disaster-recovery client: public projections only, Cookie credentials, CSRF and idempotency.
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const REGION_ID = /^reg_[0-9a-f]{32}$/u;
const PLAN_ID = /^fpl_[0-9a-f]{32}$/u;
const RUN_ID = /^drr_[0-9a-f]{32}$/u;
const OPERATOR_ID = /^pto_[0-9a-f]{32}$/u;
const EVIDENCE_ID = /^rev_[0-9a-f]{32}$/u;
const RECOVERY_ID = /^rcp_[0-9a-f]{32}$/u;
const BACKUP_ID = /^bkp_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const CODE = /^[a-z0-9][a-z0-9_.-]{2,95}$/u;
const REGION_CODE = /^[a-z0-9][a-z0-9-]{2,39}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const REASONS = new Set([
  "quarterly_drill", "regional_outage", "control_recovery", "security_containment",
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
  "dr.request_invalid": "灾难恢复请求字段无效。",
  "dr.plan_not_found": "灾难恢复计划不存在。",
  "dr.region_not_found": "目标 Region 不存在。",
  "dr.run_not_found": "灾难恢复执行不存在。",
  "dr.plan_unavailable": "灾难恢复计划当前不可用。",
  "dr.region_unavailable": "目标 Region 当前不可用。",
  "dr.residency_violation": "目标 Region 不满足驻留约束。",
  "dr.recovery_point_unavailable": "可验证恢复点当前不可用。",
  "dr.approval_required": "灾难恢复命令尚未完成双人审批。",
  "dr.approver_conflict": "发起人与安全复核人必须不同。",
  "dr.run_conflict": "已有冲突的灾难恢复执行。",
  "dr.epoch_conflict": "Writer epoch 已变化，请刷新后重试。",
  "dr.revision_conflict": "灾难恢复状态已变化，请刷新后重试。",
  "dr.idempotency_conflict": "幂等键已绑定到其他灾难恢复命令。",
  "dr.unavailable": "灾难恢复服务暂时不可用。",
  "dr.response_invalid": "灾难恢复响应格式无效。",
});

export function getDisasterRecoveryStatus({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/dr/status", { signal }, validStatusResponse);
}

export function listDisasterRecoveryBackups({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/backups", { signal }, validBackupResponse);
}

export function startDisasterRecoveryDrill(input = {}) {
  return command("/platform/dr/drills", input);
}

export function startRegionFailover({ regionId, ...input } = {}) {
  const id = String(regionId ?? "");
  if (!REGION_ID.test(id)) invalidRequest();
  return command(`/platform/regions/${encodeURIComponent(id)}/failovers`, input);
}

function command(path, {
  controlApiUrl,
  failoverPlanId,
  planVersion,
  reasonCode,
  expectedEpoch,
  idempotencyKey,
  signal,
} = {}) {
  const payload = {
    failover_plan_id: String(failoverPlanId ?? ""),
    plan_version: Number(planVersion),
    reason_code: String(reasonCode ?? ""),
    expected_epoch: Number(expectedEpoch),
    confirmed: true,
  };
  if (!PLAN_ID.test(payload.failover_plan_id) || !integer(payload.plan_version, 1)
    || !REASONS.has(payload.reason_code) || !integer(payload.expected_epoch, 0)
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) invalidRequest();
  return request(controlApiUrl, path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "Idempotency-Key": String(idempotencyKey),
    },
    body: JSON.stringify(payload),
    signal,
  }, validRunResponse);
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
    throw clientError("dr.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code : "dr.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError("dr.response_invalid", 502);
  return payload;
}

function validStatusResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "regions", "plans", "recent_runs", "objectives", "request_id", "trace_id",
  ]) && boundedArray(value.regions, 1, 50, validRegion)
    && boundedArray(value.plans, 0, 50, validPlan)
    && boundedArray(value.recent_runs, 0, 100, validRun)
    && validObjectives(value.objectives) && links(value);
}

function validRegion(value) {
  return plainObject(value) && exactKeys(value, [
    "region_id", "region_code", "residency_group", "health_status", "writer_status",
    "failover_epoch", "revision", "updated_at",
  ]) && REGION_ID.test(String(value.region_id)) && REGION_CODE.test(String(value.region_code))
    && REGION_CODE.test(String(value.residency_group))
    && new Set(["healthy", "degraded", "unavailable"]).has(value.health_status)
    && new Set(["active", "standby", "fenced"]).has(value.writer_status)
    && integer(value.failover_epoch, 0) && integer(value.revision, 1)
    && timestamp(value.updated_at);
}

function validPlan(value) {
  return plainObject(value) && exactKeys(value, [
    "failover_plan_id", "plan_code", "version", "source_region_code",
    "target_region_code", "residency_group", "component_profile", "rpo_seconds",
    "control_rto_seconds", "gateway_rto_seconds", "stop_condition_code",
    "manifest_sha256", "status",
  ]) && PLAN_ID.test(String(value.failover_plan_id)) && CODE.test(String(value.plan_code))
    && integer(value.version, 1) && REGION_CODE.test(String(value.source_region_code))
    && REGION_CODE.test(String(value.target_region_code))
    && value.source_region_code !== value.target_region_code
    && REGION_CODE.test(String(value.residency_group))
    && value.component_profile === "control-full" && integer(value.rpo_seconds, 60, 900)
    && integer(value.control_rto_seconds, 60, 7200)
    && integer(value.gateway_rto_seconds, 60, 600)
    && value.stop_condition_code === "split-brain-or-integrity-failure"
    && HASH.test(String(value.manifest_sha256))
    && new Set(["active", "disabled"]).has(value.status);
}

function validObjectives(value) {
  return plainObject(value) && exactKeys(value, [
    "control_rpo_seconds", "control_rto_seconds", "gateway_rto_seconds",
    "latest_recovery_age_seconds", "recovery_ready",
  ]) && value.control_rpo_seconds === 900 && value.control_rto_seconds === 7200
    && value.gateway_rto_seconds === 600
    && (value.latest_recovery_age_seconds === null
      || integer(value.latest_recovery_age_seconds, 0))
    && typeof value.recovery_ready === "boolean";
}

function validRun(value) {
  return plainObject(value) && exactKeys(value, [
    "disaster_recovery_run_id", "run_kind", "failover_plan_id", "plan_version",
    "source_region_code", "target_region_code", "expected_epoch", "fencing_epoch",
    "reason_code", "status", "checkpoint", "platform_job_id", "approvals",
    "restore_evidence", "safe_outcome_code", "revision", "started_at", "completed_at",
    "created_at", "updated_at",
  ]) && RUN_ID.test(String(value.disaster_recovery_run_id))
    && new Set(["drill", "failover"]).has(value.run_kind)
    && PLAN_ID.test(String(value.failover_plan_id)) && integer(value.plan_version, 1)
    && REGION_CODE.test(String(value.source_region_code))
    && REGION_CODE.test(String(value.target_region_code))
    && integer(value.expected_epoch, 0) && value.fencing_epoch === value.expected_epoch + 1
    && REASONS.has(value.reason_code)
    && new Set([
      "planned", "approved", "prechecking", "running", "verifying", "succeeded",
      "failed", "aborted", "rollback_required",
    ]).has(value.status)
    && new Set([
      "approval", "precheck", "quiesce", "restore", "route", "verify", "complete",
    ]).has(value.checkpoint)
    && (value.platform_job_id === null || JOB_ID.test(String(value.platform_job_id)))
    && boundedArray(value.approvals, 0, 2, validApproval)
    && new Set(value.approvals.map((item) => item.approval_kind)).size === value.approvals.length
    && new Set(value.approvals.map((item) => item.operator_id)).size === value.approvals.length
    && boundedArray(value.restore_evidence, 0, 1000, validEvidence)
    && (value.safe_outcome_code === null || CODE.test(String(value.safe_outcome_code)))
    && integer(value.revision, 1) && optionalTime(value.started_at)
    && optionalTime(value.completed_at) && timestamp(value.created_at)
    && timestamp(value.updated_at);
}

function validApproval(value) {
  return plainObject(value) && exactKeys(value, [
    "approval_kind", "operator_id", "approved_at",
  ]) && new Set(["execution", "security_review"]).has(value.approval_kind)
    && OPERATOR_ID.test(String(value.operator_id)) && timestamp(value.approved_at);
}

function validEvidence(value) {
  return plainObject(value) && exactKeys(value, [
    "restore_evidence_id", "workspace_id", "component_code", "integrity_status",
    "rpo_outcome", "rto_outcome", "duration_seconds", "manifest_sha256", "created_at",
  ]) && EVIDENCE_ID.test(String(value.restore_evidence_id))
    && WORKSPACE_ID.test(String(value.workspace_id))
    && new Set([
      "database", "object_store", "config", "outbox", "ledger", "gateway_route",
    ]).has(value.component_code)
    && new Set(["verified", "failed"]).has(value.integrity_status)
    && new Set(["met", "missed"]).has(value.rpo_outcome)
    && new Set(["met", "missed"]).has(value.rto_outcome)
    && integer(value.duration_seconds, 0) && HASH.test(String(value.manifest_sha256))
    && timestamp(value.created_at);
}

function validBackupResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "items", "total", "request_id", "trace_id",
  ]) && boundedArray(value.items, 0, 500, validRecoveryPoint)
    && integer(value.total, 0) && value.total === value.items.length && links(value);
}

function validRecoveryPoint(value) {
  return plainObject(value) && exactKeys(value, [
    "recovery_point_id", "backup_id", "scope_kind", "workspace_id", "schema_head",
    "integrity_status", "manifest_sha256", "source_created_at", "age_seconds",
  ]) && RECOVERY_ID.test(String(value.recovery_point_id))
    && BACKUP_ID.test(String(value.backup_id))
    && new Set(["platform", "workspace"]).has(value.scope_kind)
    && ((value.scope_kind === "platform" && value.workspace_id === null)
      || (value.scope_kind === "workspace" && WORKSPACE_ID.test(String(value.workspace_id))))
    && /^[0-9]{4}$/u.test(String(value.schema_head)) && value.integrity_status === "verified"
    && HASH.test(String(value.manifest_sha256)) && timestamp(value.source_created_at)
    && integer(value.age_seconds, 0);
}

function validRunResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "run", "approval_recorded", "idempotent_replay", "request_id", "trace_id",
  ]) && validRun(value.run) && typeof value.approval_recorded === "boolean"
    && typeof value.idempotent_replay === "boolean" && links(value);
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() { throw clientError("dr.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code,
    status,
    requestId,
    message: ERROR_MESSAGES[code] ?? "灾难恢复请求失败。",
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
function boundedArray(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && value.every(validator);
}
function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
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
