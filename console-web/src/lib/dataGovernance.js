// Strict PRD-117 client: public governance projections only, isolated Cookie credentials.
import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";
import { readWorkspaceCsrfCookie } from "./workspaces.js";

const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const HOLD_ID = /^lgh_[0-9a-f]{32}$/u;
const RESOURCE_ID = /^[a-z]{3}_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const CODE = /^[a-z0-9][a-z0-9_.-]{1,95}$/u;
const REGION_GROUP = /^[a-z0-9][a-z0-9-]{2,39}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const CLASSIFICATIONS = Object.freeze(["audit", "billing", "file", "identity", "prompt", "response"]);
const DELETION_MODES = Object.freeze(["hard_delete", "archive_then_delete", "anonymize"]);
const COMPONENTS = Object.freeze([
  "gateway", "database", "object_storage", "backup", "job_runtime",
  "disaster_recovery", "tenant_data",
]);
const OUTCOMES = Object.freeze(["compliant", "migration_required", "blocked", "violation"]);
const ERROR_MESSAGES = Object.freeze({
  "workspace_session.required": "需要有效的空间会话。",
  "workspace_session.invalid": "空间会话无效。",
  "workspace_session.expired": "空间会话已过期。",
  "workspace_session.revoked": "空间会话已撤销。",
  "workspace_session.permission_denied": "当前空间职责无权访问数据治理。",
  "workspace_session.credential_conflict": "检测到冲突的凭据域。",
  "workspace_session.csrf_invalid": "空间安全校验已失效。",
  "platform.session_required": "需要平台会话。",
  "platform.session_invalid": "平台会话无效。",
  "platform.session_expired": "平台会话已过期。",
  "platform.session_revoked": "平台会话已撤销。",
  "platform.permission_denied": "当前平台职责无权读取驻留证据。",
  "platform.credential_conflict": "检测到冲突的凭据域。",
  "data_governance.request_invalid": "数据治理请求字段无效。",
  "data_governance.not_found": "数据治理配置不存在。",
  "data_governance.revision_conflict": "数据治理状态已变化，请刷新后重试。",
  "data_governance.idempotency_conflict": "幂等键已绑定到其他数据治理操作。",
  "data_governance.region_not_found": "选择的驻留组不存在。",
  "data_governance.residency_conflict": "驻留策略与当前部署冲突。",
  "data_governance.hold_conflict": "法律保全范围与当前状态冲突。",
  "data_governance.deletion_conflict": "数据已进入不可逆删除阶段，不能创建法律保全。",
  "data_governance.unavailable": "数据治理服务暂时不可用。",
  "data_governance.response_invalid": "数据治理响应格式无效。",
});

export function getWorkspaceDataGovernance({ controlApiUrl, workspaceId, signal } = {}) {
  return request("workspace", controlApiUrl, workspacePath(workspaceId), { signal }, validGovernance);
}

export function updateWorkspaceDataGovernance({
  controlApiUrl,
  workspaceId,
  expectedRevision,
  idempotencyKey,
  allowedResidencyGroups,
  primaryResidencyGroup,
  crossRegionMode,
  retentionPolicies,
  reasonCode,
  confirmed,
  signal,
} = {}) {
  const revision = positiveClientInteger(expectedRevision);
  const key = clientIdempotencyKey(idempotencyKey);
  const payload = governanceInput({
    allowedResidencyGroups,
    primaryResidencyGroup,
    crossRegionMode,
    retentionPolicies,
    reasonCode,
    confirmed,
  });
  return request("workspace", controlApiUrl, workspacePath(workspaceId), {
    method: "PATCH",
    headers: writeHeaders("workspace", key, revision),
    body: JSON.stringify(payload),
    signal,
  }, validGovernance);
}

