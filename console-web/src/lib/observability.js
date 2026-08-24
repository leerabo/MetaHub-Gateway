// Strict platform observability client; request-level identifiers never enter its DTOs.
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const SLO_ID = /^slo_[0-9a-f]{32}$/u;
const ALERT_ID = /^alt_[0-9a-f]{32}$/u;
const JOB_ID = /^pjob_[0-9a-f]{32}$/u;
const HASH = /^[0-9a-f]{64}$/u;
const CODE = /^[a-z0-9][a-z0-9_.-]{1,63}$/u;
const SERVICES = new Set(["gateway", "control"]);
const OPERATIONS = new Set([
  "chat.completions", "embeddings", "internal.chat", "internal.batch.chat",
  "adapter.identity_probe", "observability.services", "observability.slos.read",
  "observability.slos.write", "observability.budgets", "observability.alert_ack",
  "health", "readiness", "other",
]);
const REGIONS = new Set([
  "global", "cn-east-1", "cn-north-1", "cn-south-1", "us-east-1", "us-west-1",
  "eu-west-1", "ap-southeast-1",
]);
const SERVICE_OPERATIONS = Object.freeze({
  gateway: new Set([
    "chat.completions", "embeddings", "internal.chat", "internal.batch.chat",
    "adapter.identity_probe", "health", "readiness", "other",
  ]),
  control: new Set([
    "observability.services", "observability.slos.read", "observability.slos.write",
    "observability.budgets", "observability.alert_ack", "health", "readiness", "other",
  ]),
});
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const ERROR_MESSAGES = Object.freeze({
  "platform.permission_denied": "当前平台角色无权执行该操作。",
  "observability.request_invalid": "可观测性请求字段无效。",
  "observability.service_not_found": "服务目录项不存在。",
  "observability.revision_conflict": "版本已变化，请刷新后重试。",
  "observability.idempotency_conflict": "幂等键已绑定到其他操作。",
  "observability.alert_not_found": "告警不存在。",
  "observability.cursor_invalid": "分页游标无效。",
  "observability.unavailable": "可观测性服务暂不可用。",
  "observability.response_invalid": "可观测性响应格式无效。",
});

