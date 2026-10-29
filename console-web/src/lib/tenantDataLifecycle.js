/** PAGE-121 tenant data lifecycle API client. Tenant identity is always derived from the bearer session. */
import { AuthApiError } from "./auth.js";

const ID = /^[A-Za-z0-9_.:-]{1,160}$/;
const JOB_ID = /^tex_[0-9a-f]{32}$/;
const DELETION_ID = /^del_[0-9a-f]{32}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/;
const STATUSES = new Set(["pending", "processing", "completed", "failed", "expired"]);
const DELETION_STATUSES = new Set(["requested", "quiescing", "deleting", "completed", "failed"]);
const EXPORT_KEYS = new Set(["tenant_export_job_id", "status", "schema_version", "snapshot_at", "usage_snapshot_sequence", "audit_snapshot_sequence", "manifest", "content_size", "content_sha256", "expires_at", "download_count", "error_code", "created_at", "updated_at", "completed_at"]);
const DELETION_KEYS = new Set(["deletion_request_id", "status", "tenant_revision", "archived_rows", "deleted_rows", "retained_records", "schema_manifest_sha256", "cache_invalidated_at", "completed_at", "error_code", "created_at", "updated_at"]);

export async function getTenantDataManagement({ controlApiUrl, token, signal }) {
  const payload = await requestJson(`${controlApiUrl}/tenant/data-management`, { token, signal });
  if (!payload || !exactKeys(payload, ["state", "exports", "deletion_request", "request_id", "trace_id"]) || !isState(payload.state) || !Array.isArray(payload.exports) || !ID.test(String(payload.request_id ?? "")) || !ID.test(String(payload.trace_id ?? ""))) {
    throw clientError("tenant_data.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  const exports = payload.exports.map(normalizeExport);
  const deletionRequest = payload.deletion_request ? normalizeDeletion(payload.deletion_request) : null;
  if (exports.some((item) => !item) || (payload.deletion_request && !deletionRequest)) {
    throw clientError("tenant_data.response_invalid", 502, payload.request_id, payload.trace_id);
  }
  return Object.freeze({
    state: Object.freeze({ ...payload.state }),
    exports: Object.freeze(exports),
    deletion_request: deletionRequest,
    request_id: payload.request_id,
    trace_id: payload.trace_id,
  });
}

export async function createTenantExport({ controlApiUrl, token, idempotencyKey, signal }) {
  const key = requireIdempotencyKey(idempotencyKey);
  const payload = await requestJson(`${controlApiUrl}/tenant/export`, {
    method: "POST",
    token,
    signal,
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: "{}",
  });
  if (!payload?.export_job || !exactKeys(payload, ["export_job", "status_url", "download_url", "request_id", "trace_id"]) || !ID.test(String(payload.request_id ?? "")) || !ID.test(String(payload.trace_id ?? ""))) {
    throw clientError("tenant_data.response_invalid", 502, payload?.request_id, payload?.trace_id);
  }
  const exportJob = normalizeExport(payload.export_job);
  if (!exportJob) throw clientError("tenant_data.response_invalid", 502, payload.request_id, payload.trace_id);
  if (!validExportUrls(payload, exportJob.tenant_export_job_id)) throw clientError("tenant_data.response_invalid", 502, payload.request_id, payload.trace_id);
  return Object.freeze({ export_job: exportJob, status_url: payload.status_url, download_url: payload.download_url, request_id: payload.request_id, trace_id: payload.trace_id });
}

export async function getTenantExport({ controlApiUrl, token, jobId, signal }) {
  const id = requireJobId(jobId);
  const payload = await requestJson(`${controlApiUrl}/tenant/exports/${encodeURIComponent(id)}`, { token, signal });
  const exportJob = normalizeExport(payload?.export_job);
  if (!exportJob || exportJob.tenant_export_job_id !== id || !exactKeys(payload, ["export_job", "status_url", "download_url", "request_id", "trace_id"]) || !ID.test(String(payload.request_id ?? "")) || !ID.test(String(payload.trace_id ?? "")) || !validExportUrls(payload, id)) throw clientError("tenant_data.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return Object.freeze({ export_job: exportJob, status_url: payload.status_url, download_url: payload.download_url, request_id: payload.request_id, trace_id: payload.trace_id });
}

export async function downloadTenantExport({ controlApiUrl, token, jobId, signal }) {
  const id = requireJobId(jobId);
  const response = await fetch(`${controlApiUrl}/tenant/exports/${encodeURIComponent(id)}/download`, { headers: authorizationHeaders(token), signal, cache: "no-store" });
  if (!response.ok) throw await apiError(response);
  const type = response.headers.get("content-type") ?? "";
  const cacheControl = response.headers.get("cache-control") ?? "";
  const filename = parseFilename(response.headers.get("content-disposition"));
  const contentLength = Number(response.headers.get("content-length"));
  const expectedHash = response.headers.get("x-content-sha256") ?? "";
  if (!/^application\/zip(?:\s*;|$)/i.test(type) || !/\bno-store\b/i.test(cacheControl) || (response.headers.get("x-content-type-options") ?? "").toLowerCase() !== "nosniff" || filename !== `metahub-tenant-export-${id}.zip` || !Number.isSafeInteger(contentLength) || contentLength < 1 || !SHA256.test(expectedHash)) throw clientError("tenant_data.response_invalid", 502);
  const blob = await response.blob();
  if (blob.size !== contentLength || await sha256(blob) !== expectedHash) throw clientError("tenant_data.response_invalid", 502);
  return { blob, filename, contentSha256: expectedHash, requestId: response.headers.get("x-request-id") ?? "-" };
}

export async function requestTenantDeletion({ controlApiUrl, token, idempotencyKey, confirmation, tenantRevision, signal }) {
  const key = requireIdempotencyKey(idempotencyKey);
  if (typeof confirmation !== "string" || !confirmation.trim() || !Number.isInteger(tenantRevision) || tenantRevision < 1) throw clientError("tenant_data.request_invalid", 400);
  const payload = await requestJson(`${controlApiUrl}/tenant/deletion-request`, {
    method: "POST", token, signal,
    headers: { "Content-Type": "application/json", "Idempotency-Key": key },
    body: JSON.stringify({ confirmation, tenant_revision: tenantRevision }),
  });
  if (!exactKeys(payload, ["deletion_request", "request_id", "trace_id"]) || !ID.test(String(payload.request_id ?? "")) || !ID.test(String(payload.trace_id ?? ""))) throw clientError("tenant_data.response_invalid", 502, payload?.request_id, payload?.trace_id);
  const deletionRequest = normalizeDeletion(payload?.deletion_request);
  if (!deletionRequest) throw clientError("tenant_data.response_invalid", 502, payload?.request_id, payload?.trace_id);
  return Object.freeze({ deletion_request: deletionRequest, request_id: payload.request_id, trace_id: payload.trace_id });
}

export function createTenantDataIdempotencyKey() {
  const value = globalThis.crypto?.randomUUID?.();
  if (!value) throw clientError("tenant_data.idempotency_required", 400);
  return `console_${value}`;
}

export function isTenantDataAbortError(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

function normalizeExport(value) {
  if (!value || !exactKeys(value, EXPORT_KEYS) || !JOB_ID.test(String(value.tenant_export_job_id ?? "")) || !STATUSES.has(value.status) || value.schema_version !== "metahub.tenant-export/v1" || !ISO_DATE.test(String(value.snapshot_at ?? "")) || !ISO_DATE.test(String(value.expires_at ?? "")) || !ISO_DATE.test(String(value.created_at ?? "")) || !ISO_DATE.test(String(value.updated_at ?? "")) || (value.completed_at !== null && !ISO_DATE.test(String(value.completed_at ?? ""))) || !nonnegativeInteger(value.usage_snapshot_sequence) || !nonnegativeInteger(value.audit_snapshot_sequence) || !nonnegativeInteger(value.download_count) || (value.content_size !== null && !nonnegativeInteger(value.content_size)) || (value.content_sha256 !== null && !SHA256.test(String(value.content_sha256))) || (value.manifest !== null && (!value.manifest || typeof value.manifest !== "object" || Array.isArray(value.manifest)))) return null;
  return Object.freeze({ ...value, tenant_export_job_id: String(value.tenant_export_job_id), error_code: value.error_code === null ? null : String(value.error_code) });
}

function normalizeDeletion(value) {
  if (!value || !exactKeys(value, DELETION_KEYS) || !DELETION_ID.test(String(value.deletion_request_id ?? "")) || !DELETION_STATUSES.has(value.status) || !Number.isInteger(value.tenant_revision) || value.tenant_revision < 1 || !nonnegativeInteger(value.archived_rows) || !nonnegativeInteger(value.deleted_rows) || !nonnegativeInteger(value.retained_records) || !SHA256.test(String(value.schema_manifest_sha256 ?? "")) || (value.cache_invalidated_at !== null && !ISO_DATE.test(String(value.cache_invalidated_at ?? ""))) || (value.completed_at !== null && !ISO_DATE.test(String(value.completed_at ?? ""))) || !ISO_DATE.test(String(value.created_at ?? "")) || !ISO_DATE.test(String(value.updated_at ?? ""))) return null;
  return Object.freeze({ ...value, deletion_request_id: String(value.deletion_request_id), error_code: value.error_code === null ? null : String(value.error_code) });
}

function isState(value) {
  return Boolean(value && exactKeys(value, ["lifecycle_status", "revision", "cache_generation", "updated_at"]) && ["active", "deletion_requested", "deleted"].includes(value.lifecycle_status) && Number.isInteger(value.revision) && value.revision >= 1 && Number.isInteger(value.cache_generation) && value.cache_generation >= 0 && ISO_DATE.test(String(value.updated_at ?? "")));
}

function exactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const allowed = expected instanceof Set ? expected : new Set(expected);
  return Object.keys(value).length === allowed.size && Object.keys(value).every((key) => allowed.has(key));
}

function nonnegativeInteger(value) { return Number.isSafeInteger(value) && value >= 0; }

function validExportUrls(payload, id) {
  return payload.status_url === `/tenant/exports/${id}` && payload.download_url === `/tenant/exports/${id}/download`;
}

async function sha256(blob) {
  if (!globalThis.crypto?.subtle) throw clientError("tenant_data.response_invalid", 502);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function requestJson(url, { token, signal, headers = {}, ...options } = {}) {
  const response = await fetch(url, { ...options, signal, headers: { ...authorizationHeaders(token), ...headers, "X-Request-ID": `console_tenant_data_${Date.now()}` }, cache: "no-store" });
  if (!response.ok) throw await apiError(response);
  try { return await response.json(); } catch { throw clientError("tenant_data.response_invalid", 502); }
}

async function apiError(response) {
  let payload = {};
  try { payload = await response.json(); } catch { /* fixed public error below */ }
  return new AuthApiError({ status: response.status, code: payload.code ?? "tenant_data.unavailable", message: payload.message ?? "Tenant data operation failed", requestId: payload.request_id ?? response.headers.get("x-request-id") ?? "-", traceId: payload.trace_id ?? response.headers.get("x-trace-id") ?? "-" });
}

function authorizationHeaders(token) { return token ? { Authorization: `Bearer ${token}` } : {}; }
function requireJobId(value) { if (!JOB_ID.test(String(value ?? ""))) throw clientError("tenant_data.request_invalid", 400); return String(value); }
function requireIdempotencyKey(value) { const key = String(value ?? "").trim(); if (!key || key.length > 200 || /[^\x21-\x7e]/.test(key)) throw clientError("tenant_data.idempotency_required", 400); return key; }
function clientError(code, status, requestId = "-", traceId = "-") { return new AuthApiError({ status, code, message: code, requestId: requestId ?? "-", traceId: traceId ?? "-" }); }
function parseFilename(value) { const match = /filename="([\x20-\x7e]+)"/i.exec(String(value ?? "")); return match?.[1] ?? ""; }

export const getTenantDataState = getTenantDataManagement;
export const createTenantDataExport = createTenantExport;
export const createTenantDataDeletion = requestTenantDeletion;
