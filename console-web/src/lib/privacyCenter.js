// PRD-134 privacy data is dual-session scoped; one-time receipts remain in memory only.
import { readIdentityCsrfCookie } from "./identity.js";

const PREFERENCE_ID = /^prf_[0-9a-f]{32}$/u;
const CONSENT_ID = /^con_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const REQUEST_ID = /^dsr_[0-9a-f]{32}$/u;
const REQUEST_CODE = /^prq_[0-9a-f]{16}$/u;
const EXPORT_ID = /^pex_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const RECEIPT = /^[A-Za-z0-9_-]{43,128}$/u;
const LOCALE = /^[a-z0-9][a-z0-9-]{1,19}$/u;
const EXPORT_FILE = /^metahub-identity-export-prq_[0-9a-f]{16}\.json$/u;
const PURPOSES = new Set(["privacy_terms", "analytics", "product_updates", "model_improvement"]);
const DECISIONS = new Set(["granted", "denied", "withdrawn"]);
const REQUEST_KINDS = new Set(["export", "rectify", "erase"]);
const REQUEST_STATUSES = new Set([
  "received", "identity_verified", "collecting", "reviewing", "ready", "completed",
  "partially_denied", "failed", "cancelled",
]);
const OWNER_DOMAINS = Object.freeze([
  "identity", "membership", "audit", "billing", "support", "edge", "data_governance",
]);
const OWNER_STATUSES = new Set(["pending", "running", "completed", "failed", "cancelled"]);
const OWNER_OUTCOMES = new Set([
  "exported", "rectified", "erased", "anonymized", "retained", "not_found",
]);
const ERASURE_OUTCOMES = new Set(["erase", "anonymize", "retain", "not_found"]);
const LEGAL_CODES = new Set([
  "subject_request", "legal_hold", "statutory_retention", "owner_not_found",
]);
const CLASSIFICATIONS = new Set(["identity", "audit", "billing", "file", "prompt", "response"]);
const ERROR_MESSAGES = Object.freeze({
  "privacy.request_invalid": "隐私请求字段无效。",
  "privacy.credential_conflict": "检测到冲突的会话，请关闭其他凭据域后重试。",
  "privacy.session_required": "需要有效的账号与空间会话。",
  "privacy.session_invalid": "账号或空间会话无效。",
  "privacy.origin_invalid": "请求来源校验失败。",
  "privacy.owner_mismatch": "当前账号与空间主体不一致。",
  "privacy.preference_not_found": "隐私偏好不存在，请刷新后重试。",
  "privacy.request_not_found": "数据主体请求不存在。",
  "privacy.export_not_found": "隐私导出不存在。",
  "privacy.revision_required": "缺少当前隐私版本。",
  "privacy.revision_conflict": "隐私状态已变化，请刷新后重试。",
  "privacy.idempotency_invalid": "隐私请求标识无效。",
  "privacy.idempotency_conflict": "请求标识已用于其他隐私操作。",
  "privacy.request_not_cancellable": "该请求已进入不可撤销阶段。",
  "privacy.request_state_invalid": "该请求当前不能执行此操作。",
  "privacy.export_not_ready": "隐私导出尚未就绪。",
  "privacy.export_expired": "隐私导出已过期。",
  "privacy.export_receipt_invalid": "一次性下载凭据无效或已使用。",
  "privacy.export_corrupt": "隐私导出完整性校验失败。",
  "privacy.export_too_large": "隐私导出超出安全大小限制。",
  "privacy.unavailable": "隐私服务暂时不可用。",
  "privacy.response_invalid": "隐私服务响应格式无效。",
  "platform.session_required": "需要有效的平台会话。",
  "platform.session_invalid": "平台会话无效。",
  "platform.session_expired": "平台会话已过期。",
  "platform.session_revoked": "平台会话已撤销。",
  "platform.permission_denied": "当前平台职责无权读取隐私队列。",
});

export class PrivacyApiError extends Error {
  constructor({ status = 0, code = "privacy.unavailable", requestId = "-" } = {}) {
    super(ERROR_MESSAGES[code] ?? ERROR_MESSAGES["privacy.unavailable"]);
    this.name = "PrivacyApiError";
    this.status = Number(status) || 0;
    this.code = code;
    this.requestId = requestId;
  }
}

export function getPrivacyPreferences({ controlApiUrl, signal } = {}) {
  return jsonRequest(controlApiUrl, "/identity/privacy-preferences", { signal }, validPreferenceResponse);
}

