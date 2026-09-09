import { IdentityApiError } from "./identity.js";
import { PlatformApiError, readPlatformCsrfCookie } from "./platform.js";

const CYCLE_ID = /^pbc_[0-9a-f]{32}$/u;
const WORKSPACE_ID = /^wsp_[0-9a-f]{32}$/u;
const PLAN_VERSION_ID = /^spv_[0-9a-f]{32}$/u;
const ADJUSTMENT_ID = /^pba_[0-9a-f]{32}$/u;
const OUTBOX_ID = /^pmo_[0-9a-f]{32}$/u;
const RECOVERY_ID = /^pmx_[0-9a-f]{32}$/u;
const RECOVERY_ACTIONS = new Set(["retry", "internal_cost_only"]);
const METER_ERRORS = new Set([
  "platform_metering.owner_invalid", "platform_metering.source_invalid",
  "platform_metering.meter_unknown", "platform_metering.checkpoint_conflict",
  "platform_metering.unavailable", "platform_billing.reconciliation_required",
  "platform_metering.reconciliation_required",
]);
const HASH = /^[0-9a-f]{64}$/u;
const CURSOR = /^[A-Za-z0-9_-]{1,512}$/u;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/u;
const MONEY = /^(?:-?)(?:0|[1-9][0-9]{0,10})\.[0-9]{2}$/u;
const LINK_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/u;
const METER_CODES = new Set(["api_requests", "input_tokens", "output_tokens", "total_tokens"]);
const CYCLE_STATUSES = new Set(["open", "closed", "reconciliation_required"]);
const CSV_FILE = /^metahub-billing-[0-9a-f]{8}\.csv$/u;
const MAX_CSV_BYTES = 1_000_000;

export function getWorkspaceBillingSummary({ controlApiUrl, cycleId, signal } = {}) {
  const parameters = new URLSearchParams();
  if (cycleId !== undefined && cycleId !== null) parameters.set("cycle_id", checkedCycleId(cycleId));
  const query = parameters.size ? `?${parameters.toString()}` : "";
  return jsonRequest(controlApiUrl, `/billing/summary${query}`, { signal }, "workspace");
}

export async function downloadWorkspaceBillingExport({ controlApiUrl, cycleId, signal } = {}) {
  const path = `/billing/reconciliation-export?cycle_id=${encodeURIComponent(checkedCycleId(cycleId))}`;
  const response = await rawRequest(controlApiUrl, path, { signal }, "workspace");
  if (!response.ok) throw await responseError(response, "workspace");
  const type = String(response.headers.get("Content-Type") ?? "").trim().toLowerCase();
  const disposition = String(response.headers.get("Content-Disposition") ?? "");
  const cacheControl = String(response.headers.get("Cache-Control") ?? "").toLowerCase();
  const nosniff = String(response.headers.get("X-Content-Type-Options") ?? "").toLowerCase();
  const filename = disposition.match(/^attachment;\s*filename="([A-Za-z0-9._-]+\.csv)"$/u)?.[1] ?? "";
  if (type !== "text/csv; charset=utf-8" || !CSV_FILE.test(filename)
    || !cacheControl.includes("no-store") || nosniff !== "nosniff") {
    throw clientError("workspace", "platform_billing.response_invalid", "账单导出响应无效。");
  }
  const blob = await response.blob();
  if (!(blob instanceof Blob) || blob.size < 1 || blob.size > MAX_CSV_BYTES) {
    throw clientError("workspace", "platform_billing.response_invalid", "账单导出文件无效。");
  }
  return { blob, filename };
}

