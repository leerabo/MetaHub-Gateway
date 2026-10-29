// PRD-135 compliance projections are read-only facts; browser state never receives evidence bodies.
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const CONTROL_ID = /^ctl_[0-9a-f]{32}$/u;
const EVIDENCE_ID = /^evd_[0-9a-f]{32}$/u;
const EXCEPTION_ID = /^cex_[0-9a-f]{32}$/u;
const REMEDIATION_ID = /^rem_[0-9a-f]{32}$/u;
const REPORT_ID = /^crp_[0-9a-f]{32}$/u;
const SERVICE_ACCOUNT_ID = /^svc_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const CODE = /^[a-z0-9][a-z0-9._-]{1,95}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u;
const EVIDENCE_KINDS = new Set([
  "access_review", "encryption_posture", "privacy_request_process",
]);
const CLASSIFICATIONS = new Set(["internal", "confidential", "restricted"]);
const EVIDENCE_OUTCOMES = new Set(["passed", "failed", "inconclusive", "not_applicable"]);
const REPORT_OUTCOMES = new Set([
  "implemented", "partially_implemented", "not_implemented", "not_applicable",
  "exception_active", "evidence_stale",
]);
const ERROR_MESSAGES = Object.freeze({
  "workspace_session.required": "需要有效的空间会话。",
  "workspace_session.invalid": "空间会话无效。",
  "workspace_session.expired": "空间会话已过期。",
  "workspace_session.revoked": "空间会话已撤销。",
  "workspace_session.permission_denied": "当前空间职责无权读取合规报告。",
  "platform.session_required": "需要有效的平台会话。",
  "platform.session_invalid": "平台会话无效。",
  "platform.session_expired": "平台会话已过期。",
  "platform.session_revoked": "平台会话已撤销。",
  "platform.permission_denied": "当前平台职责无权读取合规控制。",
  "compliance.request_invalid": "合规请求字段无效。",
  "compliance.credential_conflict": "检测到冲突的凭据域。",
  "compliance.workspace_not_found": "指定空间不存在。",
  "compliance.report_not_found": "当前空间报告不存在。",
  "compliance.catalog_invalid": "合规控制目录完整性校验失败。",
  "compliance.unavailable": "合规证据服务暂时不可用。",
  "compliance.response_invalid": "合规服务响应格式无效。",
});

export class ComplianceApiError extends Error {
  constructor({ status = 0, code = "compliance.unavailable", requestId = "-" } = {}) {
    super(ERROR_MESSAGES[code] ?? ERROR_MESSAGES["compliance.unavailable"]);
    this.name = "ComplianceApiError";
    this.status = Number(status) || 0;
    this.code = code;
    this.requestId = requestId;
  }
}

export function listPlatformComplianceControls({
  controlApiUrl,
  workspaceId = "",
  signal,
} = {}) {
  const query = new URLSearchParams();
  if (workspaceId !== "") query.set("workspace_id", checked(WORKSPACE_ID, workspaceId));
  const suffix = query.size ? `?${query.toString()}` : "";
  return request(controlApiUrl, `/platform/compliance/controls${suffix}`, { signal },
    validPlatformControls);
}

export function listWorkspaceComplianceReports({ controlApiUrl, workspaceId, signal } = {}) {
  const id = checked(WORKSPACE_ID, workspaceId);
  return request(
    controlApiUrl,
    `/workspaces/${encodeURIComponent(id)}/compliance-reports`,
    { signal },
    (value) => validReportList(value, id),
  );
}

async function request(controlApiUrl, path, options, validator) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ComplianceApiError({ code: "compliance.unavailable" });
  }
  const payload = await readJson(response);
  if (!response.ok) throw errorFromPayload(response, payload);
  if (!validator(payload)) responseInvalid();
  return deepFreeze(payload);
}

function validPlatformControls(value) {
  return plain(value) && exact(value, [
    "control_set_version", "workspace_id", "certification_claimed", "claim_template_code",
    "controls", "request_id", "trace_id",
  ]) && text(value.control_set_version, 3, 40)
    && (value.workspace_id === null || WORKSPACE_ID.test(String(value.workspace_id)))
    && value.certification_claimed === false && value.claim_template_code === "control_fact_only_v1"
    && array(value.controls, 0, 100, validControlState)
    && unique(value.controls.map((item) => item.control), "compliance_control_id")
    && links(value)
    && value.controls.every((item) => (
      value.workspace_id !== null || (
        item.report_outcome === null && item.evidence === null
        && item.exception === null && item.remediation === null
      )
    ));
}

function validControlState(value) {
  return plain(value) && exact(value, [
    "control", "report_outcome", "explanation_code", "evidence", "exception", "remediation",
  ]) && validControl(value.control)
    && (value.report_outcome === null || REPORT_OUTCOMES.has(value.report_outcome))
    && nullableCode(value.explanation_code)
    && (value.evidence === null || validEvidence(value.evidence, value.control))
    && (value.exception === null || validException(value.exception, value.control))
    && (value.remediation === null || validRemediation(value.remediation, value.control));
}