export function createWorkspaceLegalHold({
  controlApiUrl,
  workspaceId,
  idempotencyKey,
  classificationCode,
  scopeKind,
  selectorSha256 = null,
  reasonCode,
  expiresAt = null,
  confirmed,
  signal,
} = {}) {
  const scope = choice(scopeKind, ["classification", "object_digest"]);
  const selector = selectorSha256 === null || selectorSha256 === ""
    ? null : String(selectorSha256);
  if ((scope === "classification") !== (selector === null) || (selector && !HASH.test(selector))) {
    invalidRequest("workspace");
  }
  const expiry = expiresAt === null || expiresAt === "" ? null : clientTimestamp(expiresAt);
  const payload = {
    classification_code: choice(classificationCode, CLASSIFICATIONS),
    scope_kind: scope,
    selector_sha256: selector,
    reason_code: choice(reasonCode, [
      "litigation", "regulatory_inquiry", "internal_investigation", "preservation_notice",
    ]),
    expires_at: expiry,
    confirmed: exactTrue(confirmed),
  };
  return request("workspace", controlApiUrl, `${workspaceRootPath(workspaceId)}/legal-holds`, {
    method: "POST",
    headers: writeHeaders("workspace", clientIdempotencyKey(idempotencyKey)),
    body: JSON.stringify(payload),
    signal,
  }, validLegalHoldResponse);
}

export function releaseWorkspaceLegalHold({
  controlApiUrl,
  workspaceId,
  holdId,
  expectedRevision,
  idempotencyKey,
  reasonCode,
  confirmed,
  signal,
} = {}) {
  const id = String(holdId ?? "");
  if (!HOLD_ID.test(id)) invalidRequest("workspace");
  const payload = {
    reason_code: choice(reasonCode, ["legal_instruction", "matter_closed"]),
    confirmed: exactTrue(confirmed),
  };
  return request(
    "workspace",
    controlApiUrl,
    `${workspaceRootPath(workspaceId)}/legal-holds/${encodeURIComponent(id)}`,
    {
      method: "DELETE",
      headers: writeHeaders(
        "workspace",
        clientIdempotencyKey(idempotencyKey),
        positiveClientInteger(expectedRevision),
      ),
      body: JSON.stringify(payload),
      signal,
    },
    validLegalHoldResponse,
  );
}

export function listPlatformResidencyEvidence({
  controlApiUrl,
  workspaceId = "",
  component = "",
  outcome = "",
  cursor = null,
  limit = 50,
  signal,
} = {}) {
  const query = new URLSearchParams();
  if (workspaceId) {
    if (!WORKSPACE_ID.test(String(workspaceId))) invalidRequest("platform");
    query.set("workspace_id", String(workspaceId));
  }
  if (component) query.set("component", choice(component, COMPONENTS, "platform"));
  if (outcome) query.set("outcome", choice(outcome, OUTCOMES, "platform"));
  if (cursor !== null && cursor !== "") query.set("cursor", clientText(cursor, 512, "platform"));
  const boundedLimit = Number(limit);
  if (!Number.isSafeInteger(boundedLimit) || boundedLimit < 1 || boundedLimit > 100) {
    invalidRequest("platform");
  }
  query.set("limit", String(boundedLimit));
  return request(
    "platform",
    controlApiUrl,
    `/platform/data-residency/evidence?${query.toString()}`,
    { signal },
    validEvidenceResponse,
  );
}

function governanceInput(input) {
  const groups = Array.isArray(input.allowedResidencyGroups)
    ? input.allowedResidencyGroups.map((value) => String(value)) : [];
  if (groups.length < 1 || groups.length > 16
    || groups.some((value) => !REGION_GROUP.test(value))
    || groups.some((value, index) => index > 0 && groups[index - 1] >= value)) {
    invalidRequest("workspace");
  }
  const primary = String(input.primaryResidencyGroup ?? "");
  if (!groups.includes(primary)) invalidRequest("workspace");
  if (!Array.isArray(input.retentionPolicies) || input.retentionPolicies.length !== 6) {
    invalidRequest("workspace");
  }
  const policies = input.retentionPolicies.map((item) => ({
    classification_code: choice(item?.classificationCode, CLASSIFICATIONS),
    retention_days: clientInteger(item?.retentionDays, 1, 3650),
    deletion_mode: choice(item?.deletionMode, DELETION_MODES),
    backup_grace_days: clientInteger(item?.backupGraceDays, 0, 365),
  }));
  if (policies.map((item) => item.classification_code).join("|") !== CLASSIFICATIONS.join("|")) {
    invalidRequest("workspace");
  }
  return {
    allowed_residency_groups: groups,
    primary_residency_group: primary,
    cross_region_mode: choice(input.crossRegionMode, ["deny", "same_group"]),
    retention_policies: policies,
    reason_code: choice(input.reasonCode, [
      "compliance_update", "regional_expansion", "regional_restriction", "retention_update",
    ]),
    confirmed: clientBoolean(input.confirmed),
  };
}

