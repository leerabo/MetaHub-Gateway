import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const BACKUP_ID = /^bkp_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const TENANT_ID = /^ten_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const SCHEMA_HEAD = /^[0-9]{4}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const RESOURCE_KINDS = new Set([
  "database_cluster", "cache_cluster", "object_store", "key_ring",
]);
const PROVIDERS = new Set(["sqlite_local", "postgresql", "redis", "s3_compatible", "kms"]);
const RESOURCE_PROVIDERS = new Map([
  ["database_cluster", new Set(["sqlite_local", "postgresql"])],
  ["cache_cluster", new Set(["redis"])],
  ["object_store", new Set(["s3_compatible"])],
  ["key_ring", new Set(["kms"])],
]);
const HEALTH = new Set(["healthy", "degraded", "unavailable", "not_configured"]);
const BACKUP_STATES = new Set([
  "requested", "snapshotting", "uploading", "verifying", "ready", "failed",
]);

export function getPlatformInfrastructureStatus({ controlApiUrl, signal } = {}) {
  return jsonRequest(controlApiUrl, "/platform/infrastructure/status", { signal }, "status");
}

export function createPlatformBackup({
  controlApiUrl,
  backupType = "full",
  retentionDays = 30,
  idempotencyKey,
  signal,
} = {}) {
  const type = String(backupType);
  const retention = Number(retentionDays);
  const key = String(idempotencyKey ?? "");
  if (!new Set(["full", "incremental"]).has(type)
    || !Number.isSafeInteger(retention) || retention < 1 || retention > 3650
    || !IDEMPOTENCY_KEY.test(key)) invalidRequest();
  return jsonRequest(controlApiUrl, "/platform/backups", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "Idempotency-Key": key,
    },
    body: JSON.stringify({
      backup_type: type,
      scope_kind: "platform",
      workspace_id: null,
      tenant_id: null,
      retention_days: retention,
      confirmed: true,
    }),
    signal,
  }, "backup");
}

export function getPlatformBackup({ controlApiUrl, backupId, signal } = {}) {
  const value = String(backupId ?? "");
  if (!BACKUP_ID.test(value)) invalidRequest();
  return jsonRequest(
    controlApiUrl,
    `/platform/backups/${encodeURIComponent(value)}`,
    { signal },
    "backup",
  );
}

async function jsonRequest(controlApiUrl, path, options, responseKind) {
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
    throw clientError("infrastructure.network_unavailable", "暂时无法连接基础设施服务。");
  }
  const payload = await readJson(response);
  if (!response.ok) {
    throw clientError(
      typeof payload.code === "string" ? payload.code : "infrastructure.request_failed",
      typeof payload.message === "string" ? payload.message : "基础设施请求未完成。",
      response.status,
      linkId(payload.request_id) ? payload.request_id : "-",
    );
  }
  const valid = responseKind === "status" ? validStatus(payload) : validBackupResponse(payload);
  if (!valid) {
    throw clientError("infrastructure.response_invalid", "基础设施响应格式无效。", 502);
  }
  return payload;
}

function validStatus(value) {
  return plainObject(value) && exactKeys(value, [
    "resources", "overall_status", "rpo_minutes", "rto_minutes", "request_id", "trace_id",
  ]) && Array.isArray(value.resources) && value.resources.length === 4
    && new Set(value.resources.map((item) => item.resource_kind)).size === 4
    && value.resources.every(validResource)
    && new Set(["healthy", "degraded", "unavailable"]).has(value.overall_status)
    && boundedInteger(value.rpo_minutes, 0, 1440) && boundedInteger(value.rto_minutes, 0, 10080)
    && linkId(value.request_id) && linkId(value.trace_id);
}