function validControl(value) {
  return plain(value) && exact(value, [
    "compliance_control_id", "control_code", "control_version", "control_set_version", "title",
    "owner_code", "responsibility", "evidence_kind", "collector_schema_version",
    "freshness_seconds", "criticality", "evaluation_rule_code", "evaluation_rule_sha256",
    "mappings",
  ]) && CONTROL_ID.test(String(value.compliance_control_id)) && CODE.test(String(value.control_code))
    && integer(value.control_version, 1) && text(value.control_set_version, 3, 40)
    && text(value.title, 3, 160) && new Set(["security", "privacy", "platform"]).has(value.owner_code)
    && new Set(["platform", "workspace", "shared"]).has(value.responsibility)
    && EVIDENCE_KINDS.has(value.evidence_kind) && value.collector_schema_version === 1
    && integer(value.freshness_seconds, 300, 2_592_000)
    && new Set(["critical", "high", "medium"]).has(value.criticality)
    && value.evaluation_rule_code === "sample_failure_v1"
    && HASH.test(String(value.evaluation_rule_sha256))
    && array(value.mappings, 0, 20, validMapping);
}

function validMapping(value) {
  return plain(value) && exact(value, [
    "framework_code", "framework_version", "clause_code", "statement_template_code",
  ]) && new Set(["djcp_mlps", "pipl", "dsl"]).has(value.framework_code)
    && text(value.framework_version, 1, 40) && text(value.clause_code, 1, 80)
    && new Set(["control_fact_only_v1", "privacy_process_fact_v1"])
      .has(value.statement_template_code);
}

function validEvidence(value, control) {
  return plain(value) && exact(value, [
    "compliance_evidence_id", "workspace_id", "compliance_control_id", "evidence_sequence",
    "evidence_generation", "collector_service_account_id", "collector_version", "evidence_kind",
    "schema_version", "artifact_sha256", "classification", "scope_state", "observed_state",
    "sample_count", "failed_count", "outcome", "outcome_reason_code",
    "immutable_receipt_sha256", "captured_at", "valid_until", "submitted_at", "freshness",
  ]) && EVIDENCE_ID.test(String(value.compliance_evidence_id))
    && WORKSPACE_ID.test(String(value.workspace_id))
    && value.compliance_control_id === control.compliance_control_id
    && integer(value.evidence_sequence, 1) && integer(value.evidence_generation, 1)
    && SERVICE_ACCOUNT_ID.test(String(value.collector_service_account_id))
    && text(value.collector_version, 3, 40) && value.evidence_kind === control.evidence_kind
    && value.schema_version === 1 && HASH.test(String(value.artifact_sha256))
    && CLASSIFICATIONS.has(value.classification)
    && new Set(["present", "absent"]).has(value.scope_state)
    && new Set(["effective", "ineffective", "unknown"]).has(value.observed_state)
    && integer(value.sample_count, 0, 1_000_000) && integer(value.failed_count, 0, value.sample_count)
    && EVIDENCE_OUTCOMES.has(value.outcome)
    && new Set([
      "all_samples_effective", "sample_failure_observed", "collector_inconclusive",
      "scope_not_present",
    ]).has(value.outcome_reason_code)
    && HASH.test(String(value.immutable_receipt_sha256))
    && timestamp(value.captured_at) && timestamp(value.valid_until) && timestamp(value.submitted_at)
    && new Set(["current", "expiring", "expired"]).has(value.freshness);
}