async function request(scope, controlApiUrl, path, options, validator) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path, scope), {
      method: options?.method ?? "GET",
      headers: { Accept: "application/json", ...(options?.headers ?? {}) },
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError(scope, "data_governance.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code : "data_governance.unavailable";
    throw clientError(
      scope,
      code,
      Number(response.status) || 0,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError(scope, "data_governance.response_invalid", 502);
  return payload;
}

function validGovernance(value) {
  return plainObject(value) && exactKeys(value, [
    "workspace_id", "policy", "classifications", "retention_policies", "legal_holds",
    "deletion_schedules", "fence", "simulation", "applied", "idempotent_replay",
    "request_id", "trace_id",
  ]) && WORKSPACE_ID.test(String(value.workspace_id)) && validPolicy(value.policy)
    && boundedArray(value.classifications, 6, 6, validClassification)
    && uniqueCodes(value.classifications, "code")
    && boundedArray(value.retention_policies, 6, 6, validRetention)
    && uniqueCodes(value.retention_policies, "classification_code")
    && boundedArray(value.legal_holds, 0, 100, validLegalHold)
    && boundedArray(value.deletion_schedules, 0, 100, validDeletionSchedule)
    && validFence(value.fence)
    && (value.simulation === null || validSimulation(value.simulation))
    && typeof value.applied === "boolean" && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validPolicy(value) {
  return plainObject(value) && exactKeys(value, [
    "residency_policy_id", "allowed_residency_groups", "primary_residency_group",
    "cross_region_mode", "revision", "generation", "policy_sha256", "valid_until",
    "updated_at",
  ]) && resource(value.residency_policy_id, "rgp_")
    && boundedArray(value.allowed_residency_groups, 1, 16, (item) => REGION_GROUP.test(String(item)))
    && sortedUnique(value.allowed_residency_groups)
    && value.allowed_residency_groups.includes(value.primary_residency_group)
    && new Set(["deny", "same_group"]).has(value.cross_region_mode)
    && integer(value.revision, 1) && integer(value.generation, 1)
    && HASH.test(String(value.policy_sha256)) && timestamp(value.valid_until)
    && timestamp(value.updated_at);
}

function validClassification(value) {
  return plainObject(value) && exactKeys(value, [
    "data_classification_id", "code", "sensitivity_level", "allowed_purposes",
    "default_retention_days",
  ]) && resource(value.data_classification_id, "dcl_")
    && CLASSIFICATIONS.includes(value.code)
    && new Set(["restricted", "confidential", "internal"]).has(value.sensitivity_level)
    && boundedArray(value.allowed_purposes, 1, 16, (item) => CODE.test(String(item)))
    && integer(value.default_retention_days, 1, 3650);
}

function validRetention(value) {
  return plainObject(value) && exactKeys(value, [
    "retention_policy_id", "classification_code", "retention_days", "deletion_mode",
    "backup_grace_days", "revision", "generation",
  ]) && resource(value.retention_policy_id, "rtp_")
    && CLASSIFICATIONS.includes(value.classification_code)
    && integer(value.retention_days, 1, 3650)
    && DELETION_MODES.includes(value.deletion_mode)
    && integer(value.backup_grace_days, 0, 365)
    && integer(value.revision, 1) && integer(value.generation, 1);
}

function validLegalHoldResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "hold", "fence", "idempotent_replay", "request_id", "trace_id",
  ]) && validLegalHold(value.hold) && validFence(value.fence)
    && typeof value.idempotent_replay === "boolean" && links(value);
}