export function saveWorkspaceBillingExport(result, environment = globalThis) {
  const documentApi = environment.document;
  const urlApi = environment.URL;
  if (!(result?.blob instanceof Blob) || !CSV_FILE.test(String(result.filename ?? ""))
    || typeof urlApi?.createObjectURL !== "function"
    || typeof urlApi?.revokeObjectURL !== "function"
    || typeof documentApi?.createElement !== "function" || !documentApi.body) {
    throw clientError("workspace", "platform_billing.response_invalid", "账单导出文件无效。");
  }
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

export function listPlatformBillingSummary({
  controlApiUrl,
  status,
  cursor,
  limit = 20,
  signal,
} = {}) {
  const parameters = new URLSearchParams({ limit: String(boundedInteger(limit, 1, 100)) });
  if (status !== undefined && status !== null && status !== "") {
    if (!CYCLE_STATUSES.has(String(status))) invalidRequest("platform");
    parameters.set("status", String(status));
  }
  if (cursor !== undefined && cursor !== null) {
    const value = String(cursor);
    if (!CURSOR.test(value)) invalidRequest("platform");
    parameters.set("cursor", value);
  }
  return jsonRequest(
    controlApiUrl,
    `/platform/billing/summary?${parameters.toString()}`,
    { signal },
    "platform",
  );
}

export function getPlatformMeteringState({ controlApiUrl, signal } = {}) {
  return jsonRequest(controlApiUrl, "/platform/billing/metering-state", { signal }, "platform");
}

export function recoverPlatformMeterFact({ controlApiUrl, recovery, idempotencyKey, signal } = {}) {
  if (!plainObject(recovery) || !exactKeys(recovery, [
    "meter_outbox_id", "source_sequence", "expected_attempt_count", "expected_source_sha256",
    "action", "confirmed",
  ]) || !OUTBOX_ID.test(recovery.meter_outbox_id) || !positiveInteger(recovery.source_sequence)
    || !positiveInteger(recovery.expected_attempt_count) || recovery.expected_attempt_count > 2_147_483_647
    || !HASH.test(recovery.expected_source_sha256) || !RECOVERY_ACTIONS.has(recovery.action)
    || recovery.confirmed !== true || !IDEMPOTENCY_KEY.test(String(idempotencyKey ?? ""))) {
    invalidRequest("platform");
  }
  return jsonRequest(controlApiUrl, "/platform/billing/meter-recoveries", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(recovery), signal,
  }, "platform");
}

export function createPlatformBillingAdjustment({
  controlApiUrl,
  adjustment,
  idempotencyKey,
  signal,
} = {}) {
  const payload = checkedAdjustment(adjustment);
  const key = String(idempotencyKey ?? "");
  if (!IDEMPOTENCY_KEY.test(key)) invalidRequest("platform");
  return jsonRequest(controlApiUrl, "/platform/billing/adjustments", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": readPlatformCsrfCookie(),
      "Idempotency-Key": key,
    },
    body: JSON.stringify(payload),
    signal,
  }, "platform");
}

async function jsonRequest(controlApiUrl, path, options, domain) {
  const response = await rawRequest(controlApiUrl, path, options, domain);
  const payload = await readJson(response);
  if (!response.ok) throw responseErrorFromPayload(response, payload, domain);
  if (!validResponse(payload, path)) {
    throw clientError(domain, "platform_billing.response_invalid", "账单响应格式无效。", 502);
  }
  return payload;
}