export function listObservableServices({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/observability/services", { signal }, validServices);
}

export function listServiceLevelObjectives({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/slos", { signal }, validSlos);
}

export function listErrorBudgets({ controlApiUrl, signal } = {}) {
  return request(controlApiUrl, "/platform/error-budgets", { signal }, validBudgets);
}

export function createServiceLevelObjective({
  controlApiUrl,
  slo,
  idempotencyKey,
  signal,
} = {}) {
  if (!validSloRequest(slo) || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    "/platform/slos",
    writeOptions(slo.previous_version, idempotencyKey, slo, signal),
    validSloResponse,
  );
}

export function acknowledgePlatformAlert({
  controlApiUrl,
  alertId,
  revision,
  reasonCode,
  idempotencyKey,
  signal,
} = {}) {
  if (!ALERT_ID.test(String(alertId ?? "")) || !integer(Number(revision), 1, 1_000_000)
    || !new Set(["investigating", "known_issue", "mitigating"]).has(reasonCode)
    || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) {
    invalidRequest();
  }
  return request(
    controlApiUrl,
    `/platform/alerts/${encodeURIComponent(alertId)}/ack`,
    writeOptions(null, idempotencyKey, {
      revision: Number(revision),
      reason_code: reasonCode,
      confirmed: true,
    }, signal),
    validAckResponse,
  );
}

function writeOptions(revision, idempotencyKey, payload, signal) {
  const headers = {
    "Content-Type": "application/json",
    "X-CSRF-Token": readPlatformCsrfCookie(),
    "Idempotency-Key": idempotencyKey,
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
    throw clientError("observability.unavailable", 0);
  }
  const payload = await readJson(response);
  if (!response.ok) {
    const code = typeof payload.code === "string" && ERROR_MESSAGES[payload.code]
      ? payload.code : "observability.unavailable";
    throw clientError(
      code,
      response.status,
      LINK_ID.test(String(payload.request_id ?? "")) ? payload.request_id : "-",
    );
  }
  if (!validator(payload)) throw clientError("observability.response_invalid", 502);
  return payload;
}

function validServices(value) {
  return plainObject(value) && exactKeys(value, [
    "items", "total", "projection", "request_id", "trace_id",
  ]) && Array.isArray(value.items) && value.items.length <= 200
    && value.items.every(validService) && value.total === value.items.length
    && projection(value.projection) && links(value);
}

function validService(value) {
  return plainObject(value) && exactKeys(value, [
    "service_catalog_entry_id", "service_code", "display_name", "owner_team_code",
    "service_tier", "golden_signals", "status", "revision", "source_watermark",
    "total_count", "error_rate_bps", "latency_p95_ms", "health_status",
  ]) && /^sce_[0-9a-f]{32}$/u.test(String(value.service_catalog_entry_id))
    && CODE.test(String(value.service_code)) && typeof value.display_name === "string"
    && CODE.test(String(value.owner_team_code)) && integer(value.service_tier, 1, 3)
    && uniqueStrings(value.golden_signals, 0, 16)
    && new Set(["active", "disabled"]).has(value.status) && integer(value.revision, 1)
    && integer(value.source_watermark, 0) && integer(value.total_count, 0)
    && integer(value.error_rate_bps, 0, 10000) && integer(value.latency_p95_ms, 0)
    && new Set(["healthy", "unhealthy", "unavailable"]).has(value.health_status);
}

function validSlos(value) {
  return plainObject(value) && exactKeys(value, [
    "items", "total", "limit", "offset", "projection", "request_id", "trace_id",
  ]) && Array.isArray(value.items) && value.items.length <= 200
    && value.items.every(validSlo) && integer(value.total, 0)
    && integer(value.limit, 1, 200) && integer(value.offset, 0)
    && projection(value.projection) && links(value);
}

function validSlo(value) {
  return plainObject(value) && exactKeys(value, [
    "service_level_objective_id", "slo_code", "version_number", "service_code",
    "display_name", "signal_class", "sli_kind", "operation_code", "region_code",
    "window_minutes", "target_bps", "latency_threshold_ms", "burn_warning_bps",
    "burn_critical_bps", "definition_sha256", "is_current", "created_at",
  ]) && SLO_ID.test(String(value.service_level_objective_id))
    && CODE.test(String(value.slo_code)) && integer(value.version_number, 1)
    && CODE.test(String(value.service_code)) && typeof value.display_name === "string"
    && new Set(["reliability", "security"]).has(value.signal_class)
    && new Set(["availability", "latency"]).has(value.sli_kind)
    && CODE.test(String(value.operation_code)) && CODE.test(String(value.region_code))
    && integer(value.window_minutes, 5, 43200) && integer(value.target_bps, 1, 10000)
    && (value.latency_threshold_ms === null || integer(value.latency_threshold_ms, 10, 5000))
    && integer(value.burn_warning_bps, 10000, 100000000)
    && integer(value.burn_critical_bps, 10001, 100000000)
    && HASH.test(String(value.definition_sha256)) && typeof value.is_current === "boolean"
    && timestamp(value.created_at);
}

function validBudgets(value) {
  return plainObject(value) && exactKeys(value, [
    "items", "alerts", "limit", "next_cursor", "projection", "request_id", "trace_id",
  ]) && Array.isArray(value.items) && value.items.length <= 200
    && value.items.every(validBudget) && Array.isArray(value.alerts)
    && value.alerts.length <= 1000 && value.alerts.every(validAlert)
    && integer(value.limit, 1, 200)
    && (value.next_cursor === null || (typeof value.next_cursor === "string"
      && value.next_cursor.length <= 2048))
    && projection(value.projection) && links(value);
}

function validBudget(value) {
  return plainObject(value) && exactKeys(value, [
    "error_budget_id", "service_level_objective_id", "slo_code", "slo_version",
    "service_code", "signal_class", "region_code", "window_started_at",
    "window_ended_at", "total_count", "good_count", "allowed_bad_count",
    "consumed_bad_count", "remaining_bad_count", "burn_rate_bps", "health_status",
    "source_watermark", "source_sha256", "computed_at",
  ]) && /^ebg_[0-9a-f]{32}$/u.test(String(value.error_budget_id))
    && SLO_ID.test(String(value.service_level_objective_id)) && CODE.test(String(value.slo_code))
    && integer(value.slo_version, 1) && CODE.test(String(value.service_code))
    && new Set(["reliability", "security"]).has(value.signal_class)
    && CODE.test(String(value.region_code)) && timestamp(value.window_started_at)
    && timestamp(value.window_ended_at) && integer(value.total_count, 0)
    && integer(value.good_count, 0) && integer(value.allowed_bad_count, 0)
    && integer(value.consumed_bad_count, 0) && integer(value.remaining_bad_count, 0)
    && integer(value.burn_rate_bps, 0, 100000000)
    && new Set(["healthy", "warning", "critical", "exhausted", "unavailable"])
      .has(value.health_status)
    && integer(value.source_watermark, 0) && HASH.test(String(value.source_sha256))
    && timestamp(value.computed_at);
}

function validAlert(value) {
  return plainObject(value) && exactKeys(value, [
    "alert_id", "service_level_objective_id", "slo_code", "service_code",
    "signal_class", "region_code", "severity", "state", "safe_reason_code",
    "source_watermark", "revision", "first_seen_at", "last_seen_at", "resolved_at",
    "latest_acknowledgement_at",
  ]) && ALERT_ID.test(String(value.alert_id))
    && SLO_ID.test(String(value.service_level_objective_id)) && CODE.test(String(value.slo_code))
    && CODE.test(String(value.service_code))
    && new Set(["reliability", "security"]).has(value.signal_class)
    && CODE.test(String(value.region_code)) && new Set(["warning", "critical"]).has(value.severity)
    && new Set(["firing", "suppressed", "resolved"]).has(value.state)
    && new Set(["slo_breached", "error_budget_exhausted"]).has(value.safe_reason_code)
    && integer(value.source_watermark, 0) && integer(value.revision, 1)
    && timestamp(value.first_seen_at) && timestamp(value.last_seen_at)
    && optionalTime(value.resolved_at) && optionalTime(value.latest_acknowledgement_at);
}

function validSloResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "slo", "idempotent_replay", "aggregation_job_id", "request_id", "trace_id",
  ]) && validSlo(value.slo) && typeof value.idempotent_replay === "boolean"
    && JOB_ID.test(String(value.aggregation_job_id)) && links(value);
}