function validLegalHold(value) {
  return plainObject(value) && exactKeys(value, [
    "legal_hold_id", "classification_code", "scope_kind", "selector_configured",
    "reason_code", "status", "revision", "created_hold_generation", "expires_at",
    "released_at", "created_at", "updated_at",
  ]) && HOLD_ID.test(String(value.legal_hold_id))
    && CLASSIFICATIONS.includes(value.classification_code)
    && new Set(["classification", "object_digest"]).has(value.scope_kind)
    && typeof value.selector_configured === "boolean"
    && value.selector_configured === (value.scope_kind === "object_digest")
    && new Set([
      "litigation", "regulatory_inquiry", "internal_investigation", "preservation_notice",
    ]).has(value.reason_code)
    && new Set(["active", "released", "expired"]).has(value.status)
    && integer(value.revision, 1) && integer(value.created_hold_generation, 1)
    && nullableTimestamp(value.expires_at) && nullableTimestamp(value.released_at)
    && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validDeletionSchedule(value) {
  return plainObject(value) && exactKeys(value, [
    "deletion_schedule_id", "classification_code", "due_at", "status",
    "policy_generation", "hold_generation", "checkpoint_code", "deleted_count",
    "retained_count", "safe_outcome_code", "revision", "completed_at", "updated_at",
  ]) && resource(value.deletion_schedule_id, "dgs_")
    && CLASSIFICATIONS.includes(value.classification_code) && timestamp(value.due_at)
    && new Set(["queued", "running", "blocked", "completed", "failed"]).has(value.status)
    && integer(value.policy_generation, 1) && integer(value.hold_generation, 0)
    && responseText(value.checkpoint_code, 96)
    && integer(value.deleted_count, 0) && integer(value.retained_count, 0)
    && (value.safe_outcome_code === null || responseText(value.safe_outcome_code, 120))
    && integer(value.revision, 1) && nullableTimestamp(value.completed_at)
    && timestamp(value.updated_at);
}

function validFence(value) {
  return plainObject(value) && exactKeys(value, [
    "policy_generation", "hold_generation", "deletion_generation", "revision",
  ]) && integer(value.policy_generation, 1) && integer(value.hold_generation, 0)
    && integer(value.deletion_generation, 0) && integer(value.revision, 1);
}

function validSimulation(value) {
  return plainObject(value) && exactKeys(value, [
    "candidate_sha256", "next_generation", "placement_compliant",
    "migration_required_components",
  ]) && HASH.test(String(value.candidate_sha256)) && integer(value.next_generation, 1)
    && typeof value.placement_compliant === "boolean"
    && boundedArray(
      value.migration_required_components,
      0,
      3,
      (item) => new Set(["gateway", "backup", "disaster_recovery"]).has(item),
    ) && sortedUnique(value.migration_required_components);
}

function validEvidenceResponse(value) {
  return plainObject(value) && exactKeys(value, ["items", "next_cursor", "request_id", "trace_id"])
    && boundedArray(value.items, 0, 100, validEvidence)
    && (value.next_cursor === null || responseText(value.next_cursor, 512)) && links(value);
}

function validEvidence(value) {
  return plainObject(value) && exactKeys(value, [
    "residency_evidence_id", "workspace_id", "policy_generation", "component_code",
    "region_code", "storage_class", "outcome", "violation_code", "observed_watermark",
    "manifest_sha256", "observed_at",
  ]) && resource(value.residency_evidence_id, "rse_")
    && WORKSPACE_ID.test(String(value.workspace_id)) && integer(value.policy_generation, 1)
    && COMPONENTS.includes(value.component_code) && REGION_GROUP.test(String(value.region_code))
    && new Set(["routing", "database", "object", "backup", "job", "restore", "deletion"])
      .has(value.storage_class)
    && OUTCOMES.includes(value.outcome)
    && (value.violation_code === null || new Set([
      "region_not_allowed", "generation_stale", "policy_expired", "policy_unavailable",
      "hold_active",
    ]).has(value.violation_code))
    && integer(value.observed_watermark, 0) && HASH.test(String(value.manifest_sha256))
    && timestamp(value.observed_at);
}

function workspacePath(workspaceId) {
  return `${workspaceRootPath(workspaceId)}/data-governance`;
}

function workspaceRootPath(workspaceId) {
  const id = String(workspaceId ?? "");
  if (!WORKSPACE_ID.test(id)) invalidRequest("workspace");
  return `/workspaces/${encodeURIComponent(id)}`;
}

function apiUrl(controlApiUrl, path, scope) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest(scope);
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(scope);
  }
  return new URL(path, base).toString();
}