export function updatePrivacyPreferences({
  controlApiUrl,
  expectedRevision,
  idempotencyKey,
  analyticsEnabled,
  productUpdatesEnabled,
  modelImprovementEnabled,
  termsVersion,
  locale,
  signal,
} = {}) {
  const body = preferencePatch({
    analyticsEnabled,
    productUpdatesEnabled,
    modelImprovementEnabled,
    termsVersion,
    locale,
  });
  return jsonRequest(controlApiUrl, "/identity/privacy-preferences", {
    method: "PATCH",
    headers: writeHeaders(idempotencyKey, expectedRevision),
    body: JSON.stringify(body),
    signal,
  }, validPreferenceResponse);
}

export function listDataSubjectRequests({ controlApiUrl, limit = 50, signal } = {}) {
  const size = integer(limit, 1, 50, true);
  return jsonRequest(
    controlApiUrl,
    `/identity/data-subject-requests?limit=${size}`,
    { signal },
    validRequestListResponse,
  );
}

export function getDataSubjectRequest({ controlApiUrl, dataSubjectRequestId, signal } = {}) {
  const id = checked(REQUEST_ID, dataSubjectRequestId);
  return jsonRequest(
    controlApiUrl,
    `/identity/data-subject-requests/${encodeURIComponent(id)}`,
    { signal },
    validRequestResponse,
  );
}