async function rawRequest(controlApiUrl, path, options, domain) {
  const headers = { Accept: "application/json", ...(options?.headers ?? {}) };
  try {
    return await fetch(apiUrl(controlApiUrl, path, domain), {
      method: options?.method ?? "GET",
      headers,
      body: options?.body,
      credentials: "include",
      cache: "no-store",
      signal: options?.signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw clientError(domain, "platform_billing.network_unavailable", "暂时无法连接账单服务。");
  }
}

function validResponse(payload, path) {
  if (!plainObject(payload) || !linkId(payload.request_id) || !linkId(payload.trace_id)) return false;
  if (path === "/platform/billing/metering-state") return validMeteringState(payload);
  if (path === "/platform/billing/meter-recoveries") {
    return exactKeys(payload, ["recovery", "idempotent_replay", "request_id", "trace_id"])
      && typeof payload.idempotent_replay === "boolean" && validRecovery(payload.recovery);
  }
  if (path.startsWith("/billing/summary")) {
    return exactKeys(payload, ["cycle", "available_cycles", "request_id", "trace_id"])
      && Array.isArray(payload.available_cycles) && payload.available_cycles.length <= 24
      && payload.available_cycles.every((value) => CYCLE_ID.test(String(value)))
      && (payload.cycle === null || validCycle(payload.cycle, false));
  }
  if (path.startsWith("/platform/billing/summary")) {
    return exactKeys(payload, ["cycles", "next_cursor", "request_id", "trace_id"])
      && Array.isArray(payload.cycles) && payload.cycles.length <= 100
      && payload.cycles.every((cycle) => validCycle(cycle, true))
      && (payload.next_cursor === null || CURSOR.test(String(payload.next_cursor)));
  }
  if (path === "/platform/billing/adjustments") {
    return exactKeys(payload, ["adjustment", "idempotent_replay", "request_id", "trace_id"])
      && typeof payload.idempotent_replay === "boolean" && validAdjustmentResponse(payload.adjustment);
  }
  return false;
}

function validRecovery(value) {
  return plainObject(value) && exactKeys(value, [
    "recovery_id", "source_sequence", "workspace_id", "action", "created_at",
  ]) && RECOVERY_ID.test(value.recovery_id) && positiveInteger(value.source_sequence)
    && WORKSPACE_ID.test(value.workspace_id) && RECOVERY_ACTIONS.has(value.action)
    && timestamp(value.created_at);
}

function validMeteringState(value) {
  if (!exactKeys(value, [
    "state", "checkpoint", "pending_count", "blocked_fact", "recent_recoveries", "request_id", "trace_id",
  ]) || !nonnegativeInteger(value.checkpoint) || !nonnegativeInteger(value.pending_count)
    || !Array.isArray(value.recent_recoveries) || value.recent_recoveries.length > 10
    || !value.recent_recoveries.every(validRecovery)) return false;
  if (value.state === "idle") return value.pending_count === 0 && value.blocked_fact === null;
  if (value.state === "pending") return value.pending_count > 0 && value.blocked_fact === null;
  const fact = value.blocked_fact;
  return value.state === "blocked" && value.pending_count > 0 && plainObject(fact) && exactKeys(fact, [
    "meter_outbox_id", "source_sequence", "attempt_count", "source_sha256", "workspace_id",
    "occurred_at", "error_code", "can_archive_internal_cost",
  ]) && OUTBOX_ID.test(fact.meter_outbox_id) && positiveInteger(fact.source_sequence)
    && fact.source_sequence === value.checkpoint + 1 && positiveInteger(fact.attempt_count)
    && fact.attempt_count <= 2_147_483_647 && HASH.test(fact.source_sha256)
    && (fact.workspace_id === null || WORKSPACE_ID.test(fact.workspace_id))
    && timestamp(fact.occurred_at) && METER_ERRORS.has(fact.error_code)
    && typeof fact.can_archive_internal_cost === "boolean"
    && (!fact.can_archive_internal_cost || (fact.workspace_id !== null
      && fact.error_code === "platform_metering.owner_invalid"));
}

function validCycle(cycle, platform) {
  const keys = [
    "billing_cycle_id", "workspace_id", "status", "plan_version_id", "period_started_at",
    "period_ends_at", "currency", "source_watermark", "revision", "meters",
  ];
  if (platform) keys.push("updated_at");
  return plainObject(cycle) && exactKeys(cycle, keys)
    && CYCLE_ID.test(String(cycle.billing_cycle_id)) && WORKSPACE_ID.test(String(cycle.workspace_id))
    && CYCLE_STATUSES.has(cycle.status) && PLAN_VERSION_ID.test(String(cycle.plan_version_id))
    && timestamp(cycle.period_started_at) && timestamp(cycle.period_ends_at)
    && (!platform || timestamp(cycle.updated_at)) && new Set(["CNY", "USD"]).has(cycle.currency)
    && nonnegativeInteger(cycle.source_watermark) && positiveInteger(cycle.revision)
    && Array.isArray(cycle.meters) && cycle.meters.length === 4
    && new Set(cycle.meters.map((meter) => meter.meter_code)).size === 4
    && cycle.meters.every(validMeter);
}

function validMeter(meter) {
  return plainObject(meter) && exactKeys(meter, [
    "meter_code", "unit", "billing_mode", "usage_quantity", "allocated_quantity",
    "overage_quantity", "adjustment_quantity", "currency", "amount", "adjustment_amount",
    "effective_amount", "summary_revision", "summary_sha256",
  ]) && METER_CODES.has(meter.meter_code) && new Set(["request", "token"]).has(meter.unit)
    && new Set(["subscription_base", "informational"]).has(meter.billing_mode)
    && nonnegativeInteger(meter.usage_quantity) && nonnegativeInteger(meter.allocated_quantity)
    && nonnegativeInteger(meter.overage_quantity) && safeInteger(meter.adjustment_quantity)
    && new Set(["CNY", "USD"]).has(meter.currency) && MONEY.test(String(meter.amount))
    && MONEY.test(String(meter.adjustment_amount)) && MONEY.test(String(meter.effective_amount))
    && ((meter.summary_revision === null && meter.summary_sha256 === null)
      || (positiveInteger(meter.summary_revision) && HASH.test(String(meter.summary_sha256))));
}

function validAdjustmentResponse(value) {
  return plainObject(value) && exactKeys(value, [
    "billing_adjustment_id", "workspace_id", "billing_cycle_id", "meter_code", "reason_code",
    "quantity_delta", "amount_delta", "currency", "created_at",
  ]) && ADJUSTMENT_ID.test(String(value.billing_adjustment_id))
    && WORKSPACE_ID.test(String(value.workspace_id)) && CYCLE_ID.test(String(value.billing_cycle_id))
    && METER_CODES.has(value.meter_code)
    && new Set(["manual_credit", "manual_debit", "late_usage"]).has(value.reason_code)
    && safeInteger(value.quantity_delta) && MONEY.test(String(value.amount_delta))
    && new Set(["CNY", "USD"]).has(value.currency) && timestamp(value.created_at);
}

function checkedAdjustment(value) {
  if (!plainObject(value)) invalidRequest("platform");
  const normalized = {
    workspace_id: String(value.workspace_id ?? ""),
    billing_cycle_id: String(value.billing_cycle_id ?? ""),
    meter_code: String(value.meter_code ?? ""),
    reason_code: String(value.reason_code ?? ""),
    quantity_delta: Number(value.quantity_delta),
    amount_delta: String(value.amount_delta ?? ""),
    expected_summary_sha256: String(value.expected_summary_sha256 ?? ""),
  };
  const amount = Number(normalized.amount_delta);
  const credit = normalized.reason_code === "manual_credit";
  const debit = normalized.reason_code === "manual_debit";
  if (!WORKSPACE_ID.test(normalized.workspace_id) || !CYCLE_ID.test(normalized.billing_cycle_id)
    || !METER_CODES.has(normalized.meter_code) || (!credit && !debit)
    || !safeInteger(normalized.quantity_delta) || Math.abs(normalized.quantity_delta) > 1_000_000_000_000
    || !MONEY.test(normalized.amount_delta) || !Number.isFinite(amount)
    || !HASH.test(normalized.expected_summary_sha256)
    || (credit && (normalized.quantity_delta > 0 || amount > 0))
    || (debit && (normalized.quantity_delta < 0 || amount < 0))
    || (normalized.quantity_delta === 0 && amount === 0)) invalidRequest("platform");
  return normalized;
}

async function responseError(response, domain) {
  return responseErrorFromPayload(response, await readJson(response), domain);
}

function responseErrorFromPayload(response, payload, domain) {
  return clientError(
    domain,
    typeof payload?.code === "string" ? payload.code : "platform_billing.request_failed",
    typeof payload?.message === "string" ? payload.message : "账单请求未完成，请稍后重试。",
    response.status,
    linkId(payload?.request_id) ? payload.request_id : "-",
  );
}

function apiUrl(controlApiUrl, path, domain) {
  let base;
  try {
    base = new URL(String(controlApiUrl));
  } catch {
    invalidRequest(domain);
  }
  if (!new Set(["http:", "https:"]).has(base.protocol) || base.username || base.password) {
    invalidRequest(domain);
  }
  return new URL(path, base).toString();
}

function checkedCycleId(value) {
  const text = String(value ?? "");
  if (!CYCLE_ID.test(text)) invalidRequest("workspace");
  return text;
}

function clientError(domain, code, message, status = 0, requestId = "-") {
  const ErrorType = domain === "platform" ? PlatformApiError : IdentityApiError;
  return new ErrorType({ status, code, message, requestId });
}

function invalidRequest(domain) {
  throw clientError(domain, "platform_billing.request_invalid", "账单请求字段无效。");
}

function plainObject(value) {
  return Boolean(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && actual.every((key, index) => key === expected[index]);
}

function safeInteger(value) {
  return Number.isSafeInteger(value);
}

function nonnegativeInteger(value) {
  return safeInteger(value) && value >= 0;
}

function positiveInteger(value) {
  return safeInteger(value) && value >= 1;
}

function boundedInteger(value, minimum, maximum) {
  const number = Number(value);
  if (!safeInteger(number) || number < minimum || number > maximum) invalidRequest("platform");
  return number;
}

function timestamp(value) {
  return typeof value === "string" && value.length <= 40 && Number.isFinite(Date.parse(value));
}

function linkId(value) {
  return LINK_ID.test(String(value ?? ""));
}

async function readJson(response) {
  try {
    const value = await response.json();
    return plainObject(value) ? value : {};
  } catch {
    return {};
  }
}