function writeHeaders(scope, idempotencyKey, revision = null) {
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": scope === "platform" ? readPlatformCsrfCookie() : readWorkspaceCsrfCookie(),
    "Idempotency-Key": idempotencyKey,
  };
  if (revision !== null) headers["If-Match"] = `"${revision}"`;
  return headers;
}

function clientError(scope, code, status, requestId = "-") {
  const ErrorType = scope === "platform" ? PlatformApiError : IdentityApiError;
  return new ErrorType({
    status,
    code,
    message: ERROR_MESSAGES[code] ?? ERROR_MESSAGES["data_governance.unavailable"],
    requestId,
  });
}

function invalidRequest(scope) {
  throw clientError(scope, "data_governance.request_invalid", 0);
}

function clientIdempotencyKey(value) {
  const result = String(value ?? "");
  if (!IDEMPOTENCY_KEY.test(result)) invalidRequest("workspace");
  return result;
}

function positiveClientInteger(value) {
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < 1) invalidRequest("workspace");
  return result;
}

function clientInteger(value, minimum, maximum) {
  const result = Number(value);
  if (!Number.isSafeInteger(result) || result < minimum || result > maximum) {
    invalidRequest("workspace");
  }
  return result;
}

function choice(value, choices, scope = "workspace") {
  if (!choices.includes(value)) invalidRequest(scope);
  return value;
}

function clientBoolean(value) {
  if (typeof value !== "boolean") invalidRequest("workspace");
  return value;
}

function exactTrue(value) {
  if (value !== true) invalidRequest("workspace");
  return true;
}

function clientTimestamp(value) {
  const result = clientText(value, 64);
  if (!Number.isFinite(Date.parse(result))) invalidRequest("workspace");
  return new Date(result).toISOString();
}

function clientText(value, maximum, scope = "workspace") {
  const result = String(value ?? "");
  if (!result || result.length > maximum || /[\u0000-\u001f\u007f]/u.test(result)) {
    invalidRequest(scope);
  }
  return result;
}

function resource(value, prefix) {
  const result = String(value ?? "");
  return RESOURCE_ID.test(result) && result.startsWith(prefix);
}

function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function timestamp(value) {
  return responseText(value, 64) && Number.isFinite(Date.parse(value));
}

function nullableTimestamp(value) {
  return value === null || timestamp(value);
}

function responseText(value, maximum) {
  return typeof value === "string" && value.length > 0 && value.length <= maximum
    && !/[\u0000-\u001f\u007f]/u.test(value);
}

function links(value) {
  return LINK_ID.test(String(value.request_id)) && LINK_ID.test(String(value.trace_id));
}

function boundedArray(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && value.every(validator);
}

function sortedUnique(value) {
  return value.every((item, index) => index === 0 || String(value[index - 1]) < String(item));
}

function uniqueCodes(items, key) {
  const codes = items.map((item) => item[key]);
  return codes.join("|") === CLASSIFICATIONS.join("|");
}

function plainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, expected) {
  const actual = Object.keys(value).sort();
  const allowed = [...expected].sort();
  return actual.length === allowed.length
    && actual.every((key, index) => key === allowed[index]);
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}