function validException(value, control) {
  return plain(value) && exact(value, [
    "compliance_exception_id", "workspace_id", "compliance_control_id", "evidence_generation",
    "reason_code", "compensating_controls", "status", "review_generation", "revision",
    "starts_at", "expires_at", "approved_at", "created_at", "updated_at",
  ]) && EXCEPTION_ID.test(String(value.compliance_exception_id))
    && WORKSPACE_ID.test(String(value.workspace_id))
    && value.compliance_control_id === control.compliance_control_id
    && integer(value.evidence_generation, 1)
    && new Set(["risk_acceptance", "vendor_dependency", "planned_remediation"])
      .has(value.reason_code)
    && array(value.compensating_controls, 1, 3, (item) => new Set([
      "enhanced_monitoring", "manual_review", "access_restriction",
    ]).has(item)) && new Set(value.compensating_controls).size === value.compensating_controls.length
    && new Set(["pending", "active", "expired"]).has(value.status)
    && integer(value.review_generation, 1) && integer(value.revision, 1)
    && timestamp(value.starts_at) && timestamp(value.expires_at)
    && nullableTimestamp(value.approved_at) && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validRemediation(value, control) {
  return plain(value) && exact(value, [
    "remediation_task_id", "compliance_control_id", "failed_evidence_generation", "status",
    "owner_code", "due_at", "revision",
  ]) && REMEDIATION_ID.test(String(value.remediation_task_id))
    && value.compliance_control_id === control.compliance_control_id
    && integer(value.failed_evidence_generation, 1)
    && new Set(["open", "in_progress", "blocked", "verified", "closed"]).has(value.status)
    && new Set(["security", "privacy", "platform"]).has(value.owner_code)
    && timestamp(value.due_at) && integer(value.revision, 1);
}

function validReportList(value, workspaceId) {
  return plain(value) && exact(value, [
    "current", "recent", "certification_claimed", "request_id", "trace_id",
  ]) && (value.current === null || validReport(value.current, workspaceId))
    && array(value.recent, 0, 5, (item) => validReport(item, workspaceId))
    && unique([...(value.current ? [value.current] : []), ...value.recent], "compliance_report_id")
    && value.certification_claimed === false && links(value);
}

function validReport(value, workspaceId) {
  return plain(value) && exact(value, [
    "compliance_report_id", "workspace_id", "report_version", "control_set_version",
    "evidence_high_watermark", "generator_version", "summary_sha256", "implemented_count",
    "gap_count", "stale_count", "stale", "expires_at", "generated_at", "items",
  ]) && REPORT_ID.test(String(value.compliance_report_id)) && value.workspace_id === workspaceId
    && integer(value.report_version, 1) && text(value.control_set_version, 3, 40)
    && integer(value.evidence_high_watermark, 0) && value.generator_version === "prd135-v1"
    && HASH.test(String(value.summary_sha256)) && integer(value.implemented_count, 0)
    && integer(value.gap_count, 0) && integer(value.stale_count, 0)
    && typeof value.stale === "boolean" && timestamp(value.expires_at)
    && timestamp(value.generated_at) && array(value.items, 0, 100, validReportItem)
    && unique(value.items, "compliance_control_id");
}

function validReportItem(value) {
  return plain(value) && exact(value, [
    "compliance_control_id", "control_code", "title", "report_outcome", "explanation_code",
    "evidence_generation", "evidence_artifact_sha256", "evidence_classification",
    "evidence_valid_until", "compliance_exception_id", "remediation_task_id",
  ]) && CONTROL_ID.test(String(value.compliance_control_id)) && CODE.test(String(value.control_code))
    && text(value.title, 3, 160) && REPORT_OUTCOMES.has(value.report_outcome)
    && CODE.test(String(value.explanation_code))
    && nullableInteger(value.evidence_generation, 1)
    && (value.evidence_artifact_sha256 === null || HASH.test(String(value.evidence_artifact_sha256)))
    && (value.evidence_classification === null || CLASSIFICATIONS.has(value.evidence_classification))
    && nullableTimestamp(value.evidence_valid_until)
    && (value.compliance_exception_id === null
      || EXCEPTION_ID.test(String(value.compliance_exception_id)))
    && (value.remediation_task_id === null
      || REMEDIATION_ID.test(String(value.remediation_task_id)));
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    responseInvalid();
  }
}

function errorFromPayload(response, payload) {
  const candidate = String(payload?.code ?? "");
  const code = ERROR_MESSAGES[candidate] ? candidate : "compliance.unavailable";
  const requestId = LINK_ID.test(String(payload?.request_id ?? "")) ? payload.request_id : "-";
  return new ComplianceApiError({ status: response.status, code, requestId });
}

function apiUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest();
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function checked(pattern, value) {
  const result = String(value ?? "");
  if (!pattern.test(result)) invalidRequest();
  return result;
}

function plain(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exact(value, expected) {
  const keys = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length && keys.every((key, index) => key === wanted[index]);
}

function array(value, minimum, maximum, validator) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && value.every(validator);
}

function unique(value, key) {
  return new Set(value.map((item) => item[key])).size === value.length;
}

function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function nullableInteger(value, minimum) {
  return value === null || integer(value, minimum);
}

function text(value, minimum, maximum) {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
    && !/[\u0000-\u001f\u007f]/u.test(value);
}

function nullableCode(value) {
  return value === null || CODE.test(String(value));
}

function timestamp(value) {
  return typeof value === "string" && value.length >= 20 && value.length <= 40
    && Number.isFinite(Date.parse(value));
}

function nullableTimestamp(value) {
  return value === null || timestamp(value);
}

function links(value) {
  return LINK_ID.test(String(value.request_id)) && LINK_ID.test(String(value.trace_id));
}

function deepFreeze(value) {
  if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
  Object.freeze(value);
  Object.values(value).forEach(deepFreeze);
  return value;
}

function invalidRequest() {
  throw new ComplianceApiError({ status: 400, code: "compliance.request_invalid" });
}

function responseInvalid() {
  throw new ComplianceApiError({ status: 502, code: "compliance.response_invalid" });
}