export function createDataSubjectRequest({
  controlApiUrl,
  requestKind,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const kind = String(requestKind ?? "");
  if (!REQUEST_KINDS.has(kind)) invalidRequest();
  return jsonRequest(controlApiUrl, "/identity/data-subject-requests", {
    method: "POST",
    headers: writeHeaders(idempotencyKey),
    body: JSON.stringify({
      request_kind: kind,
      step_up_grant: checked(RECEIPT, stepUpGrant),
      confirmed: true,
    }),
    signal,
  }, validRequestResponse);
}

export function cancelDataSubjectRequest({
  controlApiUrl,
  dataSubjectRequestId,
  expectedRevision,
  stepUpGrant,
  idempotencyKey,
  signal,
} = {}) {
  const id = checked(REQUEST_ID, dataSubjectRequestId);
  return jsonRequest(controlApiUrl, `/identity/data-subject-requests/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: writeHeaders(idempotencyKey, expectedRevision),
    body: JSON.stringify({ step_up_grant: checked(RECEIPT, stepUpGrant), confirmed: true }),
    signal,
  }, validRequestResponse);
}

export function issuePrivacyExportReceipt({
  controlApiUrl,
  dataSubjectRequestId,
  expectedRevision,
  idempotencyKey,
  signal,
} = {}) {
  const id = checked(REQUEST_ID, dataSubjectRequestId);
  return jsonRequest(
    controlApiUrl,
    `/identity/data-subject-requests/${encodeURIComponent(id)}/export-receipts`,
    { method: "POST", headers: writeHeaders(idempotencyKey, expectedRevision), signal },
    validReceiptResponse,
  );
}

export async function downloadPrivacyExport({
  controlApiUrl,
  identityExport,
  receipt,
  signal,
} = {}) {
  if (!validIdentityExport(identityExport) || identityExport.status !== "ready") invalidRequest();
  let secret = checked(RECEIPT, receipt);
  try {
    const response = await rawRequest(
      controlApiUrl,
      `/identity/exports/${encodeURIComponent(identityExport.identity_export_id)}/download`,
      { headers: { "X-Privacy-Export-Receipt": secret }, signal },
    );
    if (!response.ok) throw await responseError(response);
    const filename = exportHeaders(response);
    const blob = await response.blob();
    if (!(blob instanceof Blob) || blob.size !== identityExport.size_bytes || blob.size > 524_288) {
      responseInvalid();
    }
    const content = await blob.arrayBuffer();
    const digest = await sha256Bytes(content);
    const expectedDigest = String(response.headers.get("Digest") ?? "");
    const receiptDigest = String(response.headers.get("X-Artifact-Receipt-SHA256") ?? "");
    if (expectedDigest !== `sha-256=${base64(digest)}`
      || receiptDigest !== hex(await sha256Bytes(new TextEncoder().encode(secret)))) {
      responseInvalid();
    }
    return { blob, filename };
  } finally {
    secret = "";
  }
}

export function savePrivacyExport(result, environment = globalThis) {
  const documentApi = environment.document;
  const urlApi = environment.URL;
  if (!(result?.blob instanceof Blob) || !EXPORT_FILE.test(String(result.filename ?? ""))
    || typeof urlApi?.createObjectURL !== "function"
    || typeof urlApi?.revokeObjectURL !== "function"
    || typeof documentApi?.createElement !== "function" || !documentApi.body) responseInvalid();
  const objectUrl = urlApi.createObjectURL(result.blob);
  const link = documentApi.createElement("a");
  try {
    link.href = objectUrl;
    link.download = result.filename;
    link.rel = "noopener";
    link.hidden = true;
    documentApi.body.append(link);
    link.click();
  } finally {
    link.remove();
    urlApi.revokeObjectURL(objectUrl);
  }
}

export function listPlatformPrivacyQueue({ controlApiUrl, limit = 100, signal } = {}) {
  const size = integer(limit, 1, 100, true);
  return jsonRequest(
    controlApiUrl,
    `/platform/privacy/queue?limit=${size}`,
    { signal },
    validPlatformQueueResponse,
  );
}

async function jsonRequest(controlApiUrl, path, options, validator) {
  const response = await rawRequest(controlApiUrl, path, options);
  const payload = await readJson(response);
  if (!response.ok) throw errorFromPayload(response, payload);
  if (!validator(payload)) responseInvalid();
  return payload;
}

async function rawRequest(controlApiUrl, path, options = {}) {
  let response;
  try {
    response = await fetch(apiUrl(controlApiUrl, path), {
      method: options.method ?? "GET",
      headers: { Accept: "application/json", ...(options.headers ?? {}) },
      body: options.body,
      credentials: "include",
      cache: "no-store",
      signal: options.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new PrivacyApiError({ code: "privacy.unavailable" });
  }
  return response;
}

function preferencePatch(input) {
  const value = {};
  for (const [source, target] of [
    ["analyticsEnabled", "analytics_enabled"],
    ["productUpdatesEnabled", "product_updates_enabled"],
    ["modelImprovementEnabled", "model_improvement_enabled"],
  ]) {
    if (input[source] !== undefined) {
      if (typeof input[source] !== "boolean") invalidRequest();
      value[target] = input[source];
    }
  }
  if (input.termsVersion !== undefined) {
    const terms = String(input.termsVersion);
    if (terms.length < 1 || terms.length > 80) invalidRequest();
    value.terms_version = terms;
  }
  if (input.locale !== undefined) {
    const locale = String(input.locale);
    if (!LOCALE.test(locale)) invalidRequest();
    value.locale = locale;
  }
  if (!Object.keys(value).length) invalidRequest();
  return value;
}

function writeHeaders(idempotencyKey = null, revision = null) {
  const headers = { "X-CSRF-Token": readIdentityCsrfCookie() };
  if (idempotencyKey !== null) headers["Idempotency-Key"] = checked(IDEMPOTENCY_KEY, idempotencyKey);
  if (revision !== null) headers["If-Match"] = `"${integer(revision, 1, Number.MAX_SAFE_INTEGER, true)}"`;
  return { "Content-Type": "application/json", ...headers };
}

function validPreferenceResponse(value) {
  return plain(value) && exact(value, [
    "preference", "consent_records", "idempotent_replay", "request_id", "trace_id",
  ]) && validPreference(value.preference)
    && array(value.consent_records, 0, 100, validConsent)
    && typeof value.idempotent_replay === "boolean" && links(value);
}

function validPreference(value) {
  return plain(value) && exact(value, [
    "privacy_preference_id", "workspace_id", "analytics_enabled", "product_updates_enabled",
    "model_improvement_enabled", "terms_version", "locale", "revision", "created_at", "updated_at",
  ]) && PREFERENCE_ID.test(String(value.privacy_preference_id))
    && WORKSPACE_ID.test(String(value.workspace_id))
    && [value.analytics_enabled, value.product_updates_enabled, value.model_improvement_enabled]
      .every((item) => typeof item === "boolean")
    && text(value.terms_version, 1, 80) && LOCALE.test(String(value.locale))
    && integer(value.revision, 1) && timestamp(value.created_at) && timestamp(value.updated_at);
}

function validConsent(value) {
  return plain(value) && exact(value, [
    "consent_record_id", "purpose", "terms_version", "locale", "source_kind", "decision",
    "evidence_sha256", "decided_at",
  ]) && CONSENT_ID.test(String(value.consent_record_id)) && PURPOSES.has(value.purpose)
    && text(value.terms_version, 1, 80) && LOCALE.test(String(value.locale))
    && new Set(["registration", "privacy_center"]).has(value.source_kind)
    && DECISIONS.has(value.decision) && HASH.test(String(value.evidence_sha256))
    && timestamp(value.decided_at);
}

function validRequestListResponse(value) {
  return plain(value) && exact(value, ["items", "request_id", "trace_id"])
    && array(value.items, 0, 50, validRequest) && links(value);
}

function validRequestResponse(value) {
  return plain(value) && exact(value, ["request", "idempotent_replay", "request_id", "trace_id"])
    && validRequest(value.request) && typeof value.idempotent_replay === "boolean" && links(value);
}

function validRequest(value) {
  return plain(value) && exact(value, [
    "data_subject_request_id", "request_code", "workspace_id", "request_kind", "status",
    "revision", "deadline_at", "deadline_warning_at", "warning_sent", "received_at", "updated_at",
    "terminal_reason_code", "terminal_at", "owner_progress", "identity_export",
    "erasure_decisions", "retention_exceptions",
  ]) && REQUEST_ID.test(String(value.data_subject_request_id))
    && REQUEST_CODE.test(String(value.request_code)) && WORKSPACE_ID.test(String(value.workspace_id))
    && REQUEST_KINDS.has(value.request_kind) && REQUEST_STATUSES.has(value.status)
    && integer(value.revision, 1) && timestamp(value.deadline_at)
    && timestamp(value.deadline_warning_at) && typeof value.warning_sent === "boolean"
    && timestamp(value.received_at) && timestamp(value.updated_at)
    && nullableText(value.terminal_reason_code, 160) && nullableTimestamp(value.terminal_at)
    && array(value.owner_progress, 7, 7, validOwnerProgress)
    && unique(value.owner_progress, "owner_domain")
    && (value.identity_export === null || validIdentityExport(value.identity_export))
    && array(value.erasure_decisions, 0, 7, validErasureDecision)
    && unique(value.erasure_decisions, "owner_domain")
    && array(value.retention_exceptions, 0, 7, validRetentionException)
    && unique(value.retention_exceptions, "owner_domain");
}

function validOwnerProgress(value) {
  return plain(value) && exact(value, [
    "owner_domain", "status", "outcome_code", "checkpoint_code", "attempt_count", "completed_at",
  ]) && OWNER_DOMAINS.includes(value.owner_domain) && OWNER_STATUSES.has(value.status)
    && (value.outcome_code === null || OWNER_OUTCOMES.has(value.outcome_code))
    && text(value.checkpoint_code, 1, 120) && integer(value.attempt_count, 0, 8)
    && nullableTimestamp(value.completed_at);
}

function validIdentityExport(value) {
  return plain(value) && exact(value, [
    "identity_export_id", "schema_version", "size_bytes", "revision", "status", "receipt_status",
    "ready_at", "downloaded_at", "expires_at",
  ]) && EXPORT_ID.test(String(value.identity_export_id)) && value.schema_version === 1
    && integer(value.size_bytes, 2, 524_288) && integer(value.revision, 1)
    && new Set(["ready", "downloaded", "expired"]).has(value.status)
    && new Set(["none", "active", "consumed", "expired"]).has(value.receipt_status)
    && timestamp(value.ready_at) && nullableTimestamp(value.downloaded_at)
    && timestamp(value.expires_at);
}

function validErasureDecision(value) {
  return plain(value) && exact(value, [
    "owner_domain", "outcome", "legal_code", "policy_generation", "hold_generation", "decided_at",
  ]) && OWNER_DOMAINS.includes(value.owner_domain) && ERASURE_OUTCOMES.has(value.outcome)
    && LEGAL_CODES.has(value.legal_code) && integer(value.policy_generation, 1)
    && integer(value.hold_generation, 0) && timestamp(value.decided_at);
}

function validRetentionException(value) {
  return plain(value) && exact(value, [
    "owner_domain", "classification_code", "legal_code", "explanation_code",
    "policy_generation", "hold_generation", "expires_at",
  ]) && OWNER_DOMAINS.includes(value.owner_domain) && CLASSIFICATIONS.has(value.classification_code)
    && new Set(["legal_hold", "statutory_retention"]).has(value.legal_code)
    && new Set(["active_legal_hold", "retention_period_active"]).has(value.explanation_code)
    && integer(value.policy_generation, 1) && integer(value.hold_generation, 0)
    && nullableTimestamp(value.expires_at);
}

function validReceiptResponse(value) {
  return plain(value) && exact(value, [
    "identity_export_id", "receipt", "expires_at", "request_id", "trace_id",
  ]) && EXPORT_ID.test(String(value.identity_export_id)) && RECEIPT.test(String(value.receipt))
    && timestamp(value.expires_at) && links(value);
}

function validPlatformQueueResponse(value) {
  return plain(value) && exact(value, ["items", "request_id", "trace_id"])
    && array(value.items, 0, 100, validPlatformQueueItem) && links(value);
}

function validPlatformQueueItem(value) {
  return plain(value) && exact(value, [
    "request_code", "request_kind", "status", "deadline_at", "deadline_warning_at",
    "warning_sent", "domain_progress", "legal_outcomes", "received_at", "updated_at",
  ]) && REQUEST_CODE.test(String(value.request_code)) && REQUEST_KINDS.has(value.request_kind)
    && REQUEST_STATUSES.has(value.status) && timestamp(value.deadline_at)
    && timestamp(value.deadline_warning_at) && typeof value.warning_sent === "boolean"
    && array(value.domain_progress, 7, 7, validOwnerProgress)
    && unique(value.domain_progress, "owner_domain")
    && array(value.legal_outcomes, 0, 7, (item) => ERASURE_OUTCOMES.has(item))
    && timestamp(value.received_at) && timestamp(value.updated_at);
}

function exportHeaders(response) {
  const type = String(response.headers.get("Content-Type") ?? "").split(";", 1)[0].toLowerCase();
  const cache = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const disposition = String(response.headers.get("Content-Disposition") ?? "");
  const nosniff = String(response.headers.get("X-Content-Type-Options") ?? "").toLowerCase();
  const filename = disposition.match(/^attachment;\s*filename="([A-Za-z0-9._-]+\.json)"$/u)?.[1] ?? "";
  if (type !== "application/json" || !cache.includes("no-store") || !cache.includes("private")
    || nosniff !== "nosniff" || !EXPORT_FILE.test(filename)) responseInvalid();
  return filename;
}

async function responseError(response) {
  return errorFromPayload(response, await readJson(response));
}

function errorFromPayload(response, payload) {
  const candidate = String(payload?.code ?? "");
  const code = ERROR_MESSAGES[candidate] ? candidate : "privacy.unavailable";
  const requestId = LINK_ID.test(String(payload?.request_id ?? "")) ? payload.request_id : "-";
  return new PrivacyApiError({ status: response.status, code, requestId });
}

function apiUrl(controlApiUrl, path) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    throw new PrivacyApiError({ code: "privacy.request_invalid" });
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    throw new PrivacyApiError({ code: "privacy.request_invalid" });
  }
  return new URL(path, base).toString();
}

function links(value) {
  return LINK_ID.test(String(value.request_id)) && LINK_ID.test(String(value.trace_id));
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

function integer(value, minimum, maximum = Number.MAX_SAFE_INTEGER, throwOnFailure = false) {
  const valid = Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  if (!valid && throwOnFailure) invalidRequest();
  return valid ? value : false;
}

function text(value, minimum, maximum) {
  return typeof value === "string" && value.length >= minimum && value.length <= maximum
    && !/[\u0000-\u001f\u007f]/u.test(value);
}

function nullableText(value, maximum) {
  return value === null || text(value, 1, maximum);
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40
    && Number.isFinite(Date.parse(value)) && /(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(value);
}

function nullableTimestamp(value) {
  return value === null || timestamp(value);
}

function checked(pattern, value) {
  const candidate = String(value ?? "");
  if (!pattern.test(candidate)) invalidRequest();
  return candidate;
}

function invalidRequest() {
  throw new PrivacyApiError({ code: "privacy.request_invalid" });
}

function responseInvalid() {
  throw new PrivacyApiError({ status: 502, code: "privacy.response_invalid" });
}

async function readJson(response) {
  try {
    const value = await response.json();
    return plain(value) ? value : {};
  } catch {
    return {};
  }
}

async function sha256Bytes(value) {
  if (typeof globalThis.crypto?.subtle?.digest !== "function") responseInvalid();
  return new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", value));
}

function hex(bytes) {
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function base64(bytes) {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return globalThis.btoa(binary);
}

export const PRIVACY_OWNER_DOMAINS = OWNER_DOMAINS;