function validResource(value) {
  return plainObject(value) && exactKeys(value, [
    "resource_kind", "provider_code", "status", "version_summary", "latency_ms", "verified",
    "last_checked_at", "last_verified_at", "error_code",
  ]) && RESOURCE_KINDS.has(value.resource_kind) && PROVIDERS.has(value.provider_code)
    && RESOURCE_PROVIDERS.get(value.resource_kind)?.has(value.provider_code)
    && HEALTH.has(value.status) && optionalText(value.version_summary, 80)
    && (value.latency_ms === null || boundedInteger(value.latency_ms, 0, 300000))
    && typeof value.verified === "boolean" && timestamp(value.last_checked_at)
    && (value.last_verified_at === null || timestamp(value.last_verified_at))
    && optionalText(value.error_code, 96);
}

function validBackupResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "backup", "idempotent_replay", "request_id", "trace_id",
  ]) && validBackup(value.backup) && typeof value.idempotent_replay === "boolean"
    && linkId(value.request_id) && linkId(value.trace_id);
}

function validBackup(value) {
  if (!plainObject(value) || !exactKeys(value, [
    "backup_id", "backup_type", "scope_kind", "workspace_id", "tenant_id", "status",
    "source_backend", "source_watermark", "size_bytes", "content_sha256", "manifest_sha256",
    "kms_key_version", "schema_head", "error_code", "retention_until", "created_at",
    "updated_at", "completed_at", "verification",
  ])) return false;
  const platformOwner = value.scope_kind === "platform"
    && value.workspace_id === null && value.tenant_id === null;
  const workspaceOwner = value.scope_kind === "workspace"
    && WORKSPACE_ID.test(String(value.workspace_id)) && TENANT_ID.test(String(value.tenant_id));
  return BACKUP_ID.test(String(value.backup_id))
    && new Set(["full", "incremental"]).has(value.backup_type)
    && (platformOwner || workspaceOwner) && BACKUP_STATES.has(value.status)
    && new Set(["sqlite", "postgresql"]).has(value.source_backend)
    && optionalText(value.source_watermark, 256)
    && (value.size_bytes === null || boundedInteger(value.size_bytes, 0, Number.MAX_SAFE_INTEGER))
    && optionalHash(value.content_sha256) && optionalHash(value.manifest_sha256)
    && optionalText(value.kms_key_version, 128) && optionalSchemaHead(value.schema_head)
    && optionalText(value.error_code, 96) && timestamp(value.retention_until)
    && timestamp(value.created_at) && timestamp(value.updated_at)
    && (value.completed_at === null || timestamp(value.completed_at))
    && validVerification(value.verification);
}

function validVerification(value) {
  return plainObject(value) && exactKeys(value, [
    "status", "schema_head", "integrity_status", "manifest_sha256",
    "tenant_owner_sample_count", "error_code", "started_at", "completed_at",
  ]) && new Set(["pending", "running", "passed", "failed"]).has(value.status)
    && optionalSchemaHead(value.schema_head)
    && new Set([null, "ok", "failed"]).has(value.integrity_status)
    && optionalHash(value.manifest_sha256)
    && (value.tenant_owner_sample_count === null
      || boundedInteger(value.tenant_owner_sample_count, 0, 10000))
    && optionalText(value.error_code, 96)
    && (value.started_at === null || timestamp(value.started_at))
    && (value.completed_at === null || timestamp(value.completed_at));
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
  throw clientError("infrastructure.request_invalid", "基础设施请求字段无效。");
}

function clientError(code, message, status = 0, requestId = "-") {
  return new PlatformApiError({ code, message, status, requestId });
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function boundedInteger(value, minimum, maximum) {
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function optionalText(value, maximum) {
  return value === null || (typeof value === "string" && value.length >= 1 && value.length <= maximum);
}

function optionalHash(value) { return value === null || HASH.test(String(value)); }
function optionalSchemaHead(value) { return value === null || SCHEMA_HEAD.test(String(value)); }
function timestamp(value) { return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value)); }
function linkId(value) { return LINK_ID.test(String(value ?? "")); }

async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch { return {}; }
}