function validAckResponse(value) {
  const ack = value?.acknowledgement;
  return plainObject(value) && exactKeys(value, [
    "acknowledgement", "idempotent_replay", "request_id", "trace_id",
  ]) && plainObject(ack) && exactKeys(ack, [
    "acknowledgement_id", "alert_id", "alert_revision", "reason_code", "acknowledged_at",
  ]) && /^aak_[0-9a-f]{32}$/u.test(String(ack.acknowledgement_id))
    && ALERT_ID.test(String(ack.alert_id)) && integer(ack.alert_revision, 1)
    && new Set(["investigating", "known_issue", "mitigating"]).has(ack.reason_code)
    && timestamp(ack.acknowledged_at) && typeof value.idempotent_replay === "boolean"
    && links(value);
}

function validSloRequest(value) {
  return plainObject(value) && exactKeys(value, [
    "service_code", "slo_code", "display_name", "signal_class", "sli_kind",
    "operation_code", "region_code", "window_minutes", "target_bps",
    "latency_threshold_ms", "burn_warning_bps", "burn_critical_bps",
    "previous_version", "reason_code", "confirmed",
  ]) && SERVICES.has(value.service_code) && CODE.test(String(value.slo_code))
    && typeof value.display_name === "string" && value.display_name.trim().length > 0
    && value.display_name.length <= 120
    && new Set(["reliability", "security"]).has(value.signal_class)
    && new Set(["availability", "latency"]).has(value.sli_kind)
    && OPERATIONS.has(value.operation_code)
    && SERVICE_OPERATIONS[value.service_code].has(value.operation_code)
    && REGIONS.has(value.region_code)
    && integer(value.window_minutes, 5, 43200) && integer(value.target_bps, 1, 10000)
    && (value.latency_threshold_ms === null || integer(value.latency_threshold_ms, 10, 5000))
    && integer(value.burn_warning_bps, 10000, 100000000)
    && integer(value.burn_critical_bps, 10001, 100000000)
    && value.burn_critical_bps > value.burn_warning_bps
    && integer(value.previous_version, 0)
    && value.reason_code === (value.previous_version === 0 ? "slo_create" : "slo_revision")
    && value.confirmed === true;
}

function apiUrl(controlApiUrl, path) {
  let base;
  try { base = new URL(String(controlApiUrl)); } catch { invalidRequest(); }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest();
  }
  return new URL(path, base).toString();
}

function invalidRequest() { throw clientError("observability.request_invalid", 0); }
function clientError(code, status, requestId = "-") {
  return new PlatformApiError({
    code,
    status,
    requestId,
    message: ERROR_MESSAGES[code] ?? "可观测性请求失败。",
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
function uniqueStrings(value, minimum, maximum) {
  return Array.isArray(value) && value.length >= minimum && value.length <= maximum
    && new Set(value).size === value.length && value.every((item) => typeof item === "string");
}
function projection(value) { return new Set(["operations", "security"]).has(value); }
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
